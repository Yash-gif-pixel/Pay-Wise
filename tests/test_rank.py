"""Tests for :mod:`src.rank`."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src import candidates as cand
from src import capacity, changes, extract, forecast, load, rank, state
from src.candidates import Candidate, CandidateSet, Method, Payment
from src.rank import (
    Criterion,
    DeadlineFilterError,
    RankingTieError,
    Status,
    assign_status,
    rank_key,
)
from tests.test_state import make_context, make_event


@pytest.fixture(scope="module")
def dataset() -> load.Dataset:
    return load.get_dataset()


@pytest.fixture(scope="module")
def cache():
    return extract.load_image_cache()


def pipeline(dataset: load.Dataset, request_id: str, cache):
    request = dataset.request(request_id)
    context = dataset.get_user_context(request.user_id, request_id)
    balance = state.reconstruct_balance(context)
    blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
    evidence = extract.gather_evidence(context, blank, cache=cache)
    projection = forecast.build_forecast(balance, evidence=evidence)
    assessment = capacity.assess_capacity(context, projection)
    candidate_set = cand.generate_candidates(
        context,
        projection,
        assessment,
        change_finder=changes.change_finder_for(balance),
    )
    return context, candidate_set, assessment


def make_candidate(
    method: Method,
    *,
    payments: list[tuple[str, str]],
    total: str,
    requires_changes: bool = False,
    option_id: str | None = None,
    spending_changes: tuple[str, ...] = (),
) -> Candidate:
    return Candidate(
        method=method,
        payments=tuple(
            Payment(on=date.fromisoformat(day), amount=Decimal(amount))
            for day, amount in payments
        ),
        total_paid=Decimal(total),
        requires_changes=requires_changes,
        payment_option_id=option_id,
        spending_changes=spending_changes,
    )


FALLBACK = make_candidate(Method.NOT_RECOMMENDED, payments=[], total="0")


def make_set(*candidates: Candidate, request_id: str = "request_test") -> CandidateSet:
    return CandidateSet(
        request_id=request_id,
        user_id="user_test",
        candidates=(*candidates, FALLBACK),
        rejections=(),
    )


def plain_context(**kwargs):
    """A synthetic context with a deadline far enough out to be inert.

    The deadline is a hard filter in ``candidates.py`` and `rank` asserts it
    held, so these fixtures set it past every synthetic payment date. Tests
    that want to exercise the assertion override it explicitly.
    """
    events = [make_event("event_1", day="2025-01-05", amount="100")]
    kwargs.setdefault("desired_completion_date", "2025-12-31")
    return make_context(events, request_date="2025-06-02", **kwargs)


def fake_capacity(
    context,
    *,
    safe: str = "0",
    earliest: str | None = None,
) -> capacity.CapacityAssessment:
    return capacity.CapacityAssessment(
        request_id=context.request_id,
        user_id=context.user_id,
        as_of=context.request.request_date,
        requested_amount=context.request.requested_amount,
        minimum_balance_to_keep=context.profile.minimum_balance_to_keep,
        amount_safe_to_pay=Decimal(safe),
        uncapped_capacity=Decimal(safe),
        capped_by_request=False,
        binding_trough=None,
        earliest_date_for_full_payment=(
            date.fromisoformat(earliest) if earliest else None
        ),
        earliest_binding_trough=None,
        shortfall=None,
    )


# ---------------------------------------------------------------------------
# Criterion 1: no spending changes
# ---------------------------------------------------------------------------


def test_a_no_changes_candidate_beats_a_cheaper_one_requiring_changes() -> None:
    """Criterion 1 outranks criterion 2, so cost does not rescue changes."""
    cheap_with_changes = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "100")],
        total="100",
        requires_changes=True,
        option_id="payment_option_01",
        spending_changes=("stop:event_1",),
    )
    dearer_no_changes = make_candidate(
        Method.FULL_PAYMENT,
        payments=[("2025-06-02", "5000")],
        total="5000",
    )
    context = plain_context(requested_amount="5000")
    decision = rank.rank(
        context,
        make_set(cheap_with_changes, dearer_no_changes),
        fake_capacity(context, safe="5000", earliest="2025-06-02"),
    )
    assert decision.winner is dearer_no_changes
    assert decision.criterion is Criterion.NO_SPENDING_CHANGES
    assert decision.winner.total_paid > cheap_with_changes.total_paid


# ---------------------------------------------------------------------------
# Criteria 2-5 among installment options
# ---------------------------------------------------------------------------


def test_equal_totals_the_earlier_first_payment_wins() -> None:
    later = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-20", "500"), ("2025-07-20", "500")],
        total="1000",
        option_id="payment_option_01",
    )
    earlier = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "500"), ("2025-07-05", "500")],
        total="1000",
        option_id="payment_option_02",
    )
    context = plain_context(requested_amount="1000")
    decision = rank.rank(
        context, make_set(later, earlier), fake_capacity(context)
    )
    assert decision.winner is earlier
    assert decision.criterion is Criterion.EARLIEST_FIRST_PAYMENT


def test_equal_totals_and_dates_fewer_payments_wins() -> None:
    many = make_candidate(
        Method.INSTALLMENTS,
        payments=[
            ("2025-06-05", "250"),
            ("2025-07-05", "250"),
            ("2025-08-05", "250"),
            ("2025-09-05", "250"),
        ],
        total="1000",
        option_id="payment_option_01",
    )
    few = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "500"), ("2025-07-05", "500")],
        total="1000",
        option_id="payment_option_02",
    )
    context = plain_context(requested_amount="1000")
    decision = rank.rank(context, make_set(many, few), fake_capacity(context))
    assert decision.winner is few
    assert decision.criterion is Criterion.FEWER_PAYMENTS


def test_the_lowest_option_id_is_the_final_tie_break() -> None:
    high = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "500"), ("2025-07-05", "500")],
        total="1000",
        option_id="payment_option_20",
    )
    low = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "500"), ("2025-07-05", "500")],
        total="1000",
        option_id="payment_option_3",
    )
    context = plain_context(requested_amount="1000")
    decision = rank.rank(context, make_set(high, low), fake_capacity(context))
    assert decision.winner is low
    assert decision.criterion is Criterion.LOWEST_OPTION_ID


def test_option_ids_compare_numerically_not_lexicographically() -> None:
    assert rank_key(
        make_candidate(
            Method.INSTALLMENTS,
            payments=[("2025-06-05", "1")],
            total="1",
            option_id="payment_option_3",
        )
    ) < rank_key(
        make_candidate(
            Method.INSTALLMENTS,
            payments=[("2025-06-05", "1")],
            total="1",
            option_id="payment_option_20",
        )
    )


def test_criteria_are_applied_in_strict_order() -> None:
    """A cheaper plan wins even when it starts later and has more payments."""
    cheap_late_many = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-07-05", "100")] * 5,
        total="500",
        option_id="payment_option_09",
    )
    dear_early_few = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "900")],
        total="900",
        option_id="payment_option_01",
    )
    context = plain_context(requested_amount="500")
    decision = rank.rank(
        context, make_set(cheap_late_many, dear_early_few), fake_capacity(context)
    )
    assert decision.winner is cheap_late_many
    assert decision.criterion is Criterion.MINIMUM_TOTAL_PAID


# ---------------------------------------------------------------------------
# Wait never beats an immediate plan of equal cost
# ---------------------------------------------------------------------------


def test_wait_never_wins_over_a_safe_full_payment() -> None:
    wait = make_candidate(
        Method.WAIT, payments=[("2025-07-15", "1000")], total="1000"
    )
    immediate = make_candidate(
        Method.FULL_PAYMENT, payments=[("2025-06-02", "1000")], total="1000"
    )
    context = plain_context(requested_amount="1000")
    decision = rank.rank(
        context,
        make_set(wait, immediate),
        fake_capacity(context, safe="1000", earliest="2025-06-02"),
    )
    assert decision.winner is immediate
    assert decision.criterion is Criterion.EARLIEST_FIRST_PAYMENT
    assert decision.status is Status.AFFORDABLE_NOW


def test_wait_never_wins_over_a_partial_plan() -> None:
    wait = make_candidate(
        Method.WAIT, payments=[("2025-07-15", "1000")], total="1000"
    )
    partial = make_candidate(
        Method.PARTIAL_PAYMENT,
        payments=[("2025-06-02", "400"), ("2025-07-15", "600")],
        total="1000",
    )
    context = plain_context(requested_amount="1000")
    decision = rank.rank(
        context,
        make_set(wait, partial),
        fake_capacity(context, safe="400", earliest="2025-07-15"),
    )
    assert decision.winner is partial
    assert decision.criterion is Criterion.EARLIEST_FIRST_PAYMENT


def test_wait_does_beat_a_fee_bearing_installment_plan() -> None:
    """Documented consequence of the stated criterion order.

    Criterion 2 (minimum total paid) outranks criterion 3 (start earlier), and
    installments carry a financing fee while waiting does not. So waiting
    genuinely is the cheaper plan and the rulebook's order prefers it. This is
    the one case where "wait never beats an immediate plan" does not hold, and
    it holds *because* the rulebook says cost comes first.
    """
    wait = make_candidate(
        Method.WAIT, payments=[("2025-07-15", "1000")], total="1000"
    )
    installments = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "400"), ("2025-07-05", "400")],
        total="1200",
        option_id="payment_option_01",
    )
    context = plain_context(requested_amount="1000")
    decision = rank.rank(
        context,
        make_set(wait, installments),
        fake_capacity(context, safe="0", earliest="2025-07-15"),
    )
    assert decision.winner is wait
    assert decision.criterion is Criterion.MINIMUM_TOTAL_PAID
    assert decision.status is Status.AFFORDABLE_LATER


def test_no_labelled_row_exercises_a_ranking_tie_break(
    dataset: load.Dataset, cache
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    Criteria 2-5 fire on **zero** labelled rows after calibration.

    Before Phase 11, `request_04` offered both `wait` and a changes-based full
    payment, and criterion 1 separated them. Tightening the forecast (P11-S2)
    removed the changes candidate -- the changes can no longer rescue that plan
    -- so every sample row now has zero or one eligible candidate.

    Ranking is therefore entirely unexercised by the labels, and its 19/19
    result says only that it never picks a *wrong* single candidate. Criteria
    3, 4 and 5 rest on synthetic tests alone.
    """
    multi = []
    for request in dataset.sample_requests:
        _, candidate_set, _ = pipeline(dataset, request.request_id, cache)
        if len(candidate_set.eligible) > 1:
            multi.append(request.request_id)
    assert multi == [], f"a labelled row now has a real choice: {multi}"

    context, candidate_set, assessment = pipeline(dataset, "request_04", cache)
    decision = rank.rank(context, candidate_set, assessment)
    assert decision.criterion is Criterion.ONLY_CANDIDATE
    assert decision.method is Method.WAIT
    assert context.request.expected.recommended_payment_method == "wait"


# ---------------------------------------------------------------------------
# Status assignment
# ---------------------------------------------------------------------------


def test_affordable_now_needs_the_winner_to_be_a_plain_full_payment() -> None:
    context = plain_context(requested_amount="1000")
    winner = make_candidate(
        Method.FULL_PAYMENT, payments=[("2025-06-02", "1000")], total="1000"
    )
    assert (
        assign_status(
            winner, fake_capacity(context, safe="1000", earliest="2025-06-02"), context
        )
        is Status.AFFORDABLE_NOW
    )


def test_affordable_now_is_not_assigned_when_full_payment_is_not_considered() -> None:
    """Capacity is irrelevant if the user will not pay in full.

    ``amount_safe_to_pay`` covers the whole request and
    ``earliest_date_for_full_payment`` equals the request date, because both
    measure capacity independently of preference. The status must still follow
    the winning method, which is installments.
    """
    context = plain_context(
        requested_amount="1000", payment_methods=("installments",)
    )
    assessment = fake_capacity(context, safe="1000", earliest="2025-06-02")
    assert assessment.amount_safe_to_pay >= context.request.requested_amount
    assert assessment.earliest_date_for_full_payment == context.request.request_date

    winner = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "500"), ("2025-07-05", "500")],
        total="1050",
        option_id="payment_option_01",
    )
    decision = rank.rank(context, make_set(winner), assessment)
    assert decision.status is Status.AFFORDABLE_WITH_PLAN
    assert decision.status is not Status.AFFORDABLE_NOW


def test_full_payment_with_changes_is_affordable_with_plan() -> None:
    context = plain_context(requested_amount="1000")
    winner = make_candidate(
        Method.FULL_PAYMENT,
        payments=[("2025-06-02", "1000")],
        total="1000",
        requires_changes=True,
        spending_changes=("stop:event_1",),
    )
    decision = rank.rank(context, make_set(winner), fake_capacity(context, safe="900"))
    assert decision.status is Status.AFFORDABLE_WITH_PLAN


def test_partial_is_affordable_with_plan() -> None:
    context = plain_context(requested_amount="1000")
    winner = make_candidate(
        Method.PARTIAL_PAYMENT,
        payments=[("2025-06-02", "400"), ("2025-07-15", "600")],
        total="1000",
    )
    decision = rank.rank(
        context, make_set(winner), fake_capacity(context, safe="400", earliest="2025-07-15")
    )
    assert decision.status is Status.AFFORDABLE_WITH_PLAN


def test_wait_is_affordable_later() -> None:
    context = plain_context(requested_amount="1000")
    winner = make_candidate(Method.WAIT, payments=[("2025-07-15", "1000")], total="1000")
    decision = rank.rank(
        context, make_set(winner), fake_capacity(context, safe="0", earliest="2025-07-15")
    )
    assert decision.status is Status.AFFORDABLE_LATER


def test_nothing_eligible_is_not_affordable() -> None:
    context = plain_context(requested_amount="1000")
    decision = rank.rank(context, make_set(), fake_capacity(context))
    assert decision.status is Status.NOT_AFFORDABLE
    assert decision.method is Method.NOT_RECOMMENDED
    assert decision.criterion is Criterion.NO_CANDIDATE
    assert decision.runner_up is None


def test_status_is_never_derived_from_earliest_date_alone(
    dataset: load.Dataset, cache
) -> None:
    """Across the dataset, no installments winner is ever affordable_now."""
    for request in dataset.requests[:80]:
        context, candidate_set, assessment = pipeline(
            dataset, request.request_id, cache
        )
        decision = rank.rank(context, candidate_set, assessment)
        if decision.method is Method.INSTALLMENTS:
            assert decision.status is Status.AFFORDABLE_WITH_PLAN
        if decision.status is Status.AFFORDABLE_NOW:
            assert decision.method is Method.FULL_PAYMENT
            assert not decision.winner.requires_changes


# ---------------------------------------------------------------------------
# Totality
# ---------------------------------------------------------------------------


def test_an_unbreakable_tie_raises_rather_than_picking_one() -> None:
    first = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "500"), ("2025-07-05", "500")],
        total="1000",
        option_id="payment_option_07",
    )
    second = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "500"), ("2025-07-05", "500")],
        total="1000",
        option_id="payment_option_07",
    )
    context = plain_context(requested_amount="1000")
    with pytest.raises(RankingTieError) as excinfo:
        rank.rank(context, make_set(first, second), fake_capacity(context))
    assert "equal on all five ranking criteria" in str(excinfo.value)
    assert excinfo.value.request_id == "request_test"


def test_ranking_is_total_across_the_whole_dataset(
    dataset: load.Dataset, cache
) -> None:
    for request in list(dataset.requests) + list(dataset.sample_requests):
        context, candidate_set, assessment = pipeline(
            dataset, request.request_id, cache
        )
        rank.rank(context, candidate_set, assessment)  # must not raise


# ---------------------------------------------------------------------------
# The deadline stays a hard filter
# ---------------------------------------------------------------------------


def test_the_deadline_is_never_re_applied_as_a_soft_criterion() -> None:
    """It is absent from rank_key entirely."""
    early_deadline_miss = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2025-06-05", "500")],
        total="500",
        option_id="payment_option_01",
    )
    key = rank_key(early_deadline_miss)
    assert len(key) == 5
    assert Decimal("500") in key


def test_an_eligible_candidate_finishing_late_is_an_error() -> None:
    late = make_candidate(
        Method.INSTALLMENTS,
        payments=[("2030-01-01", "1000")],
        total="1000",
        option_id="payment_option_01",
    )
    context = plain_context(
        requested_amount="1000", desired_completion_date="2025-07-01"
    )
    with pytest.raises(DeadlineFilterError, match="hard filter"):
        rank.rank(context, make_set(late), fake_capacity(context))


def test_no_eligible_candidate_fails_the_deadline_anywhere(
    dataset: load.Dataset, cache
) -> None:
    for request in list(dataset.requests) + list(dataset.sample_requests):
        context, candidate_set, _ = pipeline(dataset, request.request_id, cache)
        rank.assert_deadline_filter_held(context, candidate_set)


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


def test_trace_records_the_winner_and_the_deciding_criterion(
    dataset: load.Dataset, cache, tmp_path: Path
) -> None:
    context, candidate_set, assessment = pipeline(dataset, "request_04", cache)
    decision = rank.rank(context, candidate_set, assessment)
    path = decision.write_trace(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["request_id"] == "request_04"
    assert payload["decision"]["method"] == decision.method.value
    assert payload["decision"]["status"] == decision.status.value
    assert payload["decision"]["decided_by"] == decision.criterion.value
    assert payload["candidates_ranked"]
    assert "rejections" in payload
    assert "capacity" in payload


def test_a_trace_records_the_runner_up_when_there_is_one() -> None:
    """No labelled row has a runner-up post-calibration, so this is synthetic."""
    first = make_candidate(
        Method.FULL_PAYMENT, payments=[("2025-06-02", "1000")], total="1000"
    )
    second = make_candidate(
        Method.WAIT, payments=[("2025-07-15", "1000")], total="1000"
    )
    context = plain_context(requested_amount="1000")
    decision = rank.rank(
        context,
        make_set(first, second),
        fake_capacity(context, safe="1000", earliest="2025-06-02"),
    )
    payload = decision.trace()
    assert payload["decision"]["runner_up"] is not None
    assert payload["decision"]["runner_up"]["method"] == "wait"
    assert payload["decision"]["decided_by"] == Criterion.EARLIEST_FIRST_PAYMENT.value


def test_traces_exist_for_every_request(dataset: load.Dataset) -> None:
    """Written by ``py audit/rank_report.py``."""
    if not rank.TRACE_DIR.is_dir():
        pytest.skip("traces not generated; run audit/rank_report.py")
    written = {p.stem for p in rank.TRACE_DIR.glob("*.json")}
    expected = {r.request_id for r in dataset.requests} | {
        r.request_id for r in dataset.sample_requests
    }
    assert expected <= written


# ---------------------------------------------------------------------------
# Calibration: the three numbers
# ---------------------------------------------------------------------------


def test_ranking_picks_the_labelled_method_whenever_it_was_generated(
    dataset: load.Dataset, cache
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    The only figure that measures ranking itself: 20/20.

    Held at 20/20 through P11-S2 (which briefly dipped availability to 19) and
    P11-S4, which restored it. Ranking picks the labelled method every time
    generation offers it one.

    The other two headline numbers -- method exact and status exact -- are
    dominated by the forecast, not by ranking.
    """
    available = 0
    correct = 0
    for request in dataset.sample_requests:
        context, candidate_set, assessment = pipeline(
            dataset, request.request_id, cache
        )
        decision = rank.rank(context, candidate_set, assessment)
        generated = {c.method.value for c in candidate_set.eligible}
        truth = request.expected.recommended_payment_method
        if truth in generated or (truth == "not_recommended" and not generated):
            available += 1
            if decision.method.value == truth:
                correct += 1
    assert available == 20
    assert correct == available, "ranking failed on a row where the label was available"


def test_the_status_only_misses_are_exactly_the_spending_change_rows(
    dataset: load.Dataset, cache
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    Three rows get the method right and the status wrong.

    They are `request_06`, `request_11` and `request_21` -- precisely the rows
    whose labels carry spending changes (C7). The overstated forecast reports
    no change is needed, so the winner is a plain full payment and the status
    becomes `affordable_now` instead of `affordable_with_plan`. A status
    defect would show up on other rows; this one is C3e.
    """
    method_ok_status_wrong = []
    for request in dataset.sample_requests:
        context, candidate_set, assessment = pipeline(
            dataset, request.request_id, cache
        )
        decision = rank.rank(context, candidate_set, assessment)
        truth = request.expected
        if (
            decision.method.value == truth.recommended_payment_method
            and decision.status.value != truth.affordability_status
        ):
            method_ok_status_wrong.append(request.request_id)
    assert sorted(method_ok_status_wrong) == [
        "request_06",
        "request_11",
        "request_21",
    ]
    for request_id in method_ok_status_wrong:
        assert dataset.request(request_id).expected.spending_changes_needed != "none"

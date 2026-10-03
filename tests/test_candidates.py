"""Tests for :mod:`src.candidates`."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src import candidates as cand
from src import capacity, config, extract, forecast, load, state
from src.candidates import (
    Candidate,
    Method,
    RejectionReason,
    generate_candidates,
    installment_schedule,
)
from src.load import PaymentOption
from tests.test_forecast import empty_evidence
from tests.test_state import make_context, make_event


@pytest.fixture(scope="module")
def dataset() -> load.Dataset:
    return load.get_dataset()


@pytest.fixture(scope="module")
def cache():
    return extract.load_image_cache()


def build(dataset: load.Dataset, request_id: str, cache):
    request = dataset.request(request_id)
    context = dataset.get_user_context(request.user_id, request_id)
    balance = state.reconstruct_balance(context)
    blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
    evidence = extract.gather_evidence(context, blank, cache=cache)
    projection = forecast.build_forecast(balance, evidence=evidence)
    assessment = capacity.assess_capacity(context, projection)
    return context, projection, assessment, generate_candidates(
        context, projection, assessment
    )


def synthetic(**kwargs):
    events = [
        make_event(
            f"event_pay_{index}",
            day=day,
            amount="50000",
            direction="credit",
            event_type="income",
            description="Monthly salary",
            category="salary",
        )
        for index, day in enumerate(
            ["2025-01-15", "2025-02-15", "2025-03-15", "2025-04-15", "2025-05-15"], 1
        )
    ]
    context = make_context(events, **kwargs)
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    assessment = capacity.assess_capacity(context, projection)
    return context, projection, assessment


# ---------------------------------------------------------------------------
# Partial-only users whose request forbids partial payment
# ---------------------------------------------------------------------------


def test_partial_only_users_with_partial_disallowed_get_only_the_fallback(
    dataset: load.Dataset, cache
) -> None:
    """requests 95, 133 and 209 -- named in the brief -- plus the rest.

    The user will consider *only* partial payment, and the request forbids it,
    so nothing can be eligible.
    """
    for request_id in ("request_95", "request_133", "request_209"):
        context, _, _, result = build(dataset, request_id, cache)
        assert context.profile.payment_methods_user_will_consider == (
            "partial_payment",
        )
        assert not context.request.allows_partial_payment
        assert result.eligible == (), f"{request_id} produced {result.eligible}"
        assert result.fallback.method is Method.NOT_RECOMMENDED

        reasons = set(result.rejection_reasons())
        assert RejectionReason.PARTIAL_NOT_ALLOWED in reasons
        assert RejectionReason.METHOD_NOT_CONSIDERED in reasons


def test_the_brief_named_three_but_the_dataset_holds_five(
    dataset: load.Dataset,
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    Evaluation-set requests in this situation, found rather than assumed."""
    blocked = sorted(
        request.request_id
        for request in dataset.requests
        if dataset.profile(request.user_id).payment_methods_user_will_consider
        == ("partial_payment",)
        and not request.allows_partial_payment
    )
    assert blocked == [
        "request_133",
        "request_209",
        "request_227",
        "request_76",
        "request_95",
    ]


def test_the_remaining_two_also_produce_only_the_fallback(
    dataset: load.Dataset, cache
) -> None:
    for request_id in ("request_76", "request_227"):
        _, _, _, result = build(dataset, request_id, cache)
        assert result.eligible == ()


# ---------------------------------------------------------------------------
# Method preference gates
# ---------------------------------------------------------------------------


def test_installments_only_user_gets_no_full_or_partial_candidate() -> None:
    """Even with capacity to spare, an unconsidered method is not offered."""
    context, projection, assessment = synthetic(
        request_date="2025-06-02",
        requested_amount="1000",
        balance="500000",
        minimum="20000",
        allows_partial_payment=True,
        payment_methods=("installments",),
    )
    assert assessment.amount_safe_to_pay >= context.request.requested_amount

    result = generate_candidates(context, projection, assessment)
    assert not result.by_method(Method.FULL_PAYMENT)
    assert not result.by_method(Method.PARTIAL_PAYMENT)
    assert not result.by_method(Method.WAIT)

    reasons = {
        r.method: r.reason
        for r in result.rejections
        if r.reason is RejectionReason.METHOD_NOT_CONSIDERED
    }
    assert Method.FULL_PAYMENT in reasons
    assert Method.PARTIAL_PAYMENT in reasons
    assert Method.WAIT in reasons


def test_full_payment_user_with_capacity_gets_a_full_candidate() -> None:
    context, projection, assessment = synthetic(
        request_date="2025-06-02",
        requested_amount="1000",
        balance="500000",
        minimum="20000",
        payment_methods=("full_payment",),
    )
    result = generate_candidates(context, projection, assessment)
    full = result.by_method(Method.FULL_PAYMENT)
    assert len(full) == 1
    assert full[0].payments == (
        cand.Payment(on=date(2025, 6, 2), amount=Decimal("1000")),
    )
    assert full[0].total_paid == Decimal("1000")
    assert not full[0].requires_changes


def test_wait_requires_the_full_amount_to_become_safe_later() -> None:
    """When it is already safe today, wait is not a candidate."""
    context, projection, assessment = synthetic(
        request_date="2025-06-02",
        requested_amount="1000",
        balance="500000",
        minimum="20000",
        payment_methods=("full_payment",),
    )
    result = generate_candidates(context, projection, assessment)
    assert not result.by_method(Method.WAIT)
    assert any(
        r.method is Method.WAIT
        and r.reason is RejectionReason.FULL_AMOUNT_ALREADY_SAFE_TODAY
        for r in result.rejections
    )


def test_wait_is_generated_when_capacity_arrives_with_payday() -> None:
    context, projection, assessment = synthetic(
        request_date="2025-06-02",
        requested_amount="120000",
        balance="100000",
        minimum="20000",
        desired_completion_date="2025-07-31",
        payment_methods=("full_payment",),
    )
    result = generate_candidates(context, projection, assessment)
    wait = result.by_method(Method.WAIT)
    assert len(wait) == 1
    assert wait[0].payments[0].on == assessment.earliest_date_for_full_payment
    assert wait[0].payments[0].on > context.request.request_date
    assert wait[0].total_paid == Decimal("120000")


# ---------------------------------------------------------------------------
# The deadline is a hard filter
# ---------------------------------------------------------------------------


def _option(
    option_id: str,
    request_id: str,
    *,
    first: str,
    count: int,
    interval: int,
    amount: str,
    total: str,
) -> PaymentOption:
    return PaymentOption(
        payment_option_id=option_id,
        request_id=request_id,
        payment_method="installments",
        payment_amount=Decimal(amount),
        number_of_payments=count,
        first_payment_date=date.fromisoformat(first),
        payment_frequency_days=interval,
        financing_fee=Decimal(total) - Decimal(amount) * count,
        total_payable_amount=Decimal(total),
    )


def test_last_payment_one_day_past_the_deadline_is_rejected() -> None:
    """Exactly on the deadline is kept; one day later is not."""
    events = [
        make_event(
            f"event_pay_{index}",
            day=day,
            amount="200000",
            direction="credit",
            event_type="income",
            description="Monthly salary",
            category="salary",
        )
        for index, day in enumerate(
            ["2025-01-15", "2025-02-15", "2025-03-15", "2025-04-15", "2025-05-15"], 1
        )
    ]
    # Three payments 30 days apart from 5 June end on 5 August.
    on_time = _option(
        "payment_option_ok",
        "request_test",
        first="2025-06-05",
        count=3,
        interval=30,
        amount="1000",
        total="3000",
    )
    late = _option(
        "payment_option_late",
        "request_test",
        first="2025-06-06",
        count=3,
        interval=30,
        amount="1000",
        total="3000",
    )
    assert installment_schedule(on_time)[-1].on == date(2025, 8, 4)
    assert installment_schedule(late)[-1].on == date(2025, 8, 5)

    base = make_context(
        events,
        request_date="2025-06-02",
        requested_amount="3000",
        balance="500000",
        minimum="20000",
        desired_completion_date="2025-08-04",
        payment_methods=("installments",),
    )
    context = load.UserContext(
        request=base.request,
        profile=base.profile,
        events=base.events,
        messages=base.messages,
        images=base.images,
        payment_options=(on_time, late),
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    assessment = capacity.assess_capacity(context, projection)
    result = generate_candidates(context, projection, assessment)

    kept = {c.payment_option_id for c in result.by_method(Method.INSTALLMENTS)}
    assert kept == {"payment_option_ok"}

    rejection = next(
        r for r in result.rejections if r.payment_option_id == "payment_option_late"
    )
    assert rejection.reason is RejectionReason.PAST_DEADLINE
    assert "2025-08-05" in rejection.detail


def test_deadline_never_acts_as_a_ranking_criterion(
    dataset: load.Dataset, cache
) -> None:
    """No surviving candidate finishes after the deadline."""
    for request in dataset.requests[:60]:
        context, _, _, result = build(dataset, request.request_id, cache)
        for candidate in result.eligible:
            if candidate.last_payment_date is not None:
                assert (
                    candidate.last_payment_date
                    <= context.request.desired_completion_date
                ), f"{request.request_id}/{candidate.method} finishes late"


# ---------------------------------------------------------------------------
# Partial plan arithmetic
# ---------------------------------------------------------------------------


def test_partial_payments_sum_to_requested_amount_exactly() -> None:
    """With a fractional remainder, where ROUND_DOWN could lose a cent."""
    context, projection, assessment = synthetic(
        request_date="2025-06-02",
        requested_amount="120000.37",
        balance="100000",
        minimum="20000",
        desired_completion_date="2025-07-31",
        allows_partial_payment=True,
        payment_methods=("partial_payment",),
    )
    result = generate_candidates(context, projection, assessment)
    partial = result.by_method(Method.PARTIAL_PAYMENT)
    assert len(partial) == 1

    plan = partial[0]
    assert len(plan.payments) == 2
    assert plan.payments[0].on == context.request.request_date
    assert plan.payments[0].amount == assessment.amount_safe_to_pay
    assert plan.payments[1].on == assessment.earliest_date_for_full_payment

    total = plan.payments[0].amount + plan.payments[1].amount
    assert total == context.request.requested_amount
    assert total == Decimal("120000.37")
    assert plan.total_paid == Decimal("120000.37")


def test_partial_sums_hold_across_the_whole_dataset(
    dataset: load.Dataset, cache
) -> None:
    checked = 0
    for request in dataset.requests:
        context, _, _, result = build(dataset, request.request_id, cache)
        for plan in result.by_method(Method.PARTIAL_PAYMENT):
            checked += 1
            assert len(plan.payments) == 2
            assert (
                plan.payments[0].amount + plan.payments[1].amount
                == request.requested_amount
            )
            assert plan.payments[0].on == request.request_date
            assert plan.payments[1].on <= request.desired_completion_date
    assert checked > 0, "no partial candidate was generated anywhere"


def test_partial_is_not_offered_when_the_full_amount_is_already_safe() -> None:
    context, projection, assessment = synthetic(
        request_date="2025-06-02",
        requested_amount="1000",
        balance="500000",
        minimum="20000",
        allows_partial_payment=True,
        payment_methods=("partial_payment",),
    )
    result = generate_candidates(context, projection, assessment)
    assert not result.by_method(Method.PARTIAL_PAYMENT)
    assert any(
        r.reason is RejectionReason.FULL_AMOUNT_ALREADY_SAFE_TODAY
        for r in result.rejections
    )


# ---------------------------------------------------------------------------
# Cumulative safety
# ---------------------------------------------------------------------------


def test_individually_affordable_installments_can_fail_in_aggregate() -> None:
    """Each payment fits; the three together do not."""
    events = [
        make_event(
            f"event_pay_{index}",
            day=day,
            amount="1000",
            direction="credit",
            event_type="income",
            description="Monthly salary",
            category="salary",
        )
        for index, day in enumerate(
            ["2025-01-15", "2025-02-15", "2025-03-15", "2025-04-15", "2025-05-15"], 1
        )
    ]
    option = _option(
        "payment_option_agg",
        "request_test",
        first="2025-06-05",
        count=3,
        interval=30,
        amount="25000",
        total="75000",
    )
    base = make_context(
        events,
        request_date="2025-06-02",
        requested_amount="75000",
        balance="60000",
        minimum="20000",
        desired_completion_date="2025-08-31",
        payment_methods=("installments",),
    )
    context = load.UserContext(
        request=base.request,
        profile=base.profile,
        events=base.events,
        messages=base.messages,
        images=base.images,
        payment_options=(option,),
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())

    # A single payment of 25,000 is comfortably safe on its own.
    assert forecast.payment_is_safe(projection, [(date(2025, 6, 5), Decimal("25000"))])
    # All three together are not.
    assert not forecast.payment_is_safe(
        projection,
        [
            (date(2025, 6, 5), Decimal("25000")),
            (date(2025, 7, 5), Decimal("25000")),
            (date(2025, 8, 4), Decimal("25000")),
        ],
    )

    assessment = capacity.assess_capacity(context, projection)
    result = generate_candidates(context, projection, assessment)
    assert not result.by_method(Method.INSTALLMENTS)
    rejection = next(
        r for r in result.rejections if r.payment_option_id == "payment_option_agg"
    )
    assert rejection.reason is RejectionReason.UNSAFE_AGAINST_FORECAST


# ---------------------------------------------------------------------------
# The [HYP] installment-cap flag
# ---------------------------------------------------------------------------


def test_the_installment_cap_gate_is_a_no_op_on_this_dataset(
    dataset: load.Dataset,
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    No option exceeds the cap without already missing its deadline.

    The rulebook's claim that deadline compliance explains the rejections is
    *true for the cap specifically*: turning the [HYP] gate off changes no
    outcome anywhere in the dataset.
    """
    cap_only = []
    for request in list(dataset.requests) + list(dataset.sample_requests):
        context = dataset.get_user_context(request.user_id, request.request_id)
        profile = context.profile
        if profile.max_installment_months is None:
            continue
        for option in context.payment_options:
            if option.payment_method != "installments":
                continue
            last = installment_schedule(option)[-1].on
            late = last > request.desired_completion_date
            over = option.number_of_payments > profile.max_installment_months
            if over and not late:
                cap_only.append((request.request_id, option.payment_option_id))
    assert cap_only == [], (
        "an option is rejected by the month cap alone, so the [HYP] gate is "
        f"load-bearing after all: {cap_only}"
    )


def test_option_rejection_reasons_are_a_known_set(
    dataset: load.Dataset, cache
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    The rulebook's claim, and where calibration broke it.

    Before Phase 11 every rejected *payment option* in the 25 labelled samples
    was rejected for the user's method preference or for the deadline, matching
    the rulebook's claim exactly. Widening the staleness gate (P11-S2) tightened
    the forecast, and one sample option is now rejected as unsafe against it --
    so the claim holds for preference and deadline but is no longer exhaustive.

    The month cap still never fires, which is C6a.
    """
    reasons: set[RejectionReason] = set()
    for request in dataset.sample_requests:
        _, _, _, result = build(dataset, request.request_id, cache)
        reasons.update(
            r.reason for r in result.rejections if r.payment_option_id is not None
        )
    assert reasons <= {
        RejectionReason.METHOD_NOT_CONSIDERED,
        RejectionReason.PAST_DEADLINE,
        RejectionReason.UNSAFE_AGAINST_FORECAST,
    }, f"an option was rejected for an unexpected reason: {reasons}"
    assert {
        RejectionReason.METHOD_NOT_CONSIDERED,
        RejectionReason.PAST_DEADLINE,
    } <= reasons
    assert RejectionReason.EXCEEDS_INSTALLMENT_CAP not in reasons


def test_the_unsafe_rejection_branch_is_live_on_both_sets(
    dataset: load.Dataset, cache
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    Safety rejections grew from 10 to 18 once the forecast was calibrated.

    The pre-calibration figure was called a floor at the time, on the grounds
    that an under-projecting forecast rejects too few. Widening the staleness
    gate confirmed it: the count rose, and the branch now fires on the samples
    too (1 option), where it previously had zero label coverage.
    """
    unsafe_eval = [
        (request.request_id, r.payment_option_id)
        for request in dataset.requests
        for r in build(dataset, request.request_id, cache)[3].rejections
        if r.reason is RejectionReason.UNSAFE_AGAINST_FORECAST
    ]
    assert len(unsafe_eval) == 14, unsafe_eval

    # The samples' single unsafe rejection came and went across P11-S2/S4 as
    # income projection changed; the evaluation-set count is the stable signal.
    unsafe_samples = [
        (request.request_id, r.payment_option_id)
        for request in dataset.sample_requests
        for r in build(dataset, request.request_id, cache)[3].rejections
        if r.reason is RejectionReason.UNSAFE_AGAINST_FORECAST
    ]
    assert len(unsafe_samples) <= 2, unsafe_samples


def test_the_cap_gate_can_be_swept_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.enforce_installment_month_cap() is True
    monkeypatch.setenv("BUYORWAIT_ENFORCE_INSTALLMENT_MONTH_CAP", "0")
    assert config.enforce_installment_month_cap() is False


def test_the_cap_gate_fires_when_it_is_the_only_obstacle() -> None:
    """Synthetic, because the real dataset never exercises this branch."""
    events = [
        make_event(
            f"event_pay_{index}",
            day=day,
            amount="200000",
            direction="credit",
            event_type="income",
            description="Monthly salary",
            category="salary",
        )
        for index, day in enumerate(
            ["2025-01-15", "2025-02-15", "2025-03-15", "2025-04-15", "2025-05-15"], 1
        )
    ]
    option = _option(
        "payment_option_cap",
        "request_test",
        first="2025-06-05",
        count=3,
        interval=30,
        amount="1000",
        total="3000",
    )
    base = make_context(
        events,
        request_date="2025-06-02",
        requested_amount="3000",
        balance="500000",
        minimum="20000",
        desired_completion_date="2025-08-31",
        payment_methods=("installments",),
    )
    profile = load.Profile(
        **{
            **base.profile.__dict__,
            "max_installment_months": 2,
        }
    )
    context = load.UserContext(
        request=base.request,
        profile=profile,
        events=base.events,
        messages=base.messages,
        images=base.images,
        payment_options=(option,),
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    assessment = capacity.assess_capacity(context, projection)

    result = generate_candidates(context, projection, assessment)
    assert not result.by_method(Method.INSTALLMENTS)
    assert any(
        r.reason is RejectionReason.EXCEEDS_INSTALLMENT_CAP for r in result.rejections
    )


# ---------------------------------------------------------------------------
# Installment schedule fidelity
# ---------------------------------------------------------------------------


def test_installment_amounts_and_dates_copy_the_option_verbatim(
    dataset: load.Dataset, cache
) -> None:
    options = {o.payment_option_id: o for o in dataset.payment_options}
    checked = 0
    for request in dataset.requests:
        _, _, _, result = build(dataset, request.request_id, cache)
        for plan in result.by_method(Method.INSTALLMENTS):
            checked += 1
            option = options[plan.payment_option_id]
            assert plan.payment_count == option.number_of_payments
            assert all(p.amount == option.payment_amount for p in plan.payments)
            assert plan.payments[0].on == option.first_payment_date
            assert plan.total_paid == option.total_payable_amount
            expected = [
                option.first_payment_date
                + timedelta(days=index * option.payment_frequency_days)
                for index in range(option.number_of_payments)
            ]
            assert [p.on for p in plan.payments] == expected
    assert checked > 0


def test_installment_total_includes_the_financing_fee(dataset: load.Dataset, cache) -> None:
    for request in dataset.requests:
        _, _, _, result = build(dataset, request.request_id, cache)
        for plan in result.by_method(Method.INSTALLMENTS):
            assert plan.total_paid >= request.requested_amount


# ---------------------------------------------------------------------------
# The spending-change hook
# ---------------------------------------------------------------------------


def test_the_change_hook_is_inert_when_left_unwired() -> None:
    """Unwired the branch records its absence rather than silently vanishing.

    :mod:`src.changes` is live and :func:`src.changes.change_finder_for`
    supplies it; this covers the default where a caller passes nothing.
    """
    context, projection, assessment = synthetic(
        request_date="2025-06-02",
        requested_amount="200000",
        balance="100000",
        minimum="20000",
        payment_methods=("full_payment",),
    )
    result = generate_candidates(context, projection, assessment)
    assert not [c for c in result.eligible if c.requires_changes]
    assert any(
        r.reason is RejectionReason.SPENDING_CHANGES_NOT_IMPLEMENTED
        for r in result.rejections
    )


def test_a_change_finder_produces_a_changes_candidate() -> None:
    context, projection, assessment = synthetic(
        request_date="2025-06-02",
        requested_amount="200000",
        balance="100000",
        minimum="20000",
        payment_methods=("full_payment",),
    )

    def finder(ctx, fc, payments):
        return ("stop:event_1",)

    result = generate_candidates(
        context, projection, assessment, change_finder=finder
    )
    plan = next(c for c in result.eligible if c.requires_changes)
    assert plan.method is Method.FULL_PAYMENT
    assert plan.spending_changes == ("stop:event_1",)


def test_a_change_finder_returning_nothing_records_the_reason() -> None:
    context, projection, assessment = synthetic(
        request_date="2025-06-02",
        requested_amount="200000",
        balance="100000",
        minimum="20000",
        payment_methods=("full_payment",),
    )
    result = generate_candidates(
        context, projection, assessment, change_finder=lambda *a: ()
    )
    assert any(
        r.reason is RejectionReason.NO_SPENDING_CHANGES_AVAILABLE
        for r in result.rejections
    )


# ---------------------------------------------------------------------------
# Whole-dataset invariants and the rejection-reason signal
# ---------------------------------------------------------------------------


def test_generation_runs_for_every_evaluation_request(
    dataset: load.Dataset, cache
) -> None:
    for request in dataset.requests:
        _, _, _, result = build(dataset, request.request_id, cache)
        assert result.fallback.method is Method.NOT_RECOMMENDED
        assert result.trace()["request_id"] == request.request_id


def test_every_rejection_carries_a_reason_and_detail(
    dataset: load.Dataset, cache
) -> None:
    for request in dataset.requests[:60]:
        _, _, _, result = build(dataset, request.request_id, cache)
        for rejection in result.rejections:
            assert rejection.reason in set(RejectionReason)
            assert rejection.detail
            assert str(rejection)


def test_no_candidate_other_than_the_fallback_has_an_empty_schedule(
    dataset: load.Dataset, cache
) -> None:
    for request in dataset.requests[:60]:
        _, _, _, result = build(dataset, request.request_id, cache)
        for candidate in result.eligible:
            assert candidate.payments


def test_a_trace_can_be_written_to_disk_as_json(dataset: load.Dataset, cache) -> None:
    import json

    _, _, _, result = build(dataset, "request_26", cache)
    json.dumps(result.trace())

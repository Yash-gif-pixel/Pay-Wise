"""Tests for :mod:`src.forecast`.

The headline case is :func:`test_comfortable_on_payment_day_but_trough_fails`:
a payment that leaves a healthy balance on the day it is made, and still must
be rejected because the pre-salary dip two weeks later breaks the floor.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src import config, extract, forecast, load, state
from src.extract import Evidence, ExtractedFact, UnresolvedBlankAmountError
from src.forecast import (
    ConflictRule,
    ExclusionReason,
    SalaryPlan,
    build_forecast,
    payment_is_safe,
    troughs,
    with_payments,
)
from src.patterns import FactKind
from tests.test_state import make_context, make_event


@pytest.fixture(scope="module")
def dataset() -> load.Dataset:
    return load.get_dataset()


@pytest.fixture(scope="module")
def cache():
    return extract.load_image_cache()


def empty_evidence(request_id: str = "request_test") -> Evidence:
    return Evidence(
        request_id=request_id, user_id="user_test", facts=(), resolved_amounts={}
    )


def fact(
    kind: FactKind,
    *,
    amount: str | None = None,
    on: str | None = None,
    percent: str | None = None,
    source_id: str = "message_test",
) -> ExtractedFact:
    return ExtractedFact(
        source_id=source_id,
        source_kind="message",
        kind=kind,
        effective_date=date.fromisoformat(on) if on else None,
        amount=Decimal(amount) if amount else None,
        currency="INR",
        target_event_id=None,
        confidence=1.0,
        is_instruction_attempt=False,
        template="test",
        note="test fact",
        percent=Decimal(percent) if percent else None,
    )


# ---------------------------------------------------------------------------
# THE HEADLINE CASE: payment day is fine, the trough is not
# ---------------------------------------------------------------------------


def test_comfortable_on_payment_day_but_trough_fails() -> None:
    """A payment can look affordable and still be unsafe.

    The user holds 100,000 with a 20,000 floor. Rent of 40,000 lands on the
    3rd, salary of 45,000 on the 15th, and groceries of 2,000 run weekly --
    so 48,000 leaves each month against 45,000 arriving. Paying 35,000 on the
    2nd leaves 65,000 that day, over three times the floor, and even the first
    pre-salary trough on 9 June holds at 23,000.

    The plan still fails. Each month's trough sits ~3,000 below the last, and
    by 11 August the pre-salary dip reaches 15,000 -- under the floor, 70 days
    after a payment that looked comfortable on the day. Checking the payment
    day, or only the next few weeks, would have passed it.
    """
    events = []
    # Rent, monthly on the 3rd.
    for index, day in enumerate(
        ["2025-01-03", "2025-02-03", "2025-03-03", "2025-04-03", "2025-05-03"], 1
    ):
        events.append(
            make_event(
                f"event_rent_{index}",
                day=day,
                amount="40000",
                description="Residential rent",
                category="rent",
            )
        )
    # Salary, monthly on the 15th.
    for index, day in enumerate(
        ["2025-01-15", "2025-02-15", "2025-03-15", "2025-04-15", "2025-05-15"], 1
    ):
        events.append(
            make_event(
                f"event_pay_{index}",
                day=day,
                amount="45000",
                direction="credit",
                event_type="income",
                description="Monthly salary",
                category="salary",
            )
        )
    # Weekly groceries, 2,000 a week.
    day = date(2025, 1, 6)
    index = 1
    while day <= date(2025, 5, 26):
        events.append(
            make_event(
                f"event_food_{index}",
                day=day.isoformat(),
                amount="2000",
                description="Weekly groceries",
                category="groceries",
            )
        )
        day += timedelta(days=7)
        index += 1

    context = make_context(events, request_date="2025-06-02")
    balance = state.reconstruct_balance(context)
    # Opening balance is fixed at 100,000 by the synthetic profile.
    assert balance.available_balance == Decimal("100000")
    assert balance.minimum_balance_to_keep == Decimal("20000")

    projection = build_forecast(balance, evidence=empty_evidence())
    payment_day = date(2025, 6, 2)
    payment = Decimal("35000")

    after = with_payments(projection, [(payment_day, payment)])

    # On the day itself the balance looks entirely comfortable.
    assert after.balance_on(payment_day) == Decimal("65000")
    assert after.balance_on(payment_day) > balance.minimum_balance_to_keep * 3

    # The first trough is also fine, so a short lookahead would pass this too.
    early = [t for t in after.troughs() if t.on <= payment_day + timedelta(days=30)]
    assert early
    assert all(t.balance >= balance.minimum_balance_to_keep for t in early)

    # The binding trough is much later, and it breaks the floor.
    binding = after.binding_trough(payment_day)
    assert binding is not None
    assert binding.on > payment_day + timedelta(days=60)
    assert binding.balance < balance.minimum_balance_to_keep
    assert not payment_is_safe(projection, [(payment_day, payment)])

    # It is a pre-salary dip, not a terminal decline: the balance recovers.
    assert after.balance_on(after.end) > binding.balance


def test_a_smaller_payment_clears_the_same_trough() -> None:
    """The same setup, sized to fit, is accepted -- so the test above is sharp."""
    events = []
    for index, day in enumerate(
        ["2025-01-03", "2025-02-03", "2025-03-03", "2025-04-03", "2025-05-03"], 1
    ):
        events.append(
            make_event(
                f"event_rent_{index}",
                day=day,
                amount="40000",
                description="Residential rent",
                category="rent",
            )
        )
    for index, day in enumerate(
        ["2025-01-15", "2025-02-15", "2025-03-15", "2025-04-15", "2025-05-15"], 1
    ):
        events.append(
            make_event(
                f"event_pay_{index}",
                day=day,
                amount="45000",
                direction="credit",
                event_type="income",
                description="Monthly salary",
                category="salary",
            )
        )
    context = make_context(events, request_date="2025-06-02")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, evidence=empty_evidence())

    assert payment_is_safe(projection, [(date(2025, 6, 2), Decimal("5000"))])
    assert not payment_is_safe(projection, [(date(2025, 6, 2), Decimal("70000"))])


# ---------------------------------------------------------------------------
# Troughs
# ---------------------------------------------------------------------------


def test_troughs_finds_every_local_minimum() -> None:
    events = [
        make_event("event_1", day="2025-01-10", amount="100", status="pending",
                   settlement="2025-01-10"),
    ]
    context = make_context(events, request_date="2025-01-01")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, horizon=10, evidence=empty_evidence())
    found = troughs(projection)
    assert found
    assert all(isinstance(t.balance, Decimal) for t in found)
    # Every reported trough really is a minimum of its neighbourhood.
    values = [b for _, b in projection.balances]
    for trough in found:
        index = (trough.on - projection.start).days
        if index > 0:
            assert values[index - 1] > trough.balance
        end_index = (trough.until - projection.start).days
        if end_index < len(values) - 1:
            assert values[end_index + 1] > trough.balance


def test_a_plateau_counts_once() -> None:
    events = [make_event("event_1", day="2024-12-01", amount="500")]
    context = make_context(events, request_date="2025-01-01")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, horizon=5, evidence=empty_evidence())
    # Nothing happens in the window, so the whole flat run is one trough.
    found = troughs(projection)
    assert len(found) == 1
    assert found[0].on == projection.start
    assert found[0].until == projection.end


def test_the_final_day_can_be_a_trough() -> None:
    """The horizon can end mid-decline, and that dip still binds."""
    events = [
        make_event(
            "event_1",
            day="2025-01-20",
            amount="30000",
            status="scheduled",
            settlement="2025-01-20",
            description="Big scheduled debit",
        )
    ]
    context = make_context(events, request_date="2025-01-01")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, horizon=25, evidence=empty_evidence())
    found = troughs(projection)
    assert found[-1].until == projection.end


def test_minimum_on_or_after_ignores_earlier_dips() -> None:
    events = [
        make_event("event_1", day="2025-01-05", amount="50000", status="pending",
                   settlement="2025-01-05"),
        make_event(
            "event_2",
            day="2025-01-20",
            amount="60000",
            direction="credit",
            status="scheduled",
            settlement="2025-01-20",
            event_type="income",
            description="Salary",
            category="salary",
        ),
    ]
    context = make_context(events, request_date="2025-01-01")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, horizon=30, evidence=empty_evidence())
    assert projection.minimum_on_or_after(date(2025, 1, 25)) > projection.minimum


def test_is_safe_from_uses_the_floor() -> None:
    events = [make_event("event_1", day="2024-12-01", amount="100")]
    context = make_context(events, request_date="2025-01-01")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, horizon=10, evidence=empty_evidence())
    assert projection.is_safe_from(projection.start)


# ---------------------------------------------------------------------------
# Amendments and conflict resolution
# ---------------------------------------------------------------------------


def _salary_context(request_date: str = "2025-06-02"):
    events = []
    for index, day in enumerate(
        ["2025-01-15", "2025-02-15", "2025-03-15", "2025-04-15", "2025-05-15"], 1
    ):
        events.append(
            make_event(
                f"event_pay_{index}",
                day=day,
                amount="45000",
                direction="credit",
                event_type="income",
                description="Monthly salary",
                category="salary",
            )
        )
    return make_context(events, request_date=request_date)


def test_salary_amount_change_overrides_history_and_logs_the_rule() -> None:
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(fact(FactKind.SALARY_AMOUNT_CHANGE, amount="60000", on="2025-06-15"),),
        resolved_amounts={},
    )
    amendments = forecast.apply_amendments(balance, evidence)
    assert amendments.salary.amount == Decimal("60000")
    rules = {c.rule for c in amendments.conflicts}
    assert ConflictRule.EXPLICIT_AMENDMENT in rules
    decision = next(c for c in amendments.conflicts if c.subject == "salary amount")
    assert "45000" in decision.detail and "60000" in decision.detail


def test_salary_date_change_moves_the_payday() -> None:
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(fact(FactKind.SALARY_DATE_CHANGE, on="2025-06-23"),),
        resolved_amounts={},
    )
    projection = build_forecast(balance, evidence=evidence)
    paydays = [f.on for f in projection.flows if f.category == "salary"]
    assert date(2025, 6, 23) in paydays


def test_employment_ended_stops_all_projected_salary() -> None:
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(fact(FactKind.EMPLOYMENT_ENDED, on="2025-06-10"),),
        resolved_amounts={},
    )
    projection = build_forecast(balance, evidence=evidence)
    assert not [f for f in projection.flows if f.category == "salary"]
    assert any(
        c.rule is ConflictRule.EXPLICIT_AMENDMENT and c.subject == "salary"
        for c in projection.amendments.conflicts
    )


def test_income_ending_replaces_the_amount_with_the_remaining_salary() -> None:
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(fact(FactKind.INCOME_ENDING, amount="26000"),),
        resolved_amounts={},
    )
    amendments = forecast.apply_amendments(balance, evidence)
    assert amendments.salary.amount == Decimal("26000")


def test_salary_reduction_applies_to_the_first_payday_only() -> None:
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(fact(FactKind.SALARY_REDUCED, amount="20000"),),
        resolved_amounts={},
    )
    projection = build_forecast(balance, evidence=evidence)
    salary_flows = sorted(
        (f for f in projection.flows if f.category == "salary"), key=lambda f: f.on
    )
    assert salary_flows[0].amount == Decimal("20000")
    assert salary_flows[1].amount == Decimal("45000")
    assert any(
        c.rule is ConflictRule.SAFER_INTERPRETATION
        for c in projection.amendments.conflicts
    )


def test_confirmed_future_income_adds_a_dated_credit() -> None:
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(
            fact(FactKind.CONFIRMED_INVOICE_PAYMENT, amount="12000", on="2025-06-20"),
        ),
        resolved_amounts={},
    )
    projection = build_forecast(balance, evidence=evidence)
    credits = [f for f in projection.flows if f.category == "invoice"]
    assert len(credits) == 1
    assert credits[0].amount == Decimal("12000")
    assert credits[0].on == date(2025, 6, 20)


def test_confirmed_income_dated_before_the_request_is_ignored() -> None:
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(
            fact(FactKind.CONFIRMED_INVOICE_PAYMENT, amount="12000", on="2025-01-20"),
        ),
        resolved_amounts={},
    )
    projection = build_forecast(balance, evidence=evidence)
    assert not [f for f in projection.flows if f.category == "invoice"]


def test_rent_increase_scales_the_rent_series() -> None:
    events = [
        make_event(
            f"event_rent_{index}",
            day=day,
            amount="10000",
            description="Residential rent",
            category="rent",
        )
        for index, day in enumerate(
            ["2025-01-03", "2025-02-03", "2025-03-03", "2025-04-03", "2025-05-03"], 1
        )
    ]
    context = make_context(events, request_date="2025-05-10")
    balance = state.reconstruct_balance(context)
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(fact(FactKind.RENT_INCREASE, percent="12"),),
        resolved_amounts={},
    )
    projection = build_forecast(balance, evidence=evidence)
    rent_flows = [f for f in projection.flows if f.category == "rent"]
    assert rent_flows
    assert all(f.amount == Decimal("11200.00") for f in rent_flows)


def test_a_new_recurring_expense_without_an_amount_is_not_invented() -> None:
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(fact(FactKind.NEW_RECURRING_EXPENSE),),
        resolved_amounts={},
    )
    amendments = forecast.apply_amendments(balance, evidence)
    assert any("no amount" in note for note in amendments.unquantified)
    assert not amendments.series_multipliers


def test_instruction_attempts_never_reach_amendments() -> None:
    scam = ExtractedFact(
        source_id="message_67",
        source_kind="message",
        kind=FactKind.SALARY_AMOUNT_CHANGE,
        effective_date=date(2025, 6, 15),
        amount=Decimal("9999999"),
        currency="INR",
        target_event_id=None,
        confidence=1.0,
        is_instruction_attempt=True,
        template="prize_fee_demand",
        note="",
    )
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test", user_id="user_test", facts=(scam,), resolved_amounts={}
    )
    amendments = forecast.apply_amendments(balance, evidence)
    assert amendments.salary.amount == Decimal("45000")


def test_failed_debit_retry_defers_to_the_ledger_row() -> None:
    balance = state.reconstruct_balance(_salary_context())
    evidence = Evidence(
        request_id="request_test",
        user_id="user_test",
        facts=(fact(FactKind.FAILED_DEBIT_RETRY),),
        resolved_amounts={},
    )
    amendments = forecast.apply_amendments(balance, evidence)
    decision = next(
        c for c in amendments.conflicts if c.rule is ConflictRule.SETTLED_OVER_ESTIMATE
    )
    assert decision.winner == "scheduled retry event row"


def test_every_conflict_rule_is_representable() -> None:
    assert {r.value for r in ConflictRule} == {
        "explicit_cancellation_settlement_or_amendment",
        "newer_record_from_the_same_source",
        "settled_over_estimate_or_forecast",
        "financially_safer_interpretation",
    }


# ---------------------------------------------------------------------------
# Salary extrapolation
# ---------------------------------------------------------------------------


def _two_income_streams_context(
    *, big: str, small: str, big_months: int = 5, small_months: int = 5
):
    """A user paid by two distinct, comparable, recurring credit streams.

    Both are ``category="salary"`` and both are description-keyed into their own
    series, so :func:`forecast._salary_series` has a genuine choice to make.
    """
    events = []
    for index in range(1, big_months + 1):
        events.append(
            make_event(
                f"event_big_{index}",
                day=f"2025-{index:02d}-15",
                amount=big,
                direction="credit",
                event_type="income",
                description="Monthly salary",
                category="salary",
            )
        )
    for index in range(1, small_months + 1):
        events.append(
            make_event(
                f"event_small_{index}",
                day=f"2025-{index:02d}-22",
                amount=small,
                direction="credit",
                event_type="income",
                description="Consulting retainer",
                category="salary",
            )
        )
    return make_context(events, request_date="2025-06-02")


def test_the_larger_of_two_income_streams_is_treated_as_the_salary() -> None:
    """The `_salary_series` tie-break, asserted rather than assumed.

    Two recurring salary-category credit series exist and only one can anchor
    the projection. The comment at the tie-break says "the largest stream is the
    salary; a side income should not displace it" -- this is the test for it.

    Why the *larger* is the safe choice rather than an arbitrary one: the
    projected salary sets how much income the forecast expects. Anchoring on the
    smaller stream would under-project income, which for income is the
    conservative direction -- but it would also mis-set the payday anchor
    (``day_of_month`` 22 instead of 15), moving the modelled trough to the wrong
    part of the month. The tie-break is about identifying which rhythm is the
    payroll one, not about inflating income.
    """
    balance = state.reconstruct_balance(
        _two_income_streams_context(big="45000", small="38000")
    )

    keys = {
        s.key
        for s in balance.recurring_series
        if s.direction == "credit" and s.category == "salary"
    }
    assert len(keys) == 2, f"fixture must offer a real choice, got {keys}"

    chosen = forecast._salary_series(balance)
    assert chosen is not None
    assert chosen.description == "Monthly salary"
    assert chosen.median_amount == Decimal("45000")

    amount, day_of_month, _cadence_days, _kind, _anchor = forecast._income_plan_inputs(
        balance
    )
    assert amount == Decimal("45000")
    assert day_of_month == 15, "the payday anchor must come from the chosen stream"


def test_the_income_tie_break_is_not_an_artefact_of_series_order() -> None:
    """Same two streams, larger one declared second. Same winner.

    Guards the tie-break against being satisfied incidentally by whichever
    series :mod:`src.state` happens to emit first.
    """
    balance = state.reconstruct_balance(
        _two_income_streams_context(big="38000", small="45000")
    )
    chosen = forecast._salary_series(balance)
    assert chosen is not None
    assert chosen.median_amount == Decimal("45000")
    assert chosen.description == "Consulting retainer"


def test_equal_income_streams_break_on_occurrence_count() -> None:
    """The second key in the tie-break, exercised on its own.

    With identical medians the longer history wins -- ``len(contributing)`` is
    the second element of the sort key. Asserted because a tie on amount is the
    only case where the first key decides nothing, and an untested second key is
    where a total order quietly becomes arbitrary.
    """
    balance = state.reconstruct_balance(
        _two_income_streams_context(
            big="45000", small="45000", big_months=5, small_months=3
        )
    )
    chosen = forecast._salary_series(balance)
    assert chosen is not None
    assert chosen.median_amount == Decimal("45000")
    assert len(chosen.contributing) == 5
    assert chosen.description == "Monthly salary"


def test_salary_is_day_of_month_anchored_from_history() -> None:
    balance = state.reconstruct_balance(_salary_context())
    projection = build_forecast(balance, evidence=empty_evidence())
    paydays = [f.on for f in projection.flows if f.category == "salary"]
    assert paydays
    assert {d.day for d in paydays} == {15}


def test_salary_defaults_to_the_fifteenth_without_an_anchor() -> None:
    plan = SalaryPlan(amount=Decimal("1000"), day_of_month=config.DEFAULT_SALARY_DAY_OF_MONTH)
    assert plan.day_of_month == 15
    flows = forecast._salary_flows(plan, date(2025, 1, 1), date(2025, 3, 31))
    assert [f.on.day for f in flows] == [15, 15, 15]


def test_month_end_anchor_clamps_into_short_months() -> None:
    plan = SalaryPlan(amount=Decimal("1000"), day_of_month=31)
    flows = forecast._salary_flows(plan, date(2025, 1, 1), date(2025, 4, 30))
    assert date(2025, 2, 28) in [f.on for f in flows]


# ---------------------------------------------------------------------------
# Stale-series gating
# ---------------------------------------------------------------------------


def test_a_dead_series_is_excluded_and_fully_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUYORWAIT_SERIES_STALENESS_PERIODS", "4.0")
    events = [
        make_event(
            f"event_{index}",
            day=day,
            amount="900",
            description="Weekly market",
            category="groceries",
        )
        for index, day in enumerate(
            ["2025-01-06", "2025-01-13", "2025-01-20", "2025-01-27"], 1
        )
    ]
    context = make_context(events, request_date="2025-06-01")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, evidence=empty_evidence())

    exclusion = next(
        e for e in projection.excluded_series if e.description == "Weekly market"
    )
    assert exclusion.reason is ExclusionReason.STALE
    assert exclusion.occurrences == 4
    assert exclusion.cadence_days == 7
    assert exclusion.last_occurrence == date(2025, 1, 27)
    assert exclusion.periods_since_last is not None
    assert exclusion.periods_since_last > 4.0
    # Everything a swept run needs is in the log line.
    rendered = str(exclusion)
    assert "Weekly market" in rendered
    assert "2025-01-27" in rendered
    assert "n=4" in rendered


def test_a_live_series_is_projected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUYORWAIT_SERIES_STALENESS_PERIODS", "4.0")
    events = [
        make_event(
            f"event_{index}",
            day=day,
            amount="900",
            description="Weekly market",
            category="groceries",
        )
        for index, day in enumerate(
            ["2025-05-05", "2025-05-12", "2025-05-19", "2025-05-26"], 1
        )
    ]
    context = make_context(events, request_date="2025-05-28")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, evidence=empty_evidence())
    assert not [
        e for e in projection.excluded_series if e.description == "Weekly market"
    ]
    assert [f for f in projection.flows if f.description == "Weekly market"]


def test_staleness_gate_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUYORWAIT_SERIES_STALENESS_PERIODS", "none")
    assert config.series_staleness_periods() is None
    events = [
        make_event(
            f"event_{index}",
            day=day,
            amount="900",
            description="Weekly market",
            category="groceries",
        )
        for index, day in enumerate(
            ["2025-01-06", "2025-01-13", "2025-01-20", "2025-01-27"], 1
        )
    ]
    context = make_context(events, request_date="2025-06-01")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, evidence=empty_evidence())
    assert not [
        e
        for e in projection.excluded_series
        if e.reason is ExclusionReason.STALE
    ]


def test_dropping_a_live_series_would_overstate_capacity() -> None:
    """Documents the asymmetry the default is chosen for."""
    events = [
        make_event(
            f"event_{index}",
            day=day,
            amount="5000",
            description="Monthly rent",
            category="rent",
        )
        for index, day in enumerate(
            ["2025-02-03", "2025-03-03", "2025-04-03", "2025-05-03"], 1
        )
    ]
    context = make_context(events, request_date="2025-05-10")
    balance = state.reconstruct_balance(context)
    with_series = build_forecast(balance, evidence=empty_evidence())
    assert with_series.minimum < balance.available_balance


def test_min_series_occurrences_is_configurable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BUYORWAIT_MIN_SERIES_OCCURRENCES", "3")
    monkeypatch.setenv("BUYORWAIT_SERIES_STALENESS_PERIODS", "none")
    assert config.min_series_occurrences() == 3
    events = [
        make_event("event_1", day="2025-05-05", amount="900", description="Twice only"),
        make_event("event_2", day="2025-05-12", amount="900", description="Twice only"),
    ]
    context = make_context(events, request_date="2025-05-14")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, evidence=empty_evidence())
    exclusion = next(
        e for e in projection.excluded_series if e.description == "Twice only"
    )
    assert exclusion.reason is ExclusionReason.TOO_FEW_OCCURRENCES


# ---------------------------------------------------------------------------
# Blank amounts
# ---------------------------------------------------------------------------


def test_an_unresolved_blank_future_outflow_fails_the_request() -> None:
    events = [
        make_event(
            "event_1",
            day="2025-02-20",
            amount=None,
            status="pending",
            settlement="2025-02-20",
            description="Unknown bill",
        )
    ]
    context = make_context(events, request_date="2025-02-01")
    balance = state.reconstruct_balance(context)
    with pytest.raises(UnresolvedBlankAmountError) as excinfo:
        build_forecast(balance, evidence=empty_evidence())
    assert excinfo.value.event_id == "event_1"
    assert "never treated as zero" in str(excinfo.value)


def test_a_resolved_blank_becomes_a_real_outflow() -> None:
    events = [
        make_event(
            "event_1",
            day="2025-02-20",
            amount=None,
            status="pending",
            settlement="2025-02-20",
            description="Known bill",
        )
    ]
    context = make_context(events, request_date="2025-02-01")
    balance = state.reconstruct_balance(context)
    projection = build_forecast(
        balance,
        evidence=empty_evidence(),
        resolved_amounts={"event_1": Decimal("7500")},
    )
    flow = next(f for f in projection.flows if f.event_id == "event_1")
    assert flow.amount == Decimal("7500")
    assert flow.direction == "debit"
    assert projection.balance_on(projection.end) == Decimal("100000") - Decimal("7500")


def test_the_two_image_backed_requests_forecast_successfully(
    dataset: load.Dataset, cache
) -> None:
    for request_id, user_id, event_id in [
        ("request_64", "user_64", "event_6033"),
        ("request_73", "user_73", "event_6859"),
    ]:
        context = dataset.get_user_context(user_id, request_id)
        balance = state.reconstruct_balance(context)
        blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
        evidence = extract.gather_evidence(context, blank, cache=cache)
        projection = build_forecast(balance, evidence=evidence)
        flow = next(f for f in projection.flows if f.event_id == event_id)
        assert flow.amount > 0
        assert flow.direction == "debit"


# ---------------------------------------------------------------------------
# Whole-dataset behaviour
# ---------------------------------------------------------------------------


def test_forecast_builds_for_every_evaluation_request(
    dataset: load.Dataset, cache
) -> None:
    for request in dataset.requests:
        context = dataset.get_user_context(request.user_id, request.request_id)
        balance = state.reconstruct_balance(context)
        blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
        evidence = extract.gather_evidence(context, blank, cache=cache)
        projection = build_forecast(balance, evidence=evidence)
        assert projection.start == request.request_date
        assert (projection.end - projection.start).days == 90
        assert len(projection.balances) == 91
        assert projection.balances[0][0] == request.request_date


def test_the_window_is_inclusive_of_both_ends(dataset: load.Dataset, cache) -> None:
    request = dataset.requests[0]
    context = dataset.get_user_context(request.user_id, request.request_id)
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, horizon=30, evidence=empty_evidence())
    assert projection.balances[0][0] == projection.start
    assert projection.balances[-1][0] == projection.end
    assert len(projection.balances) == 31


def test_all_balances_are_decimals(dataset: load.Dataset, cache) -> None:
    request = dataset.requests[0]
    context = dataset.get_user_context(request.user_id, request.request_id)
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, evidence=empty_evidence())
    for _, value in projection.balances:
        assert isinstance(value, Decimal)


def test_with_payments_does_not_mutate_the_original(
    dataset: load.Dataset, cache
) -> None:
    request = dataset.requests[0]
    context = dataset.get_user_context(request.user_id, request.request_id)
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, evidence=empty_evidence())
    before = projection.balances
    with_payments(projection, [(request.request_date, Decimal("100"))])
    assert projection.balances == before


def test_pending_credits_never_appear_as_forecast_inflows(
    dataset: load.Dataset, cache
) -> None:
    context = dataset.get_user_context("user_20", "request_20")
    balance = state.reconstruct_balance(context)
    blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
    evidence = extract.gather_evidence(context, blank, cache=cache)
    projection = build_forecast(balance, evidence=evidence)
    assert all(f.event_id != "event_1785" for f in projection.flows)


def test_a_forecast_summary_can_be_logged_as_json(dataset: load.Dataset, cache) -> None:
    request = dataset.requests[0]
    context = dataset.get_user_context(request.user_id, request.request_id)
    balance = state.reconstruct_balance(context)
    projection = build_forecast(balance, evidence=empty_evidence())
    summary = projection.summary()
    assert summary["request_id"] == request.request_id
    assert isinstance(summary["troughs"], list)

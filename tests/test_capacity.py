"""Tests for :mod:`src.capacity`."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src import capacity, extract, forecast, load, state
from src.capacity import (
    amount_safe_to_pay,
    assess_capacity,
    earliest_date_for_full_payment,
    floor_to_precision,
)
from tests.test_forecast import empty_evidence
from tests.test_state import make_context, make_event


@pytest.fixture(scope="module")
def dataset() -> load.Dataset:
    return load.get_dataset()


@pytest.fixture(scope="module")
def cache():
    return extract.load_image_cache()


def _phase4_events() -> list:
    """The Phase 4 synthetic user: rent 40k on the 3rd, salary 45k on the 15th,
    groceries 2k weekly. Outflow exceeds income, so each month's trough sits
    lower than the last and the binding one is late in the horizon."""
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
    return events


# ---------------------------------------------------------------------------
# The late binding trough sets the number
# ---------------------------------------------------------------------------


def test_a_late_binding_trough_sets_the_amount() -> None:
    """The 11 August trough, not anything nearer, is what limits the payment.

    Opening balance is 100,000 against a 20,000 floor, so a naive reading
    would allow 80,000 today. The horizon's lowest point is 50,000 on
    11 August, which caps the answer at 30,000.
    """
    context = make_context(
        _phase4_events(), request_date="2025-06-02", requested_amount="1000000"
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    assessment = assess_capacity(context, projection)

    assert assessment.binding_trough is not None
    assert assessment.binding_trough.on == date(2025, 8, 11)
    assert assessment.binding_trough.balance == Decimal("50000.00")

    # Headroom at that trough, exactly.
    assert assessment.amount_safe_to_pay == Decimal("30000.00")

    # Not the naive opening-balance answer, and not an earlier trough's.
    assert assessment.amount_safe_to_pay < Decimal("80000")
    early = [
        t
        for t in projection.troughs()
        if t.on <= projection.start + timedelta(days=30)
    ]
    assert early
    assert all(t.balance > assessment.binding_trough.balance for t in early)

    # And the answer is exactly right: paying it is safe, a cent more is not.
    assert forecast.payment_is_safe(
        projection, [(projection.start, assessment.amount_safe_to_pay)]
    )
    assert not forecast.payment_is_safe(
        projection,
        [(projection.start, assessment.amount_safe_to_pay + Decimal("0.01"))],
    )


def test_an_early_tight_point_yields_a_larger_amount_than_a_late_one() -> None:
    """Same floor, same opening balance -- only the timing of the dip differs."""
    salary = [
        make_event(
            f"event_pay_{index}",
            day=day,
            amount="45000",
            direction="credit",
            event_type="income",
            description="Monthly salary",
            category="salary",
        )
        for index, day in enumerate(
            ["2025-01-15", "2025-02-15", "2025-03-15", "2025-04-15", "2025-05-15"], 1
        )
    ]
    # A single early dip that the salary immediately repairs.
    early = [
        *salary,
        make_event(
            "event_dip",
            day="2025-06-05",
            amount="30000",
            status="scheduled",
            settlement="2025-06-05",
            description="One-off early charge",
            category="other",
        ),
    ]
    early_context = make_context(
        early, request_date="2025-06-02", requested_amount="1000000"
    )
    early_state = state.reconstruct_balance(early_context)
    early_forecast = forecast.build_forecast(early_state, evidence=empty_evidence())
    early_amount = amount_safe_to_pay(early_context, early_forecast)

    late_context = make_context(
        _phase4_events(), request_date="2025-06-02", requested_amount="1000000"
    )
    late_state = state.reconstruct_balance(late_context)
    late_forecast = forecast.build_forecast(late_state, evidence=empty_evidence())
    late_amount = amount_safe_to_pay(late_context, late_forecast)

    assert late_amount < early_amount
    assert late_forecast.binding_trough(late_forecast.start).on > date(2025, 7, 1)


# ---------------------------------------------------------------------------
# earliest_date_for_full_payment
# ---------------------------------------------------------------------------


def test_earliest_date_lands_exactly_on_the_salary_credit_date() -> None:
    """Cannot pay today; can the moment payday lands."""
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
    context = make_context(
        events,
        request_date="2025-06-02",
        requested_amount="120000",
        balance="100000",
        minimum="20000",
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())

    # Today is not enough: 100,000 - 120,000 would be far below the floor.
    assert amount_safe_to_pay(context, projection) < Decimal("120000")

    earliest = earliest_date_for_full_payment(context, projection)
    assert earliest == date(2025, 6, 15)

    salary_days = {f.on for f in projection.flows if f.category == "salary"}
    assert earliest in salary_days

    # Paying on that date really is safe, and the day before really is not.
    assert forecast.payment_is_safe(projection, [(earliest, Decimal("120000"))])
    assert not forecast.payment_is_safe(
        projection, [(earliest - timedelta(days=1), Decimal("120000"))]
    )


def test_earliest_date_is_none_when_the_horizon_cannot_supply_it() -> None:
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
    context = make_context(
        events,
        request_date="2025-06-02",
        requested_amount="5000000",
        balance="100000",
        minimum="20000",
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())

    assert earliest_date_for_full_payment(context, projection) is None

    assessment = assess_capacity(context, projection)
    assert assessment.earliest_date_for_full_payment is None
    assert assessment.shortfall is not None
    assert assessment.shortfall > 0


def test_earliest_date_equals_request_date_when_affordable_today() -> None:
    events = [make_event("event_1", day="2025-05-01", amount="100")]
    context = make_context(
        events,
        request_date="2025-06-02",
        requested_amount="1000",
        balance="100000",
        minimum="20000",
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    assert earliest_date_for_full_payment(context, projection) == date(2025, 6, 2)


def test_earliest_date_ignores_payment_method_preference() -> None:
    """Capacity is measured independently of what the user will consider."""
    events = [make_event("event_1", day="2025-05-01", amount="100")]
    context = make_context(
        events,
        request_date="2025-06-02",
        requested_amount="1000",
        balance="100000",
        minimum="20000",
        payment_methods=("installments",),
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    assert "full_payment" not in context.profile.payment_methods_user_will_consider
    assert earliest_date_for_full_payment(context, projection) == context.request.request_date


def test_earliest_date_stays_inside_the_horizon(dataset: load.Dataset, cache) -> None:
    for request in dataset.requests[:60]:
        context = dataset.get_user_context(request.user_id, request.request_id)
        balance = state.reconstruct_balance(context)
        blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
        evidence = extract.gather_evidence(context, blank, cache=cache)
        projection = forecast.build_forecast(balance, evidence=evidence)
        earliest = earliest_date_for_full_payment(context, projection)
        if earliest is not None:
            assert projection.start <= earliest <= projection.end


# ---------------------------------------------------------------------------
# The diagnostic is never suppressed
# ---------------------------------------------------------------------------


def test_amount_safe_to_pay_is_reported_on_a_not_affordable_request(
    dataset: load.Dataset, cache
) -> None:
    """request_20 is labelled not_affordable, yet capacity is positive."""
    context = dataset.get_user_context("user_20", "request_20")
    balance = state.reconstruct_balance(context)
    blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
    evidence = extract.gather_evidence(context, blank, cache=cache)
    projection = forecast.build_forecast(balance, evidence=evidence)
    assessment = assess_capacity(context, projection)

    assert context.request.expected is not None
    assert context.request.expected.affordability_status == "not_affordable"
    assert context.request.expected.amount_safe_to_pay > 0
    # The label carries a positive figure, and so must we.
    assert assessment.amount_safe_to_pay > 0
    assert assessment.earliest_date_for_full_payment is None


def test_labelled_not_affordable_rows_mostly_carry_positive_capacity(
    dataset: load.Dataset, cache
) -> None:
    """Every labelled not_affordable row has a positive ground-truth figure."""
    for request in dataset.sample_requests:
        if request.expected.affordability_status != "not_affordable":
            continue
        assert request.expected.amount_safe_to_pay > 0, (
            f"{request.request_id}: the label itself zeroes the diagnostic"
        )


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


def test_the_cap_binds_exactly_for_a_wealthy_user() -> None:
    events = [make_event("event_1", day="2025-05-01", amount="100")]
    context = make_context(
        events,
        request_date="2025-06-02",
        requested_amount="2500",
        balance="10000000",
        minimum="1000",
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    assessment = assess_capacity(context, projection)

    assert assessment.amount_safe_to_pay == Decimal("2500.00")
    assert assessment.amount_safe_to_pay == context.request.requested_amount
    assert assessment.capped_by_request
    assert assessment.uncapped_capacity > context.request.requested_amount


def test_the_bound_holds_across_the_whole_dataset(
    dataset: load.Dataset, cache
) -> None:
    for request in dataset.requests:
        context = dataset.get_user_context(request.user_id, request.request_id)
        balance = state.reconstruct_balance(context)
        blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
        evidence = extract.gather_evidence(context, blank, cache=cache)
        projection = forecast.build_forecast(balance, evidence=evidence)
        safe = amount_safe_to_pay(context, projection)
        assert Decimal(0) <= safe <= request.requested_amount
        assert isinstance(safe, Decimal)


def test_capacity_is_never_negative() -> None:
    """A user already projected below the floor gets zero, not a negative."""
    events = [
        make_event(
            "event_1",
            day="2025-06-10",
            amount="500000",
            status="scheduled",
            settlement="2025-06-10",
            description="Huge scheduled debit",
        )
    ]
    context = make_context(
        events,
        request_date="2025-06-02",
        requested_amount="1000",
        balance="100000",
        minimum="20000",
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    assert projection.minimum < 0
    assert amount_safe_to_pay(context, projection) == Decimal(0)


# ---------------------------------------------------------------------------
# Precision and types
# ---------------------------------------------------------------------------


def test_amounts_floor_rather_than_round_up() -> None:
    """Rounding up could push the balance below the floor it respects."""
    assert floor_to_precision(Decimal("100.999")) == Decimal("100.99")
    assert floor_to_precision(Decimal("100.005")) == Decimal("100.00")
    assert floor_to_precision(Decimal("100.991")) == Decimal("100.99")


def test_results_are_decimal_and_date_objects(dataset: load.Dataset, cache) -> None:
    request = dataset.requests[0]
    context = dataset.get_user_context(request.user_id, request.request_id)
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    assessment = assess_capacity(context, projection)
    assert isinstance(assessment.amount_safe_to_pay, Decimal)
    assert assessment.earliest_date_for_full_payment is None or isinstance(
        assessment.earliest_date_for_full_payment, date
    )
    # No formatting leaks out of this module.
    assert not isinstance(assessment.amount_safe_to_pay, str)


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


def test_trace_carries_the_binding_trough_date_and_headroom() -> None:
    context = make_context(
        _phase4_events(), request_date="2025-06-02", requested_amount="1000000"
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    trace = assess_capacity(context, projection).trace()

    assert trace["binding_trough_date"] == "2025-08-11"
    assert trace["binding_trough_balance"] == "50000.00"
    assert trace["binding_trough_headroom"] == "30000.00"
    assert trace["amount_safe_to_pay"] == "30000.00"
    # Phase 11 needs to separate "wrong trough" from "wrong balance".
    assert set(trace) >= {
        "binding_trough_date",
        "binding_trough_balance",
        "binding_trough_headroom",
        "earliest_binding_trough_date",
        "uncapped_capacity",
        "capped_by_request",
    }


def test_a_trace_can_be_written_to_disk_as_json(dataset: load.Dataset, cache) -> None:
    import json

    request = dataset.requests[0]
    context = dataset.get_user_context(request.user_id, request.request_id)
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())
    json.dumps(assess_capacity(context, projection).trace())

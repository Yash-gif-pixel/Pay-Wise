"""Tests for :mod:`src.state`.

The four behaviours called out in the phase brief each have a dedicated
section: a cancelled event is ignored, a pending credit is ignored, a linked
refund supersedes its parent, and a blank amount does not drag a series
average down. Synthetic fixtures are used where the real dataset has no
instance of a case, so the rule is still proved rather than assumed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src import config, load, state
from src.load import DatasetError, FinancialEvent, Profile, Request, UserContext
from src.state import (
    CadenceKind,
    CashClass,
    Recurrence,
    ResolutionKind,
    reconstruct_balance,
)


@pytest.fixture(scope="module")
def dataset() -> load.Dataset:
    return load.get_dataset()


@pytest.fixture(scope="module")
def states(dataset: load.Dataset) -> dict[str, state.BalanceState]:
    return {
        ctx.request_id: reconstruct_balance(ctx)
        for ctx in list(dataset.iter_contexts())[:60]
    }


# ---------------------------------------------------------------------------
# Synthetic fixture helpers
# ---------------------------------------------------------------------------

HOME = "INR"


def make_event(
    event_id: str,
    *,
    day: str,
    amount: str | None,
    direction: str = "debit",
    status: str = "settled",
    event_type: str = "expense",
    description: str = "Recurring charge",
    category: str = "groceries",
    linked_event_id: str | None = None,
    flexibility: str = "fixed",
    settlement: str | None = None,
) -> FinancialEvent:
    parsed = Decimal(amount) if amount is not None else None
    settle = date.fromisoformat(settlement) if settlement else date.fromisoformat(day)
    return FinancialEvent(
        event_id=event_id,
        user_id="user_test",
        event_type=event_type,
        description=description,
        category=category,
        direction=direction,
        amount=parsed,
        currency=HOME,
        event_date=date.fromisoformat(day),
        settlement_date=settle,
        status=status,
        linked_event_id=linked_event_id,
        flexibility=flexibility,
        minimum_allowed_amount=None,
        home_currency=HOME,
        fx_rate=Decimal(1),
        amount_home=parsed,
        minimum_allowed_amount_home=None,
    )


def make_context(
    events: list[FinancialEvent],
    *,
    request_date: str,
    requested_amount: str = "5000",
    balance: str = "100000",
    minimum: str = "20000",
    desired_completion_date: str | None = None,
    allows_partial_payment: bool = True,
    payment_methods: tuple[str, ...] = ("full_payment",),
) -> UserContext:
    profile = Profile(
        user_id="user_test",
        home_currency=HOME,
        current_available_balance=Decimal(balance),
        minimum_balance_to_keep=Decimal(minimum),
        financial_priorities=("emergency_savings",),
        expense_categories_to_protect=("rent",),
        expense_categories_user_is_willing_to_reduce=("dining",),
        expense_categories_user_is_willing_to_stop=("streaming",),
        payment_methods_user_will_consider=payment_methods,
        max_installment_months=None,
    )
    request = Request(
        request_id="request_test",
        user_id="user_test",
        request_date=date.fromisoformat(request_date),
        request_type="purchase",
        requested_amount=Decimal(requested_amount),
        desired_completion_date=date.fromisoformat(
            desired_completion_date or request_date
        ),
        allows_partial_payment=allows_partial_payment,
        request_text="test",
    )
    return UserContext(
        request=request,
        profile=profile,
        events=tuple(sorted(events, key=lambda e: (e.event_date, e.event_id))),
        messages=(),
        images=(),
        payment_options=(),
    )


# ---------------------------------------------------------------------------
# 1. A cancelled event is ignored
# ---------------------------------------------------------------------------


def test_cancelled_event_is_classified_and_excluded() -> None:
    events = [
        make_event("event_1", day="2025-01-10", amount="500"),
        make_event("event_2", day="2025-01-11", amount="900", status="cancelled"),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))

    cancelled = result.by_id()["event_2"]
    assert cancelled.cash_class is CashClass.CANCELLED_OR_FAILED
    assert cancelled.is_excluded
    assert "never moved" in cancelled.reason
    assert cancelled not in result.settled_history
    assert all(f.event_id != "event_2" for f in result.future_flows)


def test_failed_event_is_excluded_too() -> None:
    events = [make_event("event_1", day="2025-01-10", amount="500", status="failed")]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    assert result.by_id()["event_1"].cash_class is CashClass.CANCELLED_OR_FAILED
    assert result.committed_outflow == Decimal(0)


def test_cancelled_future_event_never_becomes_a_future_flow() -> None:
    """A cancelled debit dated after the request must not be reserved."""
    events = [
        make_event("event_1", day="2025-03-10", amount="7000", status="cancelled")
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    assert result.future_flows == ()
    assert result.committed_outflow == Decimal(0)


def test_real_dataset_cancelled_and_failed_are_all_excluded(
    states: dict[str, state.BalanceState],
) -> None:
    seen = 0
    for result in states.values():
        for item in result.cancelled_or_failed:
            seen += 1
            assert item.event.status in ("cancelled", "failed")
            assert item.is_excluded
            assert all(f.event_id != item.event_id for f in result.future_flows)
    assert seen > 0, "expected some cancelled/failed rows in the sampled users"


# ---------------------------------------------------------------------------
# 2. A pending credit is ignored
# ---------------------------------------------------------------------------


def test_pending_credit_is_excluded_but_pending_debit_is_reserved() -> None:
    events = [
        make_event(
            "event_1",
            day="2025-02-10",
            amount="3000",
            direction="credit",
            status="pending",
            event_type="refund",
            description="Refund owed",
        ),
        make_event(
            "event_2",
            day="2025-02-12",
            amount="4000",
            direction="debit",
            status="pending",
            description="Utility bill",
        ),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))

    assert result.by_id()["event_1"].cash_class is CashClass.PENDING_CREDIT
    assert result.by_id()["event_1"].is_excluded
    assert result.by_id()["event_2"].cash_class is CashClass.PENDING_DEBIT

    flow_ids = {f.event_id for f in result.future_flows}
    assert flow_ids == {"event_2"}
    assert result.committed_outflow == Decimal("4000")
    assert result.confirmed_inflow == Decimal(0)


def test_pending_debit_lands_on_its_settlement_date() -> None:
    events = [
        make_event(
            "event_1",
            day="2025-02-03",
            settlement="2025-02-20",
            amount="1500",
            status="pending",
        )
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    assert [f.on for f in result.future_flows] == [date(2025, 2, 20)]


def test_scheduled_income_is_counted_but_pending_income_is_not() -> None:
    events = [
        make_event(
            "event_1",
            day="2025-02-15",
            amount="60000",
            direction="credit",
            status="scheduled",
            event_type="income",
            description="Monthly salary",
        ),
        make_event(
            "event_2",
            day="2025-02-18",
            amount="9000",
            direction="credit",
            status="pending",
            event_type="income",
            description="Quarterly bonus",
        ),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    assert result.confirmed_inflow == Decimal("60000")
    assert {f.event_id for f in result.future_flows} == {"event_1"}


def test_real_dataset_pending_credits_never_reach_future_flows(
    states: dict[str, state.BalanceState],
) -> None:
    for result in states.values():
        pending_credit_ids = {e.event_id for e in result.pending_credit}
        flow_ids = {f.event_id for f in result.future_flows}
        assert pending_credit_ids.isdisjoint(flow_ids)


def test_non_cash_investment_value_is_excluded(
    states: dict[str, state.BalanceState],
) -> None:
    events = [
        make_event(
            "event_1",
            day="2025-01-05",
            amount="500000",
            direction="non_cash",
            status="unrealized",
            event_type="investment_valuation",
            description="Portfolio value",
        )
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    assert result.by_id()["event_1"].cash_class is CashClass.NON_CASH_INVESTMENT
    assert result.by_id()["event_1"].is_excluded


# ---------------------------------------------------------------------------
# 3. A linked refund supersedes its parent
# ---------------------------------------------------------------------------


def test_linked_refund_supersedes_its_parent_and_logs_a_resolution() -> None:
    events = [
        make_event("event_1", day="2025-01-10", amount="2500", description="Gadget"),
        make_event(
            "event_2",
            day="2025-01-20",
            amount="2500",
            direction="credit",
            event_type="refund",
            description="Gadget refund",
            linked_event_id="event_1",
        ),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))

    parent = result.by_id()["event_1"]
    assert parent.superseded_by == "event_2"
    assert "superseded by event_2" in parent.reason

    assert len(result.resolutions) == 1
    resolution = result.resolutions[0]
    assert resolution.kind is ResolutionKind.REFUND
    assert resolution.parent_event_id == "event_1"
    assert resolution.child_event_id == "event_2"
    assert "reverses event_1" in str(resolution)


def test_superseded_parent_is_dropped_from_series_statistics() -> None:
    """A refunded charge is not evidence of a standing cost."""
    events = [
        make_event("event_1", day="2025-01-05", amount="100", description="Subscription"),
        make_event("event_2", day="2025-02-05", amount="100", description="Subscription"),
        make_event("event_3", day="2025-03-05", amount="900", description="Subscription"),
        make_event(
            "event_4",
            day="2025-03-08",
            amount="900",
            direction="credit",
            event_type="refund",
            description="Subscription refund",
            linked_event_id="event_3",
        ),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-04-01"))
    series = result.series_for("event_1")
    assert series is not None
    # The refunded 900 must not inflate the series.
    assert series.amounts == (Decimal("100"), Decimal("100"))
    assert series.mean_amount == Decimal("100.00")


def test_cancelled_parent_is_superseded_by_its_settled_retry() -> None:
    events = [
        make_event("event_1", day="2025-01-10", amount="816.20", status="cancelled"),
        make_event(
            "event_2",
            day="2025-01-11",
            amount="816.20",
            status="settled",
            linked_event_id="event_1",
        ),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    assert result.resolutions[0].kind is ResolutionKind.RETRY_AFTER_CANCELLATION
    assert result.by_id()["event_1"].superseded_by == "event_2"
    assert result.by_id()["event_2"].cash_class is CashClass.SETTLED_HISTORY


def test_failed_parent_is_superseded_by_its_scheduled_retry() -> None:
    events = [
        make_event(
            "event_1",
            day="2025-01-01",
            amount="129",
            status="failed",
            event_type="debt_payment",
            description="Loan instalment",
        ),
        make_event(
            "event_2",
            day="2025-02-07",
            amount="129",
            status="scheduled",
            event_type="debt_payment",
            description="Loan instalment",
            linked_event_id="event_1",
        ),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-01-15"))
    assert result.resolutions[0].kind is ResolutionKind.RETRY_AFTER_FAILURE
    assert result.by_id()["event_1"].cash_class is CashClass.CANCELLED_OR_FAILED
    assert {f.event_id for f in result.future_flows} == {"event_2"}
    assert result.committed_outflow == Decimal("129")


def test_pending_refund_is_not_counted_as_cash_but_still_supersedes() -> None:
    events = [
        make_event("event_1", day="2025-01-10", amount="65.12", description="Order"),
        make_event(
            "event_2",
            day="2025-02-10",
            amount="65.12",
            direction="credit",
            status="pending",
            event_type="refund",
            description="Order refund",
            linked_event_id="event_1",
        ),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-01-20"))
    assert result.resolutions[0].kind is ResolutionKind.PENDING_REFUND
    assert result.by_id()["event_2"].cash_class is CashClass.PENDING_CREDIT
    assert result.confirmed_inflow == Decimal(0)
    assert result.by_id()["event_1"].superseded_by == "event_2"


def test_real_dataset_resolutions_cover_every_link(
    dataset: load.Dataset, states: dict[str, state.BalanceState]
) -> None:
    for result in states.values():
        linked = [
            item for item in result.classified if item.event.linked_event_id
        ]
        resolved_children = {r.child_event_id for r in result.resolutions}
        for item in linked:
            assert item.event_id in resolved_children
        assert not any(
            r.kind is ResolutionKind.UNCLASSIFIED_LINK for r in result.resolutions
        )


# ---------------------------------------------------------------------------
# 4. A blank amount does not drag a series average down
# ---------------------------------------------------------------------------


def test_blank_amount_is_excluded_from_series_statistics_not_counted_as_zero() -> None:
    events = [
        make_event("event_1", day="2025-01-05", amount="1000", description="Groceries"),
        make_event("event_2", day="2025-02-05", amount="1000", description="Groceries"),
        make_event("event_3", day="2025-03-05", amount=None, description="Groceries"),
        make_event("event_4", day="2025-04-05", amount="1000", description="Groceries"),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-05-01"))
    series = result.series_for("event_1")
    assert series is not None

    assert series.blank_amount_count == 1
    assert series.amounts == (Decimal("1000"),) * 3
    assert series.mean_amount == Decimal("1000.00")
    # Counting the blank as zero would give 750.
    assert series.mean_amount != Decimal("750.00")
    assert series.median_amount == Decimal("1000.00")


def test_series_of_only_blank_amounts_has_no_statistics() -> None:
    events = [
        make_event("event_1", day="2025-01-05", amount=None, description="Mystery"),
        make_event("event_2", day="2025-02-05", amount=None, description="Mystery"),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-03-01"))
    series = result.series_for("event_1")
    assert series is not None
    assert series.amounts == ()
    assert series.mean_amount is None
    assert series.median_amount is None
    assert series.latest_amount is None


def test_blank_amount_future_flow_is_reported_not_silently_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events = [
        make_event(
            "event_1",
            day="2025-02-20",
            amount=None,
            status="pending",
            description="Unknown bill",
        )
    ]
    with caplog.at_level("WARNING"):
        result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    assert result.future_flows == ()
    assert "has no amount yet" in caplog.text


# ---------------------------------------------------------------------------
# Blank-amount triage (phase addition)
# ---------------------------------------------------------------------------


def test_blank_amounts_settled_before_request_are_distractors(
    dataset: load.Dataset,
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    12 of the 16 blank amounts need no image read at all.

    They settled before their user's request date and are therefore already
    inside ``current_available_balance``.
    """
    findings = []
    for request in list(dataset.requests) + list(dataset.sample_requests):
        context = dataset.get_user_context(request.user_id, request.request_id)
        findings.extend(reconstruct_balance(context).blank_amounts)

    assert len(findings) == 16
    distractors = [f for f in findings if not f.needs_image]
    needed = [f for f in findings if f.needs_image]

    assert len(distractors) == 12
    assert len(needed) == 4
    assert {f.event_id for f in needed} == {
        "event_1442",
        "event_1786",
        "event_6033",
        "event_6859",
    }
    for finding in distractors:
        assert finding.settled_before_as_of
        assert finding.status == "settled"
        assert "already inside available_balance" in finding.reason
    for finding in needed:
        assert finding.status in ("pending", "scheduled")
        assert finding.cash_date > date.fromisoformat("1900-01-01")


def test_only_two_evaluation_requests_need_an_image_read(
    dataset: load.Dataset,
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    The evaluation set needs 2 image reads, not 11."""
    needing = []
    for request in dataset.requests:
        context = dataset.get_user_context(request.user_id, request.request_id)
        for finding in reconstruct_balance(context).blank_amounts:
            if finding.needs_image:
                needing.append((request.request_id, finding.event_id))
    assert sorted(needing) == [
        ("request_64", "event_6033"),
        ("request_73", "event_6859"),
    ]


def test_event_7307_deferred_fx_is_not_needed_for_the_forecast(
    dataset: load.Dataset,
) -> None:
    """The one foreign blank-amount row settled before its request date.

    Its deferred conversion machinery stays correct but is never exercised by
    the forecast, so no request depends on it.
    """
    context = dataset.get_user_context("user_78", "request_78")
    finding = next(
        f for f in reconstruct_balance(context).blank_amounts if f.event_id == "event_7307"
    )
    assert finding.currency == "USD"
    assert finding.home_currency == "INR"
    assert finding.settled_before_as_of
    assert not finding.needs_image
    assert not finding.needs_fx_after_extraction


def test_no_blank_amount_needing_an_image_is_also_foreign(
    dataset: load.Dataset,
) -> None:
    for request in list(dataset.requests) + list(dataset.sample_requests):
        context = dataset.get_user_context(request.user_id, request.request_id)
        for finding in reconstruct_balance(context).blank_amounts:
            if finding.needs_image:
                assert not finding.needs_fx_after_extraction


# ---------------------------------------------------------------------------
# Settlement-date boundary assertion (phase addition)
# ---------------------------------------------------------------------------


def test_load_rejects_a_pending_event_with_a_blank_settlement_date() -> None:
    events = [
        FinancialEvent(
            event_id="event_1",
            user_id="user_test",
            event_type="expense",
            description="Bill",
            category="utilities",
            direction="debit",
            amount=Decimal("100"),
            currency=HOME,
            event_date=date(2025, 1, 1),
            settlement_date=None,
            status="pending",
            linked_event_id=None,
            flexibility="fixed",
            minimum_allowed_amount=None,
            home_currency=HOME,
            fx_rate=Decimal(1),
            amount_home=Decimal("100"),
            minimum_allowed_amount_home=None,
        )
    ]
    with pytest.raises(DatasetError, match="blank settlement_date"):
        load._assert_settlement_dates_present(events)


def test_load_rejects_an_unexpected_status_with_a_blank_settlement_date() -> None:
    events = [
        FinancialEvent(
            event_id="event_1",
            user_id="user_test",
            event_type="expense",
            description="Bill",
            category="utilities",
            direction="debit",
            amount=Decimal("100"),
            currency=HOME,
            event_date=date(2025, 1, 1),
            settlement_date=None,
            status="settled",
            linked_event_id=None,
            flexibility="fixed",
            minimum_allowed_amount=None,
            home_currency=HOME,
            fx_rate=Decimal(1),
            amount_home=Decimal("100"),
            minimum_allowed_amount_home=None,
        )
    ]
    with pytest.raises(DatasetError, match="only 'unrealized'"):
        load._assert_settlement_dates_present(events)


def test_real_dataset_passes_the_settlement_boundary_assertion(
    dataset: load.Dataset,
) -> None:
    load._assert_settlement_dates_present(dataset.events)
    blank = [e for e in dataset.events if e.settlement_date is None]
    assert len(blank) == 10
    assert {e.status for e in blank} == {"unrealized"}


# ---------------------------------------------------------------------------
# Balance truth
# ---------------------------------------------------------------------------


def test_settled_history_is_never_re_applied(dataset: load.Dataset) -> None:
    request = dataset.requests[0]
    context = dataset.get_user_context(request.user_id, request.request_id)
    result = reconstruct_balance(context)
    assert result.available_balance == context.profile.current_available_balance
    assert result.settled_history
    # Not one settled row appears as a future movement.
    settled_ids = {e.event_id for e in result.settled_history}
    assert settled_ids.isdisjoint({f.event_id for f in result.future_flows})


def test_every_event_gets_exactly_one_cash_class(
    states: dict[str, state.BalanceState],
) -> None:
    for result in states.values():
        assert len(result.classified) == len(
            result.settled_history
            + result.pending_debit
            + result.pending_credit
            + result.scheduled_future
            + result.cancelled_or_failed
            + result.duplicate
            + result.non_cash_investment
        )


def test_recurring_and_one_time_partition_the_stream(
    states: dict[str, state.BalanceState],
) -> None:
    for result in states.values():
        assert len(result.recurring) + len(result.one_time) == len(result.classified)


def test_future_flows_are_chronological(states: dict[str, state.BalanceState]) -> None:
    for result in states.values():
        dates = [f.on for f in result.future_flows]
        assert dates == sorted(dates)
        assert all(d > result.as_of for d in dates)


def test_signed_flow_direction() -> None:
    events = [
        make_event("event_1", day="2025-02-10", amount="100", status="pending"),
        make_event(
            "event_2",
            day="2025-02-11",
            amount="200",
            direction="credit",
            status="scheduled",
            event_type="income",
            description="Salary",
        ),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    signed = {f.event_id: f.signed for f in result.future_flows}
    assert signed == {"event_1": Decimal("-100"), "event_2": Decimal("200")}


def test_duplicate_bucket_is_empty_on_the_real_dataset(
    states: dict[str, state.BalanceState],
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    No row in the supplied data is an exact repeat -- the narrow rule holds."""
    for result in states.values():
        assert result.duplicate == ()


def test_duplicate_detection_fires_on_a_genuine_repeat() -> None:
    events = [
        make_event("event_1", day="2025-01-10", amount="500", description="Charge"),
        make_event("event_2", day="2025-01-10", amount="500", description="Charge"),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    assert {e.event_id for e in result.duplicate} == {"event_2"}
    assert result.by_id()["event_1"].cash_class is CashClass.SETTLED_HISTORY


def test_same_amount_on_a_different_date_is_not_a_duplicate() -> None:
    """A monthly charge of the same size is a real second charge."""
    events = [
        make_event("event_1", day="2025-01-10", amount="500", description="Charge"),
        make_event("event_2", day="2025-02-10", amount="500", description="Charge"),
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-03-01"))
    assert result.duplicate == ()


# ---------------------------------------------------------------------------
# Cadence inference
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected_kind", "expected_days"),
    [
        (["2025-01-06", "2025-01-13", "2025-01-20", "2025-01-27"], CadenceKind.FIXED_INTERVAL, 7),
        (["2025-01-06", "2025-01-20", "2025-02-03", "2025-02-17"], CadenceKind.FIXED_INTERVAL, 14),
        (["2025-01-06", "2025-01-27", "2025-02-17", "2025-03-10"], CadenceKind.FIXED_INTERVAL, 21),
        (["2025-01-06", "2025-01-11", "2025-01-16", "2025-01-21"], CadenceKind.FIXED_INTERVAL, 5),
        (["2025-01-06", "2025-01-16", "2025-01-26", "2025-02-05"], CadenceKind.FIXED_INTERVAL, 10),
        (["2025-01-06", "2025-02-06", "2025-03-06", "2025-04-06"], CadenceKind.MONTHLY, 30),
    ],
)
def test_cadence_snaps_to_the_allowed_set(
    days: list[str], expected_kind: CadenceKind, expected_days: int
) -> None:
    events = [
        make_event(f"event_{i}", day=day, amount="100") for i, day in enumerate(days, 1)
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-06-01"))
    series = result.series_for("event_1")
    assert series is not None
    assert series.is_recurring
    assert series.cadence_kind is expected_kind
    assert series.cadence_days == expected_days


def test_monthly_series_is_day_of_month_anchored() -> None:
    events = [
        make_event(f"event_{i}", day=day, amount="5148")
        for i, day in enumerate(
            ["2025-01-02", "2025-02-02", "2025-03-02", "2025-04-02"], 1
        )
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-05-01"))
    series = result.series_for("event_1")
    assert series is not None
    assert series.cadence_kind is CadenceKind.MONTHLY
    assert series.day_of_month == 2


def test_a_skipped_occurrence_does_not_break_cadence_detection() -> None:
    """A doubled gap means a missing month, not a two-month cadence."""
    events = [
        make_event(f"event_{i}", day=day, amount="100")
        for i, day in enumerate(
            ["2025-01-06", "2025-02-06", "2025-04-06", "2025-05-06"], 1
        )
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-06-01"))
    series = result.series_for("event_1")
    assert series is not None
    assert series.cadence_kind is CadenceKind.MONTHLY


def test_a_single_occurrence_is_one_time() -> None:
    events = [make_event("event_1", day="2025-01-06", amount="4999")]
    result = reconstruct_balance(make_context(events, request_date="2025-02-01"))
    assert result.by_id()["event_1"].recurrence is Recurrence.ONE_TIME
    series = result.series_for("event_1")
    assert series is not None
    assert not series.is_recurring
    assert "does not support recurrence" in series.reason or "no consistent" in series.reason


def test_irregular_dates_are_not_called_recurring() -> None:
    events = [
        make_event(f"event_{i}", day=day, amount="100")
        for i, day in enumerate(
            ["2025-01-03", "2025-01-19", "2025-03-27", "2025-04-02"], 1
        )
    ]
    result = reconstruct_balance(make_context(events, request_date="2025-06-01"))
    series = result.series_for("event_1")
    assert series is not None
    assert not series.is_recurring


def test_two_occurrence_debit_is_recurring_but_credit_is_not() -> None:
    """Asymmetric on purpose: under-forecasting an outflow is the unsafe error."""
    debit = [
        make_event("event_1", day="2025-01-06", amount="100", description="Bill"),
        make_event("event_2", day="2025-02-06", amount="100", description="Bill"),
    ]
    result = reconstruct_balance(make_context(debit, request_date="2025-03-01"))
    series = result.series_for("event_1")
    assert series is not None and series.is_recurring

    credit = [
        make_event(
            "event_1",
            day="2025-01-06",
            amount="100",
            direction="credit",
            event_type="income",
            description="Side income",
        ),
        make_event(
            "event_2",
            day="2025-02-06",
            amount="100",
            direction="credit",
            event_type="income",
            description="Side income",
        ),
    ]
    result = reconstruct_balance(make_context(credit, request_date="2025-03-01"))
    series = result.series_for("event_1")
    assert series is not None and not series.is_recurring


def test_series_are_keyed_by_description(dataset: load.Dataset) -> None:
    request = dataset.requests[0]
    context = dataset.get_user_context(request.user_id, request.request_id)
    result = reconstruct_balance(context)
    for series in result.series:
        descriptions = {item.event.description for item in series.occurrences}
        assert len(descriptions) == 1
        assert series.description in series.key


def test_real_dataset_finds_recurring_series_for_every_user(
    states: dict[str, state.BalanceState],
) -> None:
    for result in states.values():
        assert result.recurring_series, f"{result.user_id} has no recurring series"
        for series in result.recurring_series:
            assert series.cadence_days is not None
            assert series.cadence_kind is not CadenceKind.NONE


# ---------------------------------------------------------------------------
# Config flags
# ---------------------------------------------------------------------------


def test_fx_date_key_flag_is_readable_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert config.fx_date_key() == "cash_date"
    monkeypatch.setenv("BUYORWAIT_FX_DATE_KEY", "event_date")
    assert config.fx_date_key() == "event_date"
    monkeypatch.setenv("BUYORWAIT_FX_DATE_KEY", "nonsense")
    with pytest.raises(ValueError, match="invalid FX_DATE_KEY"):
        config.fx_date_key()


def test_quantize_at_flag_is_readable_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert config.quantize_at() == "conversion"
    monkeypatch.setenv("BUYORWAIT_QUANTIZE_AT", "render")
    assert config.quantize_at() == "render"
    monkeypatch.setenv("BUYORWAIT_QUANTIZE_AT", "nonsense")
    with pytest.raises(ValueError, match="invalid QUANTIZE_AT"):
        config.quantize_at()


def test_quantize_point_actually_changes_conversion_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = Decimal("1163530.494")
    assert load.quantize_on_conversion(raw) == Decimal("1163530.49")
    monkeypatch.setenv("BUYORWAIT_QUANTIZE_AT", "render")
    assert load.quantize_on_conversion(raw) == raw


def test_fx_date_key_changes_which_date_is_used(
    monkeypatch: pytest.MonkeyPatch, dataset: load.Dataset
) -> None:
    deferred = next(
        e
        for e in dataset.events
        if e.settlement_date is not None and e.settlement_date != e.event_date
    )
    assert deferred.fx_date == deferred.cash_date
    monkeypatch.setenv("BUYORWAIT_FX_DATE_KEY", "event_date")
    assert deferred.fx_date == deferred.event_date


def test_config_describe_reports_both_sweepable_flags() -> None:
    described = config.describe()
    assert described["fx_date_key"] == "cash_date"
    assert described["quantize_at"] == "conversion"
    assert described["forecast_horizon_days"] == 90


# ---------------------------------------------------------------------------
# Whole-dataset smoke
# ---------------------------------------------------------------------------


def test_reconstruct_balance_runs_for_every_evaluation_request(
    dataset: load.Dataset,
) -> None:
    for request in dataset.requests:
        context = dataset.get_user_context(request.user_id, request.request_id)
        result = reconstruct_balance(context)
        assert result.as_of == request.request_date
        assert result.available_balance > 0
        assert result.headroom > 0
        assert result.summary()["request_id"] == request.request_id

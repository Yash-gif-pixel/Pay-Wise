"""Tests for :mod:`src.changes`."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from src import candidates as cand
from src import capacity, changes, config, extract, forecast, load, state
from src.changes import Action, MAX_CHANGES, eligible_changes, select_changes
from tests.test_forecast import empty_evidence
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
    return context, balance, projection, assessment


# ---------------------------------------------------------------------------
# user_21 -- the rulebook's cited case
# ---------------------------------------------------------------------------


def test_user_21_eligible_changes_in_event_order(
    dataset: load.Dataset, cache
) -> None:
    """The ordering the greedy rule walks, and the shopping option it skips."""
    context, balance, _, _ = pipeline(dataset, "request_21", cache)
    rendered = [c.change.render() for c in eligible_changes(context, balance)]
    assert rendered == [
        "stop:event_1815",
        "reduce_to:event_1816:23.50",
        "reduce_to:event_1817:49.60",
        "reduce_to:event_1854:41",
    ]


def test_user_21_greedy_picks_the_first_two_not_the_larger_single(
    dataset: load.Dataset, cache
) -> None:
    """Reproduces the rulebook's cited set.

    ``event_1817`` (shopping) frees 71.14 per occurrence -- more than
    ``event_1815`` and ``event_1816`` combined -- so a fewest-changes or
    largest-first rule would pick it alone. Greedy in event order picks the
    first two, which is what the label says.

    The payment is sized to create a deficit, because the current forecast
    overstates capacity enough that this row looks safe with no change at all
    (see C3e / C7).
    """
    context, balance, projection, _ = pipeline(dataset, "request_21", cache)

    candidates = eligible_changes(context, balance)
    by_id = {c.change.event_id: c.change for c in candidates}
    assert by_id["event_1817"].freed_per_occurrence > (
        by_id["event_1815"].freed_per_occurrence
        + by_id["event_1816"].freed_per_occurrence
    )

    headroom = projection.minimum - projection.minimum_balance_to_keep

    # The label's own deficit: requested 1574.40 less amount_safe_to_pay
    # 1543.35. Applied to this forecast's headroom it lands squarely in the
    # band where exactly two changes are needed, which is what the label says.
    label_deficit = (
        context.request.requested_amount
        - context.request.expected.amount_safe_to_pay
    )
    assert label_deficit == Decimal("31.05")

    payments = [(context.request.request_date, headroom + label_deficit)]
    assert not forecast.payment_is_safe(projection, payments)

    selection = select_changes(context, balance, projection, payments)
    assert selection.sufficient
    assert selection.rendered == (
        "stop:event_1815",
        "reduce_to:event_1816:23.50",
    )

    # One change is not enough and three is more than needed, so the pair is
    # the genuine minimal sufficient prefix rather than an accident of sizing.
    only_first = forecast.apply_series_changes(
        projection, stopped=[selection.changes[0].series_key]
    )
    assert not forecast.payment_is_safe(only_first, payments)
    assert selection.changes[0].action is Action.STOP
    assert selection.changes[1].action is Action.REDUCE_TO
    assert selection.changes[1].new_amount == Decimal("23.50")
    # The events differ, as the contract requires.
    assert selection.changes[0].event_id != selection.changes[1].event_id


def test_user_21_the_larger_single_change_really_would_have_sufficed(
    dataset: load.Dataset, cache
) -> None:
    """Confirms the rule is a genuine choice, not a lack of alternatives."""
    context, balance, projection, _ = pipeline(dataset, "request_21", cache)
    headroom = projection.minimum - projection.minimum_balance_to_keep
    payments = [(context.request.request_date, headroom + Decimal("31.05"))]

    shopping = next(
        c.change for c in eligible_changes(context, balance)
        if c.change.event_id == "event_1817"
    )
    adjusted = forecast.apply_series_changes(
        projection, reduced_to={shopping.series_key: shopping.new_amount}
    )
    assert forecast.payment_is_safe(adjusted, payments)


def test_user_06_has_exactly_one_eligible_change_matching_the_label(
    dataset: load.Dataset, cache
) -> None:
    context, balance, _, _ = pipeline(dataset, "request_06", cache)
    rendered = [c.change.render() for c in eligible_changes(context, balance)]
    assert rendered == ["stop:event_476"]
    assert context.request.expected.spending_changes_needed == "stop:event_476"


# ---------------------------------------------------------------------------
# The two-layer gate
# ---------------------------------------------------------------------------


def _gate_context(*, flexibility: str, category: str, reduce_list, stop_list):
    events = [
        make_event(
            f"event_{index}",
            day=day,
            amount="1000",
            description="Flexible thing",
            category=category,
            flexibility=flexibility,
        )
        for index, day in enumerate(
            ["2025-01-05", "2025-02-05", "2025-03-05", "2025-04-05", "2025-05-05"], 1
        )
    ]
    for event in events:
        object.__setattr__(event, "minimum_allowed_amount", Decimal("200"))
        object.__setattr__(event, "minimum_allowed_amount_home", Decimal("200"))
    base = make_context(events, request_date="2025-05-20")
    profile = load.Profile(
        **{
            **base.profile.__dict__,
            "expense_categories_user_is_willing_to_reduce": reduce_list,
            "expense_categories_user_is_willing_to_stop": stop_list,
        }
    )
    context = load.UserContext(
        request=base.request,
        profile=profile,
        events=base.events,
        messages=base.messages,
        images=base.images,
        payment_options=base.payment_options,
    )
    return context, state.reconstruct_balance(context)


def test_flexible_event_in_a_category_the_user_refuses_is_never_selected() -> None:
    context, balance = _gate_context(
        flexibility="reducible",
        category="dining",
        reduce_list=(),          # user will not reduce anything
        stop_list=("streaming",),
    )
    assert eligible_changes(context, balance) == ()


def test_permitted_category_whose_event_is_fixed_is_never_selected() -> None:
    context, balance = _gate_context(
        flexibility="fixed",
        category="dining",
        reduce_list=("dining",),  # user is willing, the event is not flexible
        stop_list=(),
    )
    assert eligible_changes(context, balance) == ()


def test_both_layers_together_admit_the_change() -> None:
    context, balance = _gate_context(
        flexibility="reducible",
        category="dining",
        reduce_list=("dining",),
        stop_list=(),
    )
    candidates = eligible_changes(context, balance)
    assert len(candidates) == 1
    assert candidates[0].change.action is Action.REDUCE_TO


def test_the_category_layer_is_redundant_on_this_dataset(
    dataset: load.Dataset,
) -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    Reported, not resolved: layer two never excludes anything here.

    Every reduce-capable event sits in a permitted reduce category and every
    stop-capable event in a permitted stop category. Both layers are applied
    anyway so a future dataset that breaks the alignment is handled.
    """
    mismatches = []
    for event in dataset.events:
        profile = dataset.profile(event.user_id)
        if event.flexibility in ("reducible", "reducible_or_stoppable"):
            if event.category not in profile.expense_categories_user_is_willing_to_reduce:
                mismatches.append((event.event_id, "reduce"))
        if event.flexibility in ("stoppable", "reducible_or_stoppable"):
            if event.category not in profile.expense_categories_user_is_willing_to_stop:
                mismatches.append((event.event_id, "stop"))
    assert mismatches == []


# ---------------------------------------------------------------------------
# Action selection
# ---------------------------------------------------------------------------


def test_reducible_or_stoppable_yields_reduce_not_stop(
    dataset: load.Dataset, cache
) -> None:
    """event_1816 permits both; reduce wins."""
    context, balance, _, _ = pipeline(dataset, "request_21", cache)
    change = next(
        c.change
        for c in eligible_changes(context, balance)
        if c.change.event_id == "event_1816"
    )
    assert change.flexibility == "reducible_or_stoppable"
    assert change.action is Action.REDUCE_TO
    assert change.render().startswith("reduce_to:")


def test_reducible_or_stoppable_always_reduces_across_the_dataset(
    dataset: load.Dataset,
) -> None:
    for request in list(dataset.requests) + list(dataset.sample_requests):
        context = dataset.get_user_context(request.user_id, request.request_id)
        balance = state.reconstruct_balance(context)
        for candidate in eligible_changes(context, balance):
            events = {e.event_id: e for e in context.events}
            flexibility = events[candidate.change.event_id].flexibility
            if flexibility == "reducible_or_stoppable":
                assert candidate.change.action is Action.REDUCE_TO
            elif flexibility == "stoppable":
                assert candidate.change.action is Action.STOP
            elif flexibility == "reducible":
                assert candidate.change.action is Action.REDUCE_TO


def test_reduce_target_comes_from_the_data_never_computed(
    dataset: load.Dataset,
) -> None:
    events = {e.event_id: e for e in dataset.events}
    checked = 0
    for request in list(dataset.requests) + list(dataset.sample_requests):
        context = dataset.get_user_context(request.user_id, request.request_id)
        balance = state.reconstruct_balance(context)
        for candidate in eligible_changes(context, balance):
            change = candidate.change
            if change.action is Action.REDUCE_TO:
                checked += 1
                event = events[change.event_id]
                assert change.new_amount == event.minimum_allowed_amount_home
    assert checked > 0


def test_the_referenced_event_is_the_latest_settled_before_request_date(
    dataset: load.Dataset,
) -> None:
    events = {e.event_id: e for e in dataset.events}
    for request in list(dataset.requests)[:80] + list(dataset.sample_requests):
        context = dataset.get_user_context(request.user_id, request.request_id)
        balance = state.reconstruct_balance(context)
        for candidate in eligible_changes(context, balance):
            event = events[candidate.change.event_id]
            assert event.status == "settled"
            assert event.cash_date <= request.request_date
            later = [
                e
                for e in context.events
                if e.description == event.description
                and e.direction == event.direction
                and e.status == "settled"
                and e.cash_date <= request.request_date
                and e.cash_date > event.cash_date
            ]
            assert later == []


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


def test_the_cap_holds_at_three_even_when_a_fourth_would_help() -> None:
    events = []
    for series_index in range(5):
        for occurrence, day in enumerate(
            ["2025-01-05", "2025-02-05", "2025-03-05", "2025-04-05", "2025-05-05"], 1
        ):
            event = make_event(
                f"event_{series_index * 10 + occurrence}",
                day=day,
                amount="1000",
                description=f"Flexible series {series_index}",
                category="dining",
                flexibility="reducible",
            )
            object.__setattr__(event, "minimum_allowed_amount", Decimal("900"))
            object.__setattr__(event, "minimum_allowed_amount_home", Decimal("900"))
            events.append(event)

    base = make_context(
        events, request_date="2025-05-20", balance="100000", minimum="20000"
    )
    profile = load.Profile(
        **{
            **base.profile.__dict__,
            "expense_categories_user_is_willing_to_reduce": ("dining",),
            "expense_categories_user_is_willing_to_stop": (),
        }
    )
    context = load.UserContext(
        request=base.request,
        profile=profile,
        events=base.events,
        messages=base.messages,
        images=base.images,
        payment_options=base.payment_options,
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())

    assert len(eligible_changes(context, balance)) == 5

    # Demand more than three changes can free.
    headroom = projection.minimum - projection.minimum_balance_to_keep
    payments = [(context.request.request_date, headroom + Decimal("100000"))]
    selection = select_changes(context, balance, projection, payments)
    assert not selection.sufficient
    assert selection.changes == ()
    assert "the most" in selection.reason

    assert MAX_CHANGES == 3


def test_at_most_three_changes_are_ever_returned(
    dataset: load.Dataset, cache
) -> None:
    for request in dataset.requests[:60]:
        context, balance, projection, assessment = pipeline(
            dataset, request.request_id, cache
        )
        payments = [(request.request_date, request.requested_amount)]
        selection = select_changes(context, balance, projection, payments)
        assert len(selection.changes) <= MAX_CHANGES


# ---------------------------------------------------------------------------
# Safety is tested against every trough
# ---------------------------------------------------------------------------


def test_a_change_that_fixes_request_date_but_not_a_later_trough_is_rejected() -> None:
    """The whole horizon is re-tested, not the balance on the payment day."""
    events = []
    # Rent 40k monthly on the 3rd; salary 45k on the 15th; a small flexible
    # subscription that can be reduced but nowhere near enough.
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
    for index, day in enumerate(
        ["2025-01-08", "2025-02-08", "2025-03-08", "2025-04-08", "2025-05-08"], 1
    ):
        event = make_event(
            f"event_sub_{index}",
            day=day,
            amount="100",
            description="Streaming subscription",
            category="streaming",
            flexibility="reducible",
        )
        object.__setattr__(event, "minimum_allowed_amount", Decimal("10"))
        object.__setattr__(event, "minimum_allowed_amount_home", Decimal("10"))
        events.append(event)
    # Weekly groceries push monthly outflow above monthly income, so each
    # trough sits lower than the last and the breach is late in the horizon.
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

    base = make_context(
        events, request_date="2025-06-02", balance="100000", minimum="20000"
    )
    profile = load.Profile(
        **{
            **base.profile.__dict__,
            "expense_categories_user_is_willing_to_reduce": ("streaming",),
            "expense_categories_user_is_willing_to_stop": (),
        }
    )
    context = load.UserContext(
        request=base.request,
        profile=profile,
        events=base.events,
        messages=base.messages,
        images=base.images,
        payment_options=base.payment_options,
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())

    payment = Decimal("35000")
    payments = [(date(2025, 6, 2), payment)]

    # The payment leaves a comfortable balance on the day itself.
    after = forecast.with_payments(projection, payments)
    assert after.balance_on(date(2025, 6, 2)) > balance.minimum_balance_to_keep * 3
    # But a later trough breaks the floor.
    assert not forecast.payment_is_safe(projection, payments)

    # The only permitted change frees 90 per month, which cannot close it.
    selection = select_changes(context, balance, projection, payments)
    assert not selection.sufficient
    assert selection.changes == ()


def test_a_change_alters_the_series_for_the_rest_of_the_window() -> None:
    events = [
        make_event(
            f"event_sub_{index}",
            day=day,
            amount="1000",
            description="Streaming subscription",
            category="streaming",
            flexibility="stoppable",
        )
        for index, day in enumerate(
            ["2025-01-08", "2025-02-08", "2025-03-08", "2025-04-08", "2025-05-08"], 1
        )
    ]
    base = make_context(
        events, request_date="2025-06-02", balance="100000", minimum="20000"
    )
    profile = load.Profile(
        **{
            **base.profile.__dict__,
            "expense_categories_user_is_willing_to_stop": ("streaming",),
        }
    )
    context = load.UserContext(
        request=base.request,
        profile=profile,
        events=base.events,
        messages=base.messages,
        images=base.images,
        payment_options=base.payment_options,
    )
    balance = state.reconstruct_balance(context)
    projection = forecast.build_forecast(balance, evidence=empty_evidence())

    before = [f for f in projection.flows if f.description == "Streaming subscription"]
    assert len(before) >= 2, "need several future occurrences to be meaningful"

    adjusted = forecast.apply_series_changes(
        projection, stopped=["Streaming subscription|debit"]
    )
    after = [f for f in adjusted.flows if f.description == "Streaming subscription"]
    assert after == []
    assert adjusted.minimum > projection.minimum


# ---------------------------------------------------------------------------
# Integration with candidates
# ---------------------------------------------------------------------------


def test_the_candidates_hook_is_live(dataset: load.Dataset, cache) -> None:
    """SPENDING_CHANGES_NOT_IMPLEMENTED no longer appears once wired."""
    context, balance, projection, assessment = pipeline(
        dataset, "request_04", cache
    )
    result = cand.generate_candidates(
        context,
        projection,
        assessment,
        change_finder=changes.change_finder_for(balance),
    )
    assert not any(
        r.reason is cand.RejectionReason.SPENDING_CHANGES_NOT_IMPLEMENTED
        for r in result.rejections
    )


def test_a_changes_candidate_carries_its_changes(
    dataset: load.Dataset, cache
) -> None:
    found = False
    for request in dataset.requests[:120]:
        context, balance, projection, assessment = pipeline(
            dataset, request.request_id, cache
        )
        result = cand.generate_candidates(
            context,
            projection,
            assessment,
            change_finder=changes.change_finder_for(balance),
        )
        for candidate in result.eligible:
            if candidate.requires_changes:
                found = True
                assert candidate.spending_changes
                assert len(candidate.spending_changes) <= MAX_CHANGES
                assert all(
                    c.startswith(("stop:", "reduce_to:"))
                    for c in candidate.spending_changes
                )
    assert found, "no changes-based candidate anywhere in the first 120 requests"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_change_rendering_matches_the_label_format(
    dataset: load.Dataset, cache
) -> None:
    """Whole amounts render bare, fractional to two places."""
    context, balance, _, _ = pipeline(dataset, "request_21", cache)
    rendered = {c.change.event_id: c.change.render() for c in eligible_changes(context, balance)}
    assert rendered["event_1815"] == "stop:event_1815"
    assert rendered["event_1816"] == "reduce_to:event_1816:23.50"
    assert rendered["event_1854"] == "reduce_to:event_1854:41"


def test_stop_and_reduce_always_reference_different_events(
    dataset: load.Dataset, cache
) -> None:
    for request in dataset.requests[:60]:
        context, balance, projection, _ = pipeline(dataset, request.request_id, cache)
        payments = [(request.request_date, request.requested_amount)]
        selection = select_changes(context, balance, projection, payments)
        ids = [c.event_id for c in selection.changes]
        assert len(ids) == len(set(ids))

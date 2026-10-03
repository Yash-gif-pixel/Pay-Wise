"""Tests for :mod:`src.load`.

Three things matter most here and each has a dedicated section below:

1. No ``float`` ever appears on a money path. Scoring compares exact values
   against a ground truth whose safety margins are only a few currency
   units, so a binary-floating-point round trip is a scoring loss.
2. Every user in ``requests.csv`` resolves to exactly one profile.
3. Every ``request_id`` in ``request_payment_options.csv`` resolves to a
   known request.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src import load, paths
from src.load import (
    Dataset,
    DatasetError,
    ExchangeRate,
    ExchangeRateTable,
    MissingExchangeRateError,
    quantize,
)


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load.get_dataset()


def _raw(name: str) -> list[dict[str, str]]:
    with (paths.DATASET_DIR / name).open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# --------------------------------------------------------------------------
# 1. No float anywhere on an amount path
# --------------------------------------------------------------------------

#: Every field across the record types that carries money.
MONEY_FIELDS = {
    "current_available_balance",
    "minimum_balance_to_keep",
    "amount",
    "amount_home",
    "minimum_allowed_amount",
    "minimum_allowed_amount_home",
    "fx_rate",
    "requested_amount",
    "payment_amount",
    "financing_fee",
    "total_payable_amount",
    "amount_safe_to_pay",
    "rate",
}


def _assert_no_float(record: object, label: str) -> None:
    for name, value in load.iter_amount_fields(record):
        assert not isinstance(value, float), f"{label}.{name} is a float: {value!r}"
        leaf = name.rsplit(".", 1)[-1]
        if leaf in MONEY_FIELDS:
            assert value is None or isinstance(value, Decimal), (
                f"{label}.{name} should be Decimal or None, got {type(value).__name__}"
            )


def test_no_float_on_any_profile_amount(dataset: Dataset) -> None:
    for profile in dataset.profiles.values():
        _assert_no_float(profile, profile.user_id)


def test_no_float_on_any_event_amount(dataset: Dataset) -> None:
    for event in dataset.events:
        _assert_no_float(event, event.event_id)


def test_no_float_on_any_request_or_option_amount(dataset: Dataset) -> None:
    for request in dataset.requests + dataset.sample_requests:
        _assert_no_float(request, request.request_id)
    for option in dataset.payment_options:
        _assert_no_float(option, option.payment_option_id)


def test_decimals_are_parsed_from_text_not_via_float(dataset: Dataset) -> None:
    """``Decimal(1163530.49)`` and ``Decimal("1163530.49")`` are different numbers.

    Parsing through a float would leave a long binary tail; parsing from the
    source text keeps the exact stated value.
    """
    raw_amounts = {
        row["event_id"]: row["amount"].strip()
        for row in _raw("financial_events.csv")
        if row["amount"].strip()
    }
    for event in dataset.events:
        if event.amount is None:
            continue
        assert event.amount == Decimal(raw_amounts[event.event_id])
        assert str(event.amount) == raw_amounts[event.event_id]


def test_quantize_rounds_half_up_not_bankers() -> None:
    """Python's ``round`` is banker's rounding; money is not."""
    assert quantize(Decimal("2.345")) == Decimal("2.35")
    assert quantize(Decimal("2.355")) == Decimal("2.36")
    assert quantize(Decimal("0.005")) == Decimal("0.01")


def test_margins_of_a_few_currency_units_survive_arithmetic(dataset: Dataset) -> None:
    """A 1.90-unit margin must not be eroded by accumulation.

    Summing a long series of Decimals is exact; the same sum in float is not.
    """
    total = sum(
        (e.amount_home for e in dataset.events[:5000] if e.amount_home is not None),
        Decimal(0),
    )
    assert isinstance(total, Decimal)
    assert total - (total - Decimal("1.90")) == Decimal("1.90")
    assert total - (total - Decimal("3.45")) == Decimal("3.45")


# --------------------------------------------------------------------------
# 2. Every request user resolves to exactly one profile
# --------------------------------------------------------------------------


def test_every_request_user_resolves_to_exactly_one_profile(dataset: Dataset) -> None:
    counts: dict[str, int] = {}
    for row in _raw("financial_profiles.csv"):
        counts[row["user_id"]] = counts.get(row["user_id"], 0) + 1

    for request in dataset.requests:
        assert counts.get(request.user_id) == 1, (
            f"{request.request_id}: user {request.user_id} has "
            f"{counts.get(request.user_id, 0)} profile rows, expected exactly 1"
        )
        profile = dataset.profile(request.user_id)
        assert profile.user_id == request.user_id


def test_profile_lookup_rejects_unknown_user(dataset: Dataset) -> None:
    with pytest.raises(DatasetError):
        dataset.profile("user_does_not_exist")


def test_every_request_user_has_events(dataset: Dataset) -> None:
    for request in dataset.requests:
        context = dataset.get_user_context(request.user_id, request.request_id)
        assert context.events, f"{request.user_id} has no financial events"


# --------------------------------------------------------------------------
# 3. Payment-option request ids resolve
# --------------------------------------------------------------------------


def test_every_payment_option_request_id_resolves_to_a_known_request(
    dataset: Dataset,
) -> None:
    """Options cover both files.

    NOTE: the stricter phrasing "every request_id in
    ``request_payment_options.csv`` exists in ``requests.csv``" is false for
    this dataset by design -- 25 of the 275 covered requests belong to
    ``sample_requests.csv``. The real invariant is that every option resolves
    to *a* known request, asserted here, plus the evaluation-side coverage
    asserted in the next test.
    """
    for option in dataset.payment_options:
        request = dataset.request(option.request_id)
        assert request.request_id == option.request_id


def test_payment_option_coverage_splits_between_the_two_request_files(
    dataset: Dataset,
) -> None:
    covered = {option.request_id for option in dataset.payment_options}
    evaluation = {request.request_id for request in dataset.requests}
    samples = {request.request_id for request in dataset.sample_requests}

    assert covered - evaluation - samples == set()
    assert evaluation <= covered, "every evaluation request must have payment options"
    assert len(covered & evaluation) == 250
    assert len(covered & samples) == 25


def test_every_evaluation_request_has_between_two_and_four_options(
    dataset: Dataset,
) -> None:
    for request in dataset.requests:
        context = dataset.get_user_context(request.user_id, request.request_id)
        assert 2 <= len(context.payment_options) <= 4, (
            f"{request.request_id} has {len(context.payment_options)} options"
        )


# --------------------------------------------------------------------------
# Currency conversion
# --------------------------------------------------------------------------


def test_foreign_events_are_converted_into_home_currency(dataset: Dataset) -> None:
    """PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee."""
    converted = 0
    for event in dataset.events:
        if event.amount is None:
            continue
        if event.currency == event.home_currency:
            assert event.fx_rate == Decimal(1)
            assert event.amount_home == quantize(event.amount)
            continue
        converted += 1
        assert event.fx_rate is not None
        assert event.amount_home == quantize(event.amount * event.fx_rate)
        assert event.amount_home != event.amount
    # 140 foreign-currency events exist; event_7307 has a blank amount and so
    # has nothing to convert yet, leaving 139 actually converted at load time.
    foreign = [e for e in dataset.events if e.currency != e.home_currency]
    assert len(foreign) == 140
    assert converted == 139, f"expected 139 converted, saw {converted}"


def test_conversion_uses_the_settlement_date_rate(dataset: Dataset) -> None:
    rates = {
        (row["rate_date"], row["from_currency"], row["to_currency"]): Decimal(
            row["rate"]
        )
        for row in _raw("exchange_rates.csv")
    }
    for event in dataset.events:
        if event.currency == event.home_currency:
            continue
        key = (
            event.cash_date.isoformat(),
            event.currency,
            event.home_currency,
        )
        assert event.fx_rate == rates[key]


def test_missing_rate_raises_and_lists_every_gap() -> None:
    """A gap must surface, never fall back to a neighbouring date."""
    table = ExchangeRateTable(
        [
            ExchangeRate(date(2024, 1, 15), "USD", "INR", Decimal("83.33")),
        ]
    )
    assert table.rate("USD", "INR", date(2024, 1, 15)) == Decimal("83.33")
    # The 16th is one day away and must NOT resolve to the 15th.
    assert table.rate("USD", "INR", date(2024, 1, 16)) is None

    with pytest.raises(MissingExchangeRateError) as excinfo:
        table.convert(Decimal("100"), "USD", "INR", date(2024, 1, 16))
    assert "pair exists but not on that date" in str(excinfo.value)

    with pytest.raises(MissingExchangeRateError) as excinfo:
        table.convert(Decimal("100"), "ZAR", "INR", date(2024, 1, 15))
    assert "currency pair absent entirely" in str(excinfo.value)
    assert len(excinfo.value.gaps) == 1


def test_identity_conversion_is_exactly_one() -> None:
    table = ExchangeRateTable([])
    assert table.rate("EUR", "EUR", date(2024, 1, 1)) == Decimal(1)
    assert table.convert(Decimal("12.34"), "EUR", "EUR", date(2024, 1, 1)) == Decimal(
        "12.34"
    )


def test_conflicting_rate_rows_are_rejected() -> None:
    with pytest.raises(DatasetError, match="conflicting rates"):
        ExchangeRateTable(
            [
                ExchangeRate(date(2024, 1, 15), "USD", "INR", Decimal("83.33")),
                ExchangeRate(date(2024, 1, 15), "USD", "INR", Decimal("99.99")),
            ]
        )


# --------------------------------------------------------------------------
# Blank amounts stay None
# --------------------------------------------------------------------------


def test_blank_amounts_are_none_never_zero(dataset: Dataset) -> None:
    """PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee."""
    blank = [event for event in dataset.events if event.amount is None]
    assert len(blank) == 16
    for event in blank:
        assert event.amount is None
        assert event.amount_home is None
        assert event.amount != Decimal(0)


def test_every_blank_amount_event_has_a_linked_image(dataset: Dataset) -> None:
    linked = {
        image.related_event_id for image in dataset.images if image.related_event_id
    }
    for event in dataset.events:
        if event.amount is None:
            assert event.event_id in linked, (
                f"{event.event_id} has a blank amount and no image to recover it from"
            )


def test_blank_foreign_amount_can_still_be_converted_after_extraction(
    dataset: Dataset,
) -> None:
    """``event_7307`` is USD for an INR user, and its amount is blank.

    The rate is resolved at load time so extraction only has to supply the
    number; without this the conversion step would have nowhere to run.
    """
    event = next(e for e in dataset.events if e.event_id == "event_7307")
    assert event.amount is None
    assert event.amount_home is None
    assert event.currency == "USD"
    assert event.home_currency == "INR"
    assert event.fx_rate is not None
    assert event.to_home(Decimal("100")) == quantize(Decimal("100") * event.fx_rate)


def test_zero_amounts_do_not_exist_in_the_source(dataset: Dataset) -> None:
    """Guards the blank-vs-zero distinction from the other side."""
    assert not [e for e in dataset.events if e.amount == Decimal(0)]


# --------------------------------------------------------------------------
# Chronological ordering
# --------------------------------------------------------------------------


def test_events_are_sorted_chronologically(dataset: Dataset) -> None:
    dates = [event.event_date for event in dataset.events]
    assert dates == sorted(dates)


def test_each_user_event_stream_is_chronological(dataset: Dataset) -> None:
    for request in dataset.requests:
        context = dataset.get_user_context(request.user_id, request.request_id)
        dates = [event.event_date for event in context.events]
        assert dates == sorted(dates), f"{request.user_id} events out of order"


def test_source_file_is_grouped_by_series_so_sorting_is_required() -> None:
    """
    PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee.

    The raw file really is out of order -- the sort is not a no-op."""
    rows = _raw("financial_events.csv")
    by_user: dict[str, list[str]] = {}
    for row in rows:
        by_user.setdefault(row["user_id"], []).append(row["event_date"])
    unsorted = [user for user, dates in by_user.items() if dates != sorted(dates)]
    assert len(unsorted) == 275


def test_event_ids_sort_numerically_not_lexicographically() -> None:
    ordered = sorted(["event_10", "event_9", "event_100"], key=load.id_sort_key)
    assert ordered == ["event_9", "event_10", "event_100"]


def test_cash_date_falls_back_to_event_date(dataset: Dataset) -> None:
    """PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee."""
    without_settlement = [e for e in dataset.events if e.settlement_date is None]
    assert len(without_settlement) == 10
    for event in without_settlement:
        assert event.status == "unrealized"
        assert event.cash_date == event.event_date


# --------------------------------------------------------------------------
# Profile list columns
# --------------------------------------------------------------------------


def test_profile_list_columns_are_real_lists(dataset: Dataset) -> None:
    for profile in dataset.profiles.values():
        for value in (
            profile.financial_priorities,
            profile.expense_categories_to_protect,
            profile.expense_categories_user_is_willing_to_reduce,
            profile.expense_categories_user_is_willing_to_stop,
            profile.payment_methods_user_will_consider,
        ):
            assert isinstance(value, tuple)
            assert all(isinstance(item, str) for item in value)
            assert all(item == item.strip() and item for item in value)
            assert not any("|" in item for item in value)


def test_blank_list_columns_become_empty_tuples(dataset: Dataset) -> None:
    """PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee."""
    empty_reduce = [
        p
        for p in dataset.profiles.values()
        if p.expense_categories_user_is_willing_to_reduce == ()
    ]
    empty_stop = [
        p
        for p in dataset.profiles.values()
        if p.expense_categories_user_is_willing_to_stop == ()
    ]
    assert len(empty_reduce) == 39
    assert len(empty_stop) == 62


def test_payment_methods_parse_into_known_values(dataset: Dataset) -> None:
    allowed = {"full_payment", "partial_payment", "installments"}
    for profile in dataset.profiles.values():
        assert profile.payment_methods_user_will_consider
        assert set(profile.payment_methods_user_will_consider) <= allowed


def test_max_installment_months_is_int_or_none(dataset: Dataset) -> None:
    for profile in dataset.profiles.values():
        cap = profile.max_installment_months
        assert cap is None or isinstance(cap, int)
        # Blank exactly when the user will not consider installments.
        assert (cap is None) == (not profile.accepts("installments"))


# --------------------------------------------------------------------------
# get_user_context isolation
# --------------------------------------------------------------------------


def test_user_context_contains_only_that_user(dataset: Dataset) -> None:
    for request in dataset.requests[:40]:
        context = dataset.get_user_context(request.user_id, request.request_id)
        assert context.profile.user_id == request.user_id
        assert {e.user_id for e in context.events} == {request.user_id}
        assert {m.user_id for m in context.messages} <= {request.user_id}
        assert {i.user_id for i in context.images} <= {request.user_id}
        assert {o.request_id for o in context.payment_options} == {request.request_id}


def test_user_context_is_much_smaller_than_the_whole_file(dataset: Dataset) -> None:
    """Filtering happens in code; whole files must never reach a prompt."""
    request = dataset.requests[0]
    context = dataset.get_user_context(request.user_id, request.request_id)
    assert len(context.events) < len(dataset.events) / 100
    assert len(context.messages) <= 2
    assert len(context.images) <= 2


def test_user_context_rejects_a_request_belonging_to_another_user(
    dataset: Dataset,
) -> None:
    first, second = dataset.requests[0], dataset.requests[1]
    with pytest.raises(DatasetError, match="belongs to"):
        dataset.get_user_context(first.user_id, second.request_id)


def test_module_level_helper_matches_the_method(dataset: Dataset) -> None:
    request = dataset.requests[0]
    assert load.get_user_context(request.user_id, request.request_id) == (
        dataset.get_user_context(request.user_id, request.request_id)
    )


def test_context_history_split_is_consistent(dataset: Dataset) -> None:
    for request in dataset.requests[:25]:
        context = dataset.get_user_context(request.user_id, request.request_id)
        before = context.events_on_or_before(request.request_date)
        after = context.events_after(request.request_date)
        assert len(before) + len(after) == len(context.events)
        assert all(e.cash_date <= request.request_date for e in before)
        assert all(e.cash_date > request.request_date for e in after)


# --------------------------------------------------------------------------
# Field typing across the rest of the schema
# --------------------------------------------------------------------------


def test_dates_are_date_objects(dataset: Dataset) -> None:
    for request in dataset.requests:
        assert isinstance(request.request_date, date)
        assert isinstance(request.desired_completion_date, date)
        assert request.desired_completion_date >= request.request_date
    for option in dataset.payment_options:
        assert isinstance(option.first_payment_date, date)
    for message in dataset.messages:
        assert isinstance(message.sent_at, datetime)


def test_allows_partial_payment_is_a_real_bool(dataset: Dataset) -> None:
    for request in dataset.requests:
        assert isinstance(request.allows_partial_payment, bool)
    assert sum(1 for r in dataset.requests if r.allows_partial_payment) == 80


def test_optional_id_columns_are_none_when_blank(dataset: Dataset) -> None:
    """PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee."""
    linked = [e for e in dataset.events if e.linked_event_id is not None]
    assert len(linked) == 58
    assert all(e.linked_event_id != "" for e in linked)
    unlinked = [e for e in dataset.events if e.linked_event_id is None]
    assert len(unlinked) == 25284


def test_images_resolve_to_files_on_disk(dataset: Dataset) -> None:
    assert len(dataset.images) == 16
    for image in dataset.images:
        assert image.path == paths.MEDIA_IMAGES_DIR / f"{image.image_id}.png"
        assert image.exists, f"{image.image_id} has no PNG at {image.path}"


def test_sample_requests_carry_their_answers_and_evaluation_rows_do_not(
    dataset: Dataset,
) -> None:
    assert len(dataset.sample_requests) == 25
    for request in dataset.sample_requests:
        assert request.expected is not None
        assert isinstance(request.expected.amount_safe_to_pay, Decimal)
        assert Decimal(0) <= request.expected.amount_safe_to_pay <= (
            request.requested_amount
        )
    for request in dataset.requests:
        assert request.expected is None


def test_dataset_row_counts_are_unchanged_since_the_audit(dataset: Dataset) -> None:
    """PINNED MEASUREMENT -- asserts an observed value, not a behavioural guarantee."""
    assert len(dataset.profiles) == 275
    assert len(dataset.events) == 25342
    assert len(dataset.requests) == 250
    assert len(dataset.payment_options) == 790
    assert len(dataset.messages) == 215
    assert len(dataset.exchange_rates) == 134


def test_the_dataset_is_parsed_once_and_reused() -> None:
    assert load.get_dataset() is load.get_dataset()


def test_missing_dataset_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="missing dataset file"):
        load.load_dataset(tmp_path)


def test_iter_contexts_covers_every_evaluation_request(dataset: Dataset) -> None:
    contexts = list(dataset.iter_contexts())
    assert len(contexts) == 250
    assert [c.request_id for c in contexts] == [r.request_id for r in dataset.requests]

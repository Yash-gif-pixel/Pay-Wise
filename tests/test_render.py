"""Tests for :mod:`src.render`.

Formatting is scored, so these assert exact strings. The numeric columns and
the prose use different conventions and the first group of tests exists to
prove they cannot leak into each other.
"""

from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from src import load, paths, render
from src.candidates import Candidate, Method, Payment
from src.render import (
    OutputRow,
    format_iso_date,
    format_long_date,
    format_minimal,
    format_payment_plan,
    format_plan_amount,
    format_prose_amount,
    format_spending_changes,
)


@pytest.fixture(scope="module")
def dataset() -> load.Dataset:
    return load.get_dataset()


@pytest.fixture(scope="module")
def samples() -> list[dict[str, str]]:
    path = paths.DATASET_DIR / "sample_requests.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def payments(*entries: tuple[str, str]) -> tuple[Payment, ...]:
    return tuple(
        Payment(on=date.fromisoformat(day), amount=Decimal(amount))
        for day, amount in entries
    )


# ---------------------------------------------------------------------------
# The two decimal conventions must not leak
# ---------------------------------------------------------------------------


def test_the_same_value_renders_differently_in_the_two_columns() -> None:
    """603.3 as a safe amount, 603.30 inside a plan -- from one Decimal."""
    value = Decimal("603.30")
    assert format_minimal(value) == "603.3"
    assert format_plan_amount(value) == "603.30"
    assert format_minimal(value) != format_plan_amount(value)

    # And the equal-but-differently-scaled Decimal behaves identically.
    assert format_minimal(Decimal("603.3")) == "603.3"
    assert format_plan_amount(Decimal("603.3")) == "603.30"


def test_minimal_strips_trailing_zeros_without_exponent_notation() -> None:
    assert format_minimal(Decimal("603.30")) == "603.3"
    assert format_minimal(Decimal("603.00")) == "603"
    assert format_minimal(Decimal("25256.00")) == "25256"
    assert format_minimal(Decimal("17229139.20")) == "17229139.2"
    assert format_minimal(Decimal("0.00")) == "0"
    assert format_minimal(Decimal("12510645")) == "12510645"
    # A whole number must never come out as 2.5256E+4.
    assert "E" not in format_minimal(Decimal("25256.00"))
    assert "e" not in format_minimal(Decimal("60496000.00"))


def test_a_whole_number_plan_amount_has_no_decimal_point_at_all() -> None:
    assert format_plan_amount(Decimal("25256")) == "25256"
    assert format_plan_amount(Decimal("25256.00")) == "25256"
    assert format_plan_amount(Decimal("12693000")) == "12693000"
    assert "." not in format_plan_amount(Decimal("28820.00"))


def test_a_fractional_plan_amount_keeps_its_trailing_zero() -> None:
    assert format_plan_amount(Decimal("620.4")) == "620.40"
    assert format_plan_amount(Decimal("620.40")) == "620.40"
    assert format_plan_amount(Decimal("941.6")) == "941.60"
    assert format_plan_amount(Decimal("996.6")) == "996.60"
    assert format_plan_amount(Decimal("15952906.67")) == "15952906.67"


def test_plan_amounts_never_use_thousands_separators() -> None:
    assert format_plan_amount(Decimal("15952906.67")) == "15952906.67"
    assert "," not in format_plan_amount(Decimal("12693000"))
    assert "," not in format_payment_plan(payments(("2024-06-15", "12693000")))


def test_prose_amounts_do_use_thousands_separators() -> None:
    assert format_prose_amount(Decimal("25256")) == "25,256"
    assert format_prose_amount(Decimal("15952906.67")) == "15,952,906.67"
    assert format_prose_amount(Decimal("620.40")) == "620.40"
    assert format_prose_amount(Decimal("600")) == "600"


# ---------------------------------------------------------------------------
# payment_plan
# ---------------------------------------------------------------------------


def test_plan_is_chronological_and_pipe_joined() -> None:
    out_of_order = payments(("2024-10-10", "300"), ("2024-09-04", "28820"))
    assert (
        format_payment_plan(out_of_order) == "2024-09-04:28820|2024-10-10:300"
    )


def test_an_empty_plan_renders_none() -> None:
    assert format_payment_plan(()) == "none"


def test_wait_rows_carry_a_dated_plan_not_none(dataset: load.Dataset) -> None:
    winner = Candidate(
        method=Method.WAIT,
        payments=payments(("2024-06-15", "12693000")),
        total_paid=Decimal("12693000"),
        requires_changes=False,
    )
    rendered = format_payment_plan(winner.payments)
    assert rendered == "2024-06-15:12693000"
    assert rendered != "none"


def test_every_labelled_wait_row_has_a_dated_plan(samples) -> None:
    waits = [r for r in samples if r["recommended_payment_method"] == "wait"]
    assert len(waits) == 6
    for row in waits:
        assert row["payment_plan"] != "none"
        assert ":" in row["payment_plan"]
        assert "|" not in row["payment_plan"], "wait is a single future payment"


# ---------------------------------------------------------------------------
# Blank earliest_date
# ---------------------------------------------------------------------------


def test_blank_earliest_date_is_an_empty_string_not_none_or_nan() -> None:
    assert format_iso_date(None) == ""
    assert format_iso_date(None) is not None
    assert "None" not in format_iso_date(None)
    assert "nan" not in format_iso_date(None)
    assert format_iso_date(date(2024, 3, 3)) == "2024-03-03"


def test_a_blank_earliest_date_survives_a_csv_round_trip() -> None:
    """Guards against the value becoming 'None' or 'nan' on write."""
    row = OutputRow(
        request_id="request_05",
        amount_safe_to_pay="737",
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="none",
        earliest_date_for_full_payment=format_iso_date(None),
        spending_changes_needed="none",
        decision_explanation="Do not make this payment.",
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(paths.OUTPUT_COLUMNS)
    writer.writerow(row.as_tuple())

    text = buffer.getvalue()
    assert "None" not in text
    assert "nan" not in text
    assert text.strip().endswith("none,,none,Do not make this payment.")

    parsed = list(csv.DictReader(io.StringIO(text)))[0]
    assert parsed["earliest_date_for_full_payment"] == ""


def test_every_labelled_not_recommended_row_has_a_blank_earliest_date(
    samples,
) -> None:
    rows = [
        r for r in samples if r["recommended_payment_method"] == "not_recommended"
    ]
    assert len(rows) == 7
    for row in rows:
        assert row["earliest_date_for_full_payment"] == ""
        assert row["payment_plan"] == "none"


def test_affordable_now_rows_have_earliest_equal_to_request_date(samples) -> None:
    rows = [r for r in samples if r["affordability_status"] == "affordable_now"]
    assert len(rows) == 3
    for row in rows:
        assert row["earliest_date_for_full_payment"] == row["request_date"]


# ---------------------------------------------------------------------------
# spending_changes_needed
# ---------------------------------------------------------------------------


def test_a_reduce_amount_of_exactly_23_50_keeps_its_trailing_zero() -> None:
    assert format_plan_amount(Decimal("23.5")) == "23.50"
    assert format_plan_amount(Decimal("23.50")) == "23.50"
    rendered = format_spending_changes(
        ("stop:event_1815", "reduce_to:event_1816:23.50")
    )
    assert rendered == "stop:event_1815|reduce_to:event_1816:23.50"
    assert rendered.endswith("23.50")


def test_empty_changes_render_none() -> None:
    assert format_spending_changes(()) == "none"


def test_more_than_three_changes_is_rejected() -> None:
    with pytest.raises(AssertionError, match="at most three"):
        format_spending_changes(("a", "b", "c", "d"))


def test_labelled_change_strings_round_trip(samples) -> None:
    for row in samples:
        text = row["spending_changes_needed"]
        parsed = [] if text == "none" else text.split("|")
        assert format_spending_changes(parsed) == text


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


def test_long_dates_have_no_leading_zero_on_the_day() -> None:
    assert format_long_date(date(2024, 9, 15)) == "15 September 2024"
    assert format_long_date(date(2025, 8, 8)) == "8 August 2025"
    assert format_long_date(date(2026, 3, 1)) == "1 March 2026"
    assert format_long_date(date(2024, 4, 17)) == "17 April 2024"


# ---------------------------------------------------------------------------
# Explanation templates, checked against the labels
# ---------------------------------------------------------------------------


def test_affordable_now_template_matches_the_label(dataset: load.Dataset) -> None:
    profile = dataset.profile("user_01")
    assert render.explain_affordable_now(
        profile.home_currency, Decimal("25256"), profile.minimum_balance_to_keep
    ) == (
        "Pay ZAR 25,256 today. This leaves at least ZAR 18,000 available over "
        "the next 90 days."
    )


def test_installments_template_matches_the_label(dataset: load.Dataset) -> None:
    profile = dataset.profile("user_02")
    assert render.explain_installments(
        profile.home_currency,
        3,
        Decimal("15952906.67"),
        date(2025, 8, 8),
        profile.minimum_balance_to_keep,
    ) == (
        "Use 3 installments of IDR 15,952,906.67, starting 8 August 2025. "
        "This leaves at least IDR 29,158,400 available."
    )


def test_wait_template_matches_the_label(dataset: load.Dataset) -> None:
    profile = dataset.profile("user_23")
    assert render.explain_wait(
        profile.home_currency,
        Decimal("38016"),
        date(2025, 7, 15),
        profile.minimum_balance_to_keep,
    ) == (
        "Pay ZAR 38,016 in full on 15 July 2025. Paying earlier would take the "
        "balance below the ZAR 27,000 minimum."
    )


def test_full_with_changes_template_matches_the_two_change_label(
    dataset: load.Dataset,
) -> None:
    profile = dataset.profile("user_21")
    context = dataset.get_user_context("user_21", "request_21")
    descriptions = {e.event_id: e.description for e in context.events}
    assert render.explain_full_with_changes(
        profile.home_currency,
        Decimal("1574.40"),
        profile.minimum_balance_to_keep,
        ("stop:event_1815", "reduce_to:event_1816:23.50"),
        descriptions,
    ) == (
        "Stop the online backup subscription and reduce the streaming "
        "subscription to USD 23.50, then pay USD 1,574.40 today. This leaves "
        "at least USD 1,800 available."
    )


def test_partial_template_matches_the_label(dataset: load.Dataset) -> None:
    profile = dataset.profile("user_19")
    assert render.explain_partial(
        profile.home_currency,
        Decimal("28820"),
        Decimal("10840"),
        date(2024, 9, 15),
        profile.minimum_balance_to_keep,
    ) == (
        "Pay INR 28,820 today and the remaining INR 10,840 on 15 September "
        "2024. This completes the full request and keeps the INR 92,800 "
        "minimum protected."
    )


def test_not_affordable_a_template_matches_the_label(dataset: load.Dataset) -> None:
    profile = dataset.profile("user_25")
    assert render.explain_not_affordable_a(
        profile.home_currency, date(2024, 4, 17), profile.minimum_balance_to_keep
    ) == (
        "Do not make this payment by 17 April 2024. None of the available "
        "options keeps the IDR 23,379,100 minimum protected."
    )


def test_not_affordable_b_template_matches_the_label(dataset: load.Dataset) -> None:
    profile = dataset.profile("user_14")
    assert render.explain_not_affordable_b(
        profile.home_currency, Decimal("5414.2"), Decimal("597.74")
    ) == (
        "Do not proceed with the EUR 5,414.20 request. Although EUR 597.74 is "
        "available today, the full amount cannot be completed safely within "
        "90 days."
    )


def test_the_a_b_threshold_direction_follows_the_labels(samples) -> None:
    """Template B is used at HIGH ratios, not low ones.

    The phase brief states the opposite. The labels are unambiguous: both B
    rows sit above 11% while all five A rows sit below 5%.
    """
    ratios = {}
    for row in samples:
        if row["recommended_payment_method"] != "not_recommended":
            continue
        ratio = Decimal(row["amount_safe_to_pay"]) / Decimal(row["requested_amount"])
        uses_b = "Do not proceed with the" in row["decision_explanation"]
        ratios[row["request_id"]] = (ratio, uses_b)

    b_rows = [r for r, (_, b) in ratios.items() if b]
    a_rows = [r for r, (_, b) in ratios.items() if not b]
    assert len(b_rows) == 2 and len(a_rows) == 5

    highest_a = max(ratios[r][0] for r in a_rows)
    lowest_b = min(ratios[r][0] for r in b_rows)
    assert highest_a < render.NOT_AFFORDABLE_B_MIN_RATIO <= lowest_b


def test_a_single_change_phrase_is_capitalised(dataset: load.Dataset) -> None:
    profile = dataset.profile("user_06")
    context = dataset.get_user_context("user_06", "request_06")
    descriptions = {e.event_id: e.description for e in context.events}
    assert render.explain_full_with_changes(
        profile.home_currency,
        Decimal("620.40"),
        profile.minimum_balance_to_keep,
        ("stop:event_476",),
        descriptions,
    ) == (
        "Stop the family streaming plan, then pay EUR 620.40 today. This "
        "leaves at least EUR 800 available."
    )


# ---------------------------------------------------------------------------
# No model call
# ---------------------------------------------------------------------------


def test_rendering_makes_no_model_call() -> None:
    """Explanations are string fills; per-request model cost stays at zero."""
    import ast

    source = (paths.SRC_DIR / "render.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "anthropic" not in imported
    assert "extract" not in imported, "render must not reach the metered path"

    called = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not any("messages.create" in name for name in called)
    assert not any("TokenMeter" in name for name in called)


# ---------------------------------------------------------------------------
# Whole-row rendering against the labels
# ---------------------------------------------------------------------------


def test_every_labelled_numeric_field_round_trips(samples) -> None:
    """The formatting validation harness, condensed into one assertion."""
    for row in samples:
        assert format_minimal(Decimal(row["amount_safe_to_pay"])) == (
            row["amount_safe_to_pay"]
        )
        if row["payment_plan"] != "none":
            parsed = []
            for chunk in row["payment_plan"].split("|"):
                day, amount = chunk.split(":")
                parsed.append(
                    Payment(on=date.fromisoformat(day), amount=Decimal(amount))
                )
            assert format_payment_plan(parsed) == row["payment_plan"]


def test_output_row_column_order_matches_the_contract() -> None:
    row = OutputRow(
        request_id="r",
        amount_safe_to_pay="1",
        affordability_status="s",
        recommended_payment_method="m",
        payment_plan="p",
        earliest_date_for_full_payment="e",
        spending_changes_needed="c",
        decision_explanation="x",
    )
    assert tuple(row.as_dict()) == paths.OUTPUT_COLUMNS
    assert len(row.as_tuple()) == 8

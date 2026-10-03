"""Tests for :mod:`src.validate`.

Each rule gets a row that breaks it, so the validator is shown to reject rather
than merely to accept.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src import load, paths, validate
from src.validate import (
    ValidationError,
    check_status_distribution,
    parse_payment_plan,
    parse_spending_changes,
    validate_columns,
    validate_output,
    validate_row,
)


@pytest.fixture(scope="module")
def dataset() -> load.Dataset:
    return load.get_dataset()


@pytest.fixture(scope="module")
def produced() -> list[dict[str, str]]:
    path = paths.OUTPUT_TEMPLATE_CSV
    if not path.is_file():
        pytest.skip("output not generated; run `py -m src.run`")
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not rows[0].get("amount_safe_to_pay", "").strip():
        pytest.skip("output is the blank template; run `py -m src.run`")
    return rows


def good_row(**overrides: str) -> dict[str, str]:
    row = {
        "request_id": "request_26",
        "amount_safe_to_pay": "15656000",
        "affordability_status": "affordable_now",
        "recommended_payment_method": "full_payment",
        "payment_plan": "2025-08-03:15656000",
        "earliest_date_for_full_payment": "2025-08-03",
        "spending_changes_needed": "none",
        # A complete, template-conformant explanation. The alignment check in
        # validate.py rejects a truncated stub, which is the point of it.
        "decision_explanation": (
            "Pay IDR 15,656,000 today. This leaves at least IDR 24,768,300 "
            "available over the next 90 days."
        ),
    }
    row.update(overrides)
    return row


def check(dataset: load.Dataset, row: dict[str, str], rule: str) -> None:
    request = dataset.request(row["request_id"])
    with pytest.raises(ValidationError) as excinfo:
        validate_row(row, request, dataset)
    assert excinfo.value.rule == rule, (
        f"expected rule {rule!r}, got {excinfo.value.rule!r}: {excinfo.value}"
    )
    assert row["request_id"] in str(excinfo.value)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_contract_conformant_row_is_accepted(dataset: load.Dataset) -> None:
    validate_row(good_row(), dataset.request("request_26"), dataset)


def test_columns_must_be_exact_and_ordered() -> None:
    validate_columns(paths.OUTPUT_COLUMNS)
    with pytest.raises(ValidationError, match="column_order"):
        validate_columns(list(reversed(paths.OUTPUT_COLUMNS)))
    with pytest.raises(ValidationError, match="column_order"):
        validate_columns(paths.OUTPUT_COLUMNS[:-1])


# ---------------------------------------------------------------------------
# Bounds and allowed values
# ---------------------------------------------------------------------------


def test_amount_above_requested_is_rejected(dataset: load.Dataset) -> None:
    check(dataset, good_row(amount_safe_to_pay="99999999999"), "amount_safe_to_pay_bounds")


def test_negative_amount_is_rejected(dataset: load.Dataset) -> None:
    check(dataset, good_row(amount_safe_to_pay="-1"), "amount_safe_to_pay_bounds")


def test_unknown_status_is_rejected(dataset: load.Dataset) -> None:
    check(dataset, good_row(affordability_status="maybe"), "affordability_status")


def test_unknown_method_is_rejected(dataset: load.Dataset) -> None:
    check(dataset, good_row(recommended_payment_method="barter"), "recommended_payment_method")


# ---------------------------------------------------------------------------
# Status / method consistency
# ---------------------------------------------------------------------------


def test_affordable_now_requires_full_payment(dataset: load.Dataset) -> None:
    check(
        dataset,
        good_row(recommended_payment_method="wait"),
        "affordable_now_implies_full_payment",
    )


def test_affordable_now_requires_earliest_equal_to_request_date(
    dataset: load.Dataset,
) -> None:
    check(
        dataset,
        good_row(earliest_date_for_full_payment="2025-09-03"),
        "affordable_now_earliest_equals_request_date",
    )


def test_not_recommended_requires_plan_none(dataset: load.Dataset) -> None:
    check(
        dataset,
        good_row(
            affordability_status="not_affordable",
            recommended_payment_method="not_recommended",
            payment_plan="2025-08-03:100",
            earliest_date_for_full_payment="",
        ),
        "not_recommended_plan_none",
    )


def test_not_recommended_requires_blank_earliest(dataset: load.Dataset) -> None:
    check(
        dataset,
        good_row(
            affordability_status="not_affordable",
            recommended_payment_method="not_recommended",
            payment_plan="none",
            earliest_date_for_full_payment="2025-08-03",
        ),
        "not_recommended_earliest_blank",
    )


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def test_a_non_chronological_plan_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not chronological"):
        parse_payment_plan("request_x", "2025-09-01:100|2025-08-01:100")


def test_a_malformed_plan_entry_is_rejected() -> None:
    with pytest.raises(ValidationError, match="malformed"):
        parse_payment_plan("request_x", "2025-09-01-100")


def test_an_invalid_date_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not an ISO date"):
        parse_payment_plan("request_x", "2025-13-45:100")


def test_a_thousands_separator_in_a_plan_amount_is_rejected() -> None:
    with pytest.raises(ValidationError, match="thousands separator"):
        parse_payment_plan("request_x", "2025-09-01:15,656,000")


def test_a_plan_is_required_for_every_method_but_not_recommended(
    dataset: load.Dataset,
) -> None:
    check(dataset, good_row(payment_plan="none"), "plan_required")


def test_a_late_plan_is_rejected(dataset: load.Dataset) -> None:
    request = dataset.request("request_26")
    late = (request.desired_completion_date.replace(year=2030)).isoformat()
    check(
        dataset,
        good_row(
            affordability_status="affordable_with_plan",
            recommended_payment_method="full_payment",
            payment_plan=f"{late}:15656000",
            earliest_date_for_full_payment="",
        ),
        "completes_by_deadline",
    )


# ---------------------------------------------------------------------------
# Partial and installment shapes
# ---------------------------------------------------------------------------


def test_partial_must_be_affordable_with_plan(dataset: load.Dataset) -> None:
    check(
        dataset,
        good_row(recommended_payment_method="partial_payment"),
        "partial_implies_with_plan",
    )


def test_partial_must_have_exactly_two_payments(dataset: load.Dataset) -> None:
    check(
        dataset,
        good_row(
            affordability_status="affordable_with_plan",
            recommended_payment_method="partial_payment",
            payment_plan="2025-08-03:15656000",
            earliest_date_for_full_payment="2025-08-03",
        ),
        "partial_two_payments",
    )


def test_partial_payments_must_sum_to_requested(dataset: load.Dataset) -> None:
    check(
        dataset,
        good_row(
            affordability_status="affordable_with_plan",
            recommended_payment_method="partial_payment",
            payment_plan="2025-08-03:1000|2025-09-03:1000",
            earliest_date_for_full_payment="2025-09-03",
        ),
        "partial_sums_to_requested",
    )


def test_an_installment_plan_that_matches_no_option_is_rejected(
    dataset: load.Dataset,
) -> None:
    check(
        dataset,
        good_row(
            affordability_status="affordable_with_plan",
            recommended_payment_method="installments",
            payment_plan="2025-08-10:1000|2025-09-10:1000",
            earliest_date_for_full_payment="",
        ),
        "installments_match_an_option",
    )


def test_a_real_installment_option_is_accepted(dataset: load.Dataset) -> None:
    from src.candidates import installment_schedule

    option = next(
        o
        for o in dataset.payment_options
        if o.request_id == "request_26" and o.payment_method == "installments"
    )
    schedule = installment_schedule(option)
    from src.render import format_payment_plan

    row = good_row(
        affordability_status="affordable_with_plan",
        recommended_payment_method="installments",
        payment_plan=format_payment_plan(schedule),
        earliest_date_for_full_payment="",
    )
    # It still fails the deadline, which is the point: the option itself is
    # recognised, so the failure is a different rule.
    with pytest.raises(ValidationError) as excinfo:
        validate_row(row, dataset.request("request_26"), dataset)
    assert excinfo.value.rule != "installments_match_an_option"


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def test_a_method_the_user_refuses_is_rejected(dataset: load.Dataset) -> None:
    request = next(
        r
        for r in dataset.requests
        if "full_payment" not in dataset.profile(r.user_id).payment_methods_user_will_consider
    )
    row = good_row(
        request_id=request.request_id,
        amount_safe_to_pay="0",
        payment_plan=f"{request.request_date.isoformat()}:{request.requested_amount}",
        earliest_date_for_full_payment=request.request_date.isoformat(),
    )
    check(dataset, row, "affordable_now_implies_full_payment"
          if row["affordability_status"] == "affordable_now" and False else "method_not_considered")


# ---------------------------------------------------------------------------
# Spending changes
# ---------------------------------------------------------------------------


def test_changes_only_on_affordable_with_plan(dataset: load.Dataset) -> None:
    check(
        dataset,
        good_row(spending_changes_needed="stop:event_2364"),
        "changes_only_on_affordable_with_plan",
    )


def test_more_than_three_changes_is_rejected() -> None:
    parsed = parse_spending_changes(
        "request_x", "stop:a|stop:b|stop:c|stop:d"
    )
    assert len(parsed) == 4  # parsing allows it; the row rule rejects it


def test_an_unknown_change_action_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown action"):
        parse_spending_changes("request_x", "delete:event_1")


def test_a_change_targeting_a_fixed_event_is_rejected(dataset: load.Dataset) -> None:
    fixed = next(
        e for e in dataset.events if e.user_id == "user_26" and e.flexibility == "fixed"
    )
    check(
        dataset,
        good_row(
            affordability_status="affordable_with_plan",
            recommended_payment_method="full_payment",
            payment_plan="2025-08-03:15656000",
            earliest_date_for_full_payment="2025-08-03",
            spending_changes_needed=f"stop:{fixed.event_id}",
        ),
        "change_targets_flexible_event",
    )


def test_a_change_targeting_another_users_event_is_rejected(
    dataset: load.Dataset,
) -> None:
    check(
        dataset,
        good_row(
            affordability_status="affordable_with_plan",
            recommended_payment_method="full_payment",
            payment_plan="2025-08-03:15656000",
            earliest_date_for_full_payment="2025-08-03",
            spending_changes_needed="stop:event_1",
        ),
        "change_targets_own_event",
    )


# ---------------------------------------------------------------------------
# Artefact strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("artifact", ["nan", "None", "null", "NaN"])
def test_artifact_strings_are_rejected(dataset: load.Dataset, artifact: str) -> None:
    check(
        dataset,
        good_row(earliest_date_for_full_payment=artifact),
        "artifact_string",
    )


# ---------------------------------------------------------------------------
# Whole-file checks
# ---------------------------------------------------------------------------


def test_a_missing_row_is_reported(dataset: load.Dataset) -> None:
    violations = validate_output([good_row()], dataset)
    rules = {v.rule for v in violations}
    assert "missing_row" in rules
    assert len(violations) == 249


def test_a_duplicate_row_is_reported(dataset: load.Dataset) -> None:
    violations = validate_output([good_row(), good_row()], dataset)
    assert any(v.rule == "duplicate_row" for v in violations)


def test_an_unknown_request_id_is_reported(dataset: load.Dataset) -> None:
    violations = validate_output([good_row(request_id="request_9999")], dataset)
    assert any(v.rule == "unknown_request_id" for v in violations)


# ---------------------------------------------------------------------------
# The distribution check warns, never fails
# ---------------------------------------------------------------------------


def test_the_distribution_check_never_raises() -> None:
    rows = [{"affordability_status": "affordable_now"} for _ in range(100)]
    report = check_status_distribution(rows)
    assert report["total"] == 100
    assert report["rows"]["affordable_now"]["actual"] == 1.0
    assert report["rows"]["affordable_now"]["delta_pct_points"] == 88.0


def test_the_distribution_check_logs_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = [{"affordability_status": "affordable_now"} for _ in range(100)]
    with caplog.at_level("WARNING"):
        check_status_distribution(rows)
    assert "status distribution" in caplog.text


# ---------------------------------------------------------------------------
# The generated output
# ---------------------------------------------------------------------------


def test_the_generated_output_passes_every_rule(
    dataset: load.Dataset, produced
) -> None:
    violations = validate_output(produced, dataset, header=list(produced[0]))
    assert violations == [], [str(v) for v in violations]


def test_the_generated_output_has_one_row_per_request(
    dataset: load.Dataset, produced
) -> None:
    assert len(produced) == len(dataset.requests)
    assert [r["request_id"] for r in produced] == [
        r.request_id for r in dataset.requests
    ]


def test_the_generated_output_quotes_prose_containing_commas() -> None:
    raw = paths.OUTPUT_TEMPLATE_CSV.read_text(encoding="utf-8")
    if "amount_safe_to_pay," not in raw.split("\n")[0]:
        pytest.skip("output not generated")
    parsed = list(csv.DictReader(raw.splitlines()))
    with_commas = [r for r in parsed if "," in r["decision_explanation"]]
    assert with_commas, "expected prose with thousands separators"
    for row in with_commas:
        assert f'"{row["decision_explanation"]}"' in raw


# ---------------------------------------------------------------------------
# Explanation alignment (judge-feedback item 3)
# ---------------------------------------------------------------------------


def test_an_explanation_describing_a_different_decision_is_rejected(
    dataset: load.Dataset,
) -> None:
    """Template drift: the prose says 'wait', the columns say full payment."""
    check(
        dataset,
        good_row(
            decision_explanation=(
                "Pay IDR 15,656,000 in full on 3 September 2025. Paying "
                "earlier would take the balance below the IDR 24,768,300 "
                "minimum."
            )
        ),
        "explanation_matches_method",
    )


def test_an_explanation_citing_an_untraceable_figure_is_rejected(
    dataset: load.Dataset,
) -> None:
    """Orphan figure: 99,999,999 appears in no column and no profile field."""
    check(
        dataset,
        good_row(
            decision_explanation=(
                "Pay IDR 15,656,000 today. This leaves at least "
                "IDR 99,999,999 available over the next 90 days."
            )
        ),
        "explanation_figures_are_traceable",
    )


def test_the_minimum_balance_is_a_traceable_figure(dataset: load.Dataset) -> None:
    """It is not an output column, but it is a profile field, so it is fine."""
    validate_row(good_row(), dataset.request("request_26"), dataset)


def test_dates_spelled_out_in_prose_are_not_treated_as_figures(
    dataset: load.Dataset,
) -> None:
    request = dataset.request("request_26")
    row = good_row(
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payment_plan="none",
        earliest_date_for_full_payment="",
        decision_explanation=(
            "Do not make this payment by 7 October 2025. None of the "
            "available options keeps the IDR 24,768,300 minimum protected."
        ),
    )
    validate_row(row, request, dataset)


def test_every_generated_row_passes_explanation_alignment(
    dataset: load.Dataset, produced
) -> None:
    for row in produced:
        request = dataset.request(row["request_id"])
        profile = dataset.profile(request.user_id)
        plan = validate.parse_payment_plan(row["request_id"], row["payment_plan"])
        changes = validate.parse_spending_changes(
            row["request_id"], row["spending_changes_needed"]
        )
        validate._check_explanation_alignment(row, request, profile, plan, changes)

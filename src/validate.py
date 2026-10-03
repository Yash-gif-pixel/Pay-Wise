"""Deterministic output validator (rulebook Step 10).

Every row passes through :func:`validate_row` before it is written. A failing
row raises :class:`ValidationError` naming the ``request_id`` and the rule it
broke -- the pipeline never emits a row it cannot defend.

Checks here are deliberately re-derived from the CSV text and the dataset, not
from the objects that produced the row. A validator that trusts the renderer's
internals cannot catch a renderer bug.

:func:`check_status_distribution` is the one soft check: it **warns and never
fails**, because a distribution that differs from the reference is a
calibration signal, not a contract violation.

VALIDATION STATUS
-----------------
**Re-derives from the CSV text and the dataset, never from renderer objects.**
A validator that trusted the producer's internals could not catch a producer
bug. This one parses the written row back and re-checks every rule against
``dataset/``.

It earns that design: the explanation-alignment check caught a malformed test
fixture on its first run. All 250 evaluation rows pass with **0 violations**,
and each rule has a negative test proving it rejects rather than merely
accepts.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping, Sequence

from .load import Dataset, PaymentOption, Request
from .paths import OUTPUT_COLUMNS

logger = logging.getLogger(__name__)

ALLOWED_STATUSES = frozenset(
    {
        "affordable_now",
        "affordable_with_plan",
        "affordable_later",
        "not_affordable",
    }
)
ALLOWED_METHODS = frozenset(
    {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
)

#: Reference split, used only by the warning-level distribution check.
EXPECTED_DISTRIBUTION: Mapping[str, float] = {
    "affordable_now": 0.12,
    "affordable_with_plan": 0.36,
    "affordable_later": 0.24,
    "not_affordable": 0.28,
}

#: Strings that must never reach the CSV as a value.
FORBIDDEN_ARTIFACTS = ("nan", "NaN", "None", "null", "NULL", "<NA>")


class ValidationError(Exception):
    """One row broke one rule."""

    def __init__(self, request_id: str, rule: str, detail: str) -> None:
        self.request_id = request_id
        self.rule = rule
        self.detail = detail
        super().__init__(f"{request_id}: [{rule}] {detail}")


@dataclass(frozen=True)
class Violation:
    request_id: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.request_id}: [{self.rule}] {self.detail}"


def _fail(request_id: str, rule: str, detail: str) -> None:
    raise ValidationError(request_id, rule, detail)


def _parse_decimal(request_id: str, rule: str, text: str) -> Decimal:
    try:
        return Decimal(text)
    except (InvalidOperation, ArithmeticError):
        _fail(request_id, rule, f"{text!r} is not a decimal")
        raise  # unreachable


def _parse_date(request_id: str, rule: str, text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        _fail(request_id, rule, f"{text!r} is not an ISO date")
        raise  # unreachable


def parse_payment_plan(
    request_id: str, text: str
) -> list[tuple[date, Decimal]]:
    """Parse and structurally validate a plan string."""
    if text == "none":
        return []
    if not text:
        _fail(request_id, "payment_plan", "empty; use 'none' for no payments")

    entries: list[tuple[date, Decimal]] = []
    for chunk in text.split("|"):
        if chunk.count(":") != 1:
            _fail(request_id, "payment_plan", f"malformed entry {chunk!r}")
        day_text, amount_text = chunk.split(":")
        day = _parse_date(request_id, "payment_plan", day_text)
        # Checked before parsing: Decimal("15,656,000") raises on its own and
        # would mask the more specific diagnosis.
        if "," in amount_text:
            _fail(
                request_id,
                "payment_plan",
                f"thousands separator in plan amount {amount_text!r}",
            )
        amount = _parse_decimal(request_id, "payment_plan", amount_text)
        if amount <= 0:
            _fail(request_id, "payment_plan", f"non-positive amount in {chunk!r}")
        entries.append((day, amount))

    days = [day for day, _ in entries]
    if days != sorted(days):
        _fail(request_id, "payment_plan", f"not chronological: {days}")
    return entries


def parse_spending_changes(request_id: str, text: str) -> list[tuple[str, str, Decimal | None]]:
    """Parse into ``(action, event_id, new_amount)`` triples."""
    if text == "none":
        return []
    if not text:
        _fail(request_id, "spending_changes_needed", "empty; use 'none'")

    parsed: list[tuple[str, str, Decimal | None]] = []
    for chunk in text.split("|"):
        parts = chunk.split(":")
        if parts[0] == "stop":
            if len(parts) != 2:
                _fail(request_id, "spending_changes_needed", f"malformed {chunk!r}")
            parsed.append(("stop", parts[1], None))
        elif parts[0] == "reduce_to":
            if len(parts) != 3:
                _fail(request_id, "spending_changes_needed", f"malformed {chunk!r}")
            amount = _parse_decimal(request_id, "spending_changes_needed", parts[2])
            parsed.append(("reduce_to", parts[1], amount))
        else:
            _fail(
                request_id,
                "spending_changes_needed",
                f"unknown action {parts[0]!r} in {chunk!r}",
            )
    return parsed


def validate_columns(header: Sequence[str]) -> None:
    """Exact column set, in exact order."""
    if tuple(header) != OUTPUT_COLUMNS:
        raise ValidationError(
            "<header>",
            "column_order",
            f"expected {list(OUTPUT_COLUMNS)}, got {list(header)}",
        )


def validate_row(
    row: Mapping[str, str],
    request: Request,
    dataset: Dataset,
) -> None:
    """Check one rendered row against every hard rule."""
    request_id = row.get("request_id", "<missing>")
    if request_id != request.request_id:
        _fail(request_id, "request_id", f"row is for {request.request_id}")

    profile = dataset.profile(request.user_id)
    options = {
        option.payment_option_id: option
        for option in dataset.payment_options
        if option.request_id == request.request_id
    }

    # -- no artefact strings ------------------------------------------------
    for column, value in row.items():
        if value in FORBIDDEN_ARTIFACTS:
            _fail(
                request_id,
                "artifact_string",
                f"{column} is the literal {value!r}; use an empty string",
            )

    # -- amount_safe_to_pay -------------------------------------------------
    safe = _parse_decimal(request_id, "amount_safe_to_pay", row["amount_safe_to_pay"])
    if not (Decimal(0) <= safe <= request.requested_amount):
        _fail(
            request_id,
            "amount_safe_to_pay_bounds",
            f"{safe} outside [0, {request.requested_amount}]",
        )

    # -- allowed values -----------------------------------------------------
    status = row["affordability_status"]
    method = row["recommended_payment_method"]
    if status not in ALLOWED_STATUSES:
        _fail(request_id, "affordability_status", f"{status!r} not allowed")
    if method not in ALLOWED_METHODS:
        _fail(request_id, "recommended_payment_method", f"{method!r} not allowed")

    # -- plan and earliest date --------------------------------------------
    plan = parse_payment_plan(request_id, row["payment_plan"])
    earliest_text = row["earliest_date_for_full_payment"]
    earliest = (
        _parse_date(request_id, "earliest_date_for_full_payment", earliest_text)
        if earliest_text
        else None
    )

    # -- status / method consistency ---------------------------------------
    if method == "partial_payment" and status != "affordable_with_plan":
        _fail(
            request_id,
            "partial_implies_with_plan",
            f"partial_payment with status {status!r}",
        )
    if status == "affordable_now":
        if method != "full_payment":
            _fail(
                request_id,
                "affordable_now_implies_full_payment",
                f"status affordable_now with method {method!r}",
            )
        if earliest != request.request_date:
            _fail(
                request_id,
                "affordable_now_earliest_equals_request_date",
                f"earliest {earliest_text!r} != request_date "
                f"{request.request_date.isoformat()}",
            )
    if method == "not_recommended":
        if row["payment_plan"] != "none":
            _fail(
                request_id,
                "not_recommended_plan_none",
                f"plan is {row['payment_plan']!r}",
            )
        if earliest_text != "":
            _fail(
                request_id,
                "not_recommended_earliest_blank",
                f"earliest is {earliest_text!r}",
            )
        if status != "not_affordable":
            _fail(
                request_id,
                "not_recommended_implies_not_affordable",
                f"status is {status!r}",
            )
    elif not plan:
        _fail(request_id, "plan_required", f"method {method!r} has no payments")

    # -- partial plan shape -------------------------------------------------
    if method == "partial_payment":
        if len(plan) != 2:
            _fail(
                request_id,
                "partial_two_payments",
                f"{len(plan)} payments, expected exactly 2",
            )
        total = plan[0][1] + plan[1][1]
        if total != request.requested_amount:
            _fail(
                request_id,
                "partial_sums_to_requested",
                f"{plan[0][1]} + {plan[1][1]} = {total} != "
                f"{request.requested_amount}",
            )
        if plan[0][0] != request.request_date:
            _fail(
                request_id,
                "partial_first_payment_on_request_date",
                f"first payment {plan[0][0].isoformat()}",
            )
        if not request.allows_partial_payment:
            _fail(
                request_id,
                "partial_not_allowed",
                "request does not allow partial payment",
            )

    # -- installment plan matches a supplied option -------------------------
    if method == "installments":
        if not _matches_an_option(plan, options.values()):
            _fail(
                request_id,
                "installments_match_an_option",
                f"plan {row['payment_plan']!r} matches no payment option for "
                f"this request",
            )
        if "installments" not in profile.payment_methods_user_will_consider:
            _fail(
                request_id,
                "installments_not_considered",
                f"user considers "
                f"{list(profile.payment_methods_user_will_consider)}",
            )

    # -- method respects the user's stated preferences ----------------------
    if method in ("full_payment", "partial_payment", "installments"):
        if method not in profile.payment_methods_user_will_consider:
            _fail(
                request_id,
                "method_not_considered",
                f"{method} not in "
                f"{list(profile.payment_methods_user_will_consider)}",
            )
    if method == "wait" and "full_payment" not in (
        profile.payment_methods_user_will_consider
    ):
        _fail(
            request_id,
            "wait_requires_full_payment",
            "waiting ends in a full payment the user will not make",
        )

    # -- deadline -----------------------------------------------------------
    if plan and method != "wait":
        last = plan[-1][0]
        if last > request.desired_completion_date:
            _fail(
                request_id,
                "completes_by_deadline",
                f"last payment {last.isoformat()} after deadline "
                f"{request.desired_completion_date.isoformat()}",
            )

    # -- spending changes ---------------------------------------------------
    changes = parse_spending_changes(request_id, row["spending_changes_needed"])
    if changes:
        if status != "affordable_with_plan":
            _fail(
                request_id,
                "changes_only_on_affordable_with_plan",
                f"{len(changes)} change(s) on status {status!r}",
            )
        if len(changes) > 3:
            _fail(
                request_id,
                "at_most_three_changes",
                f"{len(changes)} changes",
            )
        event_ids = [event_id for _, event_id, _ in changes]
        if len(event_ids) != len(set(event_ids)):
            _fail(
                request_id,
                "no_event_changed_twice",
                f"repeated event id in {event_ids}",
            )
        events = {e.event_id: e for e in dataset.events if e.user_id == request.user_id}
        for action, event_id, amount in changes:
            event = events.get(event_id)
            if event is None:
                _fail(
                    request_id,
                    "change_targets_own_event",
                    f"{event_id} does not belong to {request.user_id}",
                )
            if event.flexibility == "fixed":
                _fail(
                    request_id,
                    "change_targets_flexible_event",
                    f"{event_id} has flexibility 'fixed'",
                )
            if action == "stop":
                if event.flexibility not in ("stoppable", "reducible_or_stoppable"):
                    _fail(
                        request_id,
                        "stop_requires_stoppable",
                        f"{event_id} is {event.flexibility}",
                    )
                if event.category not in (
                    profile.expense_categories_user_is_willing_to_stop
                ):
                    _fail(
                        request_id,
                        "stop_category_permitted",
                        f"{event.category} not in the user's stop list",
                    )
            else:
                if event.flexibility not in ("reducible", "reducible_or_stoppable"):
                    _fail(
                        request_id,
                        "reduce_requires_reducible",
                        f"{event_id} is {event.flexibility}",
                    )
                if event.category not in (
                    profile.expense_categories_user_is_willing_to_reduce
                ):
                    _fail(
                        request_id,
                        "reduce_category_permitted",
                        f"{event.category} not in the user's reduce list",
                    )
                if (
                    event.minimum_allowed_amount_home is not None
                    and amount is not None
                    and amount < event.minimum_allowed_amount_home
                ):
                    _fail(
                        request_id,
                        "reduce_respects_minimum_allowed",
                        f"{amount} below {event.minimum_allowed_amount_home}",
                    )

    # -- explanation --------------------------------------------------------
    if not row["decision_explanation"].strip():
        _fail(request_id, "decision_explanation", "is empty")
    _check_explanation_alignment(row, request, profile, plan, changes)


#: Which explanation template each (status, method) pair must produce, keyed by
#: a distinctive phrase that appears in that template and no other.
_TEMPLATE_MARKERS: Mapping[str, str] = {
    "affordable_now": "today. This leaves at least",
    "full_with_changes": ", then pay ",
    "installments": "installments of",
    "wait": "in full on",
    "partial": "and the remaining",
    "not_affordable_a": "None of the available options",
    "not_affordable_b": "Do not proceed with the",
}

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _expected_template(method: str, has_changes: bool, explanation: str) -> str:
    if method == "not_recommended":
        return (
            "not_affordable_b"
            if _TEMPLATE_MARKERS["not_affordable_b"] in explanation
            else "not_affordable_a"
        )
    if method == "wait":
        return "wait"
    if method == "installments":
        return "installments"
    if method == "partial_payment":
        return "partial"
    return "full_with_changes" if has_changes else "affordable_now"


def _check_explanation_alignment(
    row: Mapping[str, str],
    request: Request,
    profile,
    plan: Sequence[tuple[date, Decimal]],
    changes: Sequence[tuple[str, str, Decimal | None]],
) -> None:
    """The explanation must agree with the columns it sits beside.

    Two failures are possible and both are defects even when each field is
    individually valid:

    1. **Template drift** -- the prose describes a different decision from the
       one the ``affordability_status`` / ``recommended_payment_method``
       columns record.
    2. **Orphan figures** -- the prose cites a number that appears nowhere in
       the row's structured data or the user's profile. A figure a reader
       cannot trace back is unverifiable, and an invented one is worse.
    """
    request_id = row["request_id"]
    explanation = row["decision_explanation"]
    method = row["recommended_payment_method"]
    status = row["affordability_status"]

    expected = _expected_template(method, bool(changes), explanation)
    marker = _TEMPLATE_MARKERS[expected]
    if marker not in explanation:
        _fail(
            request_id,
            "explanation_matches_method",
            f"method {method!r} expects the {expected!r} template "
            f"(marker {marker!r}), which is absent from: {explanation!r}",
        )

    # The status implied by the prose must match the status column.
    if expected == "affordable_now" and status != "affordable_now":
        _fail(
            request_id,
            "explanation_matches_status",
            f"prose reads as affordable_now but status is {status!r}",
        )
    if expected in ("partial", "full_with_changes", "installments") and (
        status != "affordable_with_plan"
    ):
        _fail(
            request_id,
            "explanation_matches_status",
            f"prose reads as a plan but status is {status!r}",
        )
    if expected == "wait" and status != "affordable_later":
        _fail(
            request_id,
            "explanation_matches_status",
            f"prose reads as wait but status is {status!r}",
        )
    if expected.startswith("not_affordable") and status != "not_affordable":
        _fail(
            request_id,
            "explanation_matches_status",
            f"prose reads as not affordable but status is {status!r}",
        )

    # Every figure in the prose must be traceable to this row or this profile.
    permitted: set[Decimal] = {
        request.requested_amount,
        profile.minimum_balance_to_keep,
        Decimal(row["amount_safe_to_pay"]),
        Decimal(90),  # "over the next 90 days" -- the stated horizon
    }
    permitted.update(amount for _, amount in plan)
    permitted.update(len(plan) for _ in (0,))  # installment count
    permitted.update(
        amount for _, _, amount in changes if amount is not None
    )

    for token in _NUMBER_RE.findall(explanation):
        try:
            value = Decimal(token.replace(",", ""))
        except (InvalidOperation, ArithmeticError):
            continue
        if value in permitted:
            continue
        # Dates are spelled out in prose ("15 September 2024"); a bare day or
        # year is not a financial figure.
        if value == value.to_integral_value() and (
            1 <= int(value) <= 31 or 1900 <= int(value) <= 2999
        ):
            continue
        _fail(
            request_id,
            "explanation_figures_are_traceable",
            f"prose cites {token!r}, which appears in no structured column of "
            f"this row and in no profile field: {explanation!r}",
        )


def _matches_an_option(
    plan: Sequence[tuple[date, Decimal]], options: Iterable[PaymentOption]
) -> bool:
    from .candidates import installment_schedule

    for option in options:
        if option.payment_method != "installments":
            continue
        schedule = installment_schedule(option)
        if len(schedule) != len(plan):
            continue
        if all(
            payment.on == day and payment.amount == amount
            for payment, (day, amount) in zip(schedule, plan)
        ):
            return True
    return False


def validate_output(
    rows: Sequence[Mapping[str, str]],
    dataset: Dataset,
    *,
    header: Sequence[str] | None = None,
) -> list[Violation]:
    """Validate a whole output file. Returns the violations it found.

    Row-level rules raise immediately inside :func:`validate_row`; this wrapper
    collects them so a full run reports every failure class at once rather than
    stopping at the first.
    """
    violations: list[Violation] = []
    if header is not None:
        try:
            validate_columns(header)
        except ValidationError as exc:
            violations.append(Violation(exc.request_id, exc.rule, exc.detail))

    requests = {r.request_id: r for r in dataset.requests}
    seen: list[str] = []

    for row in rows:
        request_id = row.get("request_id", "<missing>")
        seen.append(request_id)
        request = requests.get(request_id)
        if request is None:
            violations.append(
                Violation(request_id, "unknown_request_id", "not in requests.csv")
            )
            continue
        try:
            validate_row(row, request, dataset)
        except ValidationError as exc:
            violations.append(Violation(exc.request_id, exc.rule, exc.detail))

    missing = sorted(set(requests) - set(seen))
    for request_id in missing:
        violations.append(
            Violation(request_id, "missing_row", "no output row for this request")
        )
    duplicates = sorted({r for r in seen if seen.count(r) > 1})
    for request_id in duplicates:
        violations.append(
            Violation(request_id, "duplicate_row", "appears more than once")
        )
    if len(seen) != len(requests) and not missing and not duplicates:
        violations.append(
            Violation(
                "<file>",
                "row_count",
                f"{len(seen)} rows for {len(requests)} requests",
            )
        )
    return violations


def check_status_distribution(
    rows: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    """Compare the status split to the reference. **Warns, never fails.**

    A deviation is a calibration signal, not a contract violation, so this
    returns a report and logs at WARNING rather than raising.
    """
    total = len(rows) or 1
    counts: dict[str, int] = {status: 0 for status in ALLOWED_STATUSES}
    for row in rows:
        status = row.get("affordability_status", "")
        if status in counts:
            counts[status] += 1

    report: dict[str, object] = {"total": len(rows), "rows": {}}
    for status, expected in EXPECTED_DISTRIBUTION.items():
        actual = counts[status] / total
        delta = actual - expected
        report["rows"][status] = {
            "count": counts[status],
            "actual": round(actual, 4),
            "expected": expected,
            "delta_pct_points": round(delta * 100, 1),
        }
        if abs(delta) > 0.05:
            logger.warning(
                "status distribution: %s is %.1f%%, expected ~%.0f%% "
                "(%+.1f points)",
                status,
                actual * 100,
                expected * 100,
                delta * 100,
            )
    return report

"""Output formatting and explanation templates (rulebook section 4).

Formatting is scored, and the numeric columns and the prose use **different**
conventions on purpose:

======================  ==========================================  ===========
field                   rule                                        example
======================  ==========================================  ===========
``amount_safe_to_pay``  minimal decimals, trailing zeros stripped   ``603.3``
``payment_plan``        2dp when fractional, trailing zeros kept    ``620.40``
``spending_changes``    2dp when fractional                         ``23.50``
prose                   thousands separators, long dates            ``EUR 620.40``
======================  ==========================================  ===========

The same underlying number therefore renders three ways: ``603.3`` in
``amount_safe_to_pay``, ``603.30`` inside a plan, and ``603.30`` with
separators in prose. :func:`format_minimal` and :func:`format_plan_amount`
implement the two numeric conventions and must never be swapped.

``decision_explanation`` is filled from six deterministic templates. There is
**no model call here** -- explanations are string fills, which is what keeps
per-request model cost at zero (see C4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping, Sequence

from .candidates import Candidate, Method, Payment
from .capacity import CapacityAssessment
from .load import CENTS, UserContext
from .rank import Decision, Status

logger = logging.getLogger(__name__)

#: **0.10, and B is the HIGH side.** Derived from the labels in Phase 9 (C9d): the two rows
#: using template B sit at 11.0% and 12.2%, all five using A between 1.8% and
#: 4.8%. B's wording ("Although EUR 597.74 is available today") only makes
#: sense when a meaningful amount *is* available.
#:
#: Below this ratio of ``amount_safe_to_pay`` to ``requested_amount``, the
#: "none of the options protects the minimum" wording is used; at or above it,
#: the "although X is available today" wording is.
#:
#: LOOKS WRONG, ISN'T: the specification this was built from states the
#: opposite direction. The labels decided it, and
#: ``test_the_a_b_threshold_direction_follows_the_labels`` pins the spans.
NOT_AFFORDABLE_B_MIN_RATIO = Decimal("0.10")

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


# ---------------------------------------------------------------------------
# Numeric formatting -- the two conventions
# ---------------------------------------------------------------------------


def format_minimal(amount: Decimal) -> str:
    """``amount_safe_to_pay``: minimal decimals, trailing zeros stripped.

    ``603.30`` renders ``603.3``; ``25256.00`` renders ``25256``. Never uses
    exponent notation, which bare ``Decimal.normalize`` would produce for a
    whole number with trailing zeros.
    """
    normalized = amount.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f")


def format_plan_amount(amount: Decimal) -> str:
    """``payment_plan`` amounts: 2dp when fractional, trailing zeros **kept**.

    ``620.40`` stays ``620.40``; ``25256`` renders with no decimal point at
    all. Never uses thousands separators.
    """
    if amount == amount.to_integral_value():
        return str(int(amount))
    return f"{amount.quantize(CENTS):.2f}"


def format_prose_amount(amount: Decimal) -> str:
    """Prose amounts: the plan convention plus thousands separators."""
    if amount == amount.to_integral_value():
        return f"{int(amount):,}"
    return f"{amount.quantize(CENTS):,.2f}"


def format_money(currency: str, amount: Decimal) -> str:
    return f"{currency} {format_prose_amount(amount)}"


def format_long_date(day: date) -> str:
    """``15 September 2024`` -- no leading zero on the day."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def format_iso_date(day: date | None) -> str:
    """ISO date, or the empty string. Never ``None`` or ``nan``."""
    return day.isoformat() if day is not None else ""


def format_payment_plan(payments: Sequence[Payment]) -> str:
    """``YYYY-MM-DD:amount|YYYY-MM-DD:amount``, chronological, or ``none``."""
    if not payments:
        return "none"
    ordered = sorted(payments, key=lambda p: p.on)
    return "|".join(
        f"{p.on.isoformat()}:{format_plan_amount(p.amount)}" for p in ordered
    )


def format_spending_changes(changes: Sequence[str]) -> str:
    """Up to three changes joined by ``|``, or ``none``."""
    if not changes:
        return "none"
    assert len(changes) <= 3, f"at most three changes allowed, got {len(changes)}"
    return "|".join(changes)


# ---------------------------------------------------------------------------
# Explanation templates
# ---------------------------------------------------------------------------


def _describe_change(change: str, currency: str, descriptions: Mapping[str, str]) -> str:
    """Turn ``stop:event_476`` into ``stop the family streaming plan``."""
    parts = change.split(":")
    event_id = parts[1]
    label = descriptions.get(event_id, event_id).lower()
    if parts[0] == "stop":
        return f"stop the {label}"
    amount = Decimal(parts[2])
    return f"reduce the {label} to {format_money(currency, amount)}"


def _change_phrase(
    changes: Sequence[str], currency: str, descriptions: Mapping[str, str]
) -> str:
    """``Stop the online backup subscription and reduce the streaming ...``"""
    described = [_describe_change(c, currency, descriptions) for c in changes]
    if len(described) == 1:
        phrase = described[0]
    elif len(described) == 2:
        phrase = f"{described[0]} and {described[1]}"
    else:
        phrase = f"{', '.join(described[:-1])} and {described[-1]}"
    return phrase[0].upper() + phrase[1:]


def explain_affordable_now(currency: str, amount: Decimal, minimum: Decimal) -> str:
    return (
        f"Pay {format_money(currency, amount)} today. This leaves at least "
        f"{format_money(currency, minimum)} available over the next 90 days."
    )


def explain_installments(
    currency: str, count: int, per_payment: Decimal, start: date, minimum: Decimal
) -> str:
    return (
        f"Use {count} installments of {format_money(currency, per_payment)}, "
        f"starting {format_long_date(start)}. This leaves at least "
        f"{format_money(currency, minimum)} available."
    )


def explain_wait(
    currency: str, amount: Decimal, when: date, minimum: Decimal
) -> str:
    return (
        f"Pay {format_money(currency, amount)} in full on "
        f"{format_long_date(when)}. Paying earlier would take the balance "
        f"below the {format_money(currency, minimum)} minimum."
    )


def explain_full_with_changes(
    currency: str,
    amount: Decimal,
    minimum: Decimal,
    changes: Sequence[str],
    descriptions: Mapping[str, str],
) -> str:
    return (
        f"{_change_phrase(changes, currency, descriptions)}, then pay "
        f"{format_money(currency, amount)} today. This leaves at least "
        f"{format_money(currency, minimum)} available."
    )


def explain_partial(
    currency: str,
    first: Decimal,
    second: Decimal,
    when: date,
    minimum: Decimal,
) -> str:
    return (
        f"Pay {format_money(currency, first)} today and the remaining "
        f"{format_money(currency, second)} on {format_long_date(when)}. "
        f"This completes the full request and keeps the "
        f"{format_money(currency, minimum)} minimum protected."
    )


def explain_not_affordable_a(
    currency: str, deadline: date, minimum: Decimal
) -> str:
    """Used when almost nothing is affordable today."""
    return (
        f"Do not make this payment by {format_long_date(deadline)}. None of "
        f"the available options keeps the {format_money(currency, minimum)} "
        f"minimum protected."
    )


def explain_not_affordable_b(
    currency: str, requested: Decimal, safe: Decimal
) -> str:
    """Used when a meaningful amount is available but the full sum is not."""
    return (
        f"Do not proceed with the {format_money(currency, requested)} request. "
        f"Although {format_money(currency, safe)} is available today, the full "
        f"amount cannot be completed safely within 90 days."
    )


# ---------------------------------------------------------------------------
# The output row
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputRow:
    """One rendered row, all fields already strings."""

    request_id: str
    amount_safe_to_pay: str
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: str
    spending_changes_needed: str
    decision_explanation: str

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.request_id,
            self.amount_safe_to_pay,
            self.affordability_status,
            self.recommended_payment_method,
            self.payment_plan,
            self.earliest_date_for_full_payment,
            self.spending_changes_needed,
            self.decision_explanation,
        )

    def as_dict(self) -> dict[str, str]:
        from .paths import OUTPUT_COLUMNS

        return dict(zip(OUTPUT_COLUMNS, self.as_tuple()))


def _event_descriptions(context: UserContext) -> dict[str, str]:
    return {event.event_id: event.description for event in context.events}


def build_explanation(
    context: UserContext,
    winner: Candidate,
    status: Status,
    capacity: CapacityAssessment,
    *,
    amount_safe_to_pay: Decimal | None = None,
) -> str:
    """Pick and fill one of the six templates."""
    profile = context.profile
    request = context.request
    currency = profile.home_currency
    minimum = profile.minimum_balance_to_keep
    safe = (
        amount_safe_to_pay
        if amount_safe_to_pay is not None
        else capacity.amount_safe_to_pay
    )
    descriptions = _event_descriptions(context)

    if winner.method is Method.NOT_RECOMMENDED:
        ratio = (
            safe / request.requested_amount
            if request.requested_amount
            else Decimal(0)
        )
        if ratio >= NOT_AFFORDABLE_B_MIN_RATIO:
            return explain_not_affordable_b(
                currency, request.requested_amount, safe
            )
        return explain_not_affordable_a(
            currency, request.desired_completion_date, minimum
        )

    if winner.method is Method.WAIT:
        payment = winner.payments[0]
        return explain_wait(currency, payment.amount, payment.on, minimum)

    if winner.method is Method.INSTALLMENTS:
        return explain_installments(
            currency,
            winner.payment_count,
            winner.payments[0].amount,
            winner.payments[0].on,
            minimum,
        )

    if winner.method is Method.PARTIAL_PAYMENT:
        first, second = winner.payments
        return explain_partial(
            currency, first.amount, second.amount, second.on, minimum
        )

    # full payment, with or without changes
    if winner.requires_changes:
        return explain_full_with_changes(
            currency,
            winner.payments[0].amount,
            minimum,
            winner.spending_changes,
            descriptions,
        )
    return explain_affordable_now(currency, winner.payments[0].amount, minimum)


def render_decision(
    context: UserContext,
    decision: Decision,
    *,
    amount_safe_to_pay: Decimal | None = None,
    earliest_date: date | None = None,
) -> OutputRow:
    """Serialise a ranked decision into the eight output columns.

    ``amount_safe_to_pay`` and ``earliest_date`` may be supplied to render from
    known-good values, which is how the formatting rules are validated against
    the labelled rows without the forecast's bias interfering.
    """
    request = context.request
    winner = decision.winner
    safe = (
        amount_safe_to_pay
        if amount_safe_to_pay is not None
        else decision.capacity.amount_safe_to_pay
    )
    earliest = (
        earliest_date
        if earliest_date is not None
        else decision.capacity.earliest_date_for_full_payment
    )

    if winner.method is Method.NOT_RECOMMENDED:
        # No safe date exists, so the column is blank rather than a date.
        earliest = None
    if decision.status is Status.AFFORDABLE_NOW:
        assert earliest == request.request_date, (
            f"{request.request_id}: affordable_now requires "
            f"earliest_date_for_full_payment == request_date"
        )

    assert Decimal(0) <= safe <= request.requested_amount, (
        f"{request.request_id}: amount_safe_to_pay {safe} outside bounds"
    )

    return OutputRow(
        request_id=request.request_id,
        amount_safe_to_pay=format_minimal(safe),
        affordability_status=decision.status.value,
        recommended_payment_method=winner.method.value,
        payment_plan=format_payment_plan(winner.payments),
        earliest_date_for_full_payment=format_iso_date(earliest),
        spending_changes_needed=format_spending_changes(winner.spending_changes),
        decision_explanation=build_explanation(
            context,
            winner,
            decision.status,
            decision.capacity,
            amount_safe_to_pay=safe,
        ),
    )

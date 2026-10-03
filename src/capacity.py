"""Spending capacity (rulebook Step 4).

Two questions, both answered exactly against the forecast and both
computed **without** any optional spending change. One is closed-form;
the other is an exact scan. The distinction is kept sharp below because
it is easy to claim more than is true here:

:func:`amount_safe_to_pay`
    The largest amount payable on ``request_date`` that keeps every trough at
    or after that date at or above ``minimum_balance_to_keep``, capped at
    ``requested_amount``.

:func:`earliest_date_for_full_payment`
    The first date in the horizon on which paying the full requested amount
    leaves every later trough above the floor. ``None`` when no such date
    exists inside the window.

**Why the amount needs no search.** A payment of *P* made on day *d* lowers the
projected balance on *every* day from *d* onward by exactly *P* -- it shifts
the whole tail down, it does not reshape it. So the tail stays above the floor
precisely when

    min(balance over [d, end]) - P >= minimum_balance_to_keep

and the largest admissible *P* is the headroom at the lowest trough in that
tail. Both answers fall out of the suffix minimum, which
:meth:`~src.forecast.Forecast.minimum_on_or_after` already computes.

**Why the date is a scan, and why that is still exact.** The suffix minimum
is monotone non-decreasing in *d* -- as *d* advances the window shrinks, so
its minimum can only rise. :func:`earliest_date_for_full_payment` therefore
walks *d* forward one day at a time over at most 91 days and returns the
first date that clears the floor. That is a search, not a closed form: there
is no expression for the date the way there is for the amount. What
monotonicity buys is that the scan never has to backtrack and never has to
compare candidates -- once the predicate turns true it stays true, so the
first success is provably the earliest date, and stopping there is not an
approximation. A binary search would give the same answer in O(log n); the
linear walk is kept because 91 iterations is not worth the subtlety.

:func:`amount_safe_to_pay` is a **diagnostic reported on every output row**,
including ``wait`` and ``not_recommended`` ones. It measures capacity, not the
recommendation, and is never zeroed out to match a chosen method.

VALIDATION STATUS
-----------------
**Provably tight given its input -- but only one answer is closed-form.**
:func:`amount_safe_to_pay` is closed-form: one subtraction off the suffix
minimum, no iteration at all, and
``test_a_late_binding_trough_sets_the_amount`` shows the computed amount is
safe and one cent more is not.
:func:`earliest_date_for_full_payment` is **not** closed-form -- it is an
exact forward scan over the horizon, correct because the suffix minimum is
monotone in the start date (see above), so it terminates at the true
earliest date rather than an approximation of it. Exact, but searched. The
function's own docstring has always said ``Scans forward``; this header
used to say both were closed-form, which was an overstatement.

**Do not tune this module.** Against the 25 labels ``amount_safe_to_pay`` is
exact on 2 and over-states on 16, but every one of those misses is an error in
:mod:`src.forecast`, which hands this module its input. Adjusting anything here
would hide the forecast defect rather than fix it. Per-request attribution is in
``audit/capacity_trace.jsonl``; the diagnosis runs P11-S1 through P11-S4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from .forecast import Forecast, Trough
from .load import CENTS, UserContext

logger = logging.getLogger(__name__)


def floor_to_precision(amount: Decimal) -> Decimal:
    """Round **down** to the dataset's two-decimal precision.

    Down, not half-up: rounding a safe amount upward could push the balance
    a fraction below the floor the figure was computed to respect.
    """
    return amount.quantize(CENTS, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class CapacityAssessment:
    """Both capacity answers plus the evidence behind them.

    The binding trough and its headroom are carried so Phase 11 can tell a
    miss caused by the *wrong trough* binding from one caused by the *right
    trough carrying a wrong balance*.
    """

    request_id: str
    user_id: str
    as_of: date
    requested_amount: Decimal
    minimum_balance_to_keep: Decimal

    amount_safe_to_pay: Decimal
    #: Headroom before the ``requested_amount`` cap is applied.
    uncapped_capacity: Decimal
    capped_by_request: bool
    binding_trough: Trough | None

    earliest_date_for_full_payment: date | None
    earliest_binding_trough: Trough | None
    #: How much more capacity the full amount would have needed, when the
    #: horizon never supplies enough.
    shortfall: Decimal | None

    @property
    def full_amount_safe_today(self) -> bool:
        return self.amount_safe_to_pay == self.requested_amount

    def trace(self) -> dict[str, object]:
        """Per-request trace line for the Phase 11 calibration sweep."""
        binding = self.binding_trough
        earliest = self.earliest_binding_trough
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "as_of": self.as_of.isoformat(),
            "requested_amount": str(self.requested_amount),
            "minimum_balance_to_keep": str(self.minimum_balance_to_keep),
            "amount_safe_to_pay": str(self.amount_safe_to_pay),
            "uncapped_capacity": str(self.uncapped_capacity),
            "capped_by_request": self.capped_by_request,
            "binding_trough_date": binding.on.isoformat() if binding else None,
            "binding_trough_balance": str(binding.balance) if binding else None,
            "binding_trough_headroom": (
                str(binding.balance - self.minimum_balance_to_keep)
                if binding
                else None
            ),
            "earliest_date_for_full_payment": (
                self.earliest_date_for_full_payment.isoformat()
                if self.earliest_date_for_full_payment
                else None
            ),
            "earliest_binding_trough_date": (
                earliest.on.isoformat() if earliest else None
            ),
            "earliest_binding_trough_balance": (
                str(earliest.balance) if earliest else None
            ),
            "shortfall": str(self.shortfall) if self.shortfall is not None else None,
        }


def amount_safe_to_pay(context: UserContext, forecast: Forecast) -> Decimal:
    """Largest amount payable on ``request_date``, capped at the request.

    Exact, not searched: a payment today shifts every later trough down by the
    same amount, so the answer is the headroom at the lowest trough across the
    whole horizon.
    """
    floor = forecast.minimum_balance_to_keep
    requested = context.request.requested_amount

    # Payment lands on request_date, so every day in the window is "at or
    # after" it and the binding constraint is the suffix minimum from the start.
    lowest = forecast.minimum_on_or_after(forecast.start)
    capacity = lowest - floor

    safe = floor_to_precision(max(Decimal(0), min(capacity, requested)))

    # The contract the validator re-checks, asserted at the source.
    assert Decimal(0) <= safe <= requested, (
        f"{context.request_id}: amount_safe_to_pay {safe} outside "
        f"[0, {requested}]"
    )
    return safe


def earliest_date_for_full_payment(
    context: UserContext, forecast: Forecast
) -> date | None:
    """First date the full requested amount clears the safety check.

    Scans forward from ``request_date`` to the end of the horizon. Because the
    suffix minimum only rises as the start date advances, the first date that
    passes is the earliest one that can.

    This measures **financial capacity alone**. It deliberately ignores which
    payment methods the user will consider, so it may equal ``request_date``
    even when the recommendation ends up being installments because the user
    will not consider paying in full.
    """
    requested = context.request.requested_amount
    floor = forecast.minimum_balance_to_keep
    needed = floor + requested

    day_count = (forecast.end - forecast.start).days
    for offset in range(day_count + 1):
        day = forecast.start + timedelta(days=offset)
        if forecast.minimum_on_or_after(day) >= needed:
            return day
    return None


def assess_capacity(context: UserContext, forecast: Forecast) -> CapacityAssessment:
    """Compute both answers and trace the binding trough."""
    floor = forecast.minimum_balance_to_keep
    requested = context.request.requested_amount

    lowest = forecast.minimum_on_or_after(forecast.start)
    uncapped = floor_to_precision(max(Decimal(0), lowest - floor))
    safe = amount_safe_to_pay(context, forecast)
    binding = forecast.binding_trough(forecast.start)

    earliest = earliest_date_for_full_payment(context, forecast)
    earliest_trough = forecast.binding_trough(earliest) if earliest else None

    shortfall: Decimal | None = None
    if earliest is None:
        # The best the horizon ever offers, against what the full amount needs.
        best = max(
            forecast.minimum_on_or_after(forecast.start + timedelta(days=offset))
            for offset in range((forecast.end - forecast.start).days + 1)
        )
        shortfall = floor_to_precision(max(Decimal(0), (floor + requested) - best))

    assessment = CapacityAssessment(
        request_id=context.request_id,
        user_id=context.user_id,
        as_of=forecast.start,
        requested_amount=requested,
        minimum_balance_to_keep=floor,
        amount_safe_to_pay=safe,
        uncapped_capacity=uncapped,
        capped_by_request=uncapped > requested,
        binding_trough=binding,
        earliest_date_for_full_payment=earliest,
        earliest_binding_trough=earliest_trough,
        shortfall=shortfall,
    )

    logger.info("capacity %s", assessment.trace())
    return assessment

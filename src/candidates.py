"""Candidate plan generation and eligibility (rulebook Step 5).

Enumerates every plan worth considering for a request -- pay in full today,
pay in full today after permitted spending changes, the two-payment partial
schedule, each supplied installment option, wait, and the
``not_recommended`` fallback -- then filters to the ones that are both
*eligible* under the user's stated preferences and *safe* against the
forecast.

Two rules shape everything here.

**Safety is cumulative, not per-payment.** Each scheduled payment lowers every
trough at or after its own date, so a plan is evaluated whole. Three
installments that are each comfortably affordable in isolation can still walk
the balance under the floor together, and only the combined walk reveals it.

**Deadline compliance is a hard filter, never a ranking criterion.** A plan
that finishes after ``desired_completion_date`` is not a worse plan; it is not
a plan. Ranking (:mod:`src.rank`) never sees it.

This module is deliberately independent of forecast accuracy. The forecast
currently under-projects outflow (see C3e in the calibration log), and nothing
here compensates for that -- eligibility structure and numeric bias are
separate problems, and mixing them would hide the second one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Callable, Protocol, Sequence

from . import config
from .capacity import CapacityAssessment
from .forecast import Forecast, payment_is_safe
from .load import PaymentOption, UserContext, id_sort_key

logger = logging.getLogger(__name__)


class Method(StrEnum):
    """The five values ``recommended_payment_method`` may take."""

    FULL_PAYMENT = "full_payment"
    PARTIAL_PAYMENT = "partial_payment"
    INSTALLMENTS = "installments"
    WAIT = "wait"
    NOT_RECOMMENDED = "not_recommended"


class RejectionReason(StrEnum):
    """Why a candidate was discarded. Recorded for every rejection."""

    METHOD_NOT_CONSIDERED = "method_not_in_payment_methods_user_will_consider"
    PARTIAL_NOT_ALLOWED = "request_does_not_allow_partial_payment"
    FULL_AMOUNT_NOT_SAFE_TODAY = "amount_safe_to_pay_is_below_requested_amount"
    FULL_AMOUNT_ALREADY_SAFE_TODAY = "full_amount_is_already_safe_today"
    NO_CAPACITY_TODAY = "amount_safe_to_pay_is_zero"
    NO_EARLIEST_DATE = "no_date_in_the_horizon_makes_the_full_amount_safe"
    PAST_DEADLINE = "completes_after_desired_completion_date"
    EXCEEDS_INSTALLMENT_CAP = "payment_count_exceeds_max_installment_months"
    UNSAFE_AGAINST_FORECAST = "schedule_takes_a_trough_below_minimum_balance"
    NO_SPENDING_CHANGES_AVAILABLE = "no_permitted_spending_change_makes_it_safe"
    SPENDING_CHANGES_NOT_IMPLEMENTED = "spending_change_search_not_yet_available"


@dataclass(frozen=True)
class Payment:
    """One dated payment in a candidate plan."""

    on: date
    amount: Decimal


@dataclass(frozen=True)
class Candidate:
    """One eligible, safe plan."""

    method: Method
    payments: tuple[Payment, ...]
    total_paid: Decimal
    requires_changes: bool
    payment_option_id: str | None = None
    spending_changes: tuple[str, ...] = ()
    note: str = ""

    @property
    def first_payment_date(self) -> date | None:
        return self.payments[0].on if self.payments else None

    @property
    def last_payment_date(self) -> date | None:
        return self.payments[-1].on if self.payments else None

    @property
    def payment_count(self) -> int:
        return len(self.payments)

    def schedule(self) -> tuple[tuple[date, Decimal], ...]:
        return tuple((payment.on, payment.amount) for payment in self.payments)


@dataclass(frozen=True)
class Rejection:
    """One discarded candidate and why."""

    method: Method
    reason: RejectionReason
    detail: str
    payment_option_id: str | None = None

    def __str__(self) -> str:
        option = f" [{self.payment_option_id}]" if self.payment_option_id else ""
        return f"{self.method}{option}: {self.reason} -- {self.detail}"


@dataclass(frozen=True)
class CandidateSet:
    """Everything generation produced for one request."""

    request_id: str
    user_id: str
    candidates: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]

    @property
    def eligible(self) -> tuple[Candidate, ...]:
        """Real plans, excluding the always-present fallback."""
        return tuple(
            c for c in self.candidates if c.method is not Method.NOT_RECOMMENDED
        )

    @property
    def fallback(self) -> Candidate:
        return next(
            c for c in self.candidates if c.method is Method.NOT_RECOMMENDED
        )

    def by_method(self, method: Method) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.method is method)

    def rejection_reasons(self) -> tuple[RejectionReason, ...]:
        return tuple(r.reason for r in self.rejections)

    def trace(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "eligible": [
                {
                    "method": c.method.value,
                    "payment_option_id": c.payment_option_id,
                    "payments": [
                        [p.on.isoformat(), str(p.amount)] for p in c.payments
                    ],
                    "total_paid": str(c.total_paid),
                    "requires_changes": c.requires_changes,
                }
                for c in self.eligible
            ],
            "rejections": [
                {
                    "method": r.method.value,
                    "payment_option_id": r.payment_option_id,
                    "reason": r.reason.value,
                    "detail": r.detail,
                }
                for r in self.rejections
            ],
        }


class SpendingChangeFinder(Protocol):
    """Hook for :mod:`src.changes` (Phase 7).

    Given the forecast and a payment schedule that is currently unsafe, return
    the permitted ``stop:``/``reduce_to:`` actions that make it safe, or an
    empty tuple when none do.
    """

    def __call__(
        self,
        context: UserContext,
        forecast: Forecast,
        payments: Sequence[tuple[date, Decimal]],
    ) -> tuple[str, ...]:
        ...


def installment_schedule(option: PaymentOption) -> tuple[Payment, ...]:
    """Dates and amounts for an installment option, copied verbatim.

    ``payment_amount`` is taken from the option exactly as supplied; dates are
    ``first_payment_date + k * payment_frequency_days``. Nothing is recomputed
    or re-derived, because an installment plan must match its option exactly.
    """
    interval = option.payment_frequency_days or 0
    return tuple(
        Payment(
            on=option.first_payment_date + timedelta(days=index * interval),
            amount=option.payment_amount,
        )
        for index in range(option.number_of_payments)
    )


def _full_payment_candidates(
    context: UserContext,
    forecast: Forecast,
    capacity: CapacityAssessment,
    change_finder: SpendingChangeFinder | None,
    candidates: list[Candidate],
    rejections: list[Rejection],
) -> None:
    request = context.request
    profile = context.profile
    requested = request.requested_amount

    if not profile.accepts("full_payment"):
        rejections.append(
            Rejection(
                method=Method.FULL_PAYMENT,
                reason=RejectionReason.METHOD_NOT_CONSIDERED,
                detail=(
                    f"user considers "
                    f"{list(profile.payment_methods_user_will_consider)}"
                ),
            )
        )
        return

    if capacity.amount_safe_to_pay >= requested:
        candidates.append(
            Candidate(
                method=Method.FULL_PAYMENT,
                payments=(Payment(on=request.request_date, amount=requested),),
                total_paid=requested,
                requires_changes=False,
                note="full amount is safe on the request date",
            )
        )
        return

    rejections.append(
        Rejection(
            method=Method.FULL_PAYMENT,
            reason=RejectionReason.FULL_AMOUNT_NOT_SAFE_TODAY,
            detail=(
                f"amount_safe_to_pay {capacity.amount_safe_to_pay} < "
                f"requested {requested}"
            ),
        )
    )

    # Full payment today, made safe by permitted spending changes.
    schedule = [(request.request_date, requested)]
    if change_finder is None:
        rejections.append(
            Rejection(
                method=Method.FULL_PAYMENT,
                reason=RejectionReason.SPENDING_CHANGES_NOT_IMPLEMENTED,
                detail="changes.py is not wired in yet; hook is exposed",
            )
        )
        return

    changes = change_finder(context, forecast, schedule)
    if not changes:
        rejections.append(
            Rejection(
                method=Method.FULL_PAYMENT,
                reason=RejectionReason.NO_SPENDING_CHANGES_AVAILABLE,
                detail="no permitted stop/reduce makes the full amount safe today",
            )
        )
        return

    candidates.append(
        Candidate(
            method=Method.FULL_PAYMENT,
            payments=(Payment(on=request.request_date, amount=requested),),
            total_paid=requested,
            requires_changes=True,
            spending_changes=changes,
            note="full amount becomes safe after permitted spending changes",
        )
    )


def _partial_payment_candidate(
    context: UserContext,
    capacity: CapacityAssessment,
    candidates: list[Candidate],
    rejections: list[Rejection],
) -> None:
    request = context.request
    profile = context.profile
    requested = request.requested_amount
    safe = capacity.amount_safe_to_pay
    earliest = capacity.earliest_date_for_full_payment

    if not request.allows_partial_payment:
        rejections.append(
            Rejection(
                method=Method.PARTIAL_PAYMENT,
                reason=RejectionReason.PARTIAL_NOT_ALLOWED,
                detail="allows_partial_payment is false for this request",
            )
        )
        return
    if not profile.accepts("partial_payment"):
        rejections.append(
            Rejection(
                method=Method.PARTIAL_PAYMENT,
                reason=RejectionReason.METHOD_NOT_CONSIDERED,
                detail=(
                    f"user considers "
                    f"{list(profile.payment_methods_user_will_consider)}"
                ),
            )
        )
        return
    if safe <= 0:
        rejections.append(
            Rejection(
                method=Method.PARTIAL_PAYMENT,
                reason=RejectionReason.NO_CAPACITY_TODAY,
                detail="amount_safe_to_pay is zero, so there is no first payment",
            )
        )
        return
    if safe >= requested:
        rejections.append(
            Rejection(
                method=Method.PARTIAL_PAYMENT,
                reason=RejectionReason.FULL_AMOUNT_ALREADY_SAFE_TODAY,
                detail=(
                    f"amount_safe_to_pay {safe} >= requested {requested}; "
                    f"splitting it would be strictly worse"
                ),
            )
        )
        return
    if earliest is None:
        rejections.append(
            Rejection(
                method=Method.PARTIAL_PAYMENT,
                reason=RejectionReason.NO_EARLIEST_DATE,
                detail="the remainder has no safe date inside the horizon",
            )
        )
        return
    if earliest > request.desired_completion_date:
        rejections.append(
            Rejection(
                method=Method.PARTIAL_PAYMENT,
                reason=RejectionReason.PAST_DEADLINE,
                detail=(
                    f"remainder falls due {earliest.isoformat()}, after the "
                    f"{request.desired_completion_date.isoformat()} deadline"
                ),
            )
        )
        return

    remainder = requested - safe
    # amount_safe_to_pay floors with ROUND_DOWN, so an off-by-a-cent split is
    # plausible enough to be worth asserting rather than assuming.
    assert safe + remainder == requested, (
        f"{context.request_id}: partial plan {safe} + {remainder} != {requested}"
    )

    candidates.append(
        Candidate(
            method=Method.PARTIAL_PAYMENT,
            payments=(
                Payment(on=request.request_date, amount=safe),
                Payment(on=earliest, amount=remainder),
            ),
            total_paid=requested,
            requires_changes=False,
            note=(
                f"pay {safe} today, {remainder} on {earliest.isoformat()}"
            ),
        )
    )


def _installment_candidates(
    context: UserContext,
    forecast: Forecast,
    candidates: list[Candidate],
    rejections: list[Rejection],
) -> None:
    request = context.request
    profile = context.profile
    options = [
        option
        for option in context.payment_options
        if option.payment_method == "installments"
    ]

    if not options:
        return
    if not profile.accepts("installments"):
        for option in options:
            rejections.append(
                Rejection(
                    method=Method.INSTALLMENTS,
                    reason=RejectionReason.METHOD_NOT_CONSIDERED,
                    detail=(
                        f"user considers "
                        f"{list(profile.payment_methods_user_will_consider)}"
                    ),
                    payment_option_id=option.payment_option_id,
                )
            )
        return

    for option in sorted(options, key=lambda o: id_sort_key(o.payment_option_id)):
        schedule = installment_schedule(option)
        last = schedule[-1].on

        # Deadline first: it is a hard filter, and passing it also guarantees
        # every payment lands inside the 90-day forecast window, which the
        # safety check below needs.
        if last > request.desired_completion_date:
            rejections.append(
                Rejection(
                    method=Method.INSTALLMENTS,
                    reason=RejectionReason.PAST_DEADLINE,
                    detail=(
                        f"{option.number_of_payments} payments end "
                        f"{last.isoformat()}, after the "
                        f"{request.desired_completion_date.isoformat()} deadline"
                    ),
                    payment_option_id=option.payment_option_id,
                )
            )
            continue

        if (
            config.enforce_installment_month_cap()
            and profile.max_installment_months is not None
            and option.number_of_payments > profile.max_installment_months
        ):
            rejections.append(
                Rejection(
                    method=Method.INSTALLMENTS,
                    reason=RejectionReason.EXCEEDS_INSTALLMENT_CAP,
                    detail=(
                        f"{option.number_of_payments} payments exceeds "
                        f"max_installment_months {profile.max_installment_months}"
                    ),
                    payment_option_id=option.payment_option_id,
                )
            )
            continue

        assert last <= forecast.end, (
            f"{option.payment_option_id}: schedule ends {last} beyond the "
            f"forecast horizon {forecast.end}"
        )

        # Cumulative: the whole schedule is walked against the forecast at once.
        if not payment_is_safe(
            forecast, [(p.on, p.amount) for p in schedule]
        ):
            rejections.append(
                Rejection(
                    method=Method.INSTALLMENTS,
                    reason=RejectionReason.UNSAFE_AGAINST_FORECAST,
                    detail=(
                        f"{option.number_of_payments} x {option.payment_amount} "
                        f"from {option.first_payment_date.isoformat()} takes a "
                        f"trough below {forecast.minimum_balance_to_keep}"
                    ),
                    payment_option_id=option.payment_option_id,
                )
            )
            continue

        candidates.append(
            Candidate(
                method=Method.INSTALLMENTS,
                payments=schedule,
                # Includes the financing fee, so it exceeds requested_amount.
                total_paid=option.total_payable_amount,
                requires_changes=False,
                payment_option_id=option.payment_option_id,
                note=(
                    f"{option.number_of_payments} payments of "
                    f"{option.payment_amount} every "
                    f"{option.payment_frequency_days} days"
                ),
            )
        )


def _wait_candidate(
    context: UserContext,
    capacity: CapacityAssessment,
    candidates: list[Candidate],
    rejections: list[Rejection],
) -> None:
    request = context.request
    profile = context.profile
    earliest = capacity.earliest_date_for_full_payment

    if not profile.accepts("full_payment"):
        rejections.append(
            Rejection(
                method=Method.WAIT,
                reason=RejectionReason.METHOD_NOT_CONSIDERED,
                detail="waiting ends in a full payment, which the user will not make",
            )
        )
        return
    if earliest is None:
        rejections.append(
            Rejection(
                method=Method.WAIT,
                reason=RejectionReason.NO_EARLIEST_DATE,
                detail="the full amount never becomes safe inside the horizon",
            )
        )
        return
    if earliest <= request.request_date:
        rejections.append(
            Rejection(
                method=Method.WAIT,
                reason=RejectionReason.FULL_AMOUNT_ALREADY_SAFE_TODAY,
                detail=(
                    "the rulebook makes wait eligible only when full payment "
                    "becomes safe *later*"
                ),
            )
        )
        return
    if earliest > request.desired_completion_date:
        rejections.append(
            Rejection(
                method=Method.WAIT,
                reason=RejectionReason.PAST_DEADLINE,
                detail=(
                    f"earliest safe date {earliest.isoformat()} is after the "
                    f"{request.desired_completion_date.isoformat()} deadline"
                ),
            )
        )
        return

    candidates.append(
        Candidate(
            method=Method.WAIT,
            payments=(Payment(on=earliest, amount=request.requested_amount),),
            total_paid=request.requested_amount,
            requires_changes=False,
            note=f"wait until {earliest.isoformat()}, then pay in full",
        )
    )


def generate_candidates(
    context: UserContext,
    forecast: Forecast,
    capacity: CapacityAssessment,
    *,
    change_finder: SpendingChangeFinder | None = None,
) -> CandidateSet:
    """Build every plan for one request and record why the rest were dropped.

    ``change_finder`` is the :mod:`src.changes` hook. Until Phase 7 wires it
    in, leaving it ``None`` produces no spending-change candidate and records
    the reason rather than silently omitting the branch.
    """
    candidates: list[Candidate] = []
    rejections: list[Rejection] = []

    _full_payment_candidates(
        context, forecast, capacity, change_finder, candidates, rejections
    )
    _partial_payment_candidate(context, capacity, candidates, rejections)
    _installment_candidates(context, forecast, candidates, rejections)
    _wait_candidate(context, capacity, candidates, rejections)

    # Always constructed; ranking uses it only when nothing else survives.
    candidates.append(
        Candidate(
            method=Method.NOT_RECOMMENDED,
            payments=(),
            total_paid=Decimal(0),
            requires_changes=False,
            note="fallback when no eligible plan is safe",
        )
    )

    result = CandidateSet(
        request_id=context.request_id,
        user_id=context.user_id,
        candidates=tuple(candidates),
        rejections=tuple(rejections),
    )
    for rejection in rejections:
        logger.info("%s discarded %s", context.request_id, rejection)
    logger.info(
        "%s produced %d eligible candidate(s), %d rejection(s)",
        context.request_id,
        len(result.eligible),
        len(rejections),
    )
    return result

"""Balance reconstruction (rulebook Step 1).

:func:`reconstruct_balance` takes a :class:`~src.load.UserContext` and an
``as_of`` date and returns a :class:`BalanceState`: the available balance,
every event classified into exactly one cash bucket, the recurring series
inferred from history, the future cash movements the forecast will consume,
and a resolution log explaining every lifecycle collapse.

The central rule is that ``current_available_balance`` is *the truth as of*
``request_date``. Settled events are already inside it and are never
re-applied; reconstructing the balance by replaying 25,000 ledger rows would
double-count every one of them. History is read for what it *tells* us --
which costs recur, at what cadence, at what size -- not for what it adds up
to.

This module decides nothing about the request itself. It answers "what is
true about this user's money right now, and what is already committed".
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from statistics import median
from typing import Iterable, Mapping, Sequence

from . import config
from .load import FinancialEvent, UserContext, id_sort_key, quantize

logger = logging.getLogger(__name__)


class CashClass(StrEnum):
    """Mutually exclusive buckets describing what an event does to cash."""

    #: Already reflected in ``current_available_balance``. Never re-applied.
    SETTLED_HISTORY = "settled_history"
    #: A debit that has not cleared. Reserved as a future outflow.
    PENDING_DEBIT = "pending_debit"
    #: An inbound amount that has not cleared. Excluded entirely.
    PENDING_CREDIT = "pending_credit"
    #: A confirmed future movement -- salary, a rescheduled debt payment.
    SCHEDULED_FUTURE = "scheduled_future"
    #: Never happened. Excluded entirely.
    CANCELLED_OR_FAILED = "cancelled_or_failed"
    #: A repeated representation of an event counted elsewhere. Excluded.
    DUPLICATE = "duplicate"
    #: Paper gains on an investment. Not spendable cash. Excluded.
    NON_CASH_INVESTMENT = "non_cash_investment"


class Recurrence(StrEnum):
    """Whether an event belongs to a repeating series."""

    RECURRING = "recurring"
    ONE_TIME = "one_time"


class ResolutionKind(StrEnum):
    """Why one event superseded another through ``linked_event_id``."""

    REFUND = "refund"
    PENDING_REFUND = "pending_refund"
    RETRY_AFTER_CANCELLATION = "retry_after_cancellation"
    RETRY_AFTER_FAILURE = "retry_after_failure"
    RECHARGE = "recharge"
    INVESTMENT_VALUATION = "investment_valuation"
    INVESTMENT_SALE = "investment_sale"
    UNCLASSIFIED_LINK = "unclassified_link"


class CadenceKind(StrEnum):
    MONTHLY = "monthly"
    FIXED_INTERVAL = "fixed_interval"
    NONE = "none"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifiedEvent:
    """One ledger row with everything :mod:`src.forecast` needs decided."""

    event: FinancialEvent
    cash_class: CashClass
    recurrence: Recurrence
    #: Series key this event belongs to, or ``None`` for unclassifiable rows.
    series_key: str | None
    #: Set when another event supersedes this one via ``linked_event_id``.
    superseded_by: str | None
    #: Free-text reason, carried into the resolution log and explanations.
    reason: str

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def is_excluded(self) -> bool:
        """True when the event contributes nothing to the forecast."""
        return self.cash_class in _EXCLUDED_CLASSES

    @property
    def is_future_cash(self) -> bool:
        return self.cash_class in (CashClass.PENDING_DEBIT, CashClass.SCHEDULED_FUTURE)


_EXCLUDED_CLASSES = frozenset(
    {
        CashClass.PENDING_CREDIT,
        CashClass.CANCELLED_OR_FAILED,
        CashClass.DUPLICATE,
        CashClass.NON_CASH_INVESTMENT,
    }
)


@dataclass(frozen=True)
class Resolution:
    """One collapsed lifecycle, logged so a decision can cite it."""

    kind: ResolutionKind
    parent_event_id: str
    child_event_id: str
    detail: str

    def __str__(self) -> str:
        return (
            f"[{self.kind}] {self.child_event_id} supersedes "
            f"{self.parent_event_id}: {self.detail}"
        )


@dataclass(frozen=True)
class CashFlow:
    """A committed movement of money on a known date, in home currency."""

    on: date
    amount: Decimal
    direction: str
    event_id: str
    category: str
    description: str
    source: str

    @property
    def signed(self) -> Decimal:
        """Negative for money leaving, positive for money arriving."""
        return -self.amount if self.direction == "debit" else self.amount


@dataclass(frozen=True)
class BlankAmountFinding:
    """Whether a blank-amount row actually needs its image read.

    A blank amount that settled before ``as_of`` is already inside
    ``current_available_balance``. Reading its image would recover a number
    that changes nothing -- the row is a distractor. Only blank amounts that
    are still pending or scheduled represent money not yet reflected in the
    balance, and those are the ones :mod:`src.extract` must resolve.
    """

    event_id: str
    status: str
    cash_date: date
    settled_before_as_of: bool
    needs_image: bool
    currency: str
    home_currency: str
    needs_fx_after_extraction: bool
    reason: str


@dataclass(frozen=True)
class RecurringSeries:
    """A repeating cost or income stream inferred from history.

    Keyed by description, as the rulebook specifies. Amount statistics use
    only occurrences that actually carry an amount: a blank is missing
    information, and averaging it in as zero would understate a real cost and
    make the forecast optimistic.
    """

    key: str
    description: str
    category: str
    event_type: str
    direction: str
    flexibility: str
    occurrences: tuple[ClassifiedEvent, ...]
    #: Occurrences with a usable amount, after superseded rows are dropped.
    contributing: tuple[ClassifiedEvent, ...]
    blank_amount_count: int
    cadence_kind: CadenceKind
    cadence_days: int | None
    day_of_month: int | None
    cadence_consistency: float
    is_recurring: bool
    reason: str

    @property
    def amounts(self) -> tuple[Decimal, ...]:
        return tuple(
            occurrence.event.amount_home
            for occurrence in self.contributing
            if occurrence.event.amount_home is not None
        )

    @property
    def latest(self) -> ClassifiedEvent | None:
        return self.contributing[-1] if self.contributing else None

    @property
    def latest_amount(self) -> Decimal | None:
        latest = self.latest
        return latest.event.amount_home if latest else None

    @property
    def mean_amount(self) -> Decimal | None:
        amounts = self.amounts
        if not amounts:
            return None
        return quantize(sum(amounts, Decimal(0)) / Decimal(len(amounts)))

    @property
    def median_amount(self) -> Decimal | None:
        amounts = self.amounts
        if not amounts:
            return None
        return quantize(Decimal(median(sorted(amounts))))

    @property
    def max_amount(self) -> Decimal | None:
        amounts = self.amounts
        return max(amounts) if amounts else None

    @property
    def last_date(self) -> date | None:
        latest = self.latest
        return latest.event.cash_date if latest else None


@dataclass(frozen=True)
class BalanceState:
    """The reconstructed financial position as of ``as_of``."""

    user_id: str
    request_id: str
    as_of: date
    home_currency: str
    available_balance: Decimal
    minimum_balance_to_keep: Decimal
    classified: tuple[ClassifiedEvent, ...]
    series: tuple[RecurringSeries, ...]
    resolutions: tuple[Resolution, ...]
    blank_amounts: tuple[BlankAmountFinding, ...]
    future_flows: tuple[CashFlow, ...]

    # -- the nine categories, as named views over `classified` -------------

    def _of_class(self, cash_class: CashClass) -> tuple[ClassifiedEvent, ...]:
        return tuple(e for e in self.classified if e.cash_class is cash_class)

    @property
    def settled_history(self) -> tuple[ClassifiedEvent, ...]:
        return self._of_class(CashClass.SETTLED_HISTORY)

    @property
    def pending_debit(self) -> tuple[ClassifiedEvent, ...]:
        return self._of_class(CashClass.PENDING_DEBIT)

    @property
    def pending_credit(self) -> tuple[ClassifiedEvent, ...]:
        return self._of_class(CashClass.PENDING_CREDIT)

    @property
    def scheduled_future(self) -> tuple[ClassifiedEvent, ...]:
        return self._of_class(CashClass.SCHEDULED_FUTURE)

    @property
    def cancelled_or_failed(self) -> tuple[ClassifiedEvent, ...]:
        return self._of_class(CashClass.CANCELLED_OR_FAILED)

    @property
    def duplicate(self) -> tuple[ClassifiedEvent, ...]:
        return self._of_class(CashClass.DUPLICATE)

    @property
    def non_cash_investment(self) -> tuple[ClassifiedEvent, ...]:
        return self._of_class(CashClass.NON_CASH_INVESTMENT)

    @property
    def recurring(self) -> tuple[ClassifiedEvent, ...]:
        return tuple(e for e in self.classified if e.recurrence is Recurrence.RECURRING)

    @property
    def one_time(self) -> tuple[ClassifiedEvent, ...]:
        return tuple(e for e in self.classified if e.recurrence is Recurrence.ONE_TIME)

    # -- convenience -------------------------------------------------------

    @property
    def recurring_series(self) -> tuple[RecurringSeries, ...]:
        return tuple(s for s in self.series if s.is_recurring)

    @property
    def excluded(self) -> tuple[ClassifiedEvent, ...]:
        return tuple(e for e in self.classified if e.is_excluded)

    @property
    def committed_outflow(self) -> Decimal:
        """Total already-committed money leaving after ``as_of``."""
        return quantize(
            sum(
                (f.amount for f in self.future_flows if f.direction == "debit"),
                Decimal(0),
            )
        )

    @property
    def confirmed_inflow(self) -> Decimal:
        return quantize(
            sum(
                (f.amount for f in self.future_flows if f.direction == "credit"),
                Decimal(0),
            )
        )

    @property
    def headroom(self) -> Decimal:
        """Balance above the minimum, before any forecasting."""
        return self.available_balance - self.minimum_balance_to_keep

    def by_id(self) -> Mapping[str, ClassifiedEvent]:
        return {e.event_id: e for e in self.classified}

    def series_for(self, event_id: str) -> RecurringSeries | None:
        classified = self.by_id().get(event_id)
        if classified is None or classified.series_key is None:
            return None
        return next(
            (s for s in self.series if s.key == classified.series_key), None
        )

    def summary(self) -> dict[str, object]:
        """Compact dict for logging and the audit trail."""
        return {
            "user_id": self.user_id,
            "request_id": self.request_id,
            "as_of": self.as_of.isoformat(),
            "available_balance": str(self.available_balance),
            "minimum_balance_to_keep": str(self.minimum_balance_to_keep),
            "headroom": str(self.headroom),
            "counts": {
                cash_class.value: len(self._of_class(cash_class))
                for cash_class in CashClass
            },
            "recurring_series": len(self.recurring_series),
            "future_flows": len(self.future_flows),
            "committed_outflow": str(self.committed_outflow),
            "confirmed_inflow": str(self.confirmed_inflow),
            "resolutions": len(self.resolutions),
            "blank_amounts_needing_image": sum(
                1 for b in self.blank_amounts if b.needs_image
            ),
        }


# ---------------------------------------------------------------------------
# Lifecycle collapsing
# ---------------------------------------------------------------------------


def _classify_link(
    parent: FinancialEvent, child: FinancialEvent
) -> tuple[ResolutionKind, str, bool]:
    """Describe a parent/child link and say whether the parent is superseded.

    Returns ``(kind, detail, supersedes_parent)``. "Supersedes" means the
    parent no longer describes a real, standing cost: its money either came
    back, or the attempt never happened and was replaced.
    """
    if child.event_type == "refund":
        if child.status == "pending":
            return (
                ResolutionKind.PENDING_REFUND,
                f"refund of {parent.event_id} has not settled; not counted as cash, "
                f"but the original expense is treated as reimbursed",
                True,
            )
        return (
            ResolutionKind.REFUND,
            f"refund settled {child.cash_date.isoformat()} reverses "
            f"{parent.event_id}",
            True,
        )
    if parent.status == "cancelled":
        return (
            ResolutionKind.RETRY_AFTER_CANCELLATION,
            f"cancelled {parent.event_id} was replaced by {child.status} "
            f"{child.event_id} on {child.cash_date.isoformat()}",
            True,
        )
    if parent.status == "failed":
        return (
            ResolutionKind.RETRY_AFTER_FAILURE,
            f"failed {parent.event_id} was rescheduled as {child.event_id} "
            f"on {child.cash_date.isoformat()}",
            True,
        )
    if child.event_type == "investment_valuation":
        return (
            ResolutionKind.INVESTMENT_VALUATION,
            f"unrealized valuation of {parent.event_id}; paper value, not cash",
            False,
        )
    if child.event_type == "investment_sale":
        return (
            ResolutionKind.INVESTMENT_SALE,
            f"sale realises {parent.event_id}; proceeds settle "
            f"{child.cash_date.isoformat()}",
            False,
        )
    if child.direction == "debit":
        return (
            ResolutionKind.RECHARGE,
            f"{child.status} re-charge of {parent.event_id} due "
            f"{child.cash_date.isoformat()}",
            False,
        )
    return (
        ResolutionKind.UNCLASSIFIED_LINK,
        f"{child.event_id} links to {parent.event_id} with no recognised pattern",
        False,
    )


def _resolve_lifecycles(
    events: Sequence[FinancialEvent],
) -> tuple[dict[str, str], list[Resolution]]:
    """Collapse ``linked_event_id`` chains.

    Returns ``(superseded_by, resolutions)`` where ``superseded_by`` maps a
    parent event id to the child that replaces it.
    """
    by_id = {event.event_id: event for event in events}
    superseded: dict[str, str] = {}
    resolutions: list[Resolution] = []

    for child in sorted(events, key=lambda e: id_sort_key(e.event_id)):
        if not child.linked_event_id:
            continue
        parent = by_id.get(child.linked_event_id)
        if parent is None:
            # Cross-user or out-of-context link; nothing to collapse here.
            continue
        kind, detail, supersedes = _classify_link(parent, child)
        resolutions.append(
            Resolution(
                kind=kind,
                parent_event_id=parent.event_id,
                child_event_id=child.event_id,
                detail=detail,
            )
        )
        if supersedes:
            superseded[parent.event_id] = child.event_id

    for resolution in resolutions:
        logger.debug("%s", resolution)
    return superseded, resolutions


# ---------------------------------------------------------------------------
# Cash classification
# ---------------------------------------------------------------------------


def _find_duplicates(events: Sequence[FinancialEvent]) -> set[str]:
    """Identify repeated representations of one real movement.

    Deliberately narrow: same user, description, direction, amount and cash
    date, with no ``linked_event_id`` relationship. A recurring charge of the
    same size on a *different* date is a real second charge, not a duplicate,
    and collapsing those would under-forecast the user's costs.

    No row in the supplied dataset matches, which is the expected result.
    """
    seen: dict[tuple[str, str, str, str, str], str] = {}
    duplicates: set[str] = set()
    for event in sorted(events, key=lambda e: id_sort_key(e.event_id)):
        key = (
            event.description,
            event.direction,
            event.status,
            str(event.amount_home),
            event.cash_date.isoformat(),
        )
        if event.amount_home is None:
            continue
        if key in seen:
            duplicates.add(event.event_id)
            logger.debug(
                "duplicate: %s repeats %s (%s)", event.event_id, seen[key], key
            )
        else:
            seen[key] = event.event_id
    return duplicates


def _cash_class(
    event: FinancialEvent,
    as_of: date,
    duplicates: set[str],
) -> tuple[CashClass, str]:
    if event.event_id in duplicates:
        return CashClass.DUPLICATE, "repeats an identical event already counted"
    if event.status in ("cancelled", "failed"):
        return (
            CashClass.CANCELLED_OR_FAILED,
            f"status {event.status}; the money never moved",
        )
    if event.status == "unrealized" or event.direction == "non_cash":
        return (
            CashClass.NON_CASH_INVESTMENT,
            "unrealized investment value is not spendable cash",
        )
    if event.status == "pending":
        if event.direction == "credit":
            return (
                CashClass.PENDING_CREDIT,
                "inbound amount has not settled; not counted until it does",
            )
        return (
            CashClass.PENDING_DEBIT,
            f"unsettled debit reserved at {event.cash_date.isoformat()}",
        )
    if event.status == "scheduled":
        return (
            CashClass.SCHEDULED_FUTURE,
            f"confirmed movement on {event.cash_date.isoformat()}",
        )
    # settled
    if event.cash_date > as_of:
        # Not present in the supplied data, but a settled row dated after the
        # request would not be inside the opening balance.
        return (
            CashClass.SCHEDULED_FUTURE,
            f"settled but dated after {as_of.isoformat()}",
        )
    return (
        CashClass.SETTLED_HISTORY,
        "already reflected in current_available_balance",
    )


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------


def _series_key(event: FinancialEvent) -> str:
    """Series identity. The rulebook keys recurring series by description."""
    return f"{event.description}|{event.direction}"


def _snap_cadence(
    gaps: Sequence[int],
) -> tuple[CadenceKind, int | None, float]:
    """Infer a cadence from observed day-gaps.

    Snaps to the allowed set -- 5, 7, 10, 14, 21 days, or monthly -- scoring
    each candidate by how many gaps it explains directly, then by how many it
    explains as a small multiple (a skipped occurrence leaves a doubled gap).
    Direct matches outrank multiples so a genuine 14-day series is not
    mistaken for a 7-day one with every second occurrence missing.
    """
    if not gaps:
        return CadenceKind.NONE, None, 0.0

    tolerance = config.cadence_tolerance_days()
    max_skips = config.max_skipped_occurrences()

    def monthly_hits(multiple: int) -> int:
        low = config.MONTHLY_GAP_MIN * multiple
        high = config.MONTHLY_GAP_MAX * multiple
        return sum(1 for gap in gaps if low <= gap <= high)

    candidates: list[tuple[int, int, CadenceKind, int]] = []

    direct = monthly_hits(1)
    multiple_total = sum(monthly_hits(k) for k in range(1, max_skips + 1))
    candidates.append(
        (direct, multiple_total, CadenceKind.MONTHLY, config.MONTHLY_NOMINAL_DAYS)
    )

    for days in config.ALLOWED_CADENCE_DAYS:
        direct = sum(1 for gap in gaps if abs(gap - days) <= tolerance)
        multiple_total = sum(
            1
            for gap in gaps
            for k in range(1, max_skips + 1)
            if abs(gap - days * k) <= tolerance
        )
        candidates.append((direct, multiple_total, CadenceKind.FIXED_INTERVAL, days))

    direct, multiple_total, kind, days = max(
        candidates, key=lambda c: (c[0], c[1], -c[3])
    )
    if direct == 0 and multiple_total == 0:
        return CadenceKind.NONE, None, 0.0

    # A gap that only matches a *multiple* of the candidate is weaker evidence
    # than one that lands on it directly: a skipped occurrence is plausible,
    # but a run of them is indistinguishable from noise. Half weight keeps a
    # genuine series with one missing month above the threshold while an
    # irregular series that merely happens to be divisible falls below it.
    #
    # SWEPT AND CONFIRMED (P11-S6). The weight, the consistency threshold it is
    # scored against, the gap tolerance and the skip cap were all swept against
    # the 25 labels -- 22 settings, no default moved. Each carries its table and
    # its rejected alternatives at its definition in src/config.py; reproduce
    # with `py -m evaluation.sweep_cadence`. Summary: MARE is flat across
    # skip weights 0.25-0.75 (0.5% spread) and the run-rate ratio is the only
    # metric that separates them, crossing 1.00 at exactly 0.5.
    # KNOWN IMPRECISION AT LONG PERIODS (P11-S3b). Spot-checked over 29 debit
    # series across three users: 28 snap to the raw modal gap. The exception is
    # request_06's 'Local market purchase', modal gap 60 days snapped to 30, so
    # it over-projects. Over-projection is the safe direction, and correcting it
    # would widen that row's gap, so it is left as measured rather than tuned.
    skipped = multiple_total - direct
    consistency = (direct + config.cadence_skip_weight() * skipped) / len(gaps)
    return kind, days, consistency


def _day_of_month(occurrences: Sequence[FinancialEvent]) -> int | None:
    """Anchor day for a monthly series, or ``None`` if it drifts."""
    days = Counter(event.cash_date.day for event in occurrences)
    if not days:
        return None
    anchor, hits = days.most_common(1)[0]
    return anchor if hits >= max(2, len(occurrences) // 2) else None


def _build_series(
    classified: Sequence[ClassifiedEvent],
    superseded: Mapping[str, str],
) -> tuple[RecurringSeries, ...]:
    grouped: dict[str, list[ClassifiedEvent]] = defaultdict(list)
    for item in classified:
        if item.series_key is not None:
            grouped[item.series_key].append(item)

    series: list[RecurringSeries] = []
    for key, members in sorted(grouped.items()):
        members = sorted(members, key=lambda c: (c.event.cash_date, c.event_id))
        head = members[0].event

        # A refunded or cancelled occurrence is not evidence of a standing
        # cost, and a blank amount is missing information rather than zero.
        contributing = tuple(
            item
            for item in members
            if item.event_id not in superseded
            and not item.is_excluded
            and item.event.amount_home is not None
        )
        blank_count = sum(1 for item in members if item.event.amount_home is None)

        dated = [item.event for item in members if not item.is_excluded]
        gaps = [
            (later.cash_date - earlier.cash_date).days
            for earlier, later in zip(dated, dated[1:])
            if (later.cash_date - earlier.cash_date).days > 0
        ]
        cadence_kind, cadence_days, consistency = _snap_cadence(gaps)
        anchor = (
            _day_of_month(dated) if cadence_kind is CadenceKind.MONTHLY else None
        )

        occurrence_count = len(dated)
        minimum = config.recurrence_min_occurrences()
        is_recurring = False
        if cadence_kind is not CadenceKind.NONE and consistency >= (
            config.cadence_consistency_threshold()
        ):
            if occurrence_count >= minimum:
                is_recurring = True
                reason = (
                    f"{occurrence_count} occurrences at a {cadence_kind} cadence "
                    f"({consistency:.0%} of gaps match)"
                )
            elif (
                occurrence_count == 2
                and head.direction == "debit"
                and config.allow_two_occurrence_debits()
            ):
                # Asymmetric on purpose: missing a recurring outflow makes the
                # forecast optimistic and the recommendation unsafe.
                is_recurring = True
                reason = (
                    "2 occurrences of an outflow at a clean "
                    f"{cadence_kind} cadence; treated as recurring because "
                    "under-forecasting an expense is the unsafe error"
                )
            else:
                reason = (
                    f"only {occurrence_count} occurrence(s); history does not "
                    f"support recurrence (need {minimum})"
                )
        else:
            reason = (
                f"no consistent cadence ({occurrence_count} occurrence(s), "
                f"{len(gaps)} gap(s), {consistency:.0%} match)"
            )

        series.append(
            RecurringSeries(
                key=key,
                description=head.description,
                category=head.category,
                event_type=head.event_type,
                direction=head.direction,
                flexibility=head.flexibility,
                occurrences=tuple(members),
                contributing=contributing,
                blank_amount_count=blank_count,
                cadence_kind=cadence_kind if is_recurring else CadenceKind.NONE,
                cadence_days=cadence_days if is_recurring else None,
                day_of_month=anchor if is_recurring else None,
                cadence_consistency=consistency,
                is_recurring=is_recurring,
                reason=reason,
            )
        )
    return tuple(series)


# ---------------------------------------------------------------------------
# Blank amounts
# ---------------------------------------------------------------------------


def _assess_blank_amounts(
    context: UserContext, as_of: date
) -> tuple[BlankAmountFinding, ...]:
    """Decide which blank-amount rows actually need an image read.

    A blank amount that settled on or before ``as_of`` is already inside
    ``current_available_balance``; recovering its number changes no forecast
    and no decision, so it is a distractor and the image read is skipped.
    A blank amount that is still pending or scheduled is money not yet in the
    balance and must be resolved before it can be reserved.
    """
    findings: list[BlankAmountFinding] = []
    for event in context.blank_amount_events():
        settled_before = event.status == "settled" and event.cash_date <= as_of
        needs_image = not settled_before
        needs_fx = needs_image and event.currency != event.home_currency
        if settled_before:
            reason = (
                f"settled {event.cash_date.isoformat()} on or before "
                f"{as_of.isoformat()}; already inside available_balance, so the "
                f"image read is skipped"
            )
        else:
            reason = (
                f"{event.status} with cash date {event.cash_date.isoformat()} "
                f"after {as_of.isoformat()}; amount is not in the balance and "
                f"must be recovered from the linked image"
            )
        finding = BlankAmountFinding(
            event_id=event.event_id,
            status=event.status,
            cash_date=event.cash_date,
            settled_before_as_of=settled_before,
            needs_image=needs_image,
            currency=event.currency,
            home_currency=event.home_currency,
            needs_fx_after_extraction=needs_fx,
            reason=reason,
        )
        findings.append(finding)
        if needs_image:
            logger.info(
                "blank amount requires image: %s (%s) -- %s",
                finding.event_id,
                finding.status,
                finding.reason,
            )
        else:
            logger.debug(
                "blank amount is a distractor: %s -- %s",
                finding.event_id,
                finding.reason,
            )
    return tuple(findings)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _future_flows(
    classified: Sequence[ClassifiedEvent], as_of: date
) -> tuple[CashFlow, ...]:
    """Committed movements after ``as_of``.

    Pending debits and scheduled events only. Pending credits are excluded by
    their classification and never reach here.
    """
    flows: list[CashFlow] = []
    for item in classified:
        if not item.is_future_cash:
            continue
        event = item.event
        if event.cash_date <= as_of:
            continue
        if event.amount_home is None:
            # Amount still unknown; extract.py must fill it before forecasting.
            logger.warning(
                "future cash flow %s has no amount yet (%s)",
                event.event_id,
                event.status,
            )
            continue
        flows.append(
            CashFlow(
                on=event.cash_date,
                amount=event.amount_home,
                direction=event.direction,
                event_id=event.event_id,
                category=event.category,
                description=event.description,
                source=item.cash_class.value,
            )
        )
    return tuple(sorted(flows, key=lambda f: (f.on, id_sort_key(f.event_id))))


def reconstruct_balance(
    context: UserContext, as_of: date | None = None
) -> BalanceState:
    """Rebuild the user's financial position as of ``as_of``.

    ``as_of`` defaults to the request date. ``current_available_balance`` is
    taken as truth at that date and settled history is never re-applied to it.
    """
    moment = as_of or context.request.request_date
    profile = context.profile
    events = context.events

    superseded, resolutions = _resolve_lifecycles(events)
    duplicates = _find_duplicates(events)

    classified: list[ClassifiedEvent] = []
    for event in events:
        cash_class, reason = _cash_class(event, moment, duplicates)
        superseded_by = superseded.get(event.event_id)
        if superseded_by is not None:
            reason = f"{reason}; superseded by {superseded_by}"
        classified.append(
            ClassifiedEvent(
                event=event,
                cash_class=cash_class,
                # Filled in below, once series membership is known.
                recurrence=Recurrence.ONE_TIME,
                series_key=_series_key(event),
                superseded_by=superseded_by,
                reason=reason,
            )
        )

    series = _build_series(classified, superseded)
    recurring_keys = {s.key for s in series if s.is_recurring}
    classified = [
        ClassifiedEvent(
            event=item.event,
            cash_class=item.cash_class,
            recurrence=(
                Recurrence.RECURRING
                if item.series_key in recurring_keys
                else Recurrence.ONE_TIME
            ),
            series_key=item.series_key,
            superseded_by=item.superseded_by,
            reason=item.reason,
        )
        for item in classified
    ]

    state = BalanceState(
        user_id=profile.user_id,
        request_id=context.request_id,
        as_of=moment,
        home_currency=profile.home_currency,
        available_balance=profile.current_available_balance,
        minimum_balance_to_keep=profile.minimum_balance_to_keep,
        classified=tuple(classified),
        series=series,
        resolutions=tuple(resolutions),
        blank_amounts=_assess_blank_amounts(context, moment),
        future_flows=_future_flows(classified, moment),
    )
    logger.debug("reconstructed state: %s", state.summary())
    return state

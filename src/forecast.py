"""90-day balance forecast (rulebook Steps 2 and 3).

Two stages:

:func:`apply_amendments`
    Folds extracted evidence into the event stream -- salary amount and date
    changes, income endings, confirmed future income, rent increases, new
    recurring expenses -- resolving conflicts by a strict precedence and
    logging which rule fired for each one.

:func:`build_forecast`
    Projects a daily balance over ``[request_date, request_date + horizon]``
    from recurring expense series, extrapolated salary, scheduled future
    payments and pending debits.

:func:`troughs` exposes the local minima, which is what safety actually turns
on. The balance on a payment day is nearly meaningless; the binding constraint
is the pre-salary dip that follows it. A payment is safe only when every trough
at or after the payment date stays at or above ``minimum_balance_to_keep``.

Nothing here invents money. An unresolved blank amount fails the request rather
than being forecast as zero, matching :mod:`src.state`'s refusal.

VALIDATION STATUS
-----------------
**This module carries the pipeline's residual defect.** ``affordable_now`` is
assigned to 26.0% of evaluation rows against a reference ~12% -- 14 points high
-- because capacity is still over-stated for some users.

Two calibrations closed most of the original gap: the staleness gate 4.0 -> 12.0
(P11-S2) and the income-aggregation fallback (P11-S4) moved the outflow
run-rate ratio from 0.78 to 1.01 median. **Three further global levers were
measured and rejected on evidence**, not left untried:

* orphan-expense aggregation (P11-S3) -- outflow was already complete at 1.01,
  so adding fragments double-counts; status accuracy 18/25 -> 9/25
* the series amount estimator (P11-S3) -- median/mean/max differ by ~2% MARE
* all-cadences income aggregation (P11-S4) -- swings ``request_10`` to a 21x
  overstatement

**Two rows remain unexplained.** ``request_05`` needs a trough unreachable from
its own cash flows under any setting swept, and is the largest single MARE
contributor. ``request_21`` needs 139.17 more outflow with no dropped series, no
occurrence shortfall, no cadence mismatch and no excluded event class to supply
it (P11-S3b).
"""

from __future__ import annotations

import calendar
import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from statistics import median
from typing import Iterable, Mapping, Sequence

from . import config
from .extract import Evidence, ExtractedFact, UnresolvedBlankAmountError
from .load import id_sort_key, quantize
from .patterns import FactKind
from .state import (
    BalanceState,
    CadenceKind,
    CashFlow,
    RecurringSeries,
    _snap_cadence,
)

logger = logging.getLogger(__name__)


class ConflictRule(StrEnum):
    """Precedence used to resolve contradictory records, highest first."""

    EXPLICIT_AMENDMENT = "explicit_cancellation_settlement_or_amendment"
    NEWER_SAME_SOURCE = "newer_record_from_the_same_source"
    SETTLED_OVER_ESTIMATE = "settled_over_estimate_or_forecast"
    SAFER_INTERPRETATION = "financially_safer_interpretation"


class ExclusionReason(StrEnum):
    STALE = "stale"
    TOO_FEW_OCCURRENCES = "too_few_occurrences"
    NO_AMOUNT = "no_amount"
    NO_CADENCE = "no_cadence"
    SUPERSEDED_BY_SCHEDULED = "superseded_by_scheduled_event"


class ForecastError(Exception):
    """The forecast cannot be built for this request."""


# ---------------------------------------------------------------------------
# Amendments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConflictDecision:
    """One resolved contradiction, logged with the rule that settled it."""

    subject: str
    rule: ConflictRule
    winner: str
    loser: str
    detail: str

    def __str__(self) -> str:
        return (
            f"[{self.rule}] {self.subject}: kept {self.winner}, dropped "
            f"{self.loser} -- {self.detail}"
        )


@dataclass(frozen=True)
class SalaryPlan:
    """How salary is expected to arrive over the horizon.

    ``cadence_days`` and ``cadence_kind`` carry the rhythm: monthly streams are
    day-of-month anchored, while weekly and fortnightly ones step from
    ``anchor_date``. An income stream paid every 7 days projected as monthly
    would lose three quarters of itself.
    """

    amount: Decimal | None
    day_of_month: int
    cadence_days: int | None = None
    cadence_kind: str = "monthly"
    #: Last observed occurrence, used to step a non-monthly cadence forward.
    anchor_date: date | None = None
    #: Amount applying only to the first payday in the horizon, if amended.
    next_payday_amount: Decimal | None = None
    #: Salary stops on or after this date.
    ends_on: date | None = None
    #: Salary returns to ``resumes_amount`` on this date.
    resumes_on: date | None = None
    resumes_amount: Decimal | None = None
    #: Explicit override of the next payday.
    next_payday_date: date | None = None
    source: str = "history"

    @property
    def is_known(self) -> bool:
        return self.amount is not None or self.next_payday_amount is not None


@dataclass(frozen=True)
class Amendments:
    """Everything extraction changed about the projection."""

    salary: SalaryPlan
    #: Extra dated credits that are confirmed, e.g. an approved invoice.
    extra_credits: tuple[CashFlow, ...]
    #: Multiplier applied to a recurring series, keyed by series key.
    series_multipliers: Mapping[str, Decimal]
    #: Series keys that stop from a date.
    series_stopped: Mapping[str, date]
    conflicts: tuple[ConflictDecision, ...]
    #: Facts recognised but not quantifiable, so deliberately not applied.
    unquantified: tuple[str, ...]

    def multiplier_for(self, key: str) -> Decimal:
        return self.series_multipliers.get(key, Decimal(1))


def _salary_series(state: BalanceState) -> RecurringSeries | None:
    candidates = [
        series
        for series in state.recurring_series
        if series.direction == "credit" and series.category == "salary"
    ]
    if not candidates:
        candidates = [
            series
            for series in state.recurring_series
            if series.direction == "credit" and series.event_type == "income"
        ]
    if not candidates:
        return None
    # The largest stream is the salary; a side income should not displace it.
    return max(
        candidates,
        key=lambda s: (s.median_amount or Decimal(0), len(s.contributing)),
    )


@dataclass(frozen=True)
class IncomeStream:
    """An income rhythm inferred across *all* of a user's salary credits.

    :mod:`src.state` keys series by description, which is right for expenses —
    it is what makes the labelled spending-change rows reproduce. But a single
    income stream is often recorded under several descriptions: one user's
    weekly gig income arrives every 7 days under four rotating payer names, and
    description-keying shatters it into four irregular series, none recurring,
    so no income projects at all.

    This aggregates every salary credit into one stream and infers the cadence
    from the union of dates. It is used **only as a fallback**, when no
    description-keyed salary series was detected, so users whose payroll is
    recorded consistently keep their existing, validated behaviour.
    """

    amount: Decimal
    cadence_days: int
    cadence_kind: str
    day_of_month: int | None
    last_date: date
    occurrences: int
    descriptions: int


def _aggregate_income_stream(state: BalanceState) -> IncomeStream | None:
    """Infer one income rhythm across every salary credit in the history."""
    events = [
        item.event
        for item in state.classified
        if item.event.direction == "credit"
        and not item.is_excluded
        and item.event.amount_home is not None
        and item.event.cash_date <= state.as_of
        and (item.event.category == "salary" or item.event.event_type == "income")
    ]
    if not config.aggregate_income_stream():
        return None
    if len(events) < config.recurrence_min_occurrences():
        return None

    events.sort(key=lambda e: (e.cash_date, id_sort_key(e.event_id)))
    gaps = [
        (later.cash_date - earlier.cash_date).days
        for earlier, later in zip(events, events[1:])
        if (later.cash_date - earlier.cash_date).days > 0
    ]
    kind, cadence_days, consistency = _snap_cadence(gaps)
    if kind is CadenceKind.NONE or cadence_days is None:
        return None
    if consistency < config.cadence_consistency_threshold():
        return None
    if config.aggregate_monthly_income_only() and kind is not CadenceKind.MONTHLY:
        # A monthly payroll recorded under two description variants is plainly
        # one salary stream. Sub-monthly platform earnings under rotating payer
        # names are not: the rulebook forbids inventing unsupported future
        # income, and under-forecasting income is the safe error.
        return None

    amounts = sorted(e.amount_home for e in events)
    median_amount = quantize(Decimal(median(amounts)))

    anchor = None
    if kind is CadenceKind.MONTHLY:
        days = Counter(e.cash_date.day for e in events)
        candidate, hits = days.most_common(1)[0]
        if hits >= max(2, len(events) // 2):
            anchor = candidate

    return IncomeStream(
        amount=median_amount,
        cadence_days=cadence_days,
        cadence_kind=str(kind),
        day_of_month=anchor,
        last_date=events[-1].cash_date,
        occurrences=len(events),
        descriptions=len({e.description for e in events}),
    )


def _income_plan_inputs(
    state: BalanceState,
) -> tuple[Decimal | None, int, int | None, str, date | None]:
    """``(amount, day_of_month, cadence_days, cadence_kind, anchor_date)``."""
    history = _salary_series(state)
    if history is not None:
        return (
            history.median_amount,
            history.day_of_month or config.default_salary_day_of_month(),
            history.cadence_days,
            str(history.cadence_kind),
            history.last_date,
        )

    stream = _aggregate_income_stream(state)
    if stream is not None:
        logger.info(
            "%s: no description-keyed salary series; aggregated %d credits "
            "across %d descriptions into a %s stream of %s every %d days",
            state.request_id,
            stream.occurrences,
            stream.descriptions,
            stream.cadence_kind,
            stream.amount,
            stream.cadence_days,
        )
        return (
            stream.amount,
            stream.day_of_month or config.default_salary_day_of_month(),
            stream.cadence_days,
            stream.cadence_kind,
            stream.last_date,
        )

    return (None, config.default_salary_day_of_month(), None, "monthly", None)


def _rent_series_keys(state: BalanceState) -> tuple[str, ...]:
    return tuple(
        series.key
        for series in state.recurring_series
        if series.category in ("rent", "housing") and series.direction == "debit"
    )


def apply_amendments(state: BalanceState, extractions: Evidence) -> Amendments:
    """Fold extracted evidence into the projection inputs.

    Only facts whose :attr:`~src.extract.ExtractedFact.affects_cash` is true are
    considered; an instruction attempt, a pending credit, or a paper gain is
    recorded by extraction and ignored here.

    Conflicts are settled strictly in the order: an explicit
    cancellation/settlement/amendment, then the newer record from the same
    source, then a settled event over an estimate, then the financially safer
    reading. Every decision is logged.
    """
    facts = [fact for fact in extractions.cash_facts]
    conflicts: list[ConflictDecision] = []
    unquantified: list[str] = []

    (
        salary_amount,
        salary_day,
        cadence_days,
        cadence_kind,
        anchor_date,
    ) = _income_plan_inputs(state)
    plan = SalaryPlan(
        amount=salary_amount,
        day_of_month=salary_day,
        cadence_days=cadence_days,
        cadence_kind=cadence_kind,
        anchor_date=anchor_date,
    )

    # Order facts so that later evidence wins ties by recency, and an explicit
    # ending outranks an amount change regardless of order.
    def precedence(fact: ExtractedFact) -> tuple[int, str]:
        rank = {
            FactKind.EMPLOYMENT_ENDED: 0,
            FactKind.INCOME_ENDING: 1,
            FactKind.SALARY_RESUMES: 2,
            FactKind.SALARY_AMOUNT_CHANGE: 3,
            FactKind.SALARY_REDUCED: 4,
            FactKind.SALARY_DATE_CHANGE: 5,
        }.get(fact.kind, 9)
        return (rank, fact.source_id)

    multipliers: dict[str, Decimal] = {}
    stopped: dict[str, date] = {}
    extra_credits: list[CashFlow] = []

    for fact in sorted(facts, key=precedence):
        if fact.kind is FactKind.EMPLOYMENT_ENDED:
            if plan.ends_on is not None:
                conflicts.append(
                    ConflictDecision(
                        subject="salary",
                        rule=ConflictRule.EXPLICIT_AMENDMENT,
                        winner=fact.source_id,
                        loser="earlier ending",
                        detail="an explicit employment ending supersedes",
                    )
                )
            plan = replace(
                plan,
                ends_on=fact.effective_date or state.as_of,
                source=fact.source_id,
            )
            conflicts.append(
                ConflictDecision(
                    subject="salary",
                    rule=ConflictRule.EXPLICIT_AMENDMENT,
                    winner=fact.source_id,
                    loser="projected salary from history",
                    detail=(
                        "employment ended; no salary is projected after "
                        f"{(fact.effective_date or state.as_of).isoformat()}"
                    ),
                )
            )
        elif fact.kind is FactKind.INCOME_ENDING:
            if fact.amount is not None:
                conflicts.append(
                    ConflictDecision(
                        subject="salary",
                        rule=ConflictRule.EXPLICIT_AMENDMENT,
                        winner=fact.source_id,
                        loser="projected salary from history",
                        detail=(
                            f"one income stream ended; the remaining confirmed "
                            f"salary of {fact.amount} replaces the historical "
                            f"{salary_amount}"
                        ),
                    )
                )
                plan = replace(
                    plan, amount=fact.amount, source=fact.source_id
                )
            else:
                unquantified.append(
                    f"{fact.source_id}: income ending with no remaining amount stated"
                )
        elif fact.kind is FactKind.SALARY_RESUMES:
            plan = replace(
                plan,
                resumes_on=fact.effective_date,
                resumes_amount=fact.amount,
                source=fact.source_id,
            )
        elif fact.kind in (
            FactKind.SALARY_AMOUNT_CHANGE,
            FactKind.CONFIRMED_BASE_SALARY,
        ):
            if fact.amount is None:
                unquantified.append(
                    f"{fact.source_id}: {fact.kind} with no amount stated"
                )
                continue
            if plan.amount is not None and plan.amount != fact.amount:
                conflicts.append(
                    ConflictDecision(
                        subject="salary amount",
                        rule=ConflictRule.EXPLICIT_AMENDMENT,
                        winner=fact.source_id,
                        loser="median of historical salary events",
                        detail=(
                            f"employer states {fact.amount}; history shows "
                            f"{plan.amount}. An explicit amendment outranks an "
                            f"estimate derived from past events."
                        ),
                    )
                )
            plan = replace(plan, amount=fact.amount, source=fact.source_id)
        elif fact.kind is FactKind.SALARY_REDUCED:
            if fact.amount is None:
                unquantified.append(f"{fact.source_id}: reduction with no amount")
                continue
            # A reduction applies to the next payroll only unless a resume date
            # says otherwise. Treating it as permanent would understate income.
            plan = replace(
                plan, next_payday_amount=fact.amount, source=fact.source_id
            )
            conflicts.append(
                ConflictDecision(
                    subject="salary amount",
                    rule=ConflictRule.SAFER_INTERPRETATION,
                    winner=fact.source_id,
                    loser="unreduced historical salary",
                    detail=(
                        f"next payroll reduced to {fact.amount}; applied to the "
                        f"first payday only, which is the safer reading for "
                        f"income"
                    ),
                )
            )
        elif fact.kind is FactKind.SALARY_DATE_CHANGE:
            if fact.effective_date is None:
                unquantified.append(f"{fact.source_id}: date change with no date")
                continue
            plan = replace(
                plan, next_payday_date=fact.effective_date, source=fact.source_id
            )
            conflicts.append(
                ConflictDecision(
                    subject="payday",
                    rule=ConflictRule.EXPLICIT_AMENDMENT,
                    winner=fact.source_id,
                    loser=f"day-{plan.day_of_month} anchor from history",
                    detail=(
                        f"employer moved the confirmed salary to "
                        f"{fact.effective_date.isoformat()}"
                    ),
                )
            )
        elif fact.kind in (
            FactKind.CONFIRMED_FUTURE_INCOME,
            FactKind.CONFIRMED_INVOICE_PAYMENT,
        ):
            if fact.amount is None or fact.effective_date is None:
                continue
            if fact.effective_date <= state.as_of:
                continue
            extra_credits.append(
                CashFlow(
                    on=fact.effective_date,
                    amount=fact.amount,
                    direction="credit",
                    event_id=fact.source_id,
                    category="salary"
                    if fact.kind is FactKind.CONFIRMED_FUTURE_INCOME
                    else "invoice",
                    description=fact.note,
                    source="amendment",
                )
            )
        elif fact.kind is FactKind.RENT_INCREASE:
            keys = _rent_series_keys(state)
            if not keys:
                unquantified.append(
                    f"{fact.source_id}: rent increase with no rent series to apply to"
                )
                continue
            if fact.percent is not None:
                multiplier = (Decimal(100) + fact.percent) / Decimal(100)
                for key in keys:
                    multipliers[key] = multiplier
                conflicts.append(
                    ConflictDecision(
                        subject="rent",
                        rule=ConflictRule.EXPLICIT_AMENDMENT,
                        winner=fact.source_id,
                        loser="rent amount from history",
                        detail=(
                            f"renewed lease raises rent by {fact.percent}% from "
                            f"the next payment"
                        ),
                    )
                )
            elif fact.amount is not None:
                for key in keys:
                    series = next(s for s in state.series if s.key == key)
                    base = series.latest_amount or series.median_amount
                    if base and base > 0:
                        multipliers[key] = fact.amount / base
            else:
                unquantified.append(f"{fact.source_id}: rent increase with no figure")
        elif fact.kind is FactKind.NEW_RECURRING_EXPENSE:
            # The message states that a new recurring payment begins but never
            # its size. Inventing one would breach "do not invent unsupported
            # expenses"; recording it keeps the omission visible.
            unquantified.append(
                f"{fact.source_id}: new recurring expense announced with no amount; "
                f"not projected"
            )
        elif fact.kind is FactKind.FAILED_DEBIT_RETRY:
            # The retry already exists as a scheduled event row, which outranks
            # the message under "settled/scheduled record over an estimate".
            conflicts.append(
                ConflictDecision(
                    subject="failed debit",
                    rule=ConflictRule.SETTLED_OVER_ESTIMATE,
                    winner="scheduled retry event row",
                    loser=fact.source_id,
                    detail=(
                        "the ledger already carries the rescheduled debit; the "
                        "message adds no new cash movement"
                    ),
                )
            )

    for decision in conflicts:
        logger.info("conflict resolved %s", decision)
    for note in unquantified:
        logger.info("unquantified evidence (not applied): %s", note)

    return Amendments(
        salary=plan,
        extra_credits=tuple(extra_credits),
        series_multipliers=multipliers,
        series_stopped=stopped,
        conflicts=tuple(conflicts),
        unquantified=tuple(unquantified),
    )


# ---------------------------------------------------------------------------
# Series projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesExclusion:
    """One series kept out of the forecast, with everything a sweep needs."""

    key: str
    description: str
    category: str
    direction: str
    reason: ExclusionReason
    occurrences: int
    cadence_days: int | None
    cadence_kind: str
    last_occurrence: date | None
    periods_since_last: float | None
    detail: str

    def __str__(self) -> str:
        last = self.last_occurrence.isoformat() if self.last_occurrence else "never"
        periods = (
            f"{self.periods_since_last:.1f}" if self.periods_since_last is not None else "n/a"
        )
        return (
            f"[{self.reason}] {self.description!r} ({self.category}/{self.direction}) "
            f"n={self.occurrences} cadence={self.cadence_days}d "
            f"last={last} periods_since={periods} -- {self.detail}"
        )


def _add_months(anchor: date, months: int, day: int) -> date:
    """Move ``months`` forward, clamping the day into the target month."""
    total = anchor.month - 1 + months
    year = anchor.year + total // 12
    month = total % 12 + 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _series_amount(series: RecurringSeries) -> Decimal | None:
    """The statistic representing this series' future amount."""
    estimator = config.series_amount_estimator()
    if estimator == "mean":
        return series.mean_amount
    if estimator == "max":
        return series.max_amount
    return series.median_amount


def _project_series(
    series: RecurringSeries, start: date, end: date
) -> list[tuple[date, Decimal]]:
    """Dates and amounts this series is expected to produce in the window."""
    amount = _series_amount(series)
    if amount is None or series.last_date is None:
        return []

    occurrences: list[tuple[date, Decimal]] = []
    if series.cadence_kind == "monthly":
        anchor_day = series.day_of_month or series.last_date.day
        cursor = _add_months(series.last_date, 1, anchor_day)
        # Walk forward a bounded number of steps; the window is 90 days.
        for _ in range(24):
            if cursor > end:
                break
            if cursor >= start:
                occurrences.append((cursor, amount))
            cursor = _add_months(cursor, 1, anchor_day)
    else:
        step = series.cadence_days
        if not step or step <= 0:
            return []
        cursor = series.last_date + timedelta(days=step)
        for _ in range(400):
            if cursor > end:
                break
            if cursor >= start:
                occurrences.append((cursor, amount))
            cursor = cursor + timedelta(days=step)
    return occurrences


def _series_gate(
    series: RecurringSeries, as_of: date
) -> tuple[bool, SeriesExclusion | None]:
    """Decide whether a recurring series is projected forward."""
    occurrences = len(series.contributing)
    periods: float | None = None
    if series.last_date and series.cadence_days:
        periods = (as_of - series.last_date).days / series.cadence_days

    def exclude(reason: ExclusionReason, detail: str) -> tuple[bool, SeriesExclusion]:
        return False, SeriesExclusion(
            key=series.key,
            description=series.description,
            category=series.category,
            direction=series.direction,
            reason=reason,
            occurrences=occurrences,
            cadence_days=series.cadence_days,
            cadence_kind=str(series.cadence_kind),
            last_occurrence=series.last_date,
            periods_since_last=periods,
            detail=detail,
        )

    if series.median_amount is None:
        return exclude(
            ExclusionReason.NO_AMOUNT,
            "no occurrence carries an amount, so there is nothing to project",
        )
    if series.cadence_days is None:
        return exclude(ExclusionReason.NO_CADENCE, "no cadence was inferred")

    minimum = config.min_series_occurrences()
    if occurrences < minimum:
        return exclude(
            ExclusionReason.TOO_FEW_OCCURRENCES,
            f"{occurrences} occurrence(s) is below MIN_SERIES_OCCURRENCES={minimum}",
        )

    limit = config.series_staleness_periods()
    if limit is not None and periods is not None and periods > limit:
        return exclude(
            ExclusionReason.STALE,
            (
                f"last seen {periods:.1f} cadence periods ago, beyond "
                f"SERIES_STALENESS_PERIODS={limit}; treated as no longer running"
            ),
        )
    return True, None


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trough:
    """A local minimum of the projected balance."""

    on: date
    balance: Decimal
    #: Last day of the plateau when the minimum spans several days.
    until: date

    def __str__(self) -> str:
        span = "" if self.until == self.on else f"..{self.until.isoformat()}"
        return f"{self.on.isoformat()}{span}: {self.balance}"


@dataclass(frozen=True)
class Forecast:
    """Daily projected balance over the horizon."""

    user_id: str
    request_id: str
    start: date
    end: date
    opening_balance: Decimal
    minimum_balance_to_keep: Decimal
    #: One entry per day in ``[start, end]``, inclusive.
    balances: tuple[tuple[date, Decimal], ...]
    flows: tuple[CashFlow, ...]
    excluded_series: tuple[SeriesExclusion, ...]
    projected_series: tuple[str, ...]
    amendments: Amendments

    def __post_init__(self) -> None:
        if not self.balances:
            raise ForecastError("forecast produced no days")

    def balance_on(self, day: date) -> Decimal:
        """Projected closing balance on ``day``, clamped to the window."""
        if day <= self.start:
            return self.balances[0][1]
        if day >= self.end:
            return self.balances[-1][1]
        index = (day - self.start).days
        return self.balances[index][1]

    def flows_on(self, day: date) -> tuple[CashFlow, ...]:
        return tuple(flow for flow in self.flows if flow.on == day)

    @property
    def minimum(self) -> Decimal:
        return min(balance for _, balance in self.balances)

    def minimum_on_or_after(self, day: date) -> Decimal:
        """The lowest projected balance from ``day`` to the end of the horizon.

        This, not the balance on the payment day, is what safety turns on.
        """
        relevant = [balance for on, balance in self.balances if on >= day]
        if not relevant:
            return self.balances[-1][1]
        return min(relevant)

    def troughs(self) -> tuple[Trough, ...]:
        return troughs(self)

    def binding_trough(self, day: date) -> Trough | None:
        """The lowest trough at or after ``day`` -- the constraint that binds."""
        candidates = [t for t in self.troughs() if t.until >= day]
        if not candidates:
            return None
        return min(candidates, key=lambda t: (t.balance, t.on))

    def is_safe_from(self, day: date) -> bool:
        return self.minimum_on_or_after(day) >= self.minimum_balance_to_keep

    def summary(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "window": f"{self.start.isoformat()}..{self.end.isoformat()}",
            "opening_balance": str(self.opening_balance),
            "minimum_balance_to_keep": str(self.minimum_balance_to_keep),
            "closing_balance": str(self.balances[-1][1]),
            "minimum": str(self.minimum),
            "troughs": [str(t) for t in self.troughs()],
            "flows": len(self.flows),
            "projected_series": len(self.projected_series),
            "excluded_series": len(self.excluded_series),
            "conflicts": len(self.amendments.conflicts),
        }


def troughs(forecast: Forecast) -> tuple[Trough, ...]:
    """Every local minimum of the projected balance, with its date.

    A plateau of equal values counts once, reported at its first day with
    ``until`` marking its last. The final day is always a candidate: the
    horizon can end mid-decline, and that dip is as binding as any interior
    one.
    """
    balances = forecast.balances
    if not balances:
        return ()

    result: list[Trough] = []
    index = 0
    count = len(balances)
    while index < count:
        start_index = index
        value = balances[index][1]
        while index + 1 < count and balances[index + 1][1] == value:
            index += 1
        end_index = index

        descends_into = start_index == 0 or balances[start_index - 1][1] > value
        ascends_out = end_index == count - 1 or balances[end_index + 1][1] > value
        if descends_into and ascends_out:
            result.append(
                Trough(
                    on=balances[start_index][0],
                    balance=value,
                    until=balances[end_index][0],
                )
            )
        index = end_index + 1
    return tuple(result)


def _salary_flows(
    plan: SalaryPlan, start: date, end: date
) -> list[CashFlow]:
    """Project salary credits across the horizon."""
    if not plan.is_known:
        return []

    flows: list[CashFlow] = []
    paydays: list[date] = []

    if plan.next_payday_date and start < plan.next_payday_date <= end:
        paydays.append(plan.next_payday_date)

    if plan.cadence_kind == "fixed_interval" and plan.cadence_days and plan.anchor_date:
        # Weekly and fortnightly income steps from the last observed payment.
        # Projecting it as monthly would drop three quarters of the stream.
        cursor = plan.anchor_date + timedelta(days=plan.cadence_days)
        for _ in range(400):
            if cursor > end:
                break
            if cursor > start and cursor not in paydays:
                paydays.append(cursor)
            cursor = cursor + timedelta(days=plan.cadence_days)
    else:
        cursor = _add_months(start, 0, plan.day_of_month)
        if cursor <= start:
            cursor = _add_months(start, 1, plan.day_of_month)
        for _ in range(6):
            if cursor > end:
                break
            if cursor not in paydays:
                paydays.append(cursor)
            cursor = _add_months(cursor, 1, plan.day_of_month)

    paydays.sort()
    for index, payday in enumerate(paydays):
        if plan.ends_on is not None and payday >= plan.ends_on:
            continue
        amount = plan.amount
        if index == 0 and plan.next_payday_amount is not None:
            amount = plan.next_payday_amount
        if plan.resumes_on is not None and payday >= plan.resumes_on:
            amount = plan.resumes_amount or plan.amount
        if amount is None:
            continue
        flows.append(
            CashFlow(
                on=payday,
                amount=amount,
                direction="credit",
                event_id=f"projected_salary_{payday.isoformat()}",
                category="salary",
                description="projected salary",
                source="projection",
            )
        )
    return flows


def _orphan_expense_flows(
    state: BalanceState,
    start: date,
    end: date,
    projected_keys: set[str],
) -> list[CashFlow]:
    """Project variable spending that description-keying left unprojected.

    Every user in the dataset fragments their grocery, dining and transport
    spend across many description variants -- one user's transport shows up as
    nine separate one- and two-occurrence series. None passes the cadence test
    individually, so all of it vanishes from the forecast even though the user
    demonstrably spends that money every week.

    Projecting each fragment on a snapped cadence over-counts badly (measured:
    median run-rate ratio 1.01 -> 1.41). Instead this measures the observed
    **daily rate** per category over a trailing window and re-emits it at the
    observed spacing. A rate is self-calibrating: it cannot invent more
    spending than the user actually did.

    Only categories the user genuinely spends in are projected, and only
    events that no recurring series already covers -- so nothing is
    double-counted.
    """
    if not config.aggregate_orphan_expenses():
        return []

    window = config.orphan_expense_window_days()
    lo = start - timedelta(days=window)

    by_category: dict[str, list] = {}
    for item in state.classified:
        event = item.event
        if item.is_excluded or event.direction != "debit":
            continue
        if event.amount_home is None or event.status != "settled":
            continue
        if not (lo < event.cash_date <= start):
            continue
        if item.series_key in projected_keys:
            continue
        by_category.setdefault(event.category, []).append(event)

    flows: list[CashFlow] = []
    for category, events in sorted(by_category.items()):
        if len(events) < config.orphan_expense_min_events():
            continue
        events.sort(key=lambda e: e.cash_date)
        observed = sum((e.amount_home for e in events), Decimal(0))
        span = (events[-1].cash_date - events[0].cash_date).days
        if span <= 0:
            continue

        # Daily rate over the span the events actually cover.
        daily = observed / Decimal(span)
        # Re-emit at the observed average spacing, so timing stays realistic
        # rather than collapsing into one lump.
        spacing = max(1, round(span / max(1, len(events) - 1)))
        per_payment = quantize(daily * Decimal(spacing))
        if per_payment <= 0:
            continue

        cursor = start + timedelta(days=spacing)
        while cursor <= end:
            flows.append(
                CashFlow(
                    on=cursor,
                    amount=per_payment,
                    direction="debit",
                    event_id=f"orphan_{category}_{cursor.isoformat()}",
                    category=category,
                    description=f"{category} variable spending",
                    source="orphan_rate",
                )
            )
            cursor += timedelta(days=spacing)
        logger.debug(
            "%s: projected %s variable spend at %s every %d days "
            "(%d unprojected events over %d days)",
            state.request_id,
            category,
            per_payment,
            spacing,
            len(events),
            span,
        )
    return flows


def _plain_salary_plan(state: BalanceState) -> SalaryPlan:
    """The income plan with no evidence applied."""
    amount, day, cadence_days, cadence_kind, anchor = _income_plan_inputs(state)
    return SalaryPlan(
        amount=amount,
        day_of_month=day,
        cadence_days=cadence_days,
        cadence_kind=cadence_kind,
        anchor_date=anchor,
    )


def build_forecast(
    state: BalanceState,
    request_date: date | None = None,
    horizon: int | None = None,
    *,
    evidence: Evidence | None = None,
    resolved_amounts: Mapping[str, Decimal] | None = None,
) -> Forecast:
    """Project a daily balance over the safety window.

    Inputs, in the order they are applied to the opening balance: recurring
    expense series that pass the staleness gate, extrapolated salary, scheduled
    future payments, and pending debits.

    Raises :class:`~src.extract.UnresolvedBlankAmountError` if a committed
    future movement still has no amount. A blank is never forecast as zero.
    """
    start = request_date or state.as_of
    days = horizon if horizon is not None else config.forecast_horizon_days()
    end = start + timedelta(days=days)

    amendments = (
        apply_amendments(state, evidence)
        if evidence is not None
        else Amendments(
            salary=_plain_salary_plan(state),
            extra_credits=(),
            series_multipliers={},
            series_stopped={},
            conflicts=(),
            unquantified=(),
        )
    )

    resolved = dict(resolved_amounts or {})
    if evidence is not None:
        resolved.update(evidence.resolved_amounts)

    flows: list[CashFlow] = []

    # 1. Committed movements already on the ledger.
    committed_dates: set[tuple[str, date]] = set()
    for classified in state.classified:
        if not classified.is_future_cash:
            continue
        event = classified.event
        if event.cash_date <= start or event.cash_date > end:
            continue
        amount = event.amount_home
        if amount is None:
            amount = resolved.get(event.event_id)
        if amount is None:
            raise UnresolvedBlankAmountError(
                event.event_id,
                f"needed for the {state.request_id} forecast as a "
                f"{classified.cash_class} on {event.cash_date.isoformat()}",
            )
        flows.append(
            CashFlow(
                on=event.cash_date,
                amount=amount,
                direction=event.direction,
                event_id=event.event_id,
                category=event.category,
                description=event.description,
                source=classified.cash_class.value,
            )
        )
        if classified.series_key:
            committed_dates.add((classified.series_key, event.cash_date))

    # 2. Recurring series, gated on staleness and occurrence count.
    salary_series = _salary_series(state)
    excluded: list[SeriesExclusion] = []
    projected: list[str] = []
    for series in state.recurring_series:
        if salary_series is not None and series.key == salary_series.key:
            # Salary is projected by the amended plan, not as a raw series.
            continue
        keep, exclusion = _series_gate(series, start)
        if not keep:
            assert exclusion is not None
            excluded.append(exclusion)
            continue
        multiplier = amendments.multiplier_for(series.key)
        stop_date = amendments.series_stopped.get(series.key)
        occurrences = _project_series(series, start + timedelta(days=1), end)
        if not occurrences:
            continue
        projected.append(series.key)
        for on, amount in occurrences:
            if stop_date is not None and on >= stop_date:
                continue
            if (series.key, on) in committed_dates:
                # A scheduled event row already covers this occurrence.
                continue
            flows.append(
                CashFlow(
                    on=on,
                    amount=quantize(amount * multiplier),
                    direction=series.direction,
                    event_id=f"projected_{series.key}_{on.isoformat()}",
                    category=series.category,
                    description=series.description,
                    source="projection",
                )
            )

    # 2b. Variable spending that fragments across description variants.
    flows.extend(_orphan_expense_flows(state, start, end, set(projected)))

    for exclusion in excluded:
        logger.info("series excluded from %s forecast: %s", state.request_id, exclusion)

    # 3. Salary and amended credits.
    flows.extend(_salary_flows(amendments.salary, start, end))
    flows.extend(
        flow for flow in amendments.extra_credits if start < flow.on <= end
    )

    flows.sort(key=lambda f: (f.on, id_sort_key(f.event_id)))

    # 4. Walk the window day by day.
    by_day: dict[date, Decimal] = {}
    for flow in flows:
        by_day[flow.on] = by_day.get(flow.on, Decimal(0)) + flow.signed

    balances: list[tuple[date, Decimal]] = []
    running = state.available_balance
    for offset in range(days + 1):
        day = start + timedelta(days=offset)
        running = running + by_day.get(day, Decimal(0))
        balances.append((day, running))

    forecast = Forecast(
        user_id=state.user_id,
        request_id=state.request_id,
        start=start,
        end=end,
        opening_balance=state.available_balance,
        minimum_balance_to_keep=state.minimum_balance_to_keep,
        balances=tuple(balances),
        flows=tuple(flows),
        excluded_series=tuple(excluded),
        projected_series=tuple(projected),
        amendments=amendments,
    )
    logger.debug("forecast: %s", forecast.summary())
    return forecast


def with_payments(
    forecast: Forecast, payments: Sequence[tuple[date, Decimal]]
) -> Forecast:
    """Re-walk the same window with extra outgoing payments applied.

    Used by :mod:`src.capacity` and :mod:`src.candidates` to test whether a
    candidate plan survives the safety check.
    """
    extra: list[CashFlow] = [
        CashFlow(
            on=on,
            amount=amount,
            direction="debit",
            event_id=f"candidate_payment_{index}_{on.isoformat()}",
            category="request",
            description="proposed payment",
            source="candidate",
        )
        for index, (on, amount) in enumerate(payments)
    ]
    flows = sorted(
        [*forecast.flows, *extra], key=lambda f: (f.on, id_sort_key(f.event_id))
    )

    by_day: dict[date, Decimal] = {}
    for flow in flows:
        by_day[flow.on] = by_day.get(flow.on, Decimal(0)) + flow.signed

    balances: list[tuple[date, Decimal]] = []
    running = forecast.opening_balance
    day_count = (forecast.end - forecast.start).days
    for offset in range(day_count + 1):
        day = forecast.start + timedelta(days=offset)
        running = running + by_day.get(day, Decimal(0))
        balances.append((day, running))

    return Forecast(
        user_id=forecast.user_id,
        request_id=forecast.request_id,
        start=forecast.start,
        end=forecast.end,
        opening_balance=forecast.opening_balance,
        minimum_balance_to_keep=forecast.minimum_balance_to_keep,
        balances=tuple(balances),
        flows=tuple(flows),
        excluded_series=forecast.excluded_series,
        projected_series=forecast.projected_series,
        amendments=forecast.amendments,
    )


def flow_series_key(flow: CashFlow) -> str:
    """Series key for a projected flow, matching ``state._series_key``."""
    return f"{flow.description}|{flow.direction}"


def apply_series_changes(
    forecast: Forecast,
    *,
    stopped: Sequence[str] = (),
    reduced_to: Mapping[str, Decimal] | None = None,
) -> Forecast:
    """Re-walk the window with recurring series stopped or reduced.

    A spending change alters a series for the **remainder of the forecast**,
    not just one occurrence, so every projected occurrence of that series is
    dropped (``stopped``) or rewritten to the new amount (``reduced_to``) and
    the balance is walked again from scratch. Only projected flows are
    touched; a scheduled ledger row is a committed payment and a spending
    change does not retract it.
    """
    stop_keys = set(stopped)
    reductions = dict(reduced_to or {})

    flows: list[CashFlow] = []
    for flow in forecast.flows:
        if flow.source != "projection":
            flows.append(flow)
            continue
        key = flow_series_key(flow)
        if key in stop_keys:
            continue
        if key in reductions:
            flows.append(
                CashFlow(
                    on=flow.on,
                    amount=quantize(reductions[key]),
                    direction=flow.direction,
                    event_id=flow.event_id,
                    category=flow.category,
                    description=flow.description,
                    source="projection_reduced",
                )
            )
            continue
        flows.append(flow)

    by_day: dict[date, Decimal] = {}
    for flow in flows:
        by_day[flow.on] = by_day.get(flow.on, Decimal(0)) + flow.signed

    balances: list[tuple[date, Decimal]] = []
    running = forecast.opening_balance
    day_count = (forecast.end - forecast.start).days
    for offset in range(day_count + 1):
        day = forecast.start + timedelta(days=offset)
        running = running + by_day.get(day, Decimal(0))
        balances.append((day, running))

    return Forecast(
        user_id=forecast.user_id,
        request_id=forecast.request_id,
        start=forecast.start,
        end=forecast.end,
        opening_balance=forecast.opening_balance,
        minimum_balance_to_keep=forecast.minimum_balance_to_keep,
        balances=tuple(balances),
        flows=tuple(flows),
        excluded_series=forecast.excluded_series,
        projected_series=forecast.projected_series,
        amendments=forecast.amendments,
    )


def payment_is_safe(
    forecast: Forecast, payments: Sequence[tuple[date, Decimal]]
) -> bool:
    """Whether a payment schedule keeps every later trough above the minimum.

    The balance on the payment day is not the test. A payment can leave a
    comfortable balance that a pre-salary trough two weeks later takes below
    the floor, and that plan is unsafe.
    """
    if not payments:
        return forecast.is_safe_from(forecast.start)
    projected = with_payments(forecast, payments)
    first = min(on for on, _ in payments)
    return projected.minimum_on_or_after(first) >= forecast.minimum_balance_to_keep

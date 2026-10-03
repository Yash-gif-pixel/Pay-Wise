"""Spending-change selection (rulebook section 3).

Chooses up to three ``stop:<event_id>`` / ``reduce_to:<event_id>:<amount>``
actions that make an otherwise unsafe plan safe.

The rulebook marks this section ``[VAL]`` -- fully determined -- so the rules
below are implemented **literally** rather than inferred:

* Eligibility is a two-layer gate: the event's own ``flexibility`` field *and*
  its category appearing in the user's willing-to-reduce or willing-to-stop
  list.
* The action follows from ``flexibility``: ``reducible`` and
  ``reducible_or_stoppable`` reduce, ``stoppable`` stops. Reduce is always
  preferred over stop.
* The ``reduce_to`` target is the event's own ``minimum_allowed_amount``,
  taken from the data and never computed.
* The referenced event is the latest settled occurrence before
  ``request_date``.
* Selection is greedy in event order and stops at the first sufficient set.
  This is deliberately **not** fewest-changes and **not** largest-freed-first.

A change alters a series for the remainder of the forecast, so after each
addition the forecast is rebuilt and the plan re-tested against **every**
trough -- never just the balance on ``request_date``.

Two divergences from the brief were found in the data and are reported rather
than resolved; see :data:`KNOWN_DIVERGENCES` and C7 in the calibration log.

VALIDATION STATUS
-----------------
**Given the labelled deficit, the selection rule reproduces 2 of the 3
labelled change rows; end to end it reproduces 0 of 3, because the forecast
finds no deficit for any of them.** Both halves are needed and neither should
be read alone: the rule is sound and the pipeline still emits ``none`` for all
three.

*The rule, fed the right deficit.* Applying ``request_21``'s own labelled
deficit of 31.05 reproduces its labelled set
``stop:event_1815|reduce_to:event_1816:23.50`` verbatim, skipping the larger
single shopping reduce exactly as the rulebook describes (C7a);
``request_06`` reproduces as ``stop:event_476``. Both are pinned by tests in
``tests/test_changes.py``. ``request_11`` does **not**, and no ordering tried
reproduces all three -- see :data:`KNOWN_DIVERGENCES`, which records that its
labelled target is a two-occurrence series the rulebook's own recurring-only
rule forbids changing.

*End to end.* All three rows currently render ``spending_changes_needed =
none``, because :mod:`src.forecast` reports no deficit to rescue. So "2 of 3"
is a statement about this module in isolation, never about ``output.csv``.

**It under-generates: 3 of 250 rows against ~30 expected.** That is a capacity
problem, not a selection problem -- rows that should need a change look already
safe because :mod:`src.forecast` over-states capacity upstream. The three
labelled change rows sit only 2-5% away from flipping (P11-S3b). Fixing the
forecast should recover them; **do not relax the gates here to force it**.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Sequence

from . import config
from .forecast import Forecast, apply_series_changes, payment_is_safe
from .load import UserContext, id_sort_key
from .state import BalanceState, RecurringSeries

logger = logging.getLogger(__name__)

#: Hard ceiling from the output contract.
MAX_CHANGES = 3


class Action(StrEnum):
    STOP = "stop"
    REDUCE_TO = "reduce_to"


#: Flexibility values that permit each action. ``reducible_or_stoppable``
#: permits both, and reduce wins.
_REDUCIBLE = frozenset({"reducible", "reducible_or_stoppable"})
_STOPPABLE = frozenset({"stoppable", "reducible_or_stoppable"})


KNOWN_DIVERGENCES = """\
1. request_11's ground truth changes event_989 ('Weekend food delivery'),
   whose series has two occurrences 126 days apart. That is not recurring by
   any reasonable test, yet the rulebook states only recurring expenses may be
   changed. Because a non-recurring series has no future occurrences, changing
   it frees nothing in a 90-day forecast, so this selection cannot reproduce
   that row.

2. Greedy-in-event-order reproduces request_06 and request_21 but not
   request_11, where ground truth selects the last eligible event by id rather
   than the first. No single ordering found reproduces all three.
"""


@dataclass(frozen=True)
class SpendingChange:
    """One selected change, with everything needed to render and audit it."""

    action: Action
    event_id: str
    new_amount: Decimal | None
    series_key: str
    category: str
    description: str
    flexibility: str
    #: Projected amount per occurrence before the change.
    current_amount: Decimal
    #: Amount freed per future occurrence.
    freed_per_occurrence: Decimal

    def render(self) -> str:
        """``stop:event_476`` or ``reduce_to:event_1816:23.50``.

        Whole amounts render bare and fractional amounts to two places, which
        is how the labelled rows are written (``665950`` and ``23.50``).
        """
        if self.action is Action.STOP:
            return f"stop:{self.event_id}"
        assert self.new_amount is not None
        text = f"{self.new_amount:.2f}"
        if text.endswith(".00"):
            text = text[:-3]
        return f"reduce_to:{self.event_id}:{text}"


@dataclass(frozen=True)
class ChangeCandidate:
    """An eligible change, before selection."""

    change: SpendingChange
    sort_key: tuple[str, int, str]


@dataclass(frozen=True)
class ChangeSelection:
    """The outcome of a selection attempt."""

    changes: tuple[SpendingChange, ...]
    sufficient: bool
    considered: tuple[SpendingChange, ...]
    reason: str

    @property
    def rendered(self) -> tuple[str, ...]:
        return tuple(change.render() for change in self.changes)

    def trace(self) -> dict[str, object]:
        return {
            "selected": list(self.rendered),
            "sufficient": self.sufficient,
            "considered": [c.render() for c in self.considered],
            "reason": self.reason,
        }


def _latest_settled(series: RecurringSeries, as_of: date):
    """Latest settled occurrence on or before ``as_of``, or ``None``."""
    settled = [
        occurrence
        for occurrence in series.occurrences
        if occurrence.event.status == "settled"
        and occurrence.event.cash_date <= as_of
    ]
    if not settled:
        return None
    return max(
        settled,
        key=lambda o: (o.event.cash_date, id_sort_key(o.event_id)),
    )


def eligible_changes(
    context: UserContext, state: BalanceState
) -> tuple[ChangeCandidate, ...]:
    """Every change the two-layer gate permits, in event order.

    Layer one is the event's own ``flexibility``; layer two is the category
    appearing in the user's willing-to-reduce or willing-to-stop list.

    NOTE: the second layer is **redundant on this dataset** (measured in
    Phase 7, C7c). Every one of the 2,907 reduce-capable events sits in a permitted reduce category and every
    one of the 1,522 stop-capable events in a permitted stop category, and no
    flexible event sits in a protected category. Both layers are applied
    anyway, as instructed, so a future dataset that breaks the alignment is
    handled correctly.
    """
    profile = context.profile
    as_of = state.as_of
    candidates: list[ChangeCandidate] = []

    for series in state.series:
        if config.changes_require_recurring() and not series.is_recurring:
            continue

        occurrence = _latest_settled(series, as_of)
        if occurrence is None:
            continue
        event = occurrence.event
        flexibility = event.flexibility
        if flexibility == "fixed":
            continue

        can_reduce = (
            flexibility in _REDUCIBLE
            and series.category in profile.expense_categories_user_is_willing_to_reduce
        )
        can_stop = (
            flexibility in _STOPPABLE
            and series.category in profile.expense_categories_user_is_willing_to_stop
        )

        current = series.median_amount
        if current is None:
            continue

        # Reduce is always preferred over stop.
        if can_reduce:
            # TARGET IS THE DATA'S OWN minimum_allowed_amount, never computed.
            # Both labelled reduce rows reduce to exactly this value
            # (event_989 -> 665,950 and event_1816 -> 23.50).
            target = event.minimum_allowed_amount_home
            if target is None:
                logger.warning(
                    "%s is reducible but carries no minimum_allowed_amount",
                    event.event_id,
                )
                continue
            if target >= current:
                continue
            change = SpendingChange(
                action=Action.REDUCE_TO,
                event_id=event.event_id,
                new_amount=target,
                series_key=series.key,
                category=series.category,
                description=series.description,
                flexibility=flexibility,
                current_amount=current,
                freed_per_occurrence=current - target,
            )
        elif can_stop:
            change = SpendingChange(
                action=Action.STOP,
                event_id=event.event_id,
                new_amount=None,
                series_key=series.key,
                category=series.category,
                description=series.description,
                flexibility=flexibility,
                current_amount=current,
                freed_per_occurrence=current,
            )
        else:
            continue

        candidates.append(
            ChangeCandidate(change=change, sort_key=id_sort_key(event.event_id))
        )

    # GREEDY IN EVENT-ID ORDER, not fewest-changes and not largest-freed-first.
    # Validated against request_21: applying its own labelled deficit of 31.05
    # selects stop:event_1815 + reduce_to:event_1816:23.50 verbatim, skipping
    # event_1817 which frees 71.14/occurrence -- more than the chosen pair
    # combined -- exactly as the rulebook describes.
    return tuple(sorted(candidates, key=lambda c: c.sort_key))


def select_changes(
    context: UserContext,
    state: BalanceState,
    forecast: Forecast,
    payments: Sequence[tuple[date, Decimal]],
) -> ChangeSelection:
    """Greedily add changes in event order until the plan is safe.

    Minimal *sufficient* set, not minimal cardinality: changes are taken in
    event order and the loop stops the moment the plan clears, even if a later
    single change would have sufficed alone. Capped at three.

    After each addition the forecast is rebuilt with the series altered for the
    remainder of the window, and the whole payment schedule is re-tested
    against every trough.
    """
    candidates = eligible_changes(context, state)
    considered = tuple(c.change for c in candidates)

    if payment_is_safe(forecast, payments):
        return ChangeSelection(
            changes=(),
            sufficient=True,
            considered=considered,
            reason="plan is already safe; no change needed",
        )
    if not candidates:
        return ChangeSelection(
            changes=(),
            sufficient=False,
            considered=(),
            reason="no event passes the flexibility and category gate",
        )

    selected: list[SpendingChange] = []
    for candidate in candidates:
        if len(selected) >= MAX_CHANGES:
            break
        selected.append(candidate.change)

        stopped = [c.series_key for c in selected if c.action is Action.STOP]
        reduced = {
            c.series_key: c.new_amount
            for c in selected
            if c.action is Action.REDUCE_TO and c.new_amount is not None
        }
        # A change alters the series for the rest of the window, so the whole
        # forecast is rebuilt and every trough re-tested.
        adjusted = apply_series_changes(
            forecast, stopped=stopped, reduced_to=reduced
        )
        if payment_is_safe(adjusted, payments):
            logger.info(
                "%s: %d change(s) make the plan safe: %s",
                context.request_id,
                len(selected),
                [c.render() for c in selected],
            )
            return ChangeSelection(
                changes=tuple(selected),
                sufficient=True,
                considered=considered,
                reason=f"{len(selected)} change(s) clear every trough",
            )

    return ChangeSelection(
        changes=(),
        sufficient=False,
        considered=considered,
        reason=(
            f"{min(len(candidates), MAX_CHANGES)} change(s) applied, the most "
            f"the contract allows, and a trough still breaks the minimum"
        ),
    )


def find_spending_changes(
    context: UserContext,
    forecast: Forecast,
    payments: Sequence[tuple[date, Decimal]],
    *,
    state: BalanceState | None = None,
) -> tuple[str, ...]:
    """Adapter matching :class:`~src.candidates.SpendingChangeFinder`.

    Returns the rendered change strings, or an empty tuple when no permitted
    set makes the plan safe.
    """
    if state is None:
        from .state import reconstruct_balance

        state = reconstruct_balance(context, forecast.start)
    selection = select_changes(context, state, forecast, payments)
    return selection.rendered if selection.sufficient else ()


def change_finder_for(state: BalanceState):
    """Bind a reconstructed state into a finder, avoiding a second rebuild."""

    def finder(
        context: UserContext,
        forecast: Forecast,
        payments: Sequence[tuple[date, Decimal]],
    ) -> tuple[str, ...]:
        return find_spending_changes(context, forecast, payments, state=state)

    return finder

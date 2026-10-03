"""Plan ranking and status assignment (rulebook Steps 6 and 7).

Orders the safe, eligible candidates by a strict five-criterion chain and maps
the winner onto an ``affordability_status``.

The rulebook's first criterion -- *complete the full request by
``desired_completion_date``* -- is deliberately **absent** from the chain. It
is a hard filter applied in :mod:`src.candidates`, not a soft preference: a
plan that finishes late is not a worse plan, it is not a plan.
:func:`assert_deadline_filter_held` re-checks that invariant here so the two
modules cannot drift apart.

Ranking is **total**. If two candidates survive all five criteria the
specification has a gap, and :class:`RankingTieError` is raised rather than one
being picked arbitrarily.

VALIDATION STATUS
-----------------
**Never wrong when given a choice: 20/20.** Every labelled row whose ground-truth
method was generated as a candidate is ranked correctly.

**But barely exercised.** Across all 275 requests, 271 are settled by having
zero or one eligible candidate; only 4 reach a tie-break, all
``installments`` versus ``partial_payment`` decided on minimum total paid.
**Criteria 3, 4 and 5 have never fired on real data** and rest on synthetic
tests alone. Zero labelled rows have more than one candidate, so the 20/20
figure says only that a single candidate is never mis-picked.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Sequence

from . import paths
from .candidates import Candidate, CandidateSet, Method
from .capacity import CapacityAssessment
from .load import UserContext, id_sort_key

logger = logging.getLogger(__name__)

TRACE_DIR = paths.AUDIT_DIR / "traces"


class Status(StrEnum):
    """The four values ``affordability_status`` may take."""

    AFFORDABLE_NOW = "affordable_now"
    AFFORDABLE_WITH_PLAN = "affordable_with_plan"
    AFFORDABLE_LATER = "affordable_later"
    NOT_AFFORDABLE = "not_affordable"


class Criterion(StrEnum):
    """Which rule separated the winner from the runner-up."""

    NO_SPENDING_CHANGES = "requires_no_spending_changes"
    MINIMUM_TOTAL_PAID = "minimum_total_amount_paid"
    EARLIEST_FIRST_PAYMENT = "earliest_first_payment_date"
    FEWER_PAYMENTS = "fewer_payments"
    LOWEST_OPTION_ID = "lowest_payment_option_id"
    ONLY_CANDIDATE = "only_eligible_candidate"
    NO_CANDIDATE = "no_eligible_candidate"


class RankingTieError(Exception):
    """Two candidates are indistinguishable under all five criteria.

    Raised rather than resolved. A tie this deep means the ranking
    specification does not determine an answer, and silently picking one would
    hide the gap.
    """

    def __init__(self, request_id: str, first: Candidate, second: Candidate) -> None:
        self.request_id = request_id
        self.first = first
        self.second = second
        super().__init__(
            f"{request_id}: {first.method}/{first.payment_option_id} and "
            f"{second.method}/{second.payment_option_id} are equal on all five "
            f"ranking criteria. The specification does not determine a winner."
        )


class DeadlineFilterError(Exception):
    """An eligible candidate finishes after the deadline."""


#: Sentinel so a candidate with no ``payment_option_id`` orders before any
#: real option id. Criterion 5 only ever separates two installment options in
#: practice, since every other pairing is settled by criteria 1-4.
_NO_OPTION: tuple[str, int, str] = ("", -1, "")


def rank_key(candidate: Candidate) -> tuple:
    """The five criteria, in strict order, as a sortable tuple."""
    return (
        # 1. Requires no spending changes.
        candidate.requires_changes,
        # 2. Minimum total amount paid.
        candidate.total_paid,
        # 3. Earliest first payment date.
        candidate.first_payment_date or date.max,
        # 4. Fewer payments.
        candidate.payment_count,
        # 5. Lowest payment_option_id.
        id_sort_key(candidate.payment_option_id)
        if candidate.payment_option_id
        else _NO_OPTION,
    )


#: Positional mapping from ``rank_key`` index to the criterion it encodes.
_CRITERIA: tuple[Criterion, ...] = (
    Criterion.NO_SPENDING_CHANGES,
    Criterion.MINIMUM_TOTAL_PAID,
    Criterion.EARLIEST_FIRST_PAYMENT,
    Criterion.FEWER_PAYMENTS,
    Criterion.LOWEST_OPTION_ID,
)


def deciding_criterion(winner: Candidate, runner_up: Candidate) -> Criterion | None:
    """First criterion on which the winner beats the runner-up."""
    for index, criterion in enumerate(_CRITERIA):
        if rank_key(winner)[index] != rank_key(runner_up)[index]:
            return criterion
    return None


def assert_deadline_filter_held(
    context: UserContext, candidate_set: CandidateSet
) -> None:
    """Every eligible candidate already completes by the deadline.

    The filter lives in :mod:`src.candidates`; this is the assertion that it
    really did its job, so the deadline never has to be re-applied as a soft
    ranking criterion.
    """
    deadline = context.request.desired_completion_date
    for candidate in candidate_set.eligible:
        last = candidate.last_payment_date
        if last is not None and last > deadline:
            raise DeadlineFilterError(
                f"{context.request_id}: {candidate.method} finishes "
                f"{last.isoformat()}, after the {deadline.isoformat()} deadline. "
                f"The hard filter in candidates.py did not hold."
            )


def assign_status(
    winner: Candidate | None,
    capacity: CapacityAssessment,
    context: UserContext,
) -> Status:
    """Map the winning plan onto an affordability status.

    ``affordable_now`` requires the *winner* to be an unmodified full payment,
    not merely that capacity exists. ``earliest_date_for_full_payment``
    measures capacity independently of preference and can equal
    ``request_date`` while installments win, because the user will not consider
    paying in full -- so status is never derived from that date alone.
    """
    if winner is None or winner.method is Method.NOT_RECOMMENDED:
        return Status.NOT_AFFORDABLE
    if winner.method is Method.WAIT:
        return Status.AFFORDABLE_LATER
    if winner.method is Method.FULL_PAYMENT and not winner.requires_changes:
        # A full payment is only eligible when the whole amount is safe today,
        # which forces the earliest date to be the request date.
        assert (
            capacity.earliest_date_for_full_payment == context.request.request_date
        ), (
            f"{context.request_id}: full payment won but "
            f"earliest_date_for_full_payment is "
            f"{capacity.earliest_date_for_full_payment}, not the request date"
        )
        return Status.AFFORDABLE_NOW
    # full payment enabled by spending changes, partial, or installments
    return Status.AFFORDABLE_WITH_PLAN


@dataclass(frozen=True)
class Decision:
    """The ranked outcome for one request."""

    request_id: str
    user_id: str
    winner: Candidate
    status: Status
    criterion: Criterion
    runner_up: Candidate | None
    ordered: tuple[Candidate, ...]
    candidate_set: CandidateSet
    capacity: CapacityAssessment

    @property
    def method(self) -> Method:
        return self.winner.method

    def trace(self) -> dict[str, object]:
        def render(candidate: Candidate) -> dict[str, object]:
            return {
                "method": candidate.method.value,
                "payment_option_id": candidate.payment_option_id,
                "payments": [
                    [p.on.isoformat(), str(p.amount)] for p in candidate.payments
                ],
                "total_paid": str(candidate.total_paid),
                "requires_changes": candidate.requires_changes,
                "spending_changes": list(candidate.spending_changes),
                "payment_count": candidate.payment_count,
                "note": candidate.note,
            }

        return {
            "request_id": self.request_id,
            "user_id": self.user_id,
            "decision": {
                "method": self.method.value,
                "status": self.status.value,
                "decided_by": self.criterion.value,
                "runner_up": (
                    render(self.runner_up) if self.runner_up is not None else None
                ),
            },
            "capacity": self.capacity.trace(),
            "candidates_ranked": [render(c) for c in self.ordered],
            "rejections": [
                {
                    "method": r.method.value,
                    "payment_option_id": r.payment_option_id,
                    "reason": r.reason.value,
                    "detail": r.detail,
                }
                for r in self.candidate_set.rejections
            ],
        }

    def write_trace(self, directory: Path | None = None) -> Path:
        target_dir = directory or TRACE_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{self.request_id}.json"
        path.write_text(
            json.dumps(self.trace(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path


def rank(
    context: UserContext,
    candidate_set: CandidateSet,
    capacity: CapacityAssessment,
) -> Decision:
    """Order the eligible candidates and pick a winner.

    Raises :class:`RankingTieError` when the criteria do not determine one.
    """
    assert_deadline_filter_held(context, candidate_set)

    eligible = list(candidate_set.eligible)
    if not eligible:
        winner = candidate_set.fallback
        return Decision(
            request_id=context.request_id,
            user_id=context.user_id,
            winner=winner,
            status=Status.NOT_AFFORDABLE,
            criterion=Criterion.NO_CANDIDATE,
            runner_up=None,
            ordered=(),
            candidate_set=candidate_set,
            capacity=capacity,
        )

    ordered = sorted(eligible, key=rank_key)
    winner = ordered[0]
    runner_up = ordered[1] if len(ordered) > 1 else None

    if runner_up is None:
        criterion = Criterion.ONLY_CANDIDATE
    else:
        decided = deciding_criterion(winner, runner_up)
        if decided is None:
            raise RankingTieError(context.request_id, winner, runner_up)
        criterion = decided

    # Nothing further down the order may tie with the winner either.
    for other in ordered[1:]:
        if rank_key(other) == rank_key(winner):
            raise RankingTieError(context.request_id, winner, other)

    status = assign_status(winner, capacity, context)
    decision = Decision(
        request_id=context.request_id,
        user_id=context.user_id,
        winner=winner,
        status=status,
        criterion=criterion,
        runner_up=runner_up,
        ordered=tuple(ordered),
        candidate_set=candidate_set,
        capacity=capacity,
    )
    logger.info(
        "%s: %s / %s decided by %s (%d candidate(s))",
        context.request_id,
        decision.method,
        decision.status,
        criterion,
        len(ordered),
    )
    return decision

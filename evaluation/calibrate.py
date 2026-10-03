"""Calibration harness: run the pipeline over the 25 labelled samples and diff
every output column against ground truth.

Usage::

    py evaluation/calibrate.py                 # per-column exactness + diff table
    py evaluation/calibrate.py --quiet         # summary line only
    py evaluation/calibrate.py --column amount_safe_to_pay
    py evaluation/calibrate.py --full          # also run the 250 eval rows

Honours every ``BUYORWAIT_*`` environment override, so a sweep is a loop over
environment values rather than an edit.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import logging
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.disable(logging.CRITICAL)

COLUMNS = (
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)


def reload_pipeline():
    """Re-import ``src`` so module-level config constants are re-read."""
    for name in [m for m in list(sys.modules) if m == "src" or m.startswith("src.")]:
        del sys.modules[name]
    return tuple(
        importlib.import_module(f"src.{name}")
        for name in (
            "config",
            "load",
            "state",
            "extract",
            "forecast",
            "capacity",
            "candidates",
            "changes",
            "rank",
            "render",
        )
    )


@dataclass
class RowResult:
    request_id: str
    truth: dict[str, str]
    produced: dict[str, str]

    def matches(self, column: str) -> bool:
        return self.truth[column] == self.produced[column]

    @property
    def safe_error(self) -> Decimal:
        truth = Decimal(self.truth["amount_safe_to_pay"])
        mine = Decimal(self.produced["amount_safe_to_pay"])
        return abs(mine - truth)

    @property
    def safe_relative_error(self) -> Decimal:
        truth = Decimal(self.truth["amount_safe_to_pay"])
        if truth == 0:
            return Decimal(0) if self.safe_error == 0 else Decimal(1)
        return self.safe_error / truth


@dataclass
class Report:
    rows: list[RowResult]
    #: Request ids where the labelled method was never generated as a
    #: candidate -- ranking could not have chosen it.
    method_not_generated: set[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.method_not_generated is None:
            self.method_not_generated = set()

    def exact(self, column: str) -> int:
        return sum(1 for r in self.rows if r.matches(column))

    @property
    def all_columns_exact(self) -> int:
        return sum(
            1 for r in self.rows if all(r.matches(c) for c in COLUMNS)
        )

    @property
    def mean_absolute_relative_error(self) -> Decimal:
        if not self.rows:
            return Decimal(0)
        total = sum((r.safe_relative_error for r in self.rows), Decimal(0))
        return total / Decimal(len(self.rows))

    @property
    def overstating(self) -> int:
        return sum(
            1
            for r in self.rows
            if Decimal(r.produced["amount_safe_to_pay"])
            > Decimal(r.truth["amount_safe_to_pay"])
        )

    @property
    def understating(self) -> int:
        return sum(
            1
            for r in self.rows
            if Decimal(r.produced["amount_safe_to_pay"])
            < Decimal(r.truth["amount_safe_to_pay"])
        )

    def summary(self) -> str:
        return (
            f"safe {self.exact('amount_safe_to_pay')}/25  "
            f"status {self.exact('affordability_status')}/25  "
            f"method {self.exact('recommended_payment_method')}/25  "
            f"plan {self.exact('payment_plan')}/25  "
            f"earliest {self.exact('earliest_date_for_full_payment')}/25  "
            f"changes {self.exact('spending_changes_needed')}/25  "
            f"expl {self.exact('decision_explanation')}/25  "
            f"MARE {float(self.mean_absolute_relative_error):.3f}  "
            f"over {self.overstating} under {self.understating}"
        )


def solve_samples(modules=None) -> Report:
    if modules is None:
        modules = reload_pipeline()
    (config, load, state, extract, forecast, capacity, cand, changes, rank, render) = (
        modules
    )

    dataset = load.get_dataset()
    cache = extract.load_image_cache()
    rows: list[RowResult] = []
    never_generated: set[str] = set()

    for request in dataset.sample_requests:
        context = dataset.get_user_context(request.user_id, request.request_id)
        balance = state.reconstruct_balance(context)
        blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
        evidence = extract.gather_evidence(context, blank, cache=cache)
        projection = forecast.build_forecast(balance, evidence=evidence)
        assessment = capacity.assess_capacity(context, projection)
        candidate_set = cand.generate_candidates(
            context,
            projection,
            assessment,
            change_finder=changes.change_finder_for(balance),
        )
        decision = rank.rank(context, candidate_set, assessment)
        produced = render.render_decision(context, decision).as_dict()

        truth = request.expected
        generated = {c.method.value for c in candidate_set.eligible}
        if not (
            truth.recommended_payment_method in generated
            or (truth.recommended_payment_method == "not_recommended" and not generated)
        ):
            never_generated.add(request.request_id)
        rows.append(
            RowResult(
                request_id=request.request_id,
                truth={
                    "amount_safe_to_pay": render.format_minimal(
                        truth.amount_safe_to_pay
                    ),
                    "affordability_status": truth.affordability_status,
                    "recommended_payment_method": truth.recommended_payment_method,
                    "payment_plan": truth.payment_plan,
                    "earliest_date_for_full_payment": (
                        truth.earliest_date_for_full_payment.isoformat()
                        if truth.earliest_date_for_full_payment
                        else ""
                    ),
                    "spending_changes_needed": truth.spending_changes_needed,
                    "decision_explanation": truth.decision_explanation,
                },
                produced=produced,
            )
        )
    return Report(rows=rows, method_not_generated=never_generated)


def solve_full(modules=None) -> dict[str, object]:
    """Run the 250 evaluation rows and report the status distribution."""
    if modules is None:
        modules = reload_pipeline()
    import src.run as runner
    import src.validate as validator

    (config, load, state, extract, forecast, capacity, cand, changes, rank, render) = (
        modules
    )
    dataset = load.get_dataset()
    cache = extract.load_image_cache()

    statuses: dict[str, int] = {}
    with_changes = 0
    methods: dict[str, int] = {}
    for request in dataset.requests:
        row = runner.solve_one(dataset, request, cache)
        statuses[row.affordability_status] = (
            statuses.get(row.affordability_status, 0) + 1
        )
        methods[row.recommended_payment_method] = (
            methods.get(row.recommended_payment_method, 0) + 1
        )
        if row.spending_changes_needed != "none":
            with_changes += 1
    return {
        "statuses": statuses,
        "methods": methods,
        "rows_with_changes": with_changes,
        "expected": validator.EXPECTED_DISTRIBUTION,
    }


#: Failure classes, ranked by frequency in the summary block. Each is a
#: predicate over one scored row.
FAILURE_CLASSES: tuple[tuple[str, str], ...] = (
    ("capacity_over", "amount_safe_to_pay above the label"),
    ("capacity_under", "amount_safe_to_pay below the label"),
    ("explanation_mismatch", "decision_explanation differs from the label"),
    ("wrong_earliest_date", "earliest_date_for_full_payment differs"),
    ("wrong_plan", "payment_plan differs"),
    ("wrong_status", "affordability_status differs"),
    ("wrong_method", "recommended_payment_method differs"),
    ("method_not_generated", "labelled method was never a candidate"),
    ("wrong_changes", "spending_changes_needed differs"),
)


def classify_failures(report: Report) -> dict[str, list[str]]:
    """Bucket every scored row by the failure classes it exhibits."""
    buckets: dict[str, list[str]] = {name: [] for name, _ in FAILURE_CLASSES}
    for row in report.rows:
        mine = Decimal(row.produced["amount_safe_to_pay"])
        truth = Decimal(row.truth["amount_safe_to_pay"])
        if mine > truth:
            buckets["capacity_over"].append(row.request_id)
        elif mine < truth:
            buckets["capacity_under"].append(row.request_id)
        if not row.matches("decision_explanation"):
            buckets["explanation_mismatch"].append(row.request_id)
        if not row.matches("earliest_date_for_full_payment"):
            buckets["wrong_earliest_date"].append(row.request_id)
        if not row.matches("payment_plan"):
            buckets["wrong_plan"].append(row.request_id)
        if not row.matches("affordability_status"):
            buckets["wrong_status"].append(row.request_id)
        if not row.matches("recommended_payment_method"):
            buckets["wrong_method"].append(row.request_id)
            if row.request_id in report.method_not_generated:
                buckets["method_not_generated"].append(row.request_id)
        if not row.matches("spending_changes_needed"):
            buckets["wrong_changes"].append(row.request_id)
    return buckets


def print_failure_summary(report: Report) -> None:
    """The one block a reviewer should read first."""
    buckets = classify_failures(report)
    descriptions = dict(FAILURE_CLASSES)
    ranked = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    print("=" * 78)
    print("FAILURE BUCKETS, ranked by frequency (25 labelled rows)")
    print("=" * 78)
    print(f"{'count':>5}  {'class':<24} {'description':<44}")
    print(f"{'-'*5}  {'-'*24} {'-'*44}")
    for name, rows in ranked:
        if not rows:
            continue
        print(f"{len(rows):>5}  {name:<24} {descriptions[name]:<44}")
    print()
    worst = ranked[0]
    print(f"  largest bucket: {worst[0]} ({len(worst[1])} rows)")
    print(f"  rows fully correct on every column: {report.all_columns_exact}/25")
    print(
        f"  amount_safe_to_pay MARE: "
        f"{float(report.mean_absolute_relative_error):.4f}"
    )
    print()


def print_report(report: Report, *, column: str | None = None) -> None:
    print_failure_summary(report)
    print("per-column exactness (25 labelled rows)")
    print("-" * 58)
    for name in COLUMNS:
        count = report.exact(name)
        bar = "#" * count + "." * (25 - count)
        print(f"  {name:<34} {count:>2}/25  {bar}")
    print(f"  {'ALL COLUMNS':<34} {report.all_columns_exact:>2}/25")
    print()
    print(
        f"mean absolute relative error on amount_safe_to_pay: "
        f"{float(report.mean_absolute_relative_error):.4f}"
    )
    print(
        f"overstating {report.overstating}/25, understating "
        f"{report.understating}/25"
    )
    print()

    target = column or "amount_safe_to_pay"
    print(f"per-request diff ({target})")
    print("-" * 100)
    print(f"{'request':<13}{'ground truth':>18}{'produced':>18}{'rel err':>10}  cols wrong")
    for row in report.rows:
        wrong = [c for c in COLUMNS if not row.matches(c)]
        err = (
            f"{float(row.safe_relative_error) * 100:8.1f}%"
            if target == "amount_safe_to_pay"
            else ""
        )
        mark = "" if not wrong else " ".join(
            c.replace("_", "")[:9] for c in wrong
        )
        print(
            f"{row.request_id:<13}{row.truth[target]:>18}"
            f"{row.produced[target]:>18}{err:>10}  {mark}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="calibration harness")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--column", default=None, choices=COLUMNS)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args(argv)

    report = solve_samples()
    if args.quiet:
        print(report.summary())
    else:
        print_report(report, column=args.column)

    if args.full:
        print()
        print("full evaluation run (250 rows)")
        print("-" * 58)
        result = solve_full()
        total = sum(result["statuses"].values())
        for status, expected in result["expected"].items():
            count = result["statuses"].get(status, 0)
            actual = count / total
            print(
                f"  {status:<22} {count:>4}  {actual * 100:5.1f}%  "
                f"expected {expected * 100:4.0f}%  "
                f"({(actual - expected) * 100:+5.1f} pts)"
            )
        print(f"  rows with spending changes: {result['rows_with_changes']}/250")
        print(f"  methods: {result['methods']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

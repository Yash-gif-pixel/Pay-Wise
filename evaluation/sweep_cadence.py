"""P11-S6 -- sweep the four cadence constants, one at a time.

These four were the only load-bearing constants in :mod:`src.config` set to
make a synthetic test pass and never swept against the 25 labels:

* ``CADENCE_CONSISTENCY_THRESHOLD`` (0.6)
* ``CADENCE_SKIP_WEIGHT`` (0.5, the half weight in ``state._snap_cadence``)
* ``CADENCE_TOLERANCE_DAYS`` (1)
* ``MAX_SKIPPED_OCCURRENCES`` (3)

They gate recurrence detection, which drives the whole forecast, so leaving
them unmeasured was the one place the project's own standard was not applied.

Each is swept holding the other three fixed, reporting the same metric set
every other Phase 11 step used: MARE and per-column exactness against the 25
labels, the median outflow run-rate ratio, the full-250 status distribution,
and the recurring-series count across all 275 users.

Judging rule, unchanged from P11-S2: continuous metrics (MARE, run-rate ratio)
beat discrete exact-counts when they disagree, and a default does not move on
exact-count alone.

Run::

    py -m evaluation.sweep_cadence            # all four
    py -m evaluation.sweep_cadence threshold  # one knob
"""

from __future__ import annotations

import os
import statistics
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evaluation.calibrate import (  # noqa: E402
    COLUMNS,
    Report,
    reload_pipeline,
    solve_samples,
)

ENV_PREFIX = "BUYORWAIT_"

#: ``(env name, current value, values to try)``. The current value is included
#: in every sweep so the confirming run is measured, not assumed.
KNOBS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "threshold": (
        "CADENCE_CONSISTENCY_THRESHOLD",
        "0.6",
        ("0.0", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "1.0"),
    ),
    "skipweight": (
        "CADENCE_SKIP_WEIGHT",
        "0.5",
        ("0.0", "0.25", "0.5", "0.75", "1.0"),
    ),
    "tolerance": (
        "CADENCE_TOLERANCE_DAYS",
        "1",
        ("0", "1", "2", "3"),
    ),
    "skips": (
        "MAX_SKIPPED_OCCURRENCES",
        "3",
        ("1", "2", "3", "4", "6"),
    ),
}


def outflow_run_rate(modules) -> tuple[float, float, float]:
    """``(median, min, max)`` of projected-over-observed 90-day outflow.

    Defined exactly as in P11-S1: projected 90-day outflow from the forecast
    over the settled debit total in the 90 days *before* ``request_date``, one
    ratio per sample user. Users with no observed outflow are skipped rather
    than counted as 0 or infinity.
    """
    (config, load, state, extract, forecast, capacity, cand, changes, rank, render) = (
        modules
    )
    dataset = load.get_dataset()
    cache = extract.load_image_cache()
    ratios: list[float] = []

    for request in dataset.sample_requests:
        context = dataset.get_user_context(request.user_id, request.request_id)
        balance = state.reconstruct_balance(context)
        blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
        evidence = extract.gather_evidence(context, blank, cache=cache)
        projection = forecast.build_forecast(balance, evidence=evidence)

        window_start = request.request_date - timedelta(days=90)
        observed = sum(
            (
                event.amount_home
                for event in context.events
                if event.direction == "debit"
                and event.status == "settled"
                and event.amount_home is not None
                and window_start <= event.cash_date <= request.request_date
            ),
            Decimal(0),
        )
        if observed <= 0:
            continue
        projected = sum(
            (
                flow.amount
                for flow in projection.flows
                if flow.direction == "debit"
                and request.request_date < flow.on <= projection.end
            ),
            Decimal(0),
        )
        ratios.append(float(projected / observed))

    if not ratios:
        return 0.0, 0.0, 0.0
    return statistics.median(ratios), min(ratios), max(ratios)


def recurring_series_count(modules) -> tuple[int, int]:
    """``(recurring, total)`` series across all 275 users."""
    (config, load, state, *_rest) = modules
    dataset = load.get_dataset()
    recurring = total = 0
    seen: set[str] = set()
    for request in list(dataset.requests) + list(dataset.sample_requests):
        if request.user_id in seen:
            continue
        seen.add(request.user_id)
        context = dataset.get_user_context(request.user_id, request.request_id)
        for series in state.reconstruct_balance(context).series:
            total += 1
            recurring += bool(series.is_recurring)
    return recurring, total


def full_distribution(modules) -> dict[str, int]:
    """Status counts over the 250 evaluation rows."""
    import src.run as runner

    (config, load, state, extract, *_rest) = modules
    dataset = load.get_dataset()
    cache = extract.load_image_cache()
    counts: dict[str, int] = {}
    for request in dataset.requests:
        try:
            row = runner.solve_one(dataset, request, cache)
        except Exception as exc:  # a setting that produces an invalid row
            counts["ERROR"] = counts.get("ERROR", 0) + 1
            counts.setdefault("_first_error", str(exc)[:120])  # type: ignore[arg-type]
            continue
        counts[row.affordability_status] = counts.get(row.affordability_status, 0) + 1
    return counts


def measure(env_name: str, value: str, *, full: bool) -> dict[str, object]:
    """One setting, end to end, from a clean re-import."""
    key = ENV_PREFIX + env_name
    previous = os.environ.get(key)
    os.environ[key] = value
    try:
        modules = reload_pipeline()
        report: Report = solve_samples(modules)
        median, low, high = outflow_run_rate(modules)
        recurring, total = recurring_series_count(modules)
        result: dict[str, object] = {
            "value": value,
            "mare": float(report.mean_absolute_relative_error),
            "safe": report.exact("amount_safe_to_pay"),
            "status": report.exact("affordability_status"),
            "method": report.exact("recommended_payment_method"),
            "plan": report.exact("payment_plan"),
            "earliest": report.exact("earliest_date_for_full_payment"),
            "changes": report.exact("spending_changes_needed"),
            "expl": report.exact("decision_explanation"),
            "over": report.overstating,
            "under": report.understating,
            "ratio_median": median,
            "ratio_min": low,
            "ratio_max": high,
            "recurring": recurring,
            "series_total": total,
            "per_row": {r.request_id: float(r.safe_relative_error) for r in report.rows},
        }
        if full:
            result["distribution"] = full_distribution(modules)
        return result
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def sweep(name: str, *, full: bool = True) -> list[dict[str, object]]:
    env_name, current, values = KNOBS[name]
    print(f"\n{'=' * 78}\n{env_name}  (current = {current})\n{'=' * 78}")
    header = (
        f"{'value':>7} | {'MARE':>6} | {'safe':>4} {'stat':>4} {'meth':>4} "
        f"{'plan':>4} {'earl':>4} {'chg':>4} {'expl':>4} | {'o/u':>5} | "
        f"{'ratio':>5} | {'recurring':>9} | distribution"
    )
    print(header)
    print("-" * len(header))

    rows: list[dict[str, object]] = []
    for value in values:
        r = measure(env_name, value, full=full)
        rows.append(r)
        dist = r.get("distribution", {})
        dist_text = (
            " ".join(
                f"{k[:4]}={v}"
                for k, v in sorted(dist.items())  # type: ignore[union-attr]
            )
            if dist
            else "-"
        )
        mark = " <- current" if value == current else ""
        print(
            f"{value:>7} | {r['mare']:>6.3f} | {r['safe']:>4} {r['status']:>4} "
            f"{r['method']:>4} {r['plan']:>4} {r['earliest']:>4} {r['changes']:>4} "
            f"{r['expl']:>4} | {r['over']:>2}/{r['under']:<2} | "
            f"{r['ratio_median']:>5.2f} | {r['recurring']:>4}/{r['series_total']:<4} | "
            f"{dist_text}{mark}"
        )

    baseline = next(r for r in rows if r["value"] == current)
    for r in rows:
        if r["value"] == current:
            continue
        moved = sorted(
            (
                (abs(v - baseline["per_row"][k]), k, baseline["per_row"][k], v)  # type: ignore[index]
                for k, v in r["per_row"].items()  # type: ignore[union-attr]
            ),
            reverse=True,
        )[:3]
        if moved and moved[0][0] > 0.01:
            detail = ", ".join(
                f"{k} {before:.2f}->{after:.2f}" for _d, k, before, after in moved
                if _d > 0.01
            )
            print(f"    {r['value']:>7}: largest per-row MARE moves -- {detail}")
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    full = "--samples-only" not in args
    args = [a for a in args if not a.startswith("--")]
    names = args or list(KNOBS)
    for name in names:
        if name not in KNOBS:
            print(f"unknown knob {name!r}; choose from {list(KNOBS)}")
            return 2
        sweep(name, full=full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

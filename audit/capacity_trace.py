"""Dump the per-request capacity trace for the Phase 11 calibration sweep.

Writes two artefacts:

``audit/capacity_trace.jsonl``
    One :meth:`~src.capacity.CapacityAssessment.trace` record per request,
    for all 275 requests. Carries the binding trough's date, balance and
    headroom so a miss can be attributed to *the wrong trough binding* versus
    *the right trough carrying a wrong balance*.

``audit/capacity_calibration.md``
    The 25 labelled sample rows scored against ground truth, with the error
    decomposed by direction.

Run from anywhere::

    py audit/capacity_trace.py
"""

from __future__ import annotations

import json
import logging
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.disable(logging.CRITICAL)

from src import capacity, extract, forecast, load, state  # noqa: E402

TRACE_PATH = REPO_ROOT / "audit" / "capacity_trace.jsonl"
REPORT_PATH = REPO_ROOT / "audit" / "capacity_calibration.md"


def main() -> int:
    dataset = load.get_dataset()
    cache = extract.load_image_cache()

    records: list[dict] = []
    sample_rows: list[dict] = []

    for request in list(dataset.requests) + list(dataset.sample_requests):
        context = dataset.get_user_context(request.user_id, request.request_id)
        balance = state.reconstruct_balance(context)
        blank = tuple(f.event_id for f in balance.blank_amounts if f.needs_image)
        evidence = extract.gather_evidence(context, blank, cache=cache)
        projection = forecast.build_forecast(balance, evidence=evidence)
        assessment = capacity.assess_capacity(context, projection)

        trace = assessment.trace()
        trace["labelled"] = request.expected is not None
        trace["projected_series"] = len(projection.projected_series)
        trace["excluded_series"] = len(projection.excluded_series)
        trace["forecast_minimum"] = str(projection.minimum)
        trace["opening_balance"] = str(projection.opening_balance)
        records.append(trace)

        if request.expected is not None:
            truth = request.expected
            mine = assessment.amount_safe_to_pay
            theirs = truth.amount_safe_to_pay
            error = (
                abs(mine - theirs) / theirs * 100 if theirs else Decimal(0)
            )
            sample_rows.append(
                {
                    "request_id": request.request_id,
                    "requested": request.requested_amount,
                    "gt_safe": theirs,
                    "mine_safe": mine,
                    "error_pct": error,
                    "direction": (
                        "over" if mine > theirs else ("under" if mine < theirs else "exact")
                    ),
                    "gt_earliest": truth.earliest_date_for_full_payment,
                    "mine_earliest": assessment.earliest_date_for_full_payment,
                    "gt_status": truth.affordability_status,
                    "binding": assessment.binding_trough,
                    "floor": assessment.minimum_balance_to_keep,
                }
            )

    TRACE_PATH.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )

    over = [r for r in sample_rows if r["direction"] == "over"]
    under = [r for r in sample_rows if r["direction"] == "under"]
    exact = [r for r in sample_rows if r["direction"] == "exact"]
    within = lambda pct: sum(1 for r in sample_rows if r["error_pct"] <= pct)
    date_match = sum(
        1 for r in sample_rows if r["gt_earliest"] == r["mine_earliest"]
    )

    lines: list[str] = []
    add = lines.append
    add("# Capacity calibration against the 25 labelled samples")
    add("")
    add(
        "`src/capacity.py` is exact **given the forecast it is handed**: it returns "
        "the headroom at the binding trough, and the tests prove a cent more "
        "breaks the floor. Any error below is therefore an error in the "
        "*forecast*, not in the capacity solve."
    )
    add("")
    add("## Accuracy")
    add("")
    add(f"- exact: **{len(exact)}/25**")
    add(f"- within 1%: **{within(1)}/25**")
    add(f"- within 5%: **{within(5)}/25**")
    add(f"- within 20%: **{within(20)}/25**")
    add(f"- `earliest_date_for_full_payment` exact: **{date_match}/25**")
    add("")
    add(f"- overstating capacity: **{len(over)}/25**")
    add(f"- understating capacity: **{len(under)}/25**")
    add("")
    add("## Per-row")
    add("")
    add(
        "| request | requested | ground truth | computed | error | dir | "
        "gt earliest | computed earliest | gt status | binding trough |"
    )
    add("| --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- | --- |")
    for row in sorted(sample_rows, key=lambda r: -r["error_pct"]):
        binding = row["binding"]
        binding_text = (
            f"{binding.on.isoformat()} bal {binding.balance} "
            f"(head {binding.balance - row['floor']})"
            if binding
            else "-"
        )
        add(
            f"| `{row['request_id']}` | {row['requested']} | {row['gt_safe']} | "
            f"{row['mine_safe']} | {row['error_pct']:.1f}% | {row['direction']} | "
            f"{row['gt_earliest'] or '-'} | {row['mine_earliest'] or '-'} | "
            f"{row['gt_status']} | {binding_text} |"
        )
    add("")
    add("## Reading the trace")
    add("")
    add(
        "`audit/capacity_trace.jsonl` holds one record per request (all 275). "
        "For a missed row, compare:"
    )
    add("")
    add(
        "- **`binding_trough_date`** -- if this is not the date the ground truth "
        "implies, the *shape* of the forecast is wrong (a series projected that "
        "should not be, or dropped that should not be)."
    )
    add(
        "- **`binding_trough_balance`** -- if the date is right but the balance is "
        "wrong, the *amounts* are wrong (series amount estimator, or a missing "
        "or double-counted flow)."
    )
    add("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")

    print(f"traces  : {len(records)} -> {TRACE_PATH}")
    print(f"report  : {REPORT_PATH}")
    print(
        f"exact {len(exact)}/25  within1% {within(1)}/25  within5% {within(5)}/25  "
        f"within20% {within(20)}/25  earliest {date_match}/25"
    )
    print(f"over {len(over)}  under {len(under)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

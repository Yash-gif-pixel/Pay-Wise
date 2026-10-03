"""Rank the whole dataset, emit per-request traces, and score the samples.

Writes ``audit/traces/<request_id>.json`` for all 275 requests and
``audit/rank_calibration.md`` for the 25 labelled rows.

Reports three numbers that diagnose different things:

* **method exact** and **status exact** -- dominated by the forecast bias
  (C3e), not by ranking.
* **method exact given the ground-truth method was generated** -- the only
  figure that measures ranking itself.

Run from anywhere::

    py audit/rank_report.py
"""

from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.disable(logging.CRITICAL)

from src import candidates as cand  # noqa: E402
from src import capacity, changes, extract, forecast, load, rank, state  # noqa: E402

REPORT_PATH = REPO_ROOT / "audit" / "rank_calibration.md"


def decide(dataset: load.Dataset, request, cache) -> rank.Decision:
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
    return rank.rank(context, candidate_set, assessment)


def main() -> int:
    dataset = load.get_dataset()
    cache = extract.load_image_cache()

    ties: list[str] = []
    criteria = Counter()
    deep_tiebreaks: list[str] = []
    rows: list[dict] = []

    for request in list(dataset.requests) + list(dataset.sample_requests):
        try:
            decision = decide(dataset, request, cache)
        except rank.RankingTieError as exc:
            ties.append(str(exc))
            continue
        decision.write_trace()
        criteria[decision.criterion.value] += 1
        if decision.criterion is rank.Criterion.LOWEST_OPTION_ID:
            deep_tiebreaks.append(request.request_id)

        if request.expected is not None:
            truth = request.expected
            generated = {c.method.value for c in decision.candidate_set.eligible}
            gt_method = truth.recommended_payment_method
            available = (
                gt_method in generated
                or (gt_method == "not_recommended" and not generated)
            )
            rows.append(
                {
                    "request_id": request.request_id,
                    "gt_method": gt_method,
                    "my_method": decision.method.value,
                    "gt_status": truth.affordability_status,
                    "my_status": decision.status.value,
                    "criterion": decision.criterion.value,
                    "available": available,
                    "candidates": len(decision.candidate_set.eligible),
                }
            )

    method_exact = sum(1 for r in rows if r["gt_method"] == r["my_method"])
    status_exact = sum(1 for r in rows if r["gt_status"] == r["my_status"])
    available_rows = [r for r in rows if r["available"]]
    method_given_available = sum(
        1 for r in available_rows if r["gt_method"] == r["my_method"]
    )

    lines: list[str] = []
    add = lines.append
    add("# Ranking calibration against the 25 labelled samples")
    add("")
    add("## The three numbers")
    add("")
    add(f"| metric | result | what it diagnoses |")
    add(f"| --- | ---: | --- |")
    add(
        f"| method exact | **{method_exact}/25** | dominated by forecast bias "
        f"(C3e), not ranking |"
    )
    add(
        f"| status exact | **{status_exact}/25** | same; status follows the "
        f"winning method |"
    )
    add(
        f"| method exact **given generated** | "
        f"**{method_given_available}/{len(available_rows)}** | "
        f"**the only figure that measures ranking** |"
    )
    add("")
    if method_given_available == len(available_rows):
        add(
            "Ranking picks the labelled method **every time it was offered one**. "
            "No ranking defect is visible."
        )
    else:
        add(
            f"**Ranking has a defect**: {len(available_rows) - method_given_available} "
            f"row(s) had the labelled method available and did not pick it."
        )
    add("")

    add("## Which criterion decided each row")
    add("")
    add("| criterion | requests (all 275) |")
    add("| --- | ---: |")
    for name, count in criteria.most_common():
        add(f"| `{name}` | {count} |")
    add("")
    add(
        f"Criterion-5 (`lowest_payment_option_id`) tie-breaks: "
        f"**{len(deep_tiebreaks)}**"
        + (f" -- {', '.join(deep_tiebreaks)}" if deep_tiebreaks else "")
    )
    add("")
    if ties:
        add(f"## Unresolvable ties ({len(ties)})")
        add("")
        for text in ties:
            add(f"- {text}")
    else:
        add("No candidate pair survived all five criteria, so ranking was total.")
    add("")

    add("## Per-row")
    add("")
    add(
        "| request | gt method | mine | gt status | mine | available | cands | decided by |"
    )
    add("| --- | --- | --- | --- | --- | :-: | ---: | --- |")
    for row in rows:
        mark = "OK" if row["gt_method"] == row["my_method"] else "**X**"
        add(
            f"| `{row['request_id']}` | {row['gt_method']} | {row['my_method']} "
            f"{mark} | {row['gt_status']} | {row['my_status']} | "
            f"{'yes' if row['available'] else 'no'} | {row['candidates']} | "
            f"`{row['criterion']}` |"
        )
    add("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")

    print(f"traces           : {rank.TRACE_DIR}")
    print(f"report           : {REPORT_PATH}")
    print(f"method exact     : {method_exact}/25")
    print(f"status exact     : {status_exact}/25")
    print(
        f"method|generated : {method_given_available}/{len(available_rows)}"
        "   <- the only ranking measure"
    )
    print(f"criteria         : {dict(criteria)}")
    print(f"criterion-5 ties : {len(deep_tiebreaks)} {deep_tiebreaks}")
    print(f"unresolvable ties: {len(ties)}")
    for text in ties:
        print(f"   {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

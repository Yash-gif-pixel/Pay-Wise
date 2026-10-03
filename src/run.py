"""CLI entrypoint.

Usage::

    py -m src.run                        # all 250 requests
    py -m src.run --limit 10             # first 10
    py -m src.run --request-id request_26
    py -m src.run --resume               # skip requests already in the output

Reads ``dataset/``, writes ``dataset/output.csv`` (and the root-level
``output.csv``) through :mod:`src.validate`, and drops a per-request decision
trace into ``audit/traces/``.

Secrets come from environment variables only. The default path makes **no
model calls at all**: template coverage is 100% so the LLM fallback never
fires, and image amounts are read from ``audit/image_cache.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from . import candidates as cand
from . import (
    capacity,
    changes,
    config,
    extract,
    forecast,
    load,
    paths,
    rank,
    render,
    state,
    validate,
)

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    attempted: int = 0
    rendered: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def solve_one(
    dataset: load.Dataset,
    request: load.Request,
    cache,
) -> render.OutputRow:
    """Run the whole pipeline for a single request."""
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
    decision.write_trace()
    row = render.render_decision(context, decision)
    validate.validate_row(row.as_dict(), request, dataset)
    return row


def read_existing(path: Path) -> dict[str, dict[str, str]]:
    """Rows already written, keyed by request_id, for ``--resume``."""
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "amount_safe_to_pay" not in reader.fieldnames:
            return {}
        return {
            row["request_id"]: row
            for row in reader
            if row.get("amount_safe_to_pay", "").strip()
        }


def write_output(path: Path, rows: Sequence[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=list(paths.OUTPUT_COLUMNS), lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def select_requests(
    dataset: load.Dataset,
    *,
    limit: int | None,
    request_id: str | None,
) -> list[load.Request]:
    requests = list(dataset.requests)
    if request_id:
        requests = [r for r in requests if r.request_id == request_id]
        if not requests:
            raise SystemExit(f"unknown request_id {request_id!r}")
    if limit is not None:
        requests = requests[:limit]
    return requests


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Buy or Wait? -- generate output.csv"
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--request-id", default=None)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="keep rows already present in the output and only solve the rest",
    )
    parser.add_argument("--out", default=None, help="output path")
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="record failures and continue instead of stopping",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )

    started = time.time()
    dataset = load.get_dataset()
    cache = extract.load_image_cache()
    out_path = Path(args.out) if args.out else paths.OUTPUT_TEMPLATE_CSV

    existing = read_existing(out_path) if args.resume else {}
    requests = select_requests(
        dataset, limit=args.limit, request_id=args.request_id
    )

    print(f"config: {json.dumps(config.describe(), default=str)}")
    print(f"solving {len(requests)} request(s) -> {out_path}")
    if existing:
        print(f"resuming: {len(existing)} row(s) already present")

    stats = RunStats()
    by_id: dict[str, dict[str, str]] = dict(existing)

    for request in requests:
        if args.resume and request.request_id in existing:
            stats.skipped += 1
            continue
        stats.attempted += 1
        try:
            row = solve_one(dataset, request, cache)
            by_id[request.request_id] = row.as_dict()
            stats.rendered += 1
        except Exception as exc:  # noqa: BLE001 - we want the class and message
            stats.failed += 1
            stats.errors.append((request.request_id, f"{type(exc).__name__}: {exc}"))
            logger.error("%s failed: %s", request.request_id, exc)
            if not args.keep_going:
                raise

    # Preserve requests.csv order.
    ordered = [
        by_id[r.request_id] for r in dataset.requests if r.request_id in by_id
    ]
    write_output(out_path, ordered)
    if out_path == paths.OUTPUT_TEMPLATE_CSV:
        write_output(paths.OUTPUT_CSV, ordered)

    with out_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        written = list(reader)

    # Completeness only means something on a full run; a --limit or
    # --request-id run would otherwise report every unsolved request as missing.
    partial = args.limit is not None or args.request_id is not None
    violations = [
        v
        for v in validate.validate_output(written, dataset, header=header)
        if not (partial and v.rule == "missing_row")
    ]
    if partial:
        print("(partial run: completeness check skipped)")
    distribution = validate.check_status_distribution(written)

    elapsed = time.time() - started
    print()
    print(f"attempted {stats.attempted}, rendered {stats.rendered}, "
          f"skipped {stats.skipped}, failed {stats.failed} in {elapsed:.1f}s")
    if stats.errors:
        print(f"\npipeline errors ({len(stats.errors)}):")
        for request_id, message in stats.errors[:20]:
            print(f"   {request_id}: {message}")

    print(f"\nvalidator: {len(violations)} violation(s)")
    if violations:
        by_rule: dict[str, list[str]] = {}
        for violation in violations:
            by_rule.setdefault(violation.rule, []).append(violation.request_id)
        for rule, ids in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
            print(f"   [{rule}] {len(ids)}: {', '.join(ids[:6])}"
                  f"{' ...' if len(ids) > 6 else ''}")

    print("\nstatus distribution (pre-calibration baseline):")
    for status, info in distribution["rows"].items():
        print(
            f"   {status:<22} {info['count']:>4}  {info['actual'] * 100:5.1f}%  "
            f"expected {info['expected'] * 100:4.0f}%  "
            f"({info['delta_pct_points']:+.1f} pts)"
        )

    return 1 if (violations or stats.failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())

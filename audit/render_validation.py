"""Validate every formatting rule against the 25 labelled rows.

Renders from **ground-truth values**, not from the pipeline's own numbers, so
this measures formatting alone and is unaffected by the forecast bias in C3e.

For each formatting rule, finds a labelled row that exercises it and checks the
renderer reproduces the ground-truth string character for character. Rules that
no sample exercises are reported as unverified.

Writes ``audit/render_validation.md``. Run from anywhere::

    py audit/render_validation.py
"""

from __future__ import annotations

import csv
import logging
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

logging.disable(logging.CRITICAL)

from src import load, render  # noqa: E402

REPORT_PATH = REPO_ROOT / "audit" / "render_validation.md"


def raw_samples() -> list[dict[str, str]]:
    path = REPO_ROOT / "dataset" / "sample_requests.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parse_plan(text: str) -> list[render.Payment]:
    from src.candidates import Payment

    if text in ("none", ""):
        return []
    entries = []
    for chunk in text.split("|"):
        day, amount = chunk.split(":")
        entries.append(Payment(on=date.fromisoformat(day), amount=Decimal(amount)))
    return entries


def main() -> int:
    dataset = load.get_dataset()
    rows = raw_samples()
    profiles = dataset.profiles

    checks: list[dict] = []

    def check(rule: str, request_id: str, expected: str, actual: str, note: str = ""):
        checks.append(
            {
                "rule": rule,
                "request_id": request_id,
                "expected": expected,
                "actual": actual,
                "ok": expected == actual,
                "note": note,
            }
        )

    for row in rows:
        rid = row["request_id"]
        profile = profiles[row["user_id"]]
        currency = profile.home_currency
        minimum = profile.minimum_balance_to_keep

        # --- amount_safe_to_pay: minimal decimals ---------------------------
        safe = Decimal(row["amount_safe_to_pay"])
        note = ""
        if "." in row["amount_safe_to_pay"]:
            note = "fractional"
        check(
            "amount_safe_to_pay minimal decimals",
            rid,
            row["amount_safe_to_pay"],
            render.format_minimal(safe),
            note,
        )

        # --- payment_plan ----------------------------------------------------
        payments = parse_plan(row["payment_plan"])
        check(
            "payment_plan format",
            rid,
            row["payment_plan"],
            render.format_payment_plan(payments) if payments else "none",
            f"{len(payments)} payment(s)",
        )

        # --- earliest_date ---------------------------------------------------
        earliest = row["earliest_date_for_full_payment"]
        parsed = date.fromisoformat(earliest) if earliest else None
        check(
            "earliest_date_for_full_payment",
            rid,
            earliest,
            render.format_iso_date(parsed),
            "blank" if not earliest else "",
        )

        # --- spending_changes ------------------------------------------------
        changes_text = row["spending_changes_needed"]
        parsed_changes = [] if changes_text == "none" else changes_text.split("|")
        check(
            "spending_changes_needed",
            rid,
            changes_text,
            render.format_spending_changes(parsed_changes),
            f"{len(parsed_changes)} change(s)",
        )

    # -- rule coverage ------------------------------------------------------
    def find(predicate, label):
        hits = [r for r in rows if predicate(r)]
        return hits, label

    coverage = [
        find(lambda r: "." in r["amount_safe_to_pay"]
             and r["amount_safe_to_pay"].rstrip("0").endswith("."),
             "amount_safe_to_pay with a stripped trailing zero"),
        find(lambda r: "." in r["amount_safe_to_pay"],
             "amount_safe_to_pay fractional"),
        find(lambda r: "." not in r["amount_safe_to_pay"],
             "amount_safe_to_pay whole"),
        find(lambda r: any(
            "." in c.split(":")[1] and c.split(":")[1].endswith("0")
            for c in r["payment_plan"].split("|") if ":" in c),
             "payment_plan amount keeping a trailing zero"),
        find(lambda r: any(
            "." not in c.split(":")[1]
            for c in r["payment_plan"].split("|") if ":" in c),
             "payment_plan whole-number amount"),
        find(lambda r: r["payment_plan"].count("|") >= 1,
             "payment_plan multi-payment"),
        find(lambda r: r["recommended_payment_method"] == "wait",
             "wait row carries a dated plan"),
        find(lambda r: r["recommended_payment_method"] == "not_recommended",
             "not_recommended: plan none, earliest blank"),
        find(lambda r: r["affordability_status"] == "affordable_now",
             "affordable_now: earliest == request_date"),
        find(lambda r: r["spending_changes_needed"] != "none",
             "spending_changes non-empty"),
        find(lambda r: "reduce_to" in r["spending_changes_needed"]
             and "." in r["spending_changes_needed"],
             "reduce_to amount with decimals"),
        find(lambda r: r["spending_changes_needed"].count("|") >= 1,
             "spending_changes multiple"),
        find(lambda r: r["recommended_payment_method"] == "installments",
             "installments amounts copied verbatim"),
        find(lambda r: r["recommended_payment_method"] == "partial_payment",
             "partial two-payment plan"),
        find(lambda r: "," in r["decision_explanation"]
             and any(ch.isdigit() for ch in r["decision_explanation"]),
             "prose thousands separators"),
    ]

    # -- the six templates ---------------------------------------------------
    template_rows: dict[str, list[tuple[str, str, str]]] = {}
    for row in rows:
        rid = row["request_id"]
        profile = profiles[row["user_id"]]
        currency = profile.home_currency
        minimum = profile.minimum_balance_to_keep
        method = row["recommended_payment_method"]
        changes_text = row["spending_changes_needed"]
        payments = parse_plan(row["payment_plan"])
        requested = Decimal(row["requested_amount"])
        safe = Decimal(row["amount_safe_to_pay"])
        descriptions = {
            e.event_id: e.description
            for e in dataset.get_user_context(row["user_id"], rid).events
        }

        if method == "full_payment" and changes_text != "none":
            name = "full+changes"
            produced = render.explain_full_with_changes(
                currency, payments[0].amount, minimum,
                changes_text.split("|"), descriptions,
            )
        elif method == "full_payment":
            name = "affordable_now"
            produced = render.explain_affordable_now(
                currency, payments[0].amount, minimum
            )
        elif method == "installments":
            name = "installments"
            produced = render.explain_installments(
                currency, len(payments), payments[0].amount, payments[0].on, minimum
            )
        elif method == "wait":
            name = "wait"
            produced = render.explain_wait(
                currency, payments[0].amount, payments[0].on, minimum
            )
        elif method == "partial_payment":
            name = "partial"
            produced = render.explain_partial(
                currency, payments[0].amount, payments[1].amount,
                payments[1].on, minimum,
            )
        else:
            ratio = safe / requested if requested else Decimal(0)
            if ratio >= render.NOT_AFFORDABLE_B_MIN_RATIO:
                name = "not_affordable_B"
                produced = render.explain_not_affordable_b(currency, requested, safe)
            else:
                name = "not_affordable_A"
                produced = render.explain_not_affordable_a(
                    currency,
                    date.fromisoformat(row["desired_completion_date"]),
                    minimum,
                )
        template_rows.setdefault(name, []).append(
            (rid, row["decision_explanation"], produced)
        )

    # -- report --------------------------------------------------------------
    lines: list[str] = []
    add = lines.append
    add("# Formatting validation against the 25 labelled rows")
    add("")
    add(
        "Rendered from **ground-truth values**, so this measures formatting "
        "only and is unaffected by the forecast bias in C3e."
    )
    add("")

    failed = [c for c in checks if not c["ok"]]
    add(f"- field checks run: **{len(checks)}**")
    add(f"- character-for-character matches: **{len(checks) - len(failed)}**")
    add(f"- mismatches: **{len(failed)}**")
    add("")
    if failed:
        add("## Mismatches")
        add("")
        add("| rule | request | expected | produced |")
        add("| --- | --- | --- | --- |")
        for item in failed:
            add(
                f"| {item['rule']} | `{item['request_id']}` | "
                f"`{item['expected']}` | `{item['actual']}` |"
            )
        add("")

    add("## Rule coverage in the samples")
    add("")
    add("| rule | rows exercising it | verified |")
    add("| --- | ---: | --- |")
    unverified: list[str] = []
    for hits, label in coverage:
        if hits:
            add(f"| {label} | {len(hits)} | yes (`{hits[0]['request_id']}`) |")
        else:
            unverified.append(label)
            add(f"| {label} | **0** | **NO SAMPLE EXERCISES THIS** |")
    add("")
    if unverified:
        add(f"### Unverified rules ({len(unverified)})")
        add("")
        add("No labelled row exercises these, so they rest on the spec alone:")
        add("")
        for label in unverified:
            add(f"- {label}")
        add("")

    add("## Template inventory")
    add("")
    add("| template | rows | exact | closest divergence |")
    add("| --- | ---: | ---: | --- |")
    incomplete: list[tuple[str, str]] = []
    for name in (
        "affordable_now",
        "installments",
        "wait",
        "full+changes",
        "partial",
        "not_affordable_A",
        "not_affordable_B",
    ):
        items = template_rows.get(name, [])
        exact = sum(1 for _, truth, mine in items if truth == mine)
        divergent = [(rid, truth, mine) for rid, truth, mine in items if truth != mine]
        note = (
            f"`{divergent[0][0]}`" if divergent else "-"
        )
        add(f"| `{name}` | {len(items)} | {exact}/{len(items)} | {note} |")
        for rid, truth, mine in divergent:
            incomplete.append((rid, f"GT: {truth}\n      MINE: {mine}"))
    add("")

    if incomplete:
        add("### Rows whose label differs from the filled template")
        add("")
        for rid, text in incomplete:
            add(f"**`{rid}`**")
            add("")
            add("```")
            add(text.replace("      ", "  "))
            add("```")
            add("")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")

    print(f"report              : {REPORT_PATH}")
    print(f"field checks        : {len(checks) - len(failed)}/{len(checks)} exact")
    for item in failed:
        print(f"   MISMATCH {item['rule']} {item['request_id']}: "
              f"{item['expected']!r} vs {item['actual']!r}")
    print(f"unverified rules    : {len(unverified)}")
    for label in unverified:
        print(f"   {label}")
    print("template exact-match:")
    for name in (
        "affordable_now", "installments", "wait", "full+changes",
        "partial", "not_affordable_A", "not_affordable_B",
    ):
        items = template_rows.get(name, [])
        exact = sum(1 for _, truth, mine in items if truth == mine)
        print(f"   {name:<18} {exact}/{len(items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

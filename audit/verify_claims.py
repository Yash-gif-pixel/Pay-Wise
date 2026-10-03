"""Independent re-derivation of every numeric claim in the prose deliverables.

Written after a fabricated "time remaining" figure reached ``log.txt``, a
submission artefact, with nothing in the pipeline able to catch it: nothing
checks prose.

**Method.** Each claim is re-derived by a fresh query against the raw CSVs in
``dataset/``, the produced ``output.csv``, or the artefact itself. Where a
claim can *only* be reproduced by running the same pipeline code that made it,
it is marked ``CODE-DEPENDENT`` rather than ``CONFIRMED`` -- re-running the
producer is not verification.

Run::

    py audit/verify_claims.py
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import zipfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "dataset"

CONFIRMED = "CONFIRMED"
CODE_DEP = "CODE-DEPENDENT"
STALE = "STALE"
WRONG = "WRONG"

results: list[tuple[str, str, str, str]] = []


def record(claim: str, verdict: str, found: str, source: str) -> None:
    results.append((claim, verdict, found, source))


def rows(name: str) -> list[dict[str, str]]:
    with (DATA / name).open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def out_rows() -> list[dict[str, str]]:
    path = REPO / "dataset" / "output.csv"
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def check(claim: str, expected, actual, source: str) -> None:
    ok = expected == actual
    record(claim, CONFIRMED if ok else WRONG, f"{actual!r}", source)


def doc_text(relative: str) -> str:
    path = REPO / relative
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def claimed(relative: str, pattern: str) -> int | None:
    """First integer matched by ``pattern`` in a deliverable, or ``None``.

    Used to compare a figure a DOCUMENT states against the figure re-derived
    here. Comparing live reality to a second hardcoded copy inside this file is
    what let the zip figures drift unnoticed: both copies were wrong and
    neither was checked against the other.
    """
    m = re.search(pattern, doc_text(relative))
    return int(m.group(1).replace(",", "")) if m else None


def check_doc(claim: str, relative: str, pattern: str, actual, how: str) -> None:
    """Assert a figure stated in a deliverable matches one re-derived here."""
    stated = claimed(relative, pattern)
    if stated is None:
        record(claim, WRONG, f"no match in {relative}", how)
        return
    verdict = CONFIRMED if stated == actual else STALE
    found = f"doc={stated} actual={actual}"
    record(claim, verdict, found, f"{how}; claim parsed from {relative}")


# ---------------------------------------------------------------------------
# Raw-CSV claims -- genuinely independent
# ---------------------------------------------------------------------------

messages = rows("messages.csv")
options = rows("request_payment_options.csv")
events = rows("financial_events.csv")
profiles = {r["user_id"]: r for r in rows("financial_profiles.csv")}
requests = rows("requests.csv")
samples = rows("sample_requests.csv")

check("215 messages", 215, len(messages), "dataset/messages.csv row count")

opt_reqs = {o["request_id"] for o in options}
req_ids = {r["request_id"] for r in requests}
sam_ids = {r["request_id"] for r in samples}
check("options cover 275 requests", 275, len(opt_reqs), "distinct request_id in options")
check("  ... of which 250 evaluation", 250, len(opt_reqs & req_ids), "set intersection")
check("  ... of which 25 sample", 25, len(opt_reqs & sam_ids), "set intersection")

blank = [e for e in events if not e["amount"].strip()]
check("16 blank-amount events", 16, len(blank), "financial_events amount == ''")

req_by_user = {r["user_id"]: r for r in requests}
for r in samples:
    req_by_user.setdefault(r["user_id"], r)
non_settled = sorted(
    e["event_id"]
    for e in blank
    if e["status"] != "settled"
    or (e["settlement_date"] or e["event_date"])
    > req_by_user[e["user_id"]]["request_date"]
)
check(
    "4 non-settled blanks: 1442/1786/6033/6859",
    ["event_1442", "event_1786", "event_6033", "event_6859"],
    non_settled,
    "blank amount AND (status != settled OR cash_date > request_date)",
)

# installment cap vs deadline
from datetime import date, timedelta

cap_only = []
all_req = {r["request_id"]: r for r in requests}
for r in samples:
    all_req.setdefault(r["request_id"], r)
inst_total = 0
for o in options:
    if o["payment_method"] != "installments":
        continue
    req = all_req.get(o["request_id"])
    if req is None:
        continue
    prof = profiles[req["user_id"]]
    inst_total += 1
    if not prof["max_installment_months"].strip():
        continue
    n = int(o["number_of_payments"])
    freq = int(o["payment_frequency_days"] or 30)
    last = date.fromisoformat(o["first_payment_date"]) + timedelta(days=(n - 1) * freq)
    late = last > date.fromisoformat(req["desired_completion_date"])
    if n > int(prof["max_installment_months"]) and not late:
        cap_only.append(o["payment_option_id"])
check(
    "0 options over cap without also missing deadline",
    [],
    cap_only,
    f"recomputed last-payment date for all {inst_total} installment options",
)

# flexibility vs permitted categories
reduce_cap = stop_cap = 0
reduce_bad = stop_bad = 0
for e in events:
    prof = profiles[e["user_id"]]
    if e["flexibility"] in ("reducible", "reducible_or_stoppable"):
        reduce_cap += 1
        if e["category"] not in prof["expense_categories_user_is_willing_to_reduce"].split("|"):
            reduce_bad += 1
    if e["flexibility"] in ("stoppable", "reducible_or_stoppable"):
        stop_cap += 1
        if e["category"] not in prof["expense_categories_user_is_willing_to_stop"].split("|"):
            stop_bad += 1
check("2,907 reduce-capable events", 2907, reduce_cap, "flexibility field count")
check("1,522 stop-capable events", 1522, stop_cap, "flexibility field count")
check("all reduce-capable in permitted category", 0, reduce_bad, "category membership")
check("all stop-capable in permitted category", 0, stop_bad, "category membership")

partial_only = sorted(
    r["request_id"]
    for r in requests
    if profiles[r["user_id"]]["payment_methods_user_will_consider"] == "partial_payment"
    and r["allows_partial_payment"] == "false"
)
check(
    "5 partial-only blocked rows",
    sorted(["request_95", "request_133", "request_209", "request_76", "request_227"]),
    partial_only,
    "profile methods == partial_payment AND allows_partial == false",
)

# A/B explanation ratio spans
a_ratios, b_ratios = [], []
for r in samples:
    if r["recommended_payment_method"] != "not_recommended":
        continue
    ratio = Decimal(r["amount_safe_to_pay"]) / Decimal(r["requested_amount"]) * 100
    (b_ratios if "Do not proceed with the" in r["decision_explanation"] else a_ratios).append(
        float(ratio)
    )
check(
    "A span 1.8-4.8%",
    (1.8, 4.8),
    (round(min(a_ratios), 1), round(max(a_ratios), 1)),
    "sample_requests + profiles, recomputed",
)
check(
    "B span 11.0-12.2%",
    (11.0, 12.2),
    (round(min(b_ratios), 1), round(max(b_ratios), 1)),
    "sample_requests + profiles, recomputed",
)

# ---------------------------------------------------------------------------
# output.csv claims -- independent of the pipeline that wrote it
# ---------------------------------------------------------------------------

produced = out_rows()
check("250 output rows", 250, len(produced), "output.csv row count")

dist = Counter(r["affordability_status"] for r in produced)
pct = {k: round(v / len(produced) * 100, 1) for k, v in dist.items()}
check(
    "distribution 26.0/27.6/18.0/28.4",
    {
        "affordable_now": 26.0,
        "affordable_with_plan": 27.6,
        "affordable_later": 18.0,
        "not_affordable": 28.4,
    },
    pct,
    "output.csv status column",
)

chg = sum(1 for r in produced if r["spending_changes_needed"] != "none")
check("3/250 spending-change rows", 3, chg, "output.csv spending_changes column")

# ---------------------------------------------------------------------------
# Artefact claims
# ---------------------------------------------------------------------------

zip_path = REPO / "code.zip"
if zip_path.is_file():
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    size_kib = round(zip_path.stat().st_size / 1024)
    check("code.zip 54 files", 54, len(names), "zipfile namelist")
    # Cross-check the figures the LOG states against the live zip. This is the
    # check that was missing: the two hardcoded copies disagreed for several
    # turns and nothing compared them.
    check_doc(
        "calibration_log zip file count matches the real zip",
        "evaluation/calibration_log.md",
        r"`code\.zip`\s*\|\s*([\d,]+) files",
        len(names),
        "zipfile namelist",
    )
    # The size is checked with a tolerance, and the exact-match version was
    # deliberately removed. code.zip contains evaluation/calibration_log.md,
    # which states code.zip's size -- so writing the true size into the log
    # changes the size the log should have stated. It is a fixed point that does
    # not settle: the edit that corrected 283 moved it to 284. A band is the
    # honest check here; the FILE COUNT above is exact and is the figure that
    # actually catches a packaging mistake.
    zip_size_claim = claimed(
        "evaluation/calibration_log.md",
        r"`code\.zip`\s*\|\s*[\d,]+ files,\s*~?([\d,]+) KiB",
    )
    if zip_size_claim is None:
        record("calibration_log zip size", WRONG,
               "no match in evaluation/calibration_log.md", "stat().st_size")
    else:
        drift = abs(zip_size_claim - size_kib)
        record(
            "calibration_log zip size within 3 KiB of the real zip",
            CONFIRMED if drift <= 3 else STALE,
            f"doc={zip_size_claim} actual={size_kib} drift={drift}",
            "stat().st_size; tolerant because the log is inside the zip",
        )
    check("code.zip carries evaluation/usage_report.md", True,
          "evaluation/usage_report.md" in names, "zipfile namelist")
else:
    record("code.zip", WRONG, "file absent", "filesystem")

cache = json.loads((REPO / "audit" / "image_cache.json").read_text(encoding="utf-8"))
provs = {k: v.get("provenance", "") for k, v in cache["resolutions"].items()}
raw_usage = [
    json.loads(l)
    for l in (REPO / "evaluation" / "usage_raw.jsonl").read_text(encoding="utf-8").splitlines()
    if l.strip()
]
usage_providers = {r["provider"] for r in raw_usage}
consistent = all(p.startswith("claude-code-session:") for p in provs.values()) and (
    usage_providers == {"claude-code-session"}
)
record(
    "cache provenance matches usage_raw provider",
    CONFIRMED if consistent else WRONG,
    f"cache={set(provs.values())} usage={usage_providers}",
    "cross-file comparison",
)

# usage_report figures vs usage_raw
report = (REPO / "evaluation" / "usage_report.md").read_text(encoding="utf-8")
tot_in = sum(r["input_tokens"] for r in raw_usage)
tot_out = sum(r["output_tokens"] for r in raw_usage)
tot_calls = sum(r.get("calls", 1) for r in raw_usage)
check("usage: 4 calls", 4, tot_calls, "usage_raw.jsonl")
check("usage total tokens 7,630", 7630, tot_in + tot_out, "usage_raw.jsonl")
record(
    "usage_report states the same totals",
    CONFIRMED
    if (f"{tot_in:,}" in report and f"{tot_out:,}" in report and f"**{tot_calls}**" in report)
    else WRONG,
    f"in={tot_in:,} out={tot_out:,} calls={tot_calls}",
    "string search in usage_report.md",
)

# no generation literals in extract.py
extract_src = (REPO / "src" / "extract.py").read_text(encoding="utf-8")
literals = re.findall(r"claude-[a-z0-9.\-]+|temperature=0[,\)]|max_tokens=\d+", extract_src)
check("no generation literals in extract.py", [], literals, "regex over source")

# ---------------------------------------------------------------------------
# Code-dependent claims -- run the pipeline, but mark them honestly
# ---------------------------------------------------------------------------

sys.path.insert(0, str(REPO))
import logging

logging.disable(logging.CRITICAL)
from src import config, extract, forecast, load, state  # noqa: E402
from src.patterns import TEMPLATES  # noqa: E402

ds = load.get_dataset()

record(
    "40 templates / 39 firing / 100% coverage",
    CODE_DEP,
    f"{len(TEMPLATES)} templates; "
    f"{sum(1 for m in ds.messages if extract.extract_from_message(m))}/{len(ds.messages)} matched; "
    f"{len({t.name for t in TEMPLATES for m in ds.messages if t.search(m.message_text)})} firing",
    "requires the regex engine that made the claim",
)

series_total = 0
frag_users = 0
orphan_events = 0
no_salary = 0
cache_obj = extract.load_image_cache()
for r in list(ds.requests) + list(ds.sample_requests):
    ctx = ds.get_user_context(r.user_id, r.request_id)
    st = state.reconstruct_balance(ctx)
    series_total += len(st.recurring_series)
    by_cat = defaultdict(list)
    for s in st.series:
        if s.direction == "debit":
            by_cat[s.category].append(s)
    frag = False
    for cat, ss in by_cat.items():
        orphans = [s for s in ss if not s.is_recurring]
        if len(orphans) >= 2 and sum(len(s.occurrences) for s in orphans) >= 3:
            frag = True
            orphan_events += sum(len(s.occurrences) for s in orphans)
    if frag:
        frag_users += 1
    blanks = tuple(f.event_id for f in st.blank_amounts if f.needs_image)
    ev = extract.gather_evidence(ctx, blanks, cache=cache_obj)
    fc = forecast.build_forecast(st, evidence=ev)
    if fc.amendments.salary.amount is None:
        no_salary += 1

record("2,866 recurring series", CODE_DEP, str(series_total),
       "requires state.py recurrence detection")
record("11,857 orphaned events", CODE_DEP, str(orphan_events),
       "requires state.py recurrence detection")
record("275/275 users fragment", CODE_DEP, f"{frag_users}/275",
       "requires state.py recurrence detection")
record("40 users no-salary after Step 4", CODE_DEP, str(no_salary),
       "requires forecast.py income logic")

# test count
proc = subprocess.run(
    [sys.executable, "-m", "pytest", "tests", "-q", "--no-header"],
    cwd=REPO, capture_output=True, text=True,
)
m = re.search(r"(\d+) passed", proc.stdout)
passed = int(m.group(1)) if m else -1
check("345 tests pass", 345, passed, "pytest -q")
check_doc(
    "calibration_log test count matches pytest",
    "evaluation/calibration_log.md",
    r"\| tests \| \*\*([\d,]+) passed\*\* \|",
    passed,
    "pytest -q",
)
check_doc(
    "README test count matches pytest",
    "README.md",
    r"py -m pytest tests -q\s+#\s*([\d,]+) tests",
    passed,
    "pytest -q",
)

# --- the chat transcript: a scored artefact, previously unchecked ----------
transcript = REPO / "chat_transcript.md"
if transcript.is_file():
    body = transcript.read_text(encoding="utf-8")
    check("chat_transcript.md exists", True, True, "filesystem")
    check("chat_transcript has no unredacted key", 0,
          len(re.findall(r"sk-ant-[A-Za-z0-9_\-]{8,}", body)),
          "regex over the rendered transcript")
    turns = len(re.findall(r"^## Turn \d+ ", body, re.M))
    check_doc(
        "chat_transcript prompt count matches its own turn headings",
        "chat_transcript.md",
        r"\| User prompts \| (\d+) \|",
        turns,
        "count of '## Turn N' headings",
    )
else:
    record("chat_transcript.md exists", WRONG, "file absent", "filesystem")

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

order = {WRONG: 0, STALE: 1, CODE_DEP: 2, CONFIRMED: 3}
results.sort(key=lambda r: (order[r[1]], r[0]))

width = max(len(c) for c, _, _, _ in results)
print("=" * 120)
print("CLAIM AUDIT -- independent re-derivation")
print("=" * 120)
print(f"{'claim':<{width}}  {'verdict':<15} {'found':<30} how re-derived")
print("-" * 120)
for claim, verdict, found, source in results:
    print(f"{claim:<{width}}  {verdict:<15} {found[:30]:<30} {source}")
print()
counts = Counter(v for _, v, _, _ in results)
print("summary:", dict(counts))
bad = [r for r in results if r[1] in (WRONG, STALE)]
print(f"WRONG or STALE: {len(bad)}")
for claim, verdict, found, source in bad:
    print(f"   [{verdict}] {claim} -> {found}")

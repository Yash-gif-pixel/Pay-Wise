"""Phase-1 data audit for the Buy or Wait? dataset.

Prints, for every CSV under ``dataset/``: row count, column names, guessed
dtypes, per-column null counts, and the complete distinct-value set for any
column with fewer than 30 distinct values. Adds targeted sections for
``financial_events.csv`` and ``financial_profiles.csv``, plus cross-file
consistency checks against the contract in ``AGENTS.md`` and
``problem_statement.md``.

This module is descriptive only. It contains no financial decision logic.

Run from anywhere::

    py audit/audit_data.py > audit/audit_report.txt
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "dataset"
MEDIA_IMAGES_DIR = DATASET_DIR / "media" / "images"

DISTINCT_VALUE_LIMIT = 30
SAMPLE_VALUES_SHOWN = 5

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:?\d{2})?$"
)
INT_RE = re.compile(r"^-?\d+$")
FLOAT_RE = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")
ID_RE = re.compile(r"^[a-z_]+_\d+$")
BOOL_VALUES = {"true", "false"}

# Columns known to hold "|"-separated lists.
LIST_COLUMNS = {
    "financial_priorities",
    "expense_categories_to_protect",
    "expense_categories_user_is_willing_to_reduce",
    "expense_categories_user_is_willing_to_stop",
    "payment_methods_user_will_consider",
}

# Names that would carry an explicit recurrence/cadence signal if the dataset
# supplied one. Checked against the real header so the audit reports absence
# rather than assuming presence.
RECURRENCE_FIELD_CANDIDATES = (
    "recurrence",
    "recurring",
    "is_recurring",
    "cadence",
    "frequency",
    "interval",
    "period",
    "periodicity",
    "schedule",
    "repeat",
    "recurrence_rule",
    "payment_frequency_days",
)

RECURRENCE_TOKENS = (
    "recur",
    "freq",
    "cadence",
    "interval",
    "period",
    "repeat",
    "schedule",
)


# ---------------------------------------------------------------- utilities


def out(line: str = "") -> None:
    print(line)


def rule(char: str, title: str = "", width: int = 78) -> None:
    if title:
        out(f"{char * 3} {title} {char * max(3, width - len(title) - 5)}")
    else:
        out(char * width)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV preserving blanks verbatim (no type coercion, no NaN)."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = [{k: (row.get(k) or "") for k in fieldnames} for row in reader]
    return fieldnames, rows


def is_blank(value: str) -> bool:
    return str(value).strip() == ""


def to_float(value) -> float | None:
    if value is None or is_blank(str(value)):
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def tokens(raw: str) -> set[str]:
    return {t.strip() for t in raw.split("|") if t.strip()}


def num_key(value: str) -> float:
    parsed = to_float(value)
    return parsed if parsed is not None else float("inf")


def days_between(start: str, end: str) -> int | None:
    try:
        return (date.fromisoformat(end.strip()) - date.fromisoformat(start.strip())).days
    except ValueError:
        return None


def guess_dtype(values: list[str], column: str) -> str:
    """Guess a column's type from its non-blank values."""
    non_blank = [v.strip() for v in values if not is_blank(v)]
    if not non_blank:
        return "empty (all blank)"

    if column in LIST_COLUMNS or any("|" in v for v in non_blank):
        return "list[str] (pipe-separated)"
    if all(v.lower() in BOOL_VALUES for v in non_blank):
        return "bool (true/false)"
    if all(DATE_RE.match(v) for v in non_blank):
        return "date (YYYY-MM-DD)"
    if all(DATETIME_RE.match(v) for v in non_blank):
        return "datetime (ISO-8601)"
    if all(INT_RE.match(v) for v in non_blank):
        return "int"
    if all(FLOAT_RE.match(v) for v in non_blank):
        int_like = sum(1 for v in non_blank if INT_RE.match(v))
        return f"float (int-like {int_like}/{len(non_blank)})"
    if all(ID_RE.match(v) for v in non_blank):
        return "str (id token)"

    lengths = [len(v) for v in non_blank]
    return f"str (len {min(lengths)}-{max(lengths)})"


def truncate(value: str, limit: int = 70) -> str:
    flat = value.replace("\n", "\\n").replace("\r", "")
    return flat if len(flat) <= limit else flat[: limit - 1] + "..."


# ---------------------------------------------------------- per-file report


def audit_file(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    fieldnames, rows = read_csv(path)

    rule("=")
    out(f"FILE: dataset/{path.name}")
    rule("=")
    out(f"rows (excluding header): {len(rows)}")
    out(f"columns ({len(fieldnames)}): {', '.join(fieldnames)}")
    out()

    out(f"{'column':<46} {'dtype':<28} {'nulls':>7} {'distinct':>9}")
    out(f"{'-' * 46} {'-' * 28} {'-' * 7} {'-' * 9}")
    for column in fieldnames:
        values = [row[column] for row in rows]
        nulls = sum(1 for v in values if is_blank(v))
        distinct = len({v.strip() for v in values if not is_blank(v)})
        pct = f"{(nulls / len(rows) * 100):.1f}%" if rows else "n/a"
        dtype = guess_dtype(values, column)
        out(f"{column:<46} {dtype:<28} {nulls:>7} {distinct:>9}   (null {pct})")
    out()

    out(
        f"DISTINCT VALUES (columns with fewer than {DISTINCT_VALUE_LIMIT} "
        "distinct non-blank values)"
    )
    out("-" * 78)
    any_listed = False
    for column in fieldnames:
        counts = Counter(
            row[column].strip() for row in rows if not is_blank(row[column])
        )
        if 0 < len(counts) < DISTINCT_VALUE_LIMIT:
            any_listed = True
            out(f"  {column}  ({len(counts)} distinct)")
            for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                out(f"      {truncate(value):<62} {count:>7}")
            out()
    if not any_listed:
        out(
            f"  (none: every column has {DISTINCT_VALUE_LIMIT}+ distinct values "
            "or is entirely blank)"
        )
        out()

    out("SAMPLE VALUES (first few distinct values per high-cardinality column)")
    out("-" * 78)
    for column in fieldnames:
        counts = Counter(
            row[column].strip() for row in rows if not is_blank(row[column])
        )
        if len(counts) >= DISTINCT_VALUE_LIMIT:
            samples: list[str] = []
            for row in rows:
                value = row[column].strip()
                if value and value not in samples:
                    samples.append(value)
                if len(samples) == SAMPLE_VALUES_SHOWN:
                    break
            out(f"  {column}:")
            for value in samples:
                out(f"      {truncate(value)}")
    out()

    return fieldnames, rows


# ------------------------------------------------ financial_events focus


def audit_financial_events(fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    rule("#", "financial_events.csv - focused checks")

    for column in ("event_type", "status", "flexibility", "direction", "currency"):
        if column not in fieldnames:
            out(f"{column}: COLUMN ABSENT")
            out()
            continue
        counts = Counter(r[column].strip() for r in rows if not is_blank(r[column]))
        blanks = sum(1 for r in rows if is_blank(r[column]))
        out(f"{column}: {len(counts)} distinct, {blanks} blank")
        for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            out(f"    {value:<40} {count:>8}")
        out()

    if "category" in fieldnames:
        counts = Counter(r["category"].strip() for r in rows if not is_blank(r["category"]))
        blanks = sum(1 for r in rows if is_blank(r["category"]))
        out(f"category: {len(counts)} distinct, {blanks} blank")
        for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            out(f"    {value:<40} {count:>8}")
        out()

    out("event_type x status matrix:")
    matrix = Counter(
        (r["event_type"].strip(), r["status"].strip()) for r in rows
    )
    statuses = sorted({s for _, s in matrix})
    header = "    " + f"{'event_type':<24}" + "".join(f"{s:>14}" for s in statuses)
    out(header)
    for event_type in sorted({t for t, _ in matrix}):
        line = "    " + f"{event_type:<24}"
        for status in statuses:
            line += f"{matrix.get((event_type, status), 0):>14}"
        out(line)
    out()

    out("recurrence / cadence fields:")
    present = [f for f in fieldnames if f.lower() in RECURRENCE_FIELD_CANDIDATES]
    fuzzy = [
        f
        for f in fieldnames
        if f not in present and any(tok in f.lower() for tok in RECURRENCE_TOKENS)
    ]
    if present:
        for field in present:
            counts = Counter(r[field].strip() for r in rows if not is_blank(r[field]))
            out(f"    {field}: {len(counts)} distinct")
            for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[
                :DISTINCT_VALUE_LIMIT
            ]:
                out(f"        {value:<36} {count:>8}")
    else:
        out("    NONE PRESENT. financial_events.csv has no explicit recurrence,")
        out("    cadence, frequency or interval column. Recurrence must be INFERRED")
        out("    from repeated (user_id, category, description) history and the")
        out("    spacing between event dates.")
    if fuzzy:
        out(f"    near-miss column names present: {', '.join(fuzzy)}")
    out()

    out("inferred monthly-recurrence signal (descriptive only):")
    groups: dict[tuple[str, str, str], list[str]] = {}
    for r in rows:
        key = (r["user_id"].strip(), r["category"].strip(), r["description"].strip())
        groups.setdefault(key, []).append(r["event_date"].strip())
    repeat_counts = Counter(len(v) for v in groups.values())
    out(f"    distinct (user, category, description) groups: {len(groups)}")
    out("    group-size distribution (how many times the same label repeats):")
    for size, count in sorted(repeat_counts.items())[:20]:
        out(f"        appears {size:>3}x : {count:>6} groups")
    if len(repeat_counts) > 20:
        out(f"        ... {len(repeat_counts) - 20} larger group sizes not shown")
    gaps: Counter[int] = Counter()
    for dates in groups.values():
        ordered = sorted(d for d in dates if DATE_RE.match(d))
        for earlier, later in zip(ordered, ordered[1:]):
            gap = days_between(earlier, later)
            if gap is not None:
                gaps[gap] += 1
    out("    most common day-gaps between consecutive same-label events:")
    for gap, count in gaps.most_common(12):
        out(f"        {gap:>4} days : {count:>7}")
    out()

    blank_amount = [r for r in rows if is_blank(r.get("amount", ""))]
    zero_amount = [
        r for r in rows if r.get("amount", "").strip() in {"0", "0.0", "0.00"}
    ]
    out(f"rows with a BLANK amount: {len(blank_amount)}")
    out(f"rows with amount exactly zero: {len(zero_amount)}")
    if blank_amount:
        out("    blank-amount rows by event_type:")
        for value, count in sorted(
            Counter(r["event_type"].strip() for r in blank_amount).items()
        ):
            out(f"        {value:<36} {count:>5}")
        out("    blank-amount rows by status:")
        for value, count in sorted(
            Counter(r["status"].strip() for r in blank_amount).items()
        ):
            out(f"        {value:<36} {count:>5}")
        out("    blank-amount rows by direction:")
        for value, count in sorted(
            Counter(r["direction"].strip() for r in blank_amount).items()
        ):
            out(f"        {value:<36} {count:>5}")
        ids = sorted(r["event_id"] for r in blank_amount)
        out(f"    blank-amount event_ids: {', '.join(ids)}")
    out()

    linked = [r for r in rows if not is_blank(r.get("linked_event_id", ""))]
    out(f"rows with a linked_event_id: {len(linked)} of {len(rows)}")
    if linked:
        out("    linked rows by event_type:")
        for value, count in sorted(
            Counter(r["event_type"].strip() for r in linked).items(),
            key=lambda kv: -kv[1],
        ):
            out(f"        {value:<36} {count:>5}")
        out("    linked rows by status:")
        for value, count in sorted(
            Counter(r["status"].strip() for r in linked).items(), key=lambda kv: -kv[1]
        ):
            out(f"        {value:<36} {count:>5}")
        out("    (parent status -> child status) pairs:")
        by_id = {r["event_id"].strip(): r for r in rows}
        pair_counts = Counter(
            (
                by_id[r["linked_event_id"].strip()]["status"].strip()
                if r["linked_event_id"].strip() in by_id
                else "<missing parent>",
                r["status"].strip(),
            )
            for r in linked
        )
        for (parent, child), count in sorted(pair_counts.items(), key=lambda kv: -kv[1]):
            out(f"        {parent:<20} -> {child:<20} {count:>6}")
        known_ids = set(by_id)
        dangling = sorted({r["linked_event_id"].strip() for r in linked} - known_ids)
        out(f"    linked_event_id values not present as an event_id: {len(dangling)}")
        for value in dangling[:20]:
            out(f"        {value}")
        target_counts = Counter(r["linked_event_id"].strip() for r in linked)
        multi = [(k, v) for k, v in target_counts.items() if v > 1]
        out(f"    link targets referenced by more than one row: {len(multi)}")
        for value, count in sorted(multi, key=lambda kv: -kv[1])[:20]:
            out(f"        {value:<36} {count:>5}")
        cross_user = [
            r["event_id"]
            for r in linked
            if r["linked_event_id"].strip() in by_id
            and by_id[r["linked_event_id"].strip()]["user_id"].strip()
            != r["user_id"].strip()
        ]
        out(f"    links crossing user boundaries: {len(cross_user)} {cross_user[:10]}")
    out()

    out("minimum_allowed_amount vs flexibility:")
    for flex, count in sorted(
        Counter(r.get("flexibility", "").strip() or "<blank>" for r in rows).items()
    ):
        with_min = sum(
            1
            for r in rows
            if (r.get("flexibility", "").strip() or "<blank>") == flex
            and not is_blank(r.get("minimum_allowed_amount", ""))
        )
        out(
            f"    flexibility={flex:<20} rows={count:<8} "
            f"with minimum_allowed_amount={with_min}"
        )
    bad_min = [
        r["event_id"]
        for r in rows
        if to_float(r.get("minimum_allowed_amount")) is not None
        and to_float(r.get("amount")) is not None
        and to_float(r["minimum_allowed_amount"]) > to_float(r["amount"])
    ]
    out(f"    rows where minimum_allowed_amount > amount: {len(bad_min)} {bad_min[:10]}")
    out()

    out("settlement_date vs event_date:")
    same = later = earlier = missing = 0
    for r in rows:
        event_date, settlement = r.get("event_date", "").strip(), r.get(
            "settlement_date", ""
        ).strip()
        if not settlement or not event_date:
            missing += 1
        elif settlement == event_date:
            same += 1
        elif settlement > event_date:
            later += 1
        else:
            earlier += 1
    out(f"    settlement == event : {same}")
    out(f"    settlement  > event : {later}")
    out(f"    settlement  < event : {earlier}")
    out(f"    one or both blank   : {missing}")
    out("    blank settlement_date by status:")
    for value, count in sorted(
        Counter(
            r["status"].strip() for r in rows if is_blank(r.get("settlement_date", ""))
        ).items(),
        key=lambda kv: -kv[1],
    ):
        out(f"        {value:<36} {count:>6}")
    out()


# ---------------------------------------------- financial_profiles focus


def audit_financial_profiles(fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    rule("#", "financial_profiles.csv - list-column contents")

    for column in sorted(LIST_COLUMNS):
        if column not in fieldnames:
            out(f"{column}: COLUMN ABSENT")
            out()
            continue
        token_counts: Counter[str] = Counter()
        combo_counts: Counter[str] = Counter()
        blank_rows = 0
        for r in rows:
            raw = r[column].strip()
            if not raw:
                blank_rows += 1
                combo_counts["<blank>"] += 1
                continue
            combo_counts[raw] += 1
            for token in tokens(raw):
                token_counts[token] += 1
        out(f"{column}")
        out(f"    rows with a blank value: {blank_rows} of {len(rows)}")
        out(f"    distinct tokens ({len(token_counts)}):")
        for value, count in sorted(token_counts.items(), key=lambda kv: (-kv[1], kv[0])):
            out(f"        {value:<44} {count:>5} users")
        out(f"    distinct whole-value combinations ({len(combo_counts)}):")
        for value, count in sorted(
            combo_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )[:DISTINCT_VALUE_LIMIT]:
            out(f"        {truncate(value, 58):<58} {count:>5}")
        if len(combo_counts) > DISTINCT_VALUE_LIMIT:
            out(
                f"        ... and {len(combo_counts) - DISTINCT_VALUE_LIMIT} "
                "more combinations"
            )
        out()

    if "max_installment_months" in fieldnames:
        out("max_installment_months")
        counts = Counter(
            r["max_installment_months"].strip() or "<blank>" for r in rows
        )
        for value, count in sorted(
            counts.items(), key=lambda kv: (kv[0] == "<blank>", num_key(kv[0]))
        ):
            out(f"    {value:<20} {count:>5} users")
        blank_but_installments = [
            r["user_id"]
            for r in rows
            if is_blank(r["max_installment_months"])
            and "installments" in tokens(r.get("payment_methods_user_will_consider", ""))
        ]
        set_but_no_installments = [
            r["user_id"]
            for r in rows
            if not is_blank(r["max_installment_months"])
            and "installments"
            not in tokens(r.get("payment_methods_user_will_consider", ""))
        ]
        out(
            "    users with blank max_installment_months but 'installments' "
            f"accepted: {len(blank_but_installments)}"
        )
        for value in blank_but_installments[:20]:
            out(f"        {value}")
        out(
            "    users with max_installment_months set but 'installments' NOT "
            f"accepted: {len(set_but_no_installments)}"
        )
        for value in set_but_no_installments[:20]:
            out(f"        {value}")
        out()

    out("minimum_balance_to_keep vs current_available_balance:")
    below = [
        r["user_id"]
        for r in rows
        if to_float(r.get("current_available_balance")) is not None
        and to_float(r.get("minimum_balance_to_keep")) is not None
        and to_float(r["current_available_balance"])
        < to_float(r["minimum_balance_to_keep"])
    ]
    out(f"    users already BELOW their minimum balance: {len(below)}")
    for value in below[:30]:
        out(f"        {value}")
    ratios = [
        to_float(r["minimum_balance_to_keep"]) / to_float(r["current_available_balance"])
        for r in rows
        if to_float(r.get("current_available_balance"))
        and to_float(r.get("minimum_balance_to_keep")) is not None
    ]
    if ratios:
        ordered = sorted(ratios)
        out(
            "    minimum/balance ratio  min={:.3f}  median={:.3f}  max={:.3f}".format(
                ordered[0], ordered[len(ordered) // 2], ordered[-1]
            )
        )
    out()

    out("protect / reduce / stop category overlap:")
    overlap_reduce: list[tuple[str, list[str]]] = []
    overlap_stop: list[tuple[str, list[str]]] = []
    overlap_both: list[tuple[str, list[str]]] = []
    for r in rows:
        protect = tokens(r.get("expense_categories_to_protect", ""))
        reduce_ = tokens(r.get("expense_categories_user_is_willing_to_reduce", ""))
        stop = tokens(r.get("expense_categories_user_is_willing_to_stop", ""))
        if protect & reduce_:
            overlap_reduce.append((r["user_id"], sorted(protect & reduce_)))
        if protect & stop:
            overlap_stop.append((r["user_id"], sorted(protect & stop)))
        if reduce_ & stop:
            overlap_both.append((r["user_id"], sorted(reduce_ & stop)))
    out(f"    categories both PROTECTED and reducible: {len(overlap_reduce)} users")
    for uid, cats in overlap_reduce[:20]:
        out(f"        {uid}: {', '.join(cats)}")
    out(f"    categories both PROTECTED and stoppable: {len(overlap_stop)} users")
    for uid, cats in overlap_stop[:20]:
        out(f"        {uid}: {', '.join(cats)}")
    out(f"    categories both reducible and stoppable: {len(overlap_both)} users")
    for uid, cats in overlap_both[:20]:
        out(f"        {uid}: {', '.join(cats)}")
    out()

    out("users who accept no payment method at all:")
    none_accepted = [
        r["user_id"]
        for r in rows
        if not tokens(r.get("payment_methods_user_will_consider", ""))
    ]
    out(f"    {len(none_accepted)} {none_accepted[:20]}")
    out()


# ------------------------------------------------------- cross-file checks


def cross_file_checks(
    data: dict[str, tuple[list[str], list[dict[str, str]]]]
) -> None:
    rule("#", "CROSS-FILE CHECKS vs AGENTS.md / problem_statement.md")

    profiles = {r["user_id"]: r for r in data["financial_profiles.csv"][1]}
    events = data["financial_events.csv"][1]
    requests = data["requests.csv"][1]
    samples = data["sample_requests.csv"][1]
    options = data["request_payment_options.csv"][1]
    messages = data["messages.csv"][1]
    images = data["images.csv"][1]
    rates = data["exchange_rates.csv"][1]
    template = data["output.csv"][1]

    request_ids = [r["request_id"] for r in requests]
    request_id_set = set(request_ids)
    sample_ids = {r["request_id"] for r in samples}
    event_ids = {r["event_id"].strip() for r in events}

    out(f"requests.csv rows: {len(requests)}  (README states 250)")
    out(f"sample_requests.csv rows: {len(samples)}  (README states 25)")
    out(f"dataset/output.csv template rows: {len(template)}")
    template_ids = [r["request_id"] for r in template]
    out(
        "    request_id sets identical (requests vs output template): "
        f"{request_id_set == set(template_ids)}"
    )
    out(f"    request_id order identical: {request_ids == template_ids}")
    out(
        "    request_ids appearing in BOTH requests.csv and sample_requests.csv: "
        f"{len(request_id_set & sample_ids)}"
    )
    duplicates = [k for k, v in Counter(request_ids).items() if v > 1]
    out(f"    duplicate request_id values in requests.csv: {len(duplicates)} {duplicates[:10]}")
    out()

    out("users:")
    out(f"    financial_profiles.csv users: {len(profiles)}")
    request_users = {r["user_id"] for r in requests}
    sample_users = {r["user_id"] for r in samples}
    event_users = {r["user_id"] for r in events}
    out(f"    distinct user_id in requests.csv: {len(request_users)}")
    out(f"    request users missing a profile: {sorted(request_users - set(profiles))}")
    out(
        "    request users with no financial_events rows: "
        f"{sorted(request_users - event_users)}"
    )
    per_user = Counter(r["user_id"] for r in requests)
    out(f"    exactly one request per user: {all(v == 1 for v in per_user.values())}")
    out(f"    sample users also appearing in requests.csv: {len(sample_users & request_users)}")
    out(f"    profile users never referenced by any request: "
        f"{len(set(profiles) - request_users - sample_users)}")
    out()

    out("request_payment_options.csv:")
    per_request = Counter(r["request_id"] for r in options)
    out(f"    distinct request_id covered: {len(per_request)}")
    out("    option-count distribution (contract says 2-4 per request):")
    for count, n in sorted(Counter(per_request.values()).items()):
        flag = "" if 2 <= count <= 4 else "   <-- OUTSIDE THE STATED 2-4 RANGE"
        out(f"        {count} options: {n} requests{flag}")
    no_options = sorted(request_id_set - set(per_request))
    out(f"    evaluation requests with NO payment options: {len(no_options)} {no_options[:10]}")
    methods_by_request: dict[str, set[str]] = {}
    for option in options:
        methods_by_request.setdefault(option["request_id"], set()).add(
            option["payment_method"].strip()
        )
    no_full = sorted(
        rid
        for rid in request_id_set
        if rid in methods_by_request and "full_payment" not in methods_by_request[rid]
    )
    out(f"    evaluation requests with no full_payment option: {len(no_full)} {no_full[:10]}")
    out(
        "    payment_method values: "
        f"{dict(Counter(o['payment_method'].strip() for o in options))}"
    )
    out(
        "    number_of_payments == 1 rows that are not full_payment: "
        + str(
            sum(
                1
                for o in options
                if o["number_of_payments"].strip() == "1"
                and o["payment_method"].strip() != "full_payment"
            )
        )
    )
    out(
        "    blank payment_frequency_days on installment rows: "
        + str(
            sum(
                1
                for o in options
                if o["payment_method"].strip() == "installments"
                and is_blank(o["payment_frequency_days"])
            )
        )
    )
    out(
        "    non-zero financing_fee on full_payment rows: "
        + str(
            sum(
                1
                for o in options
                if o["payment_method"].strip() == "full_payment"
                and (to_float(o["financing_fee"]) or 0) != 0
            )
        )
    )
    mismatch = []
    for option in options:
        amount = to_float(option["payment_amount"])
        count = to_float(option["number_of_payments"])
        total = to_float(option["total_payable_amount"])
        if amount is None or count is None or total is None:
            continue
        if abs(amount * count - total) > max(0.02, abs(total) * 1e-6):
            mismatch.append(
                (option["payment_option_id"], round(amount * count, 2), total)
            )
    out(
        "    options where payment_amount * number_of_payments != "
        f"total_payable_amount: {len(mismatch)}"
    )
    for pid, computed, total in mismatch[:15]:
        out(f"        {pid}: computed {computed} vs stated {total}")
    fee_mismatch = []
    request_by_id = {r["request_id"]: r for r in requests}
    all_requests_by_id = dict(request_by_id)
    for r in samples:
        all_requests_by_id.setdefault(r["request_id"], r)
    for option in options:
        request = all_requests_by_id.get(option["request_id"])
        if not request:
            continue
        requested = to_float(request.get("requested_amount"))
        total = to_float(option["total_payable_amount"])
        fee = to_float(option["financing_fee"])
        if requested is None or total is None or fee is None:
            continue
        if abs(requested + fee - total) > max(0.02, abs(total) * 1e-6):
            fee_mismatch.append(
                (option["payment_option_id"], requested, fee, total)
            )
    out(
        "    options where requested_amount + financing_fee != "
        f"total_payable_amount: {len(fee_mismatch)}"
    )
    for pid, requested, fee, total in fee_mismatch[:15]:
        out(f"        {pid}: {requested} + {fee} = {round(requested + fee, 2)} vs stated {total}")
    early = [
        o["payment_option_id"]
        for o in options
        if o["request_id"] in all_requests_by_id
        and o["first_payment_date"].strip()
        < all_requests_by_id[o["request_id"]]["request_date"].strip()
    ]
    out(f"    options whose first_payment_date precedes request_date: {len(early)} {early[:10]}")
    out()

    out("installment options vs profile preferences:")
    exceed = []
    offered_to_non_acceptors = set()
    for option in options:
        if option["payment_method"].strip() != "installments":
            continue
        request = all_requests_by_id.get(option["request_id"])
        if not request:
            continue
        profile = profiles.get(request["user_id"])
        if not profile:
            continue
        cap = to_float(profile.get("max_installment_months"))
        count = to_float(option["number_of_payments"])
        if cap is not None and count is not None and count > cap:
            exceed.append(option["payment_option_id"])
        if "installments" not in tokens(
            profile.get("payment_methods_user_will_consider", "")
        ):
            offered_to_non_acceptors.add(request["user_id"])
    out(
        "    installment options exceeding the user's max_installment_months: "
        f"{len(exceed)} {exceed[:10]}"
    )
    out(
        "    users offered installments who do not accept installments: "
        f"{len(offered_to_non_acceptors)}"
    )
    partial_not_allowed_but_offered = [
        o["payment_option_id"]
        for o in options
        if o["payment_method"].strip() == "partial_payment"
    ]
    out(
        "    partial_payment rows present in request_payment_options.csv: "
        f"{len(partial_not_allowed_but_offered)}"
    )
    out()

    out("images.csv:")
    out(f"    rows: {len(images)}")
    present_files = (
        sorted(p.name for p in MEDIA_IMAGES_DIR.glob("*.png"))
        if MEDIA_IMAGES_DIR.exists()
        else []
    )
    out(f"    PNG files in dataset/media/images: {len(present_files)}")
    referenced = [r["image_id"].strip() for r in images]
    missing_files = [
        i for i in referenced if not (MEDIA_IMAGES_DIR / f"{i}.png").exists()
    ]
    orphan_files = sorted(set(present_files) - {f"{i}.png" for i in referenced})
    out(f"    image_ids with NO matching PNG on disk: {len(missing_files)} {missing_files[:10]}")
    out(f"    PNG files not referenced by images.csv: {len(orphan_files)} {orphan_files[:10]}")
    image_events = {
        r["related_event_id"].strip()
        for r in images
        if not is_blank(r["related_event_id"])
    }
    out(
        "    related_event_id values not found in financial_events.csv: "
        f"{sorted(image_events - event_ids)}"
    )
    image_requests = {
        r["request_id"].strip() for r in images if not is_blank(r["request_id"])
    }
    out(
        "    image request_ids not in requests.csv: "
        f"{sorted(image_requests - request_id_set)}"
    )
    blank_amount_ids = {
        r["event_id"].strip() for r in events if is_blank(r.get("amount", ""))
    }
    out(
        f"    blank-amount events: {len(blank_amount_ids)}; of those linked from "
        f"images.csv: {len(blank_amount_ids & image_events)}"
    )
    out(
        "    blank-amount events with NO image link: "
        f"{sorted(blank_amount_ids - image_events)}"
    )
    out(
        "    images pointing at an event that already has an amount: "
        f"{sorted(image_events - blank_amount_ids)}"
    )
    out(
        "    evaluation requests with an image: "
        f"{len(image_requests & request_id_set)} of {len(request_ids)}"
    )
    out()

    out("messages.csv:")
    out(f"    rows: {len(messages)}")
    out(
        "    source_type values: "
        f"{dict(Counter(m['source_type'].strip() for m in messages))}"
    )
    out(
        "    rows with a related_event_id: "
        f"{sum(1 for m in messages if not is_blank(m['related_event_id']))}"
    )
    out(
        "    rows with a request_id: "
        f"{sum(1 for m in messages if not is_blank(m['request_id']))}"
    )
    message_events = {
        m["related_event_id"].strip()
        for m in messages
        if not is_blank(m["related_event_id"])
    }
    out(
        "    related_event_id values not found in financial_events.csv: "
        f"{sorted(message_events - event_ids)}"
    )
    message_requests = {
        m["request_id"].strip() for m in messages if not is_blank(m["request_id"])
    }
    out(
        "    message request_ids in neither requests.csv nor sample_requests.csv: "
        f"{sorted(message_requests - request_id_set - sample_ids)}"
    )
    message_users = {
        m["user_id"].strip() for m in messages if not is_blank(m["user_id"])
    }
    out(f"    message user_ids with no profile: {sorted(message_users - set(profiles))}")
    out(
        "    evaluation requests with at least one message: "
        f"{len(message_requests & request_id_set)} of {len(request_ids)}"
    )
    per_request_messages = Counter(
        m["request_id"] for m in messages if m["request_id"] in request_id_set
    )
    out(
        "    messages-per-request distribution: "
        f"{dict(sorted(Counter(per_request_messages.values()).items()))}"
    )
    non_ascii = sum(
        1 for m in messages if any(ord(c) > 127 for c in m["message_text"])
    )
    out(f"    messages containing non-ASCII characters: {non_ascii}")
    imperative = [
        m["message_id"]
        for m in messages
        if re.search(
            r"\b(ignore (the |all )?(previous|above|prior)|you must|instruct(ion)?s?:|"
            r"disregard|override|system prompt)\b",
            m["message_text"],
            re.IGNORECASE,
        )
    ]
    out(
        "    messages containing instruction-like phrasing (untrusted-content "
        f"watchlist): {len(imperative)} {imperative[:10]}"
    )
    out()

    out("exchange_rates.csv:")
    pairs = Counter(
        (r["from_currency"].strip(), r["to_currency"].strip()) for r in rates
    )
    out(f"    distinct currency pairs: {len(pairs)}")
    for (frm, to), count in sorted(pairs.items()):
        out(f"        {frm} -> {to:<6} {count:>5} dated rows")
    home_currencies = {p["home_currency"].strip() for p in profiles.values()}
    out(f"    home_currency values in profiles: {sorted(home_currencies)}")
    out(
        "    home currencies never present as a to_currency: "
        f"{sorted(home_currencies - {to for _, to in pairs})}"
    )
    event_currencies = {
        e["currency"].strip() for e in events if not is_blank(e["currency"])
    }
    out(f"    currencies used by financial_events: {sorted(event_currencies)}")
    reverse_missing = sorted({(a, b) for (a, b) in pairs if (b, a) not in pairs})
    out(f"    pairs supplied in one direction only: {len(reverse_missing)}")
    for pair in reverse_missing:
        out(f"        {pair[0]} -> {pair[1]}")
    rate_dates = sorted({r["rate_date"].strip() for r in rates})
    out(
        f"    rate_date range: {rate_dates[0]} .. {rate_dates[-1]}  "
        f"({len(rate_dates)} distinct dates)"
    )
    out(f"    rate_date day-of-month values: {sorted({d[-2:] for d in rate_dates})}")
    duplicate_rates = [
        k
        for k, v in Counter(
            (
                r["rate_date"].strip(),
                r["from_currency"].strip(),
                r["to_currency"].strip(),
            )
            for r in rates
        ).items()
        if v > 1
    ]
    out(f"    duplicate (date, from, to) rows: {len(duplicate_rates)} {duplicate_rates[:5]}")
    out()

    out("foreign-currency events vs available rates:")
    rate_index = {
        (
            r["rate_date"].strip(),
            r["from_currency"].strip(),
            r["to_currency"].strip(),
        )
        for r in rates
    }
    rate_dates_by_pair: dict[tuple[str, str], list[str]] = {}
    for r in rates:
        rate_dates_by_pair.setdefault(
            (r["from_currency"].strip(), r["to_currency"].strip()), []
        ).append(r["rate_date"].strip())
    unconvertible: list[tuple[str, str, str, str, str]] = []
    foreign = 0
    for event in events:
        profile = profiles.get(event["user_id"].strip())
        if not profile:
            continue
        currency = event["currency"].strip()
        home = profile["home_currency"].strip()
        if not currency or currency == home:
            continue
        foreign += 1
        settlement = event["settlement_date"].strip() or event["event_date"].strip()
        if (settlement, currency, home) not in rate_index:
            has_pair = (currency, home) in rate_dates_by_pair
            reason = "no rate on that date" if has_pair else "pair absent entirely"
            unconvertible.append(
                (event["event_id"], settlement, currency, home, reason)
            )
    out(f"    events in a currency other than the user's home_currency: {foreign}")
    out(
        "    of those, with NO exact (settlement_date, from, to) rate row: "
        f"{len(unconvertible)}"
    )
    for event_id, settlement, frm, to, reason in unconvertible[:25]:
        out(f"        {event_id:<14} {settlement}  {frm} -> {to:<5} ({reason})")
    if len(unconvertible) > 25:
        out(f"        ... and {len(unconvertible) - 25} more")
    out()

    out("requests.csv field sanity:")
    out(
        "    allows_partial_payment values: "
        f"{dict(Counter(r['allows_partial_payment'].strip() for r in requests))}"
    )
    out(
        "    request_type values: "
        f"{dict(Counter(r['request_type'].strip() for r in requests))}"
    )
    backwards = [
        r["request_id"]
        for r in requests
        if r["desired_completion_date"].strip()
        and r["request_date"].strip()
        and r["desired_completion_date"].strip() < r["request_date"].strip()
    ]
    out(f"    desired_completion_date BEFORE request_date: {len(backwards)} {backwards[:10]}")
    blank_deadline = [
        r["request_id"] for r in requests if is_blank(r["desired_completion_date"])
    ]
    out(f"    blank desired_completion_date: {len(blank_deadline)} {blank_deadline[:10]}")
    far = [
        r["request_id"]
        for r in requests
        if r["desired_completion_date"].strip()
        and (days_between(r["request_date"], r["desired_completion_date"]) or 0) > 90
    ]
    out(
        "    desired_completion_date more than 90 days after request_date: "
        f"{len(far)} {far[:10]}"
    )
    horizon_gaps = [
        days_between(r["request_date"], r["desired_completion_date"])
        for r in requests
        if r["desired_completion_date"].strip()
    ]
    horizon_gaps = [g for g in horizon_gaps if g is not None]
    if horizon_gaps:
        ordered = sorted(horizon_gaps)
        out(
            "    days from request_date to desired_completion_date: "
            f"min={ordered[0]} median={ordered[len(ordered) // 2]} max={ordered[-1]}"
        )
    nonpositive = [
        r["request_id"] for r in requests if (to_float(r["requested_amount"]) or 0) <= 0
    ]
    out(f"    non-positive requested_amount: {len(nonpositive)} {nonpositive[:10]}")
    dates = sorted(r["request_date"].strip() for r in requests)
    out(f"    request_date range: {dates[0]} .. {dates[-1]}")
    out()

    out("requested_amount vs the user's balance and minimum:")
    over_balance = 0
    over_headroom = 0
    for request in requests:
        profile = profiles.get(request["user_id"])
        if not profile:
            continue
        amount = to_float(request["requested_amount"])
        balance = to_float(profile["current_available_balance"])
        minimum = to_float(profile["minimum_balance_to_keep"])
        if amount is None or balance is None or minimum is None:
            continue
        if amount > balance:
            over_balance += 1
        if amount > balance - minimum:
            over_headroom += 1
    out(f"    requests larger than current_available_balance: {over_balance} of {len(requests)}")
    out(
        "    requests larger than (balance - minimum_balance_to_keep): "
        f"{over_headroom} of {len(requests)}"
    )
    out("    (an upper bound on how many can be affordable_now from the profile alone)")
    out()

    out("events available relative to each request_date:")
    events_by_user: dict[str, list[dict[str, str]]] = {}
    for event in events:
        events_by_user.setdefault(event["user_id"].strip(), []).append(event)
    future_rows = 0
    requests_with_future = 0
    beyond_90 = 0
    for request in requests:
        request_date = request["request_date"].strip()
        future = [
            e
            for e in events_by_user.get(request["user_id"].strip(), [])
            if (e["settlement_date"].strip() or e["event_date"].strip()) > request_date
        ]
        future_rows += len(future)
        if future:
            requests_with_future += 1
        beyond_90 += sum(
            1
            for e in future
            if (
                days_between(
                    request_date, e["settlement_date"].strip() or e["event_date"].strip()
                )
                or 0
            )
            > 90
        )
    out(
        f"    requests whose user has events dated AFTER request_date: "
        f"{requests_with_future} of {len(requests)}"
    )
    out(f"    total future-dated event rows: {future_rows}")
    out(f"    of those, dated beyond the 90-day horizon: {beyond_90}")
    out(
        "    history depth per user (events at or before request_date) "
        "min/median/max:"
    )
    depths = []
    for request in requests:
        request_date = request["request_date"].strip()
        depths.append(
            sum(
                1
                for e in events_by_user.get(request["user_id"].strip(), [])
                if e["event_date"].strip() <= request_date
            )
        )
    if depths:
        ordered = sorted(depths)
        out(
            f"        {ordered[0]} / {ordered[len(ordered) // 2]} / {ordered[-1]}"
        )
    out()

    out("sample_requests.csv - label distributions (format reference, not labels):")
    for column in (
        "affordability_status",
        "recommended_payment_method",
        "spending_changes_needed",
    ):
        counts = Counter(r[column].strip() or "<blank>" for r in samples)
        out(f"    {column}: {dict(sorted(counts.items(), key=lambda kv: -kv[1]))}")
    now_rows = [r for r in samples if r["affordability_status"].strip() == "affordable_now"]
    out(
        "    affordable_now rows where earliest_date_for_full_payment == "
        "request_date: "
        + str(
            sum(
                1
                for r in now_rows
                if r["earliest_date_for_full_payment"].strip() == r["request_date"].strip()
            )
        )
        + f" of {len(now_rows)}"
    )
    partial_rows = [
        r for r in samples if r["recommended_payment_method"].strip() == "partial_payment"
    ]
    out(
        f"    partial_payment rows: {len(partial_rows)}; all with status "
        "affordable_with_plan: "
        + str(
            all(
                r["affordability_status"].strip() == "affordable_with_plan"
                for r in partial_rows
            )
        )
    )
    out(
        "    partial_payment rows with exactly two plan entries: "
        + str(sum(1 for r in partial_rows if len(r["payment_plan"].split("|")) == 2))
    )
    bad_bounds = [
        r["request_id"]
        for r in samples
        if not (
            0
            <= (to_float(r["amount_safe_to_pay"]) or -1)
            <= (to_float(r["requested_amount"]) or 0)
        )
    ]
    out(
        "    sample rows violating 0 <= amount_safe_to_pay <= requested_amount: "
        f"{len(bad_bounds)} {bad_bounds}"
    )
    empty_earliest = [
        r["request_id"]
        for r in samples
        if is_blank(r["earliest_date_for_full_payment"])
    ]
    out(
        f"    sample rows with an empty earliest_date_for_full_payment: "
        f"{len(empty_earliest)} {empty_earliest}"
    )
    late_earliest = [
        r["request_id"]
        for r in samples
        if not is_blank(r["earliest_date_for_full_payment"])
        and not is_blank(r["desired_completion_date"])
        and r["earliest_date_for_full_payment"].strip()
        > r["desired_completion_date"].strip()
    ]
    out(
        "    sample rows where earliest_date_for_full_payment is after "
        f"desired_completion_date: {len(late_earliest)} {late_earliest}"
    )
    options_by_request: dict[str, list[dict[str, str]]] = {}
    for option in options:
        options_by_request.setdefault(option["request_id"], []).append(option)
    unmatched = []
    for r in samples:
        if r["recommended_payment_method"].strip() != "installments":
            continue
        entries = [p.split(":") for p in r["payment_plan"].split("|") if ":" in p]
        count = len(entries)
        amounts = {round(to_float(e[1]) or 0, 2) for e in entries}
        matched = any(
            option["payment_method"].strip() == "installments"
            and (to_float(option["number_of_payments"]) or 0) == count
            and round(to_float(option["payment_amount"]) or 0, 2) in amounts
            for option in options_by_request.get(r["request_id"], [])
        )
        if not matched:
            unmatched.append(r["request_id"])
    out(
        "    installment sample rows whose plan does NOT match a supplied option: "
        f"{len(unmatched)} {unmatched}"
    )
    plan_sum_mismatch = []
    for r in samples:
        entries = [p.split(":") for p in r["payment_plan"].split("|") if ":" in p]
        if not entries:
            continue
        total = sum(to_float(e[1]) or 0 for e in entries)
        requested = to_float(r["requested_amount"]) or 0
        if abs(total - requested) > max(0.02, requested * 1e-6):
            plan_sum_mismatch.append(
                (
                    r["request_id"],
                    r["recommended_payment_method"].strip(),
                    round(total, 2),
                    requested,
                )
            )
    out(
        "    sample rows whose payment_plan does not sum to requested_amount: "
        f"{len(plan_sum_mismatch)}"
    )
    for rid, method, total, requested in plan_sum_mismatch:
        out(f"        {rid:<14} {method:<16} plan sums to {total} vs requested {requested}")
    out(
        "    (expected for installments: the plan sums to total_payable_amount, "
        "which includes the financing fee)"
    )
    first_payment_off = [
        r["request_id"]
        for r in samples
        if r["payment_plan"].strip() not in {"", "none"}
        and r["payment_plan"].split(":")[0].strip() != r["request_date"].strip()
    ]
    out(
        "    sample rows whose first plan date is not request_date: "
        f"{len(first_payment_off)} {first_payment_off[:10]}"
    )
    out()


def main() -> int:
    if not DATASET_DIR.is_dir():
        print(f"dataset directory not found: {DATASET_DIR}", file=sys.stderr)
        return 1

    csv_paths = sorted(DATASET_DIR.glob("*.csv"))

    rule("=")
    out("BUY OR WAIT? - PHASE 1 DATA AUDIT")
    rule("=")
    out(f"repository root : {REPO_ROOT}")
    out(f"dataset         : {DATASET_DIR}")
    out(f"csv files       : {len(csv_paths)} ({', '.join(p.name for p in csv_paths)})")
    media_count = (
        len(list(MEDIA_IMAGES_DIR.glob("*"))) if MEDIA_IMAGES_DIR.exists() else 0
    )
    out(f"media/images    : {media_count} files")
    out(
        "distinct-value listing threshold: fewer than "
        f"{DISTINCT_VALUE_LIMIT} distinct values"
    )
    out()

    data: dict[str, tuple[list[str], list[dict[str, str]]]] = {}
    for path in csv_paths:
        data[path.name] = audit_file(path)

    if "financial_events.csv" in data:
        audit_financial_events(*data["financial_events.csv"])
    if "financial_profiles.csv" in data:
        audit_financial_profiles(*data["financial_profiles.csv"])

    required = {
        "financial_profiles.csv",
        "financial_events.csv",
        "requests.csv",
        "sample_requests.csv",
        "request_payment_options.csv",
        "messages.csv",
        "images.csv",
        "exchange_rates.csv",
        "output.csv",
    }
    if required <= set(data):
        cross_file_checks(data)
    else:
        rule("#", "CROSS-FILE CHECKS SKIPPED")
        out(f"missing files: {sorted(required - set(data))}")

    rule("=")
    out("END OF AUDIT")
    rule("=")
    return 0


if __name__ == "__main__":
    sys.exit(main())

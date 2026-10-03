"""Measure deterministic-extractor coverage over ``messages.csv``.

Writes ``audit/extraction_coverage.md``: per-template hit counts, the overall
share of messages that at least one template classifies, and the full text of
every message that nothing matched. The unmatched list is the input to the
LLM-fallback design -- its shape depends entirely on whether there are five
stragglers or two hundred.

Run from anywhere::

    py audit/extraction_coverage.py
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.patterns import TEMPLATES, FactKind, is_boilerplate  # noqa: E402

DATASET = REPO_ROOT / "dataset"
REPORT = REPO_ROOT / "audit" / "extraction_coverage.md"

#: Below this, stop and reconsider before building the fallback.
COVERAGE_GATE = 0.85


def read_messages() -> list[dict[str, str]]:
    with (DATASET / "messages.csv").open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def main() -> int:
    messages = read_messages()
    template_hits: Counter[str] = Counter()
    kind_hits: Counter[str] = Counter()
    per_message: dict[str, list[str]] = {}
    unmatched: list[dict[str, str]] = []
    multi: list[tuple[str, list[str]]] = []
    by_language: Counter[tuple[str, bool]] = Counter()

    for row in messages:
        text = row["message_text"]
        fired = [t.name for t in TEMPLATES if t.search(text)]
        kinds = {t.kind.value for t in TEMPLATES if t.search(text)}
        per_message[row["message_id"]] = fired
        indonesian = any(ord(c) > 127 for c in text) or bool(
            re.search(r"\b(Anda|yang|dan|untuk|pada|akan)\b", text)
        )
        by_language[(row["source_type"], indonesian)] += 1
        if fired:
            for name in fired:
                template_hits[name] += 1
            for kind in kinds:
                kind_hits[kind] += 1
            if len(kinds) > 1:
                multi.append((row["message_id"], sorted(kinds)))
        else:
            unmatched.append(row)

    matched = len(messages) - len(unmatched)
    coverage = matched / len(messages) if messages else 0.0

    lines: list[str] = []
    add = lines.append
    add("# Deterministic extraction coverage")
    add("")
    add(f"- messages: **{len(messages)}**")
    add(f"- classified by at least one template: **{matched}**")
    add(f"- unmatched: **{len(unmatched)}**")
    add(f"- **coverage: {coverage:.1%}** (gate: {COVERAGE_GATE:.0%})")
    add("")
    add(
        "Gate "
        + ("**PASSED** -- proceed to the LLM fallback." if coverage >= COVERAGE_GATE
           else "**FAILED** -- stop and report before building the fallback.")
    )
    add("")

    add("## Hits per template")
    add("")
    add("| template | kind | messages | note |")
    add("| --- | --- | ---: | --- |")
    for template in TEMPLATES:
        add(
            f"| `{template.name}` | `{template.kind.value}` | "
            f"{template_hits.get(template.name, 0)} | {template.note} |"
        )
    add("")

    add("## Hits per fact kind")
    add("")
    add("| kind | messages |")
    add("| --- | ---: |")
    for kind, count in sorted(kind_hits.items(), key=lambda kv: (-kv[1], kv[0])):
        add(f"| `{kind}` | {count} |")
    add("")

    zero = [t.name for t in TEMPLATES if template_hits.get(t.name, 0) == 0]
    add(f"## Templates that never fired ({len(zero)})")
    add("")
    if zero:
        add(
            "These are defensive -- written from the message vocabulary but not "
            "exercised by the current dataset."
        )
        add("")
        for name in zero:
            add(f"- `{name}`")
    else:
        add("None; every template earns its place.")
    add("")

    add(f"## Messages matching more than one kind ({len(multi)})")
    add("")
    add(
        "Expected: a single message often carries two claims, such as a salary "
        "resuming *and* a new recurring expense starting."
    )
    add("")
    for message_id, kinds in multi[:40]:
        add(f"- `{message_id}`: {', '.join(f'`{k}`' for k in kinds)}")
    if len(multi) > 40:
        add(f"- ... and {len(multi) - 40} more")
    add("")

    add(f"## Unmatched messages ({len(unmatched)})")
    add("")
    if not unmatched:
        add("None.")
    else:
        add("Full text, for fallback design:")
        add("")
        for row in unmatched:
            residue = [s for s in sentences(row["message_text"]) if not is_boilerplate(s)]
            add(f"### `{row['message_id']}` ({row['source_type']}, user `{row['user_id']}`)")
            add("")
            add(f"> {row['message_text']}")
            add("")
            if residue:
                add("Non-boilerplate residue:")
                for sentence in residue:
                    add(f"- {sentence}")
            else:
                add("_Entirely boilerplate; no financial claim._")
            add("")

    add("## Source-type / language split")
    add("")
    add("| source_type | english | indonesian |")
    add("| --- | ---: | ---: |")
    for source in sorted({s for s, _ in by_language}):
        add(
            f"| {source} | {by_language.get((source, False), 0)} | "
            f"{by_language.get((source, True), 0)} |"
        )
    add("")

    REPORT.write_text("\n".join(lines), encoding="utf-8")

    print(f"messages          : {len(messages)}")
    print(f"matched           : {matched}")
    print(f"unmatched         : {len(unmatched)}")
    print(f"coverage          : {coverage:.1%}  (gate {COVERAGE_GATE:.0%})")
    print(f"templates firing  : {len(template_hits)}/{len(TEMPLATES)}")
    print(f"report            : {REPORT}")
    if unmatched:
        print()
        print("unmatched message ids:")
        for row in unmatched:
            print(f"   {row['message_id']} ({row['source_type']}) {row['message_text'][:110]}")
    return 0 if coverage >= COVERAGE_GATE else 2


if __name__ == "__main__":
    sys.exit(main())

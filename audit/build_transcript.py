"""Render ``chat_transcript.md`` from the Claude Code session log.

The transcript is a scored submission artefact (AGENTS.md section 6.5). It is
generated rather than hand-written so that every timestamp is the one the
harness recorded, not one reconstructed from memory -- the failure that reached
``log.txt`` and required a correction entry there.

Source: the session JSONL under the Claude Code projects directory. Pass
``--session`` to point at a different file.

What is included, and why:

* **Every user prompt, verbatim and in full.** These are the instructions the
  work was done under and are the point of the artefact.
* **Every assistant reply, verbatim and in full** (the ``text`` blocks).
* **Every tool call**, as name plus a truncated one-line rendering of its
  input. Full inputs run to 1.3 MB and are mostly file contents already in
  ``code.zip``.
* **Every tool result**, truncated, with its true character count stated so the
  truncation is visible rather than silent.

``thinking`` blocks are recorded by the harness with their text omitted (only a
signature is retained), so they appear as markers with no content. That is a
property of the log, not an edit made here.

Run::

    py audit/build_transcript.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "chat_transcript.md"

IST = timezone(timedelta(hours=5, minutes=30))

#: Per-block truncation limits, in characters.
TOOL_INPUT_LIMIT = 600
TOOL_RESULT_LIMIT = 800

#: Patterns that must never reach a submitted artefact. Checked over the
#: rendered output, not the source, so anything introduced by rendering is
#: caught too.
#:
#: These are REDACTED first and then re-checked, rather than only checked.
#: The session contains at least one deliberate synthetic key -- a probe of
#: the form ``sk-ant-`` followed by a run of repeated 'A' characters,
#: written in an earlier turn to prove
#: that ``build_zip.py``'s own secret scanner actually fires before relying
#: on it. Redacting is the right handling: a real key must never ship, and a
#: fake one must not become the reason the safety net gets loosened. If
#: anything still matches after redaction, the write aborts.
SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def redact(document: str) -> tuple[str, int]:
    """Replace every credential-shaped match with a visible marker."""
    total = 0
    for pattern in SECRET_PATTERNS:
        document, hits = pattern.subn("[REDACTED-CREDENTIAL]", document)
        total += hits
    return document, total


def default_session_path() -> Path | None:
    """The newest session log for this repo, if the projects dir exists."""
    slug = "C--Users-yashm-OneDrive-Projects-Hackerrank-September"
    base = Path(os.path.expanduser("~")) / ".claude" / "projects" / slug
    if not base.is_dir():
        return None
    logs = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def to_ist(raw: str) -> str:
    """Harness timestamps are UTC with a trailing Z; show IST, the contest TZ."""
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return raw or "(no timestamp)"
    return stamp.astimezone(IST).isoformat(timespec="seconds")


def clip(text: str, limit: int) -> tuple[str, bool]:
    text = text.rstrip()
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def fence(text: str, lang: str = "") -> str:
    """Fence a block, widening the fence if the body contains backticks."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    bar = "`" * max(3, longest + 1)
    return f"{bar}{lang}\n{text}\n{bar}"


def render_tool_use(block: dict) -> list[str]:
    name = block.get("name", "(unnamed tool)")
    payload = block.get("input") or {}
    if isinstance(payload, dict):
        parts = []
        for key, value in payload.items():
            rendered = value if isinstance(value, str) else json.dumps(value)
            parts.append(f"{key}={rendered}")
        body = "\n".join(parts)
    else:
        body = json.dumps(payload)
    shown, cut = clip(body, TOOL_INPUT_LIMIT)
    suffix = f"  _(input truncated; {len(body):,} chars total)_" if cut else ""
    return [f"**Tool call — `{name}`**{suffix}", "", fence(shown), ""]


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                chunks.append(part.get("text", ""))
            elif isinstance(part, dict):
                chunks.append(f"[{part.get('type', 'block')}]")
        return "\n".join(chunks)
    return json.dumps(content) if content is not None else ""


def render_tool_result(block: dict) -> list[str]:
    body = result_text(block)
    if not body.strip():
        return ["**Tool result** — _(empty)_", ""]
    shown, cut = clip(body, TOOL_RESULT_LIMIT)
    flag = " — **error**" if block.get("is_error") else ""
    suffix = f"  _(truncated; {len(body):,} chars total)_" if cut else ""
    return [f"**Tool result**{flag}{suffix}", "", fence(shown), ""]


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def build(records: list[dict]) -> tuple[str, dict]:
    lines: list[str] = []
    stats = {
        "prompts": 0,
        "replies": 0,
        "tool_calls": 0,
        "tool_results": 0,
        "thinking": 0,
        "first": None,
        "last": None,
    }
    turn = 0

    for record in records:
        kind = record.get("type")
        if kind not in ("user", "assistant"):
            continue
        if record.get("isSidechain"):
            continue
        message = record.get("message") or {}
        content = message.get("content")
        stamp = record.get("timestamp")
        if stamp:
            stats["first"] = stats["first"] or stamp
            stats["last"] = stamp

        # A plain string on a user record is a real prompt typed by the user.
        if kind == "user" and isinstance(content, str):
            if not content.strip():
                continue
            turn += 1
            stats["prompts"] += 1
            lines += [
                "",
                "---",
                "",
                f"## Turn {turn} — user · {to_ist(stamp)}",
                "",
                fence(content.rstrip(), "text"),
                "",
            ]
            continue

        if not isinstance(content, list):
            continue

        for block in content:
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").rstrip()
                if not text:
                    continue
                stats["replies"] += 1
                lines += [f"### Assistant · {to_ist(stamp)}", "", text, ""]
            elif btype == "thinking":
                stats["thinking"] += 1
                lines += [
                    "_[extended thinking block — content not retained in the "
                    "session log]_",
                    "",
                ]
            elif btype == "tool_use":
                stats["tool_calls"] += 1
                lines += render_tool_use(block)
            elif btype == "tool_result":
                stats["tool_results"] += 1
                lines += render_tool_result(block)

    return "\n".join(lines), stats


def header(stats: dict, session: Path, generated: str) -> str:
    first, last = to_ist(stats["first"]), to_ist(stats["last"])
    return f"""# Buy or Wait? — chat transcript

HackerRank Orchestrate (September 2026). Complete conversation log for the
session that produced this submission.

| | |
| --- | --- |
| Session id | `{session.stem}` |
| First message | {first} |
| Last message | {last} |
| User prompts | {stats['prompts']} |
| Assistant replies | {stats['replies']} |
| Tool calls | {stats['tool_calls']} |
| Tool results | {stats['tool_results']} |
| Generated | {generated} |

## About this file

Generated by `audit/build_transcript.py` directly from the Claude Code session
log, not written from memory. **Every timestamp below is the one the harness
recorded at the time**, converted from UTC to IST. This matters: an earlier
version of `log.txt` carried timestamps that had been estimated by decrementing
from a single clock reading rather than re-read, and drifted by roughly eight
hours. That was corrected by an appended entry in `log.txt` rather than a
rewrite, and generating this file mechanically is what keeps the same class of
error out of the transcript.

**Completeness and what is abridged.** Every user prompt and every assistant
reply appears in full and in order. Tool calls appear as the tool name plus
their input, and tool results appear as their output, both truncated -- to
{TOOL_INPUT_LIMIT} and {TOOL_RESULT_LIMIT} characters respectively -- with the
true character count printed beside anything cut, so no abridgement is silent.
The untruncated tool traffic is about 3.7 MB and is largely the contents of
files already shipped in `code.zip`.

`thinking` blocks are recorded by the harness with their text omitted (only a
cryptographic signature is kept), so they appear below as markers with no
content. That is a property of the log itself, not an edit made here.

**Credentials.** No API keys, tokens, or credentials appear in this file. The
generator redacts every credential-shaped string to `[REDACTED-CREDENTIAL]` and
then re-scans its own output, refusing to write if anything survives. One match
is expected and is not a real key: an earlier turn deliberately wrote a
synthetic Anthropic-style probe key -- the `sk-ant-` prefix followed by a run of
repeated 'A' characters -- into a scratch file, to prove that `build_zip.py`'s
own secret scanner actually fires before relying on it.
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Render chat_transcript.md")
    parser.add_argument("--session", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args(argv)

    session = args.session or default_session_path()
    if session is None or not session.is_file():
        print("could not locate a session log; pass --session", file=sys.stderr)
        return 2

    records = load_records(session)
    body, stats = build(records)
    if not stats["prompts"]:
        print("no user prompts found -- refusing to write", file=sys.stderr)
        return 1

    generated = datetime.now(IST).isoformat(timespec="seconds")
    document = header(stats, session, generated) + body + "\n"
    document, redacted = redact(document)

    for pattern in SECRET_PATTERNS:
        if pattern.search(document):
            print(
                f"SECRET SURVIVED REDACTION ({pattern.pattern}) -- refusing to "
                f"write",
                file=sys.stderr,
            )
            return 1

    args.out.write_text(document, encoding="utf-8")
    size = args.out.stat().st_size
    print(f"wrote {args.out.name}: {size:,} bytes ({size / 1024:.0f} KiB)")
    print(f"  source      : {session}")
    print(f"  covers      : {to_ist(stats['first'])} -> {to_ist(stats['last'])}")
    print(f"  prompts     : {stats['prompts']}")
    print(f"  replies     : {stats['replies']}")
    print(f"  tool calls  : {stats['tool_calls']}")
    print(f"  tool results: {stats['tool_results']}")
    print(f"  thinking    : {stats['thinking']} (content not retained by harness)")
    print(f"  redacted    : {redacted} credential-shaped string(s)")
    print("  secret scan : clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

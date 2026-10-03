"""Build ``code.zip`` for submission, refusing to run if a secret is present.

The scan runs **before** anything is written. A hit fails loudly and produces
no archive, so a leaked key cannot reach a submission artefact.

Run from anywhere::

    py build_zip.py
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
ZIP_PATH = REPO_ROOT / "code.zip"

#: Submission-side limit; also a sanity bound on accidentally zipping the media
#: directory or the trace folder.
MAX_ZIP_BYTES = 50 * 1024 * 1024

INCLUDE_DIRS = ("src", "tests", "evaluation", "audit", "docs")
INCLUDE_FILES = ("README.md", "AGENTS.md", "problem_statement.md", "build_zip.py")

EXCLUDE_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    "traces",  # 275 per-request traces; regenerate with audit/rank_report.py
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip"}

#: Anything matching these must not be inside the archive.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic api key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{8,}")),
    ("openai api key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{24,}")),
    (
        "hardcoded assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|passwd|password|token)\b\s*[=:]\s*"
            r"['\"][A-Za-z0-9\-_]{16,}['\"]"
        ),
    ),
)

TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".jsonl", ".cfg", ".toml", ".ini", ".yml", ".yaml"}


def collect() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_FILES:
        path = REPO_ROOT / name
        if path.is_file():
            files.append(path)
    for directory in INCLUDE_DIRS:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if EXCLUDE_PARTS & set(path.relative_to(REPO_ROOT).parts):
                continue
            if path.suffix in EXCLUDE_SUFFIXES:
                continue
            files.append(path)
    return files


def scan(files: list[Path]) -> list[tuple[Path, int, str, str]]:
    hits: list[tuple[Path, int, str, str]] = []
    for path in files:
        if path.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for label, pattern in SECRET_PATTERNS:
                match = pattern.search(line)
                if match:
                    hits.append(
                        (
                            path.relative_to(REPO_ROOT),
                            line_number,
                            label,
                            match.group(0)[:12] + "...",
                        )
                    )
    return hits


def main() -> int:
    files = collect()
    print(f"candidate files: {len(files)}")

    hits = scan(files)
    if hits:
        print("\nSECRET SCAN FAILED -- no archive written:", file=sys.stderr)
        for path, line_number, label, sample in hits:
            print(f"  {path}:{line_number}  [{label}]  {sample}", file=sys.stderr)
        return 2
    print("secret scan: clean")

    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(REPO_ROOT).as_posix())

    size = ZIP_PATH.stat().st_size
    print(f"wrote {ZIP_PATH.name}: {len(files)} files, {size / 1024:.0f} KiB")
    if size > MAX_ZIP_BYTES:
        print(
            f"ERROR: {size / 1024 / 1024:.1f} MiB exceeds the "
            f"{MAX_ZIP_BYTES / 1024 / 1024:.0f} MiB limit",
            file=sys.stderr,
        )
        return 3
    print(f"size ok ({size / 1024 / 1024:.2f} MiB of "
          f"{MAX_ZIP_BYTES / 1024 / 1024:.0f} MiB)")

    with zipfile.ZipFile(ZIP_PATH) as archive:
        names = archive.namelist()
    required = [
        "README.md",
        "src/run.py",
        "src/validate.py",
        "evaluation/usage_report.md",
    ]
    missing = [name for name in required if name not in names]
    if missing:
        print(f"ERROR: required entries missing: {missing}", file=sys.stderr)
        return 4
    print(f"required entries present: {', '.join(required)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())



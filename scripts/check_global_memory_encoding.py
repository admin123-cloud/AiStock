#!/usr/bin/env python3
"""Check Codex global memory files for invalid UTF-8, BOM, and mojibake markers."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_ROOT = Path(r"C:\Users\Administrator\.codex\memories")
TARGET_NAMES = {
    "PROFILE.md",
    "ACTIVE.md",
    "LEARNINGS.md",
    "ERRORS.md",
    "FEATURE_REQUESTS.md",
}

MOJIBAKE_FRAGMENTS = tuple(
    chr(codepoint)
    for codepoint in (
        0x9366,
        0x9428,
        0x7EDB,
        0x95C8,
        0x7487,
        0x93C3,
        0x951B,
        0x9286,
        0x9225,
        0xFFFD,
    )
)

SUSPICIOUS_PATTERNS = [
    ("replacement character", re.compile(chr(0xFFFD))),
    ("classic mojibake marker", re.compile("[" + chr(0x00C3) + chr(0x00C2) + "]")),
    ("lost Chinese placeholder", re.compile(r"(?<![A-Za-z0-9_])\?{3,}(?![A-Za-z0-9_])")),
]


def iter_targets(root: Path) -> list[Path]:
    if not root.exists():
        raise SystemExit(f"memory root does not exist: {root}")
    return [root / name for name in sorted(TARGET_NAMES) if (root / name).is_file()]


def check_file(path: Path) -> list[str]:
    raw = path.read_bytes()
    errors: list[str] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        text = raw.decode("utf-8", errors="replace")
        errors.append(f"not valid UTF-8: byte {exc.start}")

    bom_count = raw.count(b"\xef\xbb\xbf")
    if bom_count:
        errors.append(f"UTF-8 BOM detected {bom_count} time(s)")
    if b"\r\n" in raw or b"\r" in raw:
        errors.append("CRLF/CR line endings detected")

    for line_no, line in enumerate(text.splitlines(), start=1):
        flagged = False
        for label, pattern in SUSPICIOUS_PATTERNS:
            if pattern.search(line):
                errors.append(f"line {line_no}: {label}")
                flagged = True
                break
        if flagged:
            continue

        hits = sum(line.count(fragment) for fragment in MOJIBAKE_FRAGMENTS)
        if hits >= 2:
            errors.append(f"line {line_no}: likely mojibake")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="memory root directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    failures: list[tuple[Path, list[str]]] = []
    for path in iter_targets(args.root):
        issues = check_file(path)
        if issues:
            failures.append((path, issues))

    if not failures:
        print("global memory encoding check passed")
        return 0

    print("global memory encoding check failed:")
    for path, issues in failures:
        print(f"\n{path}")
        for issue in issues[:20]:
            print(f"  - {issue}")
        if len(issues) > 20:
            print(f"  - ... {len(issues) - 20} more issue(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

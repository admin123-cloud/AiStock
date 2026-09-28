#!/usr/bin/env python3
"""Repair common GBK/CP936 mojibake in UTF-8 text files.

This is intentionally conservative: a line is replaced only when a GB18030
round-trip produces a lower mojibake score.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]

MOJIBAKE_FRAGMENTS = tuple(
    chr(codepoint)
    for codepoint in (
        0x9359,
        0x7EDB,
        0x7487,
        0x93B8,
        0x93C2,
        0x93BF,
        0x93C1,
        0x93AC,
        0x74A7,
        0x9472,
        0x7EEF,
        0x7F01,
        0x59F3,
        0x6924,
        0x93C3,
        0x5815,
        0x935A,
        0x5D87,
        0x934F,
        0x6D94,
        0x9357,
        0x9358,
        0x5F76,
        0x93B4,
        0x612A,
        0x6C26,
        0x95AB,
        0x934A,
        0x6B13,
        0x9418,
        0x8235,
        0xE18F,
        0xE11B,
        0xE0A4,
        0xE1BC,
        0xE1BB,
        0xE17B,
        0xE1BF,
        0xE787,
        0xE6E6,
        0x30E8,
        0x305A,
        0x3086,
        0xFF45,
        0xFF47,
        0x2545,
        0x3222,
        0x93CC,
        0x93C7,
        0x941C,
        0x9429,
        0x93C9,
        0x9352,
        0x93BA,
        0x20AC,
    )
) + (chr(0xFFFD), "".join(chr(c) for c in (0x951F, 0x65A4, 0x62F7)))


def mojibake_score(text: str) -> int:
    score = text.count("\ufffd") * 10
    for fragment in MOJIBAKE_FRAGMENTS:
        score += text.count(fragment)
    return score


def repair_line(line: str) -> tuple[str, bool]:
    before_score = mojibake_score(line)
    if before_score <= 0:
        return line, False
    candidates: list[str] = []
    for mode in ("replace", "ignore"):
        try:
            candidates.append(line.encode("gb18030", errors=mode).decode("utf-8", errors=mode))
        except UnicodeError:
            pass
    best = min(candidates, key=mojibake_score, default=line)
    after_score = mojibake_score(best)
    replacement_increase = best.count("\ufffd") - line.count("\ufffd")
    if after_score < before_score and replacement_increase <= 0:
        return best, True
    return line, False


def repair_file(path: Path, dry_run: bool) -> tuple[int, bool]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    changed_lines = 0
    repaired_lines: list[str] = []
    for line in text.split("\n"):
        repaired, changed = repair_line(line)
        if changed:
            changed_lines += 1
        repaired_lines.append(repaired)

    repaired_text = "\n".join(repaired_lines)
    repaired_raw = repaired_text.encode("utf-8")
    changed_file = repaired_raw != raw
    if changed_file and not dry_run:
        path.write_bytes(repaired_raw)
    return changed_lines, changed_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report what would change without writing")
    parser.add_argument("paths", nargs="+", help="files to repair")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    changed_files = 0
    total_lines = 0
    for item in args.paths:
        path = Path(item)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            print(f"skip missing file: {path}", file=sys.stderr)
            continue
        changed_lines, changed_file = repair_file(path, dry_run=args.check)
        total_lines += changed_lines
        if changed_file:
            changed_files += 1
            action = "would repair" if args.check else "repaired"
            print(f"{action}: {path.relative_to(ROOT).as_posix()} ({changed_lines} line(s))")
    if changed_files == 0:
        print("no repairs needed")
    else:
        print(f"files: {changed_files}, lines: {total_lines}")
    return 1 if args.check and changed_files else 0


if __name__ == "__main__":
    raise SystemExit(main())

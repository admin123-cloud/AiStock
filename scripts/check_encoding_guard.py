#!/usr/bin/env python3
"""Guard source files against non-UTF-8 bytes and common mojibake."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]

TEXT_EXTENSIONS = {
    ".bat",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".scss",
    ".sql",
    ".toml",
    ".txt",
    ".vue",
    ".yaml",
    ".yml",
}

TEXT_FILENAMES = {
    ".editorconfig",
    ".gitignore",
    "pre-commit",
}

SKIP_PARTS = {
    ".git",
    ".idea",
    ".pytest_cache",
    "__pycache__",
    "artifacts",
    "data",
    "runtime",
    "dist",
    "logs",
    "node_modules",
}

SKIP_PREFIXES = {
    Path("data") / "runtime",
    Path("data") / "warehouse",
    Path("frontend") / "dist",
}

MOJIBAKE_CODEPOINTS = {
    0x5158,
    0x53E9,
    0x5A09,
    0x5D23,
    0x619F,
    0x6541,
    0x6578,
    0x67E9,
    0x6A39,
    0x6D93,
    0x6DE7,
    0x6E36,
    0x6FEE,
    0x6DC7,
    0x6FCA,
    0x7035,
    0x699B,
    0x6B12,
    0x742C,
    0x7459,
    0x7CBA,
    0x7F02,
    0x7F01,
    0x7EDB,
    0x7FEE,
    0x837B,
    0x85C9,
    0x8AAB,
    0x8BF2,
    0x8BE7,
    0x8BEA,
    0x927F,
    0x93C1,
    0x93C3,
    0x93C5,
    0x934F,
    0x9359,
    0x935A,
    0x93B4,
    0x9405,
    0x9422,
    0x9428,
    0x9442,
    0x9472,
}

MOJIBAKE_CHARS = {chr(codepoint) for codepoint in MOJIBAKE_CODEPOINTS}

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
)

SUSPICIOUS_PATTERNS = [
    ("replacement character", re.compile(chr(0xFFFD))),
    (
        "classic mojibake marker",
        re.compile("".join(chr(c) for c in (0x951F, 0x65A4, 0x62F7)) + "|" + chr(0x00C3) + "|" + chr(0x00C2)),
    ),
    ("lost Chinese placeholder", re.compile(r"(?<![A-Za-z0-9_])\?{3,}(?![A-Za-z0-9_])")),
]


def run_git(args: list[str]) -> list[Path]:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip()
        raise SystemExit(f"git command failed: git {' '.join(args)}\n{message}")
    return [Path(line.strip()) for line in proc.stdout.splitlines() if line.strip()]


def is_text_candidate(path: Path) -> bool:
    return path.name in TEXT_FILENAMES or path.suffix.lower() in TEXT_EXTENSIONS


def is_skipped(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_PARTS:
        return True
    return any(path == prefix or prefix in path.parents for prefix in SKIP_PREFIXES)


def iter_all_files() -> Iterable[Path]:
    for path in ROOT.rglob("*"):
        if path.is_file():
            rel = path.relative_to(ROOT)
            if not is_skipped(rel) and is_text_candidate(rel):
                yield rel


def iter_staged_files() -> Iterable[Path]:
    for path in run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"]):
        if not is_skipped(path) and is_text_candidate(path):
            yield path


def iter_changed_files() -> Iterable[Path]:
    seen: set[Path] = set()
    for args in (
        ["diff", "--name-only", "--diff-filter=ACMR"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        for path in run_git(args):
            if path not in seen and not is_skipped(path) and is_text_candidate(path):
                seen.add(path)
                yield path


def iter_explicit_files(paths: Iterable[str]) -> Iterable[Path]:
    for item in paths:
        path = Path(item)
        if path.is_absolute():
            try:
                rel = path.relative_to(ROOT)
            except ValueError:
                continue
        else:
            rel = path
        abs_path = ROOT / rel
        if abs_path.is_file() and not is_skipped(rel) and is_text_candidate(rel):
            yield rel


def check_file(path: Path) -> list[str]:
    abs_path = ROOT / path
    errors: list[str] = []
    try:
        raw = abs_path.read_bytes()
    except OSError as exc:
        return [f"cannot read file: {exc}"]

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"not valid UTF-8: byte {exc.start}"]

    if raw.startswith(b"\xef\xbb\xbf"):
        errors.append("UTF-8 BOM is not allowed; use plain UTF-8")
    if b"\r\n" in raw or b"\r" in raw:
        errors.append("CRLF/CR line endings are not allowed; use LF")

    for line_no, line in enumerate(text.splitlines(), start=1):
        for label, pattern in SUSPICIOUS_PATTERNS:
            if pattern.search(line):
                snippet = line.strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                errors.append(f"line {line_no}: {label}: {snippet}")
                break
        else:
            mojibake_hits = sum(1 for ch in line if ch in MOJIBAKE_CHARS)
            mojibake_hits += sum(line.count(fragment) for fragment in MOJIBAKE_FRAGMENTS)
            if mojibake_hits >= 2:
                snippet = line.strip()
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                errors.append(f"line {line_no}: likely GBK/CP936 mojibake: {snippet}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check text files for UTF-8 decoding errors and common mojibake."
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="scan all tracked-style text files")
    scope.add_argument("--staged", action="store_true", help="scan staged files only")
    scope.add_argument("--changed", action="store_true", help="scan unstaged changed and untracked files")
    parser.add_argument("paths", nargs="*", help="specific files to scan")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.paths:
        paths = list(iter_explicit_files(args.paths))
    elif args.all:
        paths = list(iter_all_files())
    elif args.staged:
        paths = list(iter_staged_files())
    else:
        paths = list(iter_changed_files())

    failures: list[tuple[Path, list[str]]] = []
    for path in sorted(set(paths)):
        errors = check_file(path)
        if errors:
            failures.append((path, errors))

    if not failures:
        print(f"encoding guard passed: {len(set(paths))} file(s) checked")
        return 0

    print("encoding guard failed:")
    for path, errors in failures:
        print(f"\n{path.as_posix()}")
        for error in errors[:20]:
            print(f"  - {error}")
        if len(errors) > 20:
            print(f"  - ... {len(errors) - 20} more issue(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

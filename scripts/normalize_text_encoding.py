#!/usr/bin/env python3
"""Normalize repository text files to UTF-8 without BOM and LF line endings."""

from __future__ import annotations

import argparse
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
    "dist",
    "logs",
    "node_modules",
}


def is_text_candidate(path: Path) -> bool:
    return path.name in TEXT_FILENAMES or path.suffix.lower() in TEXT_EXTENSIONS


def is_skipped(path: Path) -> bool:
    return bool(set(path.parts) & SKIP_PARTS)


def iter_files(paths: Iterable[str] | None) -> Iterable[Path]:
    if paths:
        for item in paths:
            path = Path(item)
            if path.is_absolute():
                try:
                    rel = path.relative_to(ROOT)
                except ValueError:
                    continue
            else:
                rel = path
            if rel.is_file():
                yield rel
            elif (ROOT / rel).is_file():
                yield rel
        return

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if not is_skipped(rel) and is_text_candidate(rel):
            yield rel


def normalize(path: Path, dry_run: bool) -> tuple[bool, str]:
    abs_path = ROOT / path
    raw = abs_path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        return False, f"skip non-UTF-8: byte {exc.start}"

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    if normalized == raw:
        return False, "already normalized"

    if not dry_run:
        abs_path.write_bytes(normalized)
    return True, "normalized"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="only report files that need normalization")
    parser.add_argument("paths", nargs="*", help="specific files to normalize")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    changed: list[Path] = []
    skipped: list[tuple[Path, str]] = []
    for path in sorted(set(iter_files(args.paths))):
        if is_skipped(path) or not is_text_candidate(path):
            continue
        did_change, reason = normalize(path, dry_run=args.check)
        if did_change:
            changed.append(path)
        elif reason.startswith("skip"):
            skipped.append((path, reason))

    if changed:
        verb = "would normalize" if args.check else "normalized"
        print(f"{verb}: {len(changed)} file(s)")
        for path in changed[:100]:
            print(f"  - {path.as_posix()}")
        if len(changed) > 100:
            print(f"  - ... {len(changed) - 100} more")
    else:
        print("all scanned files are normalized")

    if skipped:
        print(f"skipped: {len(skipped)} file(s)")
        for path, reason in skipped[:50]:
            print(f"  - {path.as_posix()}: {reason}")
    return 1 if args.check and (changed or skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())

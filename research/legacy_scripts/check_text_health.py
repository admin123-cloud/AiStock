#!/usr/bin/env python3
"""Run all text-encoding safety checks used by this repo."""

from __future__ import annotations

import argparse
import subprocess
import sys


def run_check(args: list[str], label: str) -> None:
    proc = subprocess.run(
        [sys.executable, *args],
        check=False,
        capture_output=False,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"{label} failed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="include repository staged file check",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="check all tracked text files instead of staged/default",
    )
    parser.add_argument(
        "--include-global-memory",
        action="store_true",
        help="also check the global Codex memory directory",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all:
        repo_check = ["scripts/check_encoding_guard.py", "--all"]
    elif args.staged:
        repo_check = ["scripts/check_encoding_guard.py", "--staged"]
    else:
        repo_check = ["scripts/check_encoding_guard.py", "--changed"]

    try:
        run_check(repo_check, "repository encoding guard")
        if args.include_global_memory:
            run_check(["scripts/check_global_memory_encoding.py"], "global memory encoding guard")
    except SystemExit as exc:
        print(exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

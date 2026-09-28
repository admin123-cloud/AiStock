from __future__ import annotations

"""Attach an isolated daily-candidate completion to the versioned dry-run.

This creates a new evidence manifest; it does not rewrite the original
dry-run manifest and does not issue any database command.
"""

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dryrun", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dryrun = json.loads(args.dryrun.read_text(encoding="utf-8"))
    gate = json.loads(args.gate.read_text(encoding="utf-8"))
    pending = int(dryrun["summary"]["pending_1d_2000_2002"])
    completed = int(gate["total_candidate_rows"])
    report = {
        "schema_version": "pre2010-versioned-formal-daily-completion-v1",
        "parent_dryrun": str(args.dryrun),
        "parent_dryrun_sha256": sha256(args.dryrun),
        "daily_gate": str(args.gate),
        "daily_gate_sha256": sha256(args.gate),
        "scope": {"start_year": 2000, "end_year": 2002},
        "planned_pending_rows": pending,
        "completed_candidate_rows": completed,
        "remaining_pending_rows": max(pending - completed, 0),
        "candidate_root": gate["root"],
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "create_executed": False,
        "insert_executed": False,
        "status": "complete" if pending == completed and gate["status"] == "complete" else "review",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())

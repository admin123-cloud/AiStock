"""Run read-only local Ocean daily reconciliation one year at a time.

The sequence keeps the old 2000 watcher independent, gives each year its own
output/lock, and starts the next year only after the prior ``--once`` worker
exits successfully.  It refuses to consume an incomplete local stage and has
no database client or write path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.reconcile_local_ocean_daily import SingleWriterLock
from utils.ocean_audit import iter_local_manifests
from utils.verified_zip import inspect_zip, verify_ready


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
SEQUENCE_SCHEMA = "ocean-local-daily-sequence-v1"


def _now() -> str:
    return datetime.now(BUSINESS_TZ).replace(microsecond=0).isoformat()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _disk(root: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(root)
    return {
        "path": str(root.resolve()),
        "free_bytes": int(usage.free),
        "total_bytes": int(usage.total),
        "used_bytes": int(usage.used),
    }


def _stage_manifest_count(local_root: Path, archive_sha256: str, policy_hash: str) -> int:
    return sum(
        1
        for _ in iter_local_manifests(local_root, archive_sha256=archive_sha256)
        if _.payload.get("accepted_stock_policy_hash") == policy_hash
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--daily-root", type=Path, required=True)
    parser.add_argument("--output-parent", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--policy-hash", required=True)
    parser.add_argument("--years", type=int, nargs="+", required=True)
    parser.add_argument("--min-free-bytes", type=int, default=20 * 1024**3)
    parser.add_argument("--poll-seconds", type=int, default=15)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    years = list(dict.fromkeys(args.years))
    if years != sorted(years):
        raise SystemExit("years_must_be_in_ascending_order")
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    args.output_parent.mkdir(parents=True, exist_ok=True)
    lock = SingleWriterLock(args.runtime_dir / ".ocean_daily_sequence.lock")
    lock.acquire()
    state_path = args.runtime_dir / "daily_sequence_2001_2002.state.json"
    history: list[dict[str, Any]] = []
    try:
        for year in years:
            archive = args.archive_dir / f"{year}_5min.zip"
            verified = verify_ready(archive)
            expected_entries = int(inspect_zip(archive)["csv_entries"])
            actual_entries = _stage_manifest_count(args.local_root, verified.sha256, args.policy_hash)
            if actual_entries != expected_entries:
                raise RuntimeError(
                    f"stage_incomplete={year}:{actual_entries}/{expected_entries}"
                )
            disk_before = _disk(args.output_parent)
            if disk_before["free_bytes"] < args.min_free_bytes:
                raise RuntimeError(
                    f"free_disk_guard={disk_before['free_bytes']}<{args.min_free_bytes};year={year}"
                )
            output_root = args.output_parent / f"daily_reconciliation_{year}_fast"
            stdout_path = args.runtime_dir / f"daily_reconciliation_{year}_fast.out.log"
            stderr_path = args.runtime_dir / f"daily_reconciliation_{year}_fast.err.log"
            child_args = [
                str(ROOT / "scripts" / "reconcile_local_ocean_daily.py"),
                "--archive", str(archive),
                "--local-root", str(args.local_root),
                "--daily-root", str(args.daily_root),
                "--output-root", str(output_root),
                "--policy-hash", args.policy_hash,
                "--poll-seconds", str(args.poll_seconds),
                "--once",
            ]
            with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
                child = subprocess.Popen(
                    [sys.executable, *child_args],
                    cwd=str(ROOT),
                    stdout=stdout,
                    stderr=stderr,
                    close_fds=True,
                )
            record: dict[str, Any] = {
                "year": year,
                "archive": str(archive),
                "archive_sha256": verified.sha256,
                "expected_stage_entries": expected_entries,
                "stage_entries_verified": actual_entries,
                "output_root": str(output_root),
                "pid": child.pid,
                "started_at": _now(),
                "status": "running",
                "disk_before": disk_before,
                "stdout": str(stdout_path),
                "stderr": str(stderr_path),
            }
            history.append(record)
            _atomic_json(state_path, {
                "schema_version": SEQUENCE_SCHEMA,
                "supervisor_pid": os.getpid(),
                "status": "running",
                "current_year": year,
                "current_pid": child.pid,
                "years": years,
                "completed_years": [item["year"] for item in history if item["status"] == "complete"],
                "history": history,
                "formal_authorized": False,
                "disk": _disk(args.output_parent),
            })
            while True:
                return_code = child.poll()
                record["disk_latest"] = _disk(args.output_parent)
                checkpoint_state = output_root / "state.json"
                if checkpoint_state.is_file():
                    try:
                        record["worker_state"] = json.loads(checkpoint_state.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        record["worker_state_read"] = "not_checked"
                if return_code is not None:
                    record["returncode"] = int(return_code)
                    record["finished_at"] = _now()
                    record["status"] = "complete" if return_code == 0 else "failed"
                    break
                _atomic_json(state_path, {
                    "schema_version": SEQUENCE_SCHEMA,
                    "supervisor_pid": os.getpid(),
                    "status": "running",
                    "current_year": year,
                    "current_pid": child.pid,
                    "years": years,
                    "completed_years": [item["year"] for item in history if item["status"] == "complete"],
                    "history": history,
                    "formal_authorized": False,
                    "disk": record["disk_latest"],
                })
                time.sleep(max(1, args.poll_seconds))
            _atomic_json(state_path, {
                "schema_version": SEQUENCE_SCHEMA,
                "supervisor_pid": os.getpid(),
                "status": "failed" if record["status"] == "failed" else "between_years",
                "current_year": year,
                "current_pid": None,
                "years": years,
                "completed_years": [item["year"] for item in history if item["status"] == "complete"],
                "history": history,
                "formal_authorized": False,
                "disk": record["disk_latest"],
            })
            if record["status"] != "complete":
                return int(record["returncode"] or 1)
        _atomic_json(state_path, {
            "schema_version": SEQUENCE_SCHEMA,
            "supervisor_pid": os.getpid(),
            "status": "complete",
            "current_pid": None,
            "years": years,
            "completed_years": years,
            "history": history,
            "formal_authorized": False,
            "disk": _disk(args.output_parent),
        })
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())


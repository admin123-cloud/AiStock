"""Run verified Ocean local stages serially with durable runtime evidence.

This supervisor is deliberately file-only.  It starts the existing
``stage_verified_ocean_5m.py`` child for one year at a time, records disk
capacity and real child PIDs, and stops before the next year on any failure.
It never imports a database client or formal promotion code.
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
from utils.verified_zip import verify_ready


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
SEQUENCE_SCHEMA = "ocean-local-stage-sequence-v1"


def _now() -> str:
    return datetime.now(BUSINESS_TZ).replace(microsecond=0).isoformat()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, required=True)
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--accepted-stock-file", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--years", type=int, nargs="+", required=True)
    parser.add_argument("--policy-hash", required=True)
    parser.add_argument("--min-free-bytes", type=int, default=20 * 1024**3)
    parser.add_argument("--poll-seconds", type=int, default=15)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    years = list(dict.fromkeys(args.years))
    if years != sorted(years):
        raise SystemExit("years_must_be_in_ascending_order")
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    args.local_root.mkdir(parents=True, exist_ok=True)
    lock = SingleWriterLock(args.runtime_dir / ".ocean_stage_sequence.lock")
    lock.acquire()
    state_path = args.runtime_dir / "stage_sequence_2003_2004.state.json"
    history: list[dict[str, Any]] = []
    try:
        _atomic_json(state_path, {
            "schema_version": SEQUENCE_SCHEMA,
            "supervisor_pid": os.getpid(),
            "status": "starting",
            "years": years,
            "completed_years": [],
            "history": history,
            "formal_authorized": False,
            "disk": _disk(args.local_root),
        })
        for year in years:
            archive = args.archive_dir / f"{year}_5min.zip"
            if not archive.is_file():
                raise RuntimeError(f"archive_missing={archive}")
            verified = verify_ready(archive)
            disk_before = _disk(args.local_root)
            if disk_before["free_bytes"] < args.min_free_bytes:
                raise RuntimeError(
                    f"free_disk_guard={disk_before['free_bytes']}<{args.min_free_bytes};year={year}"
                )
            out_log = args.runtime_dir / f"stage_{year}.out.log"
            err_log = args.runtime_dir / f"stage_{year}.err.log"
            child_args = [
                str(ROOT / "scripts" / "stage_verified_ocean_5m.py"),
                "--archive", str(archive),
                "--accepted-stock-file", str(args.accepted_stock_file),
                "--local-root", str(args.local_root),
            ]
            started_at = _now()
            with out_log.open("ab") as stdout, err_log.open("ab") as stderr:
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
                "source_marker_sha256": _sha256_path(verified.marker),
                "pid": child.pid,
                "started_at": started_at,
                "status": "running",
                "disk_before": disk_before,
                "stdout": str(out_log),
                "stderr": str(err_log),
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
                "disk": _disk(args.local_root),
            })
            while True:
                return_code = child.poll()
                record["disk_latest"] = _disk(args.local_root)
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
            "disk": _disk(args.local_root),
        })
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

"""Checkpointed, read-only daily reconciliation for local Ocean evidence.

The script consumes only complete local stage manifests and one Parquet day at
a time.  It never connects to ClickHouse, creates tables, or promotes formal
rows.  Pass and unresolved evidence are written to separate compressed files;
the per-entry checkpoint manifest is the commit marker.
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.ocean_audit import (
    DailySnapshotReader,
    OceanAuditError,
    LocalOceanManifest,
    _as_date,
    iter_local_manifests,
    reconcile_stage_rows,
    select_manifest_scope,
)
from utils.verified_zip import inspect_zip, verify_ready


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


class LocalReconciliationError(RuntimeError):
    """A local checkpoint cannot be safely resumed."""


def _now() -> str:
    return datetime.now(BUSINESS_TZ).replace(microsecond=0).isoformat()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8"))


def _atomic_jsonl_gzip(path: Path, rows: Iterable[Mapping[str, Any]]) -> tuple[int, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    count = 0
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                for row in rows:
                    compressed.write((json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8"))
                    count += 1
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, path)
        return count, _sha256_path(path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


class SingleWriterLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.handle: Any | None = None

    def acquire(self) -> None:
        if self.handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.handle = handle
        except (OSError, IOError) as exc:
            handle.close()
            raise LocalReconciliationError(f"reconciliation_lock_busy={self.path}") from exc

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _entry_dir(root: Path, manifest: LocalOceanManifest) -> Path:
    source_entry = str(manifest.payload["source_entry"])
    entry_hash = hashlib.sha256(source_entry.encode("utf-8")).hexdigest()[:16]
    return (
        root
        / str(manifest.payload["archive_sha256"])[:16]
        / str(manifest.payload["accepted_stock_policy_hash"])[:16]
        / entry_hash
    )


def _identity(manifest: LocalOceanManifest, source_manifest_sha256: str) -> dict[str, Any]:
    return {
        "archive_sha256": manifest.payload["archive_sha256"],
        "batch_id": manifest.payload["batch_id"],
        "source_entry": manifest.payload["source_entry"],
        "accepted_stock_policy_hash": manifest.payload["accepted_stock_policy_hash"],
        "source_manifest_sha256": source_manifest_sha256,
    }


def _write_entry_checkpoint(
    output_root: Path,
    manifest: LocalOceanManifest,
    daily_reader: DailySnapshotReader,
    daily_root: Path,
) -> dict[str, Any]:
    source_manifest_sha256 = _sha256_path(manifest.path)
    entry_dir = _entry_dir(output_root, manifest)
    checkpoint_path = entry_dir / "manifest.json"
    identity = _identity(manifest, source_manifest_sha256)
    if checkpoint_path.exists():
        existing = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if any(existing.get(key) != value for key, value in identity.items()):
            raise LocalReconciliationError(f"reconciliation_checkpoint_identity_mismatch={checkpoint_path}")
        return existing
    if any((entry_dir / name).exists() for name in ("pass.jsonl.gz", "review.jsonl.gz")):
        raise LocalReconciliationError(f"reconciliation_checkpoint_incomplete={entry_dir}")

    rows = list(manifest.iter_stage_rows())
    audits = list(manifest.iter_day_audits())
    rows_by_day: dict[date, list[Mapping[str, Any]]] = {}
    for row in rows:
        rows_by_day.setdefault(_as_date(row.get("trade_date") or row.get("datetime")), []).append(row)
    audits_by_day: dict[date, list[Mapping[str, Any]]] = {}
    for audit in audits:
        audits_by_day.setdefault(_as_date(audit.get("trade_date")), []).append(audit)
    reconciliations = []
    all_days = sorted(set(rows_by_day) | set(audits_by_day))
    # One bounded scan per entry/year.  The daily loop below only indexes the
    # small code/year result, avoiding one Parquet scan for every code/day.
    days_by_year: dict[int, list[date]] = {}
    codes_by_year: dict[int, set[str]] = {}
    for trade_date in all_days:
        days_by_year.setdefault(trade_date.year, []).append(trade_date)
        codes_by_year.setdefault(trade_date.year, set()).update(
            str(row.get("code") or "") for row in rows_by_day.get(trade_date, [])
        )
        codes_by_year[trade_date.year].update(
            str(audit.get("code") or "") for audit in audits_by_day.get(trade_date, [])
        )
    for year in sorted(days_by_year):
        year_daily = daily_reader.load_code_year(year, codes=codes_by_year[year])
        for trade_date in days_by_year[year]:
            daily = {
                key: row for key, row in year_daily.items()
                if key[1] == trade_date
            }
            reconciliations.extend(reconcile_stage_rows(
                rows_by_day.get(trade_date, []),
                daily,
                day_audits=audits_by_day.get(trade_date, []),
            ))
        # Explicitly drop the year-local lookup before moving to the next one.
        del year_daily
    pass_rows = [
        {**_as_reconciliation(item), "evidence_partition": "pass"}
        for item in reconciliations if item.overall_status == "pass"
    ]
    review_rows = [
        {**_as_reconciliation(item), "evidence_partition": "review"}
        for item in reconciliations if item.overall_status != "pass"
    ]
    if not reconciliations and manifest.payload.get("entry_status") != "accepted_stage":
        review_rows.append({
            **identity,
            "evidence_partition": "review",
            "status": "review",
            "reason": str(manifest.payload.get("reason") or manifest.payload.get("entry_status") or "no_day_audit"),
        })
    pass_count, pass_sha = _atomic_jsonl_gzip(entry_dir / "pass.jsonl.gz", pass_rows)
    review_count, review_sha = _atomic_jsonl_gzip(entry_dir / "review.jsonl.gz", review_rows)
    checkpoint = {
        "schema_version": "ocean-daily-reconciliation-v1",
        **identity,
        "daily_snapshot_root": str(daily_root),
        "created_at": _now(),
        "formal_authorized": False,
        "gate_scope": "structure_plus_daily_read_only",
        "status": "complete",
        "pass_file": "pass.jsonl.gz",
        "pass_sha256": pass_sha,
        "pass_rows": pass_count,
        "review_file": "review.jsonl.gz",
        "review_sha256": review_sha,
        "review_rows": review_count,
        "pass_days": sum(item.overall_status == "pass" for item in reconciliations),
        "review_days": sum(item.overall_status != "pass" for item in reconciliations),
        "stage_rows_read": len(rows),
        "audit_rows_read": len(audits),
    }
    _atomic_json(checkpoint_path, checkpoint)
    return checkpoint


def _as_reconciliation(item: Any) -> dict[str, Any]:
    result = dict(item.__dict__)
    result["trade_date"] = item.trade_date.isoformat()
    return result


def _archive_expected(archive: Path) -> tuple[Any, int, str]:
    verified = verify_ready(archive)
    inspection = inspect_zip(archive)
    return verified, int(inspection["csv_entries"]), f"ocean_vps_5m_{archive.stem.split('_', 1)[0]}_{verified.sha256[:12]}"


def _write_state(path: Path, *, expected: int, completed: int, pass_days: int, review_days: int, status: str, error: str | None = None) -> None:
    _atomic_json(path, {
        "updated_at": _now(),
        "status": status,
        "expected_csv_entries": expected,
        "completed_entries": completed,
        "pass_days": pass_days,
        "review_days": review_days,
        "formal_authorized": False,
        "error": error,
    })


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--daily-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--policy-hash", required=True)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--timeout-seconds", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    verified, expected_entries, batch_id = _archive_expected(args.archive)
    reader = DailySnapshotReader(args.daily_root)
    args.output_root.mkdir(parents=True, exist_ok=True)
    state_path = args.output_root / "state.json"
    lock = SingleWriterLock(args.output_root / ".reconciliation.lock")
    lock.acquire()
    started = time.monotonic()
    try:
        while True:
            manifests = []
            try:
                manifests = select_manifest_scope(
                    iter_local_manifests(args.local_root, archive_sha256=verified.sha256),
                    batch_id=batch_id,
                    policy_hash=args.policy_hash,
                )
            except OceanAuditError as exc:
                if "no_local_manifest_in_selected_scope" not in str(exc):
                    raise
            completed = 0
            pass_days = 0
            review_days = 0
            for manifest in manifests:
                checkpoint = _write_entry_checkpoint(args.output_root, manifest, reader, args.daily_root)
                completed += 1
                pass_days += int(checkpoint.get("pass_days") or 0)
                review_days += int(checkpoint.get("review_days") or 0)
                _write_state(
                    state_path,
                    expected=expected_entries,
                    completed=completed,
                    pass_days=pass_days,
                    review_days=review_days,
                    status="complete" if completed >= expected_entries else "processing_stage_manifests",
                )
            _write_state(
                state_path,
                expected=expected_entries,
                completed=completed,
                pass_days=pass_days,
                review_days=review_days,
                status="complete" if completed >= expected_entries else "waiting_for_stage_manifests",
            )
            if completed >= expected_entries or args.once:
                return 0
            if args.timeout_seconds and time.monotonic() - started >= args.timeout_seconds:
                _write_state(
                    state_path,
                    expected=expected_entries,
                    completed=completed,
                    pass_days=pass_days,
                    review_days=review_days,
                    status="timeout_waiting_for_stage_manifests",
                )
                return 2
            time.sleep(max(1, args.poll_seconds))
    except Exception as exc:
        _write_state(
            state_path,
            expected=expected_entries,
            completed=0,
            pass_days=0,
            review_days=0,
            status="failed_preserved",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())

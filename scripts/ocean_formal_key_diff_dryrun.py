from __future__ import annotations

"""Read-only formal key/value diff for completed local Ocean checkpoints.

Every entry is independently fail-closed.  The only ClickHouse operation is a
bounded SELECT against the formal minute table; there is no INSERT, DDL,
candidate materialization, retry, or promotion path.
"""

import argparse
import gzip
import hashlib
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.reconcile_local_ocean_daily import SingleWriterLock
from utils.market_warehouse import clickhouse_client
from utils.ocean_audit import (
    LocalOceanManifest,
    OceanAuditError,
    _as_date,
    canonical_datetime_key,
    dryrun_missing_formal_keys,
    iter_local_manifests,
    select_manifest_scope,
)
from utils.ocean_formal_units import classify_formal_conflict
from utils.verified_zip import inspect_zip, verify_ready


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
FORMAL_TABLE = "stock.kline_minute_5"
CHECKPOINT_SCHEMA = "ocean-daily-reconciliation-v1"
DIFF_SCHEMA = "ocean-formal-key-diff-v1"
CONFLICT_CONTRACT_VERSION = "ocean-formal-conflict-v2"


class FormalDiffError(RuntimeError):
    """Local formal-diff evidence cannot be safely checked."""


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


def _atomic_gzip_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> tuple[int, str]:
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


def _iter_jsonl_gzip(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as handle:
        result = []
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise FormalDiffError(f"checkpoint_jsonl_not_object={path}:{line_number}")
            result.append(value)
        return result


def _snapshot_identity(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise FormalDiffError(f"snapshot_root_missing={root}")
    digest = hashlib.sha256()
    files = 0
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
        files += 1
        total_bytes += stat.st_size
    return {
        "resolved_root": str(root),
        "root_name": root.name,
        "files": files,
        "total_bytes": total_bytes,
        "tree_signature": digest.hexdigest(),
    }


def _checkpoint_paths(checkpoint_path: Path, checkpoint: Mapping[str, Any]) -> tuple[Path, Path]:
    paths = []
    for key in ("pass_file", "review_file"):
        name = str(checkpoint.get(key) or "")
        if not name or Path(name).name != name:
            raise FormalDiffError(f"unsafe_checkpoint_filename={checkpoint_path}:{key}")
        path = checkpoint_path.parent / name
        if not path.is_file():
            raise FormalDiffError(f"checkpoint_file_missing={path}")
        expected = str(checkpoint.get(key.replace("_file", "_sha256")) or "")
        if _sha256_path(path) != expected:
            raise FormalDiffError(f"checkpoint_file_hash_mismatch={path}")
        paths.append(path)
    return paths[0], paths[1]


def _validate_checkpoint(
    checkpoint_path: Path,
    checkpoint: Mapping[str, Any],
    source_manifest: LocalOceanManifest,
    *,
    archive_sha256: str,
    batch_id: str,
    policy_hash: str,
    snapshot_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    expected = {
        "archive_sha256": archive_sha256,
        "batch_id": batch_id,
        "source_entry": source_manifest.payload["source_entry"],
        "accepted_stock_policy_hash": policy_hash,
    }
    if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA:
        raise FormalDiffError(f"unsupported_checkpoint_schema={checkpoint_path}")
    for key, value in expected.items():
        if checkpoint.get(key) != value:
            raise FormalDiffError(f"checkpoint_identity_mismatch={checkpoint_path}:{key}")
    if checkpoint.get("status") != "complete" or checkpoint.get("formal_authorized") is not False:
        raise FormalDiffError(f"checkpoint_not_readonly_complete={checkpoint_path}")
    checkpoint_source_hash = str(checkpoint.get("source_manifest_sha256") or "")
    if _sha256_path(source_manifest.path) != checkpoint_source_hash:
        raise FormalDiffError(f"source_manifest_hash_mismatch={source_manifest.path}")
    recorded_snapshot = Path(str(checkpoint.get("daily_snapshot_root") or "")).resolve()
    if recorded_snapshot != snapshot_root.resolve():
        raise FormalDiffError(f"snapshot_identity_mismatch={checkpoint_path}")
    pass_path, review_path = _checkpoint_paths(checkpoint_path, checkpoint)
    pass_rows = _iter_jsonl_gzip(pass_path)
    review_rows = _iter_jsonl_gzip(review_path)
    if len(pass_rows) != int(checkpoint.get("pass_rows") or 0):
        raise FormalDiffError(f"checkpoint_pass_count_mismatch={checkpoint_path}")
    if len(review_rows) != int(checkpoint.get("review_rows") or 0):
        raise FormalDiffError(f"checkpoint_review_count_mismatch={checkpoint_path}")
    source_manifest.stage_path()
    source_manifest.audit_path()
    # iter_* verifies the stage and audit source hashes before any formal query.
    list(source_manifest.iter_stage_rows())
    list(source_manifest.iter_day_audits())
    return pass_rows, review_rows, {
        "checkpoint_sha256": _sha256_path(checkpoint_path),
        "source_manifest_sha256": checkpoint_source_hash,
        "snapshot_root": str(snapshot_root.resolve()),
    }


def _formal_query(client: Any, code: str, start: date, end: date, *, timeout_seconds: int, memory_bytes: int) -> list[tuple[Any, ...]]:
    start_text = f"{start.isoformat()} 00:00:00"
    end_text = f"{end.isoformat()} 00:00:00"
    sql = f"""
        SELECT code, datetime, open, high, low, close, volume, amount
        FROM {FORMAL_TABLE}
        WHERE code = %(code)s
          AND datetime >= toDateTime(%(start)s, 'Asia/Shanghai')
          AND datetime < toDateTime(%(end)s, 'Asia/Shanghai')
        SETTINGS max_threads=1, max_memory_usage={int(memory_bytes)}, max_execution_time={int(timeout_seconds)}
    """
    result = client.query(sql, parameters={"code": code, "start": start_text, "end": end_text})
    return list(result.result_rows)


def _value_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def _formal_key(row: tuple[Any, ...]) -> tuple[str, str]:
    return str(row[0]), canonical_datetime_key(row[1])


def _conflicts(candidate_rows: Iterable[Mapping[str, Any]], formal_rows: Iterable[tuple[Any, ...]], identity: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for row in formal_rows:
        by_key.setdefault(_formal_key(row), []).append(row)
    result: list[dict[str, Any]] = []
    fields = ("open", "high", "low", "close", "source_volume_lots", "source_amount_yuan")
    for candidate in candidate_rows:
        key = (str(candidate.get("code") or ""), canonical_datetime_key(candidate.get("datetime")))
        matches = by_key.get(key, [])
        if len(matches) > 1:
            result.append({
                **identity,
                "code": key[0],
                "datetime": key[1],
                "reason": "formal_duplicate_key",
                "formal_rows": len(matches),
                **classify_formal_conflict({"reason": "formal_duplicate_key"}),
            })
        for formal in matches:
            formal_values = (formal[2], formal[3], formal[4], formal[5], formal[6], formal[7])
            candidate_values = tuple(candidate.get(field) for field in fields)
            differences = [
                name for name, left, right in zip(fields, candidate_values, formal_values)
                if not _value_equal(left, right)
            ]
            if differences:
                result.append({
                    **identity,
                    "code": key[0],
                    "datetime": key[1],
                    "reason": "formal_existing_value_conflict",
                    "different_fields": differences,
                    "candidate": dict(zip(fields, candidate_values)),
                    "formal": dict(zip(fields, formal_values)),
                    **classify_formal_conflict({
                        "reason": "formal_existing_value_conflict",
                        "different_fields": differences,
                        "candidate": dict(zip(fields, candidate_values)),
                        "formal": dict(zip(fields, formal_values)),
                    }),
                })
    return result


def _entry_dir(root: Path, checkpoint: Mapping[str, Any]) -> Path:
    entry_hash = hashlib.sha256(str(checkpoint["source_entry"]).encode("utf-8")).hexdigest()[:16]
    return root / str(checkpoint["archive_sha256"])[:16] / str(checkpoint["accepted_stock_policy_hash"])[:16] / entry_hash


def _write_not_checked(root: Path, checkpoint: Mapping[str, Any], reason: str, snapshot_identity: Mapping[str, Any]) -> dict[str, Any]:
    directory = _entry_dir(root, checkpoint)
    result = {
        "schema_version": DIFF_SCHEMA,
        "archive_sha256": checkpoint.get("archive_sha256"),
        "batch_id": checkpoint.get("batch_id"),
        "source_entry": checkpoint.get("source_entry"),
        "accepted_stock_policy_hash": checkpoint.get("accepted_stock_policy_hash"),
        "status": "not_checked",
        "reason": reason,
        "formal_authorized": False,
        "conflict_contract_version": CONFLICT_CONTRACT_VERSION,
        "snapshot_identity": dict(snapshot_identity),
        "checked_at": _now(),
    }
    _atomic_json(directory / "manifest.json", result)
    _atomic_json(directory / "not_checked.json", result)
    return result


def _write_checked(
    root: Path,
    checkpoint: Mapping[str, Any],
    audit_identity: Mapping[str, Any],
    snapshot_identity: Mapping[str, Any],
    *,
    candidate_rows: list[Mapping[str, Any]],
    formal_rows: list[tuple[Any, ...]],
) -> dict[str, Any]:
    identity = {
        "archive_sha256": checkpoint["archive_sha256"],
        "batch_id": checkpoint["batch_id"],
        "source_entry": checkpoint["source_entry"],
        "accepted_stock_policy_hash": checkpoint["accepted_stock_policy_hash"],
    }
    missing = dryrun_missing_formal_keys(
        candidate_rows,
        [(_formal_key(row)[0], _formal_key(row)[1]) for row in formal_rows],
    )
    conflicts = _conflicts(candidate_rows, formal_rows, identity)
    directory = _entry_dir(root, checkpoint)
    missing_rows = [
        {**identity, "code": code, "datetime": dt, "reason": "formal_key_missing"}
        for code, dt in (missing.missing_keys or ())
    ]
    missing_count, missing_sha = _atomic_gzip_jsonl(directory / "missing_keys.jsonl.gz", missing_rows)
    conflict_count, conflict_sha = _atomic_gzip_jsonl(directory / "conflicts.jsonl.gz", conflicts)
    result = {
        "schema_version": DIFF_SCHEMA,
        **identity,
        "status": "checked",
        "formal_table": FORMAL_TABLE,
        "formal_authorized": False,
        "conflict_contract_version": CONFLICT_CONTRACT_VERSION,
        "checked_at": _now(),
        "candidate_rows": len(candidate_rows),
        "candidate_unique_keys": missing.candidate_unique_keys,
        "formal_rows_read": len(formal_rows),
        "formal_unique_keys": missing.existing_formal_keys,
        "missing_keys": missing_count,
        "conflicts": conflict_count,
        "missing_file": "missing_keys.jsonl.gz",
        "missing_sha256": missing_sha,
        "conflict_file": "conflicts.jsonl.gz",
        "conflict_sha256": conflict_sha,
        "checkpoint_sha256": audit_identity["checkpoint_sha256"],
        "source_manifest_sha256": audit_identity["source_manifest_sha256"],
        "snapshot_identity": dict(snapshot_identity),
    }
    _atomic_json(directory / "manifest.json", result)
    return result


def _checkpoint_files(root: Path, archive_sha256: str, policy_hash: str) -> list[Path]:
    checkpoint_root = root / archive_sha256[:16] / policy_hash[:16]
    return sorted(checkpoint_root.glob("*/manifest.json")) if checkpoint_root.is_dir() else []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--reconciliation-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--daily-root", type=Path, required=True)
    parser.add_argument("--policy-hash", required=True)
    parser.add_argument("--max-memory-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--query-timeout-seconds", type=int, default=45)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    verified = verify_ready(args.archive)
    archive_csv_entries = int(inspect_zip(args.archive)["csv_entries"])
    snapshot_identity = _snapshot_identity(args.daily_root)
    stage_manifests = select_manifest_scope(
        iter_local_manifests(args.local_root, archive_sha256=verified.sha256),
        policy_hash=args.policy_hash,
    )
    by_entry = {str(item.payload["source_entry"]): item for item in stage_manifests}
    checkpoint_paths = _checkpoint_files(args.reconciliation_root, verified.sha256, args.policy_hash)
    args.output_root.mkdir(parents=True, exist_ok=True)
    lock = SingleWriterLock(args.output_root / ".formal_diff.lock")
    lock.acquire()
    state_path = args.output_root / "state.json"
    client: Any | None = None
    query_blocked: str | None = None
    checked = not_checked = missing_total = conflict_total = 0
    try:
        for checkpoint_path in checkpoint_paths:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(checkpoint, dict):
                continue
            output_dir = _entry_dir(args.output_root, checkpoint)
            output_manifest = output_dir / "manifest.json"
            if output_manifest.exists():
                existing = json.loads(output_manifest.read_text(encoding="utf-8"))
                if existing.get("status") in {"checked", "not_checked"}:
                    checked += existing.get("status") == "checked"
                    not_checked += existing.get("status") == "not_checked"
                    missing_total += int(existing.get("missing_keys") or 0)
                    conflict_total += int(existing.get("conflicts") or 0)
                    continue
            source_entry = str(checkpoint.get("source_entry") or "")
            source_manifest = by_entry.get(source_entry)
            if source_manifest is None:
                result = _write_not_checked(args.output_root, checkpoint, "source_stage_manifest_missing", snapshot_identity)
                not_checked += 1
                continue
            try:
                pass_rows, _review_rows, audit_identity = _validate_checkpoint(
                    checkpoint_path,
                    checkpoint,
                    source_manifest,
                    archive_sha256=verified.sha256,
                    batch_id=str(source_manifest.payload["batch_id"]),
                    policy_hash=args.policy_hash,
                    snapshot_root=args.daily_root,
                )
                pass_keys = {
                    (str(row.get("code") or ""), _as_date(row.get("trade_date")))
                    for row in pass_rows
                    if row.get("evidence_partition") == "pass" and row.get("overall_status") == "pass"
                }
                source_rows = list(source_manifest.iter_stage_rows())
                candidate_rows = [
                    row for row in source_rows
                    if (str(row.get("code") or ""), _as_date(row.get("trade_date") or row.get("datetime"))) in pass_keys
                ]
                if not candidate_rows:
                    raise FormalDiffError("no_pass_candidate_rows")
                codes = {str(row.get("code") or "") for row in candidate_rows}
                if len(codes) != 1:
                    raise FormalDiffError(f"entry_multiple_codes={sorted(codes)}")
                code = next(iter(codes))
                dates = [_as_date(row.get("trade_date") or row.get("datetime")) for row in candidate_rows]
                if client is None:
                    client = clickhouse_client()
                formal_rows = _formal_query(
                    client,
                    code,
                    min(dates),
                    max(dates) + timedelta(days=1),
                    timeout_seconds=args.query_timeout_seconds,
                    memory_bytes=args.max_memory_bytes,
                )
                result = _write_checked(
                    args.output_root,
                    checkpoint,
                    audit_identity,
                    snapshot_identity,
                    candidate_rows=candidate_rows,
                    formal_rows=formal_rows,
                )
                checked += 1
                missing_total += int(result["missing_keys"])
                conflict_total += int(result["conflicts"])
            except Exception as exc:
                if query_blocked is not None:
                    reason = query_blocked
                elif isinstance(exc, (FormalDiffError, OceanAuditError)):
                    reason = f"not_checked:{exc}"
                else:
                    query_blocked = f"formal_query_blocked:{type(exc).__name__}"
                    reason = query_blocked
                _write_not_checked(args.output_root, checkpoint, reason, snapshot_identity)
                not_checked += 1
                if query_blocked is not None:
                    # One query failure is terminal for this read-only pass;
                    # do not retry the failing formal endpoint for later entries.
                    continue
            _atomic_json(state_path, {
                "schema_version": DIFF_SCHEMA,
                "updated_at": _now(),
                "status": "query_blocked" if query_blocked else "processing",
                "checkpoint_entries": len(checkpoint_paths),
                "scope_checkpoint_entries": len(checkpoint_paths),
                "scope_archive_csv_entries": archive_csv_entries,
                "scope_label": f"{len(checkpoint_paths)}/{archive_csv_entries}",
                "scope_is_full_year": len(checkpoint_paths) == archive_csv_entries,
                "checked_entries": checked,
                "not_checked_entries": not_checked,
                "missing_keys": missing_total,
                "conflicts": conflict_total,
                "formal_authorized": False,
                "query_blocked": query_blocked,
            })
        _atomic_json(state_path, {
            "schema_version": DIFF_SCHEMA,
            "updated_at": _now(),
            "status": "query_blocked" if query_blocked else "complete",
            "checkpoint_entries": len(checkpoint_paths),
            "scope_checkpoint_entries": len(checkpoint_paths),
            "scope_archive_csv_entries": archive_csv_entries,
            "scope_label": f"{len(checkpoint_paths)}/{archive_csv_entries}",
            "scope_is_full_year": len(checkpoint_paths) == archive_csv_entries,
            "checked_entries": checked,
            "not_checked_entries": not_checked,
            "missing_keys": missing_total,
            "conflicts": conflict_total,
            "formal_authorized": False,
            "query_blocked": query_blocked,
        })
        return 2 if query_blocked else 0
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())

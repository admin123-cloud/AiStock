from __future__ import annotations

"""Consume only atomically verified annual ocean 5m ZIPs.

The downloader owns the network and emits ``<ZIP>.verified.json``.  This
worker never talks to SSH, never touches the downloader lock, and never reads
an archive without that marker.  It streams one ZIP member at a time into the
existing purchased-history stage importer, then optionally runs the existing
daily reconciliation/candidate builder and a separate missing-key promoter.

Formal writes are opt-in.  Even with ``--promote-missing``, only candidate rows
that pass the purchased daily snapshot gate are inserted, and an existing
``(code, datetime)`` key is preserved regardless of whether its values agree.
"""

import argparse
import csv
import json
import os
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zipfile import ZipFile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.import_purchased_5m_stage import (
    AUDIT_COLUMNS,
    MINUTE_STAGE,
    SOURCE_AUDIT,
    STAGE_COLUMNS,
    canonical_stock,
    create_tables,
    load_index_map,
    load_master,
    process_entry,
    source_code_from_entry,
)
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path
from utils.verified_zip import (
    VerifiedArchive,
    VerifiedZipError,
    inspect_zip,
    verify_ready,
    zip_member_name,
)

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
YEAR_RE = re.compile(r"^(20(?:0[0-9]|1[0-9]|2[0-6]))_5min\.zip$", re.IGNORECASE)
EXPECTED_HEADER = ("时间", "代码", "名称", "开盘价", "收盘价", "最高价", "最低价", "成交量", "成交额")
DEFAULT_ARCHIVE_DIR = Path(r"E:\AStockHistory\harry")
DEFAULT_DAILY_ROOT = Path(
    r"F:\AiStock\data\data\warehouse\purchased_history\daily_snapshot\purchased_daily_snapshot_20260916_r1"
)
DEFAULT_STATE_ROOT = runtime_path("ocean_vps_ingest")
DEFAULT_MIN_FREE_BYTES = 2 * 1024**3
CHUNK_BYTES = 1024 * 1024


def now() -> str:
    return datetime.now(BUSINESS_TZ).replace(microsecond=0).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def year_from_archive(path: Path) -> int | None:
    match = YEAR_RE.fullmatch(path.name)
    return int(match.group(1)) if match else None


def archive_paths(root: Path, years: set[int]) -> list[tuple[int, Path]]:
    if not root.is_dir():
        return []
    result: list[tuple[int, Path]] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        year = year_from_archive(path)
        if year is not None and (not years or year in years):
            result.append((year, path))
    return result


def format_sample_ok(sample: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
    if not sample:
        return False, {"reason": "no_csv_sample"}
    lines = list(sample.get("lines") or [])
    try:
        header = next(csv.reader([lines[0]]))
    except (IndexError, csv.Error) as exc:
        return False, {"reason": f"header_parse_error:{exc}"}
    if tuple(header[: len(EXPECTED_HEADER)]) != EXPECTED_HEADER:
        return False, {"reason": "unsupported_header", "header": header[:12]}
    try:
        data = next(csv.reader([lines[1]]))
    except (IndexError, csv.Error) as exc:
        return False, {"reason": f"data_parse_error:{exc}"}
    if len(data) != 11:
        return False, {"reason": "unsupported_column_count", "column_count": len(data)}
    if not re.fullmatch(r"(?:sh|sz|bj)\d{6}", data[1].strip(), re.IGNORECASE):
        return False, {"reason": "unsupported_source_code", "source_code": data[1].strip()}
    try:
        datetime.strptime(data[0].strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False, {"reason": "unsupported_timestamp", "timestamp": data[0].strip()}
    return True, {
        "header": header,
        "column_count": len(data),
        "timestamp_format": "%Y-%m-%d %H:%M:%S",
        "source_contract": "raw_unadjusted_5m; stock volume lots; amount yuan",
        "sample_entry": sample.get("entry"),
    }


def _stream_crc(archive: ZipFile, info: Any) -> int:
    total = 0
    with archive.open(info, "r") as stream:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
    return total


class StateStore:
    def __init__(self, root: Path, archive_dir: Path, args: argparse.Namespace) -> None:
        self.root = root
        self.path = root / "status.json"
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                loaded = {}
        else:
            loaded = {}
        self.payload: dict[str, Any] = loaded if isinstance(loaded, dict) else {}
        self.payload.setdefault("schema_version", 1)
        self.payload.setdefault("worker", "consume_verified_ocean_5m")
        self.payload.setdefault("started_at", now())
        self.payload["pid"] = os.getpid()
        self.payload["archive_dir"] = str(archive_dir)
        self.payload["state_root"] = str(root)
        self.payload["mode"] = {
            "write_stage": bool(args.write_stage),
            "write_candidates": bool(args.write_candidates),
            "promote_missing": bool(args.promote_missing),
            "poll_seconds": int(args.poll_seconds),
        }
        self.payload.setdefault("years", {})
        self.flush()

    def flush(self) -> None:
        self.payload["updated_at"] = now()
        atomic_json(self.path, self.payload)

    def year(self, year: int) -> dict[str, Any]:
        years = self.payload.setdefault("years", {})
        item = years.setdefault(str(year), {"year": year})
        return item

    def update(self, year: int, **fields: Any) -> dict[str, Any]:
        item = self.year(year)
        item.update(fields)
        self.flush()
        return item


@contextmanager
def worker_lock(path: Path) -> Iterator[None]:
    """Prevent two consumers from writing the same stage batch concurrently."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"consumer_lock_busy={path}") from exc
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


def _query_existing_hashes(client: Any, batch_id: str, source_entry: str) -> set[str]:
    code = canonical_stock(source_code_from_entry(source_entry))
    result = client.query(
        f"SELECT row_hash FROM {MINUTE_STAGE} WHERE code=%(code)s AND batch_id=%(batch)s AND source_entry=%(entry)s SETTINGS max_threads=1, max_block_size=8192",
        parameters={"batch": batch_id, "entry": source_entry, "code": code or ""},
    )
    return {str(row[0]) for row in result.result_rows}


def _query_existing_audit(client: Any, batch_id: str, source_entry: str) -> tuple[Any, ...] | None:
    rows = client.query(
        f"""
        SELECT source_row_count, valid_ohlc_row_count, unique_datetime_count,
               invalid_row_count, file_sha256
        FROM {SOURCE_AUDIT}
        WHERE batch_id=%(batch)s AND source_entry=%(entry)s
        ORDER BY audited_at DESC
        LIMIT 1
        """,
        parameters={"batch": batch_id, "entry": source_entry},
    ).result_rows
    return tuple(rows[0]) if rows else None


def _stage_entry(client: Any, audit: list[Any], rows: list[list[Any]], batch_id: str, batch_rows: int) -> dict[str, int]:
    source_entry = str(audit[2])
    existing_audit = _query_existing_audit(client, batch_id, source_entry)
    source_sha = str(audit[17])
    if existing_audit is not None:
        if str(existing_audit[4]) != source_sha:
            raise RuntimeError(f"existing_audit_sha_mismatch={source_entry}")
        if tuple(map(int, existing_audit[:4])) != tuple(int(value) for value in (audit[9], audit[10], audit[12], audit[13])):
            raise RuntimeError(f"existing_audit_shape_mismatch={source_entry}")

    existing_hashes = _query_existing_hashes(client, batch_id, source_entry)
    missing = [row for row in rows if str(row[18]) not in existing_hashes]
    for start in range(0, len(missing), batch_rows):
        client.insert(MINUTE_STAGE, missing[start:start + batch_rows], column_names=STAGE_COLUMNS,
                      settings={"max_threads": 1, "max_insert_threads": 1,
                                "max_memory_usage": 268435456})

    actual = client.query(
        f"SELECT count(), uniqExact(row_hash) FROM {MINUTE_STAGE} WHERE code=%(code)s AND batch_id=%(batch)s AND source_entry=%(entry)s SETTINGS max_threads=1, max_block_size=8192",
        parameters={"batch": batch_id, "entry": source_entry, "code": str(audit[5] or "")},
    ).result_rows[0]
    expected = len(rows)
    if int(actual[1]) != expected:
        raise RuntimeError(
            f"stage_entry_incomplete={source_entry}; expected_unique={expected}; actual_unique={actual[1]}"
        )
    if existing_audit is None:
        client.insert(SOURCE_AUDIT, [audit], column_names=AUDIT_COLUMNS)
    return {"stage_rows": expected, "inserted_rows": len(missing)}


def _run_child(script_name: str, arguments: list[str], timeout: int = 7200) -> dict[str, Any]:
    command = [sys.executable, str(ROOT / "scripts" / script_name), *arguments]
    result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    return {
        "script": script_name,
        "returncode": int(result.returncode),
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }


def _candidate_count(client: Any, batch_id: str) -> int:
    table = "stock.purchased_history_minute_5_formal_candidate_20260916"
    return int(client.query(
        f"SELECT count() FROM {table} WHERE batch_id=%(batch)s", parameters={"batch": batch_id}
    ).result_rows[0][0])


def run_downstream(
    client: Any,
    state: StateStore,
    year: int,
    batch_id: str,
    args: argparse.Namespace,
) -> None:
    item = state.year(year)
    item["batch_id"] = batch_id
    if not args.write_candidates:
        state.update(year, state="staged", downstream="not_requested")
        return
    if not args.daily_root.is_dir():
        state.update(
            year,
            state="staged_review_required",
            downstream="blocked_daily_snapshot_missing",
            daily_root=str(args.daily_root),
        )
        return

    recon = _run_child(
        "audit_purchased_5m_against_daily_snapshot.py",
        ["--batch-id", batch_id, "--daily-root", str(args.daily_root), "--refresh", "--write"],
    )
    item["daily_reconciliation"] = recon
    if recon["returncode"] != 0:
        state.update(year, state="staged_review_required", downstream="daily_reconciliation_failed")
        return
    candidate = _run_child(
        "materialize_purchased_5m_formal_candidate.py",
        ["--batch-id", batch_id, "--write"],
    )
    item["candidate_materialization"] = candidate
    if candidate["returncode"] != 0:
        state.update(year, state="audited_candidate_failed", downstream="candidate_materialization_failed")
        return
    candidate_rows = _candidate_count(client, batch_id)
    state.update(year, state="candidate_ready", candidate_rows=candidate_rows)
    if not args.promote_missing:
        return
    promotion = _run_child(
        "promote_verified_purchased_5m_missing.py",
        ["--batch-id", batch_id, "--write", "--report-dir", str(state.root / "reports")],
        timeout=12 * 3600,
    )
    item["promotion"] = promotion
    state.update(
        year,
        state="promoted" if promotion["returncode"] == 0 else "candidate_ready_promotion_failed",
        promotion_returncode=promotion["returncode"],
    )


def consume_archive(
    archive_info: VerifiedArchive,
    state: StateStore,
    year: int,
    args: argparse.Namespace,
    client: Any | None,
) -> None:
    batch_id = f"ocean_vps_5m_{year}_{archive_info.sha256[:12]}"
    item = state.year(year)
    prior = item.get("verified_sha256")
    prior_signature = (
        int(item.get("archive_size", -1)),
        int(item.get("archive_mtime_ns", -1)),
        int(item.get("marker_mtime_ns", -1)),
        str(prior or ""),
    )
    current_signature = (
        archive_info.archive.stat().st_size,
        archive_info.archive.stat().st_mtime_ns,
        archive_info.marker.stat().st_mtime_ns,
        archive_info.sha256,
    )
    if prior == archive_info.sha256 and item.get("stage_complete"):
        if client is not None:
            run_downstream(client, state, year, batch_id, args)
        return

    state.update(
        year,
        state="crc_and_format_check",
        archive=str(archive_info.archive),
        marker=str(archive_info.marker),
        verified_sha256=archive_info.sha256,
        verified_size=archive_info.size,
        archive_size=current_signature[0],
        archive_mtime_ns=current_signature[1],
        marker_mtime_ns=current_signature[2],
        batch_id=batch_id,
        source_remote=archive_info.remote,
        source_remote_stat=archive_info.remote_stat,
        verified_at=archive_info.verified_at,
    )
    inspection = inspect_zip(archive_info.archive)
    ok, format_info = format_sample_ok(inspection.get("sample"))
    if not ok:
        state.update(year, state="isolated_unsupported_format", zip_inspection=inspection, format=format_info)
        return
    state.update(year, zip_inspection=inspection, format=format_info)

    if client is not None:
        create_tables(client)
        master = load_master(client)
        index_map: dict[str, str] = {}
    else:
        master = {}
        index_map = {}

    totals = {
        "csv_entries": 0,
        "non_csv_entries": 0,
        "crc_verified_entries": 0,
        "source_rows": 0,
        "valid_ohlc_rows": 0,
        "invalid_rows": 0,
        "stage_rows": 0,
        "inserted_stage_rows": 0,
    }
    archive = None
    try:
        archive = ZipFile(archive_info.archive, "r")
        for info in archive.infolist():
            name = zip_member_name(info)
            if name.endswith("/"):
                continue
            if name.lower().endswith(".csv"):
                audit, rows = process_entry(
                    archive,
                    info,
                    archive_info.archive,
                    batch_id,
                    "stock",
                    "purchased_hs_5m",
                    master,
                    index_map,
                    include_pending=True,
                )
                totals["csv_entries"] += 1
                totals["source_rows"] += int(audit[9])
                totals["valid_ohlc_rows"] += int(audit[10])
                totals["invalid_rows"] += int(audit[13])
                totals["crc_verified_entries"] += 1
                if client is not None and args.write_stage:
                    result = _stage_entry(client, audit, rows, batch_id, args.batch_rows)
                    totals["stage_rows"] += result["stage_rows"]
                    totals["inserted_stage_rows"] += result["inserted_rows"]
            else:
                _stream_crc(archive, info)
                totals["non_csv_entries"] += 1
                totals["crc_verified_entries"] += 1
            state.update(year, state="staging", progress=totals.copy())
        state.update(
            year,
            state="stage_complete" if args.write_stage else "crc_verified_dry_run",
            stage_complete=bool(args.write_stage),
            progress=totals,
            crc_verified=True,
        )
        if client is not None and args.write_stage:
            run_downstream(client, state, year, batch_id, args)
    except Exception as exc:
        state.update(
            year,
            state="failed_preserved",
            error=f"{type(exc).__name__}: {exc}",
            progress=totals,
            evidence="archive and any isolated stage rows were preserved; no formal delete/overwrite was attempted",
        )
        raise
    finally:
        if archive is not None:
            archive.close()


def readiness_for_archive(path: Path, state: StateStore, year: int) -> VerifiedArchive | None:
    item = state.year(year)
    marker = path.with_suffix(path.suffix + ".verified.json")
    if not marker.is_file():
        state.update(year, state="waiting_verified", archive=str(path), marker=str(marker))
        return None
    try:
        known = None
        if item.get("verified_sha256"):
            known = (
                int(item.get("archive_size", -1)),
                int(item.get("archive_mtime_ns", -1)),
                int(item.get("marker_mtime_ns", -1)),
                str(item.get("verified_sha256")),
            )
        # Re-hash only on the first observation or when either local file's
        # stat changed.  The marker itself was atomically created after the
        # downloader completed its full local hash.
        current = path.stat()
        marker_stat = marker.stat()
        if known and known[:3] == (current.st_size, current.st_mtime_ns, marker_stat.st_mtime_ns):
            return verify_ready(path, marker, known_signature=known)
        return verify_ready(path, marker)
    except (OSError, VerifiedZipError) as exc:
        state.update(year, state="waiting_or_rejected", error=f"{type(exc).__name__}: {exc}")
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--daily-root", type=Path, default=DEFAULT_DAILY_ROOT)
    parser.add_argument("--year", action="append", type=int, dest="years")
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--max-years-per-scan", type=int, default=1)
    parser.add_argument("--batch-rows", type=int, default=5000)
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    parser.add_argument("--write-stage", action="store_true")
    parser.add_argument("--write-candidates", action="store_true")
    parser.add_argument("--promote-missing", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def run_once(args: argparse.Namespace, state: StateStore) -> int:
    years = set(args.years or range(2000, 2027))
    candidates = archive_paths(args.archive_dir, years)
    processed = 0
    client: Any | None = None
    for year, path in candidates:
        previous = state.year(year)
        terminal = {"promoted", "staged_review_required", "audited_candidate_failed",
                    "candidate_ready_promotion_failed", "isolated_unsupported_format"}
        if not args.write_candidates:
            terminal.add("staged")
        if not args.promote_missing:
            terminal.add("candidate_ready")
        if previous.get("state") in terminal:
            continue
        if processed >= args.max_years_per_scan:
            break
        if args.min_free_bytes > 0 and path.drive:
            free = shutil_disk_free(path)
            if free < args.min_free_bytes:
                state.update(year, state="blocked_disk", free_bytes=free, min_free_bytes=args.min_free_bytes)
                continue
        verified = readiness_for_archive(path, state, year)
        if verified is None:
            continue
        if client is None and args.write_stage:
            client = clickhouse_client()
        try:
            consume_archive(verified, state, year, args, client)
        except Exception:
            # Evidence is already persisted in status.json; continue only when
            # explicitly polling so an unrelated year can become ready later.
            if args.once:
                return 4
        processed += 1
    state.payload["last_scan"] = {"candidate_archives": len(candidates), "processed": processed, "at": now()}
    state.flush()
    return 0


def shutil_disk_free(path: Path) -> int:
    usage = os.statvfs(path) if hasattr(os, "statvfs") else None
    if usage is not None:
        return int(usage.f_bavail * usage.f_frsize)
    import shutil

    return int(shutil.disk_usage(path).free)


def main() -> int:
    args = parse_args()
    if args.poll_seconds < 5 or args.max_years_per_scan < 1 or args.batch_rows < 1:
        raise SystemExit("poll-seconds, max-years-per-scan, and batch-rows must be positive")
    with worker_lock(args.state_root / "consumer.lock"):
        state = StateStore(args.state_root, args.archive_dir, args)
        while True:
            code = run_once(args, state)
            if args.once:
                return code
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

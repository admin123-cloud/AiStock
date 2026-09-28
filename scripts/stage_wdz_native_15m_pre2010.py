from __future__ import annotations

"""Stage native 15-minute bars from the purchased A股分时数据 archive.

This is deliberately an isolated, append-only evidence build.  It never writes
``stock.kline_minute_15`` or any other formal table.  The source files are the
annual CSV archives under ``A股_分时数据_沪深/15分钟_按年汇总``; they carry 16
standard A-share 15-minute endpoints per complete trading day.

The stage keeps the source provenance on every row and a per-file audit row.
Rows with invalid OHLC, malformed timestamps, or non-standard time slots are
excluded from the candidate stage but remain visible in the audit manifest.
Missing slots are evidence, not automatically filled here.  A later merge
step can fill only the missing logical keys from complete 5m/1m aggregation.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zipfile import ZipFile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_ROOT = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\15分钟_按年汇总")
DEFAULT_BATCH = "wdz_native_15_pre2010_v20260921"
TARGET = "stock.wdz_native_15_pre2010_v20260921_stage"
AUDIT = "stock.wdz_native_15_pre2010_v20260921_file_audit"
OUT = Path(r"F:\AiStock\data\data\runtime\wdz_native_15_pre2010_v20260921")
EXPECTED_SLOTS = {
    "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30",
    "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "15:00",
}
EXPECTED_COUNT = 16

STAGE_COLUMNS = [
    "code", "datetime", "open", "high", "low", "close", "volume", "amount",
    "volume_unit", "amount_unit",
    "source_file", "source_member", "source_frequency", "source_kind", "source_year",
    "source_row_number", "batch_id", "quality_status", "created_at", "id",
]
AUDIT_COLUMNS = [
    "source_year", "source_file", "source_member", "source_code", "code",
    "source_rows", "valid_stage_rows", "invalid_rows", "duplicate_keys",
    "unique_datetime_count", "day_count", "complete_days", "incomplete_days",
    "missing_slot_count", "extra_slot_count", "code_mismatch_rows",
    "min_datetime", "max_datetime", "file_size", "file_sha256", "audit_status",
    "source_frequency", "source_kind", "batch_id", "audited_at",
]


def ddl_stage() -> str:
    return f"""CREATE TABLE {TARGET}
(
 `code` String, `datetime` DateTime('Asia/Shanghai'),
 `open` Float64, `high` Float64, `low` Float64, `close` Float64,
 `volume` Float64, `amount` Float64,
 `volume_unit` LowCardinality(String), `amount_unit` LowCardinality(String),
 `source_file` String, `source_member` String,
 `source_frequency` LowCardinality(String), `source_kind` LowCardinality(String),
 `source_year` UInt16, `source_row_number` UInt32, `batch_id` String,
 `quality_status` LowCardinality(String), `created_at` DateTime('Asia/Shanghai'),
 `id` UInt64
) ENGINE = MergeTree PARTITION BY toYYYYMM(datetime)
ORDER BY (code, datetime) SETTINGS index_granularity=8192"""


def ddl_audit() -> str:
    return f"""CREATE TABLE {AUDIT}
(
 `source_year` UInt16, `source_file` String, `source_member` String,
 `source_code` String, `code` String, `source_rows` UInt32,
 `valid_stage_rows` UInt32, `invalid_rows` UInt32, `duplicate_keys` UInt32,
 `unique_datetime_count` UInt32, `day_count` UInt32, `complete_days` UInt32,
 `incomplete_days` UInt32, `missing_slot_count` UInt32, `extra_slot_count` UInt32,
 `code_mismatch_rows` UInt32, `min_datetime` Nullable(DateTime('Asia/Shanghai')),
 `max_datetime` Nullable(DateTime('Asia/Shanghai')), `file_size` UInt64,
 `file_sha256` String, `audit_status` LowCardinality(String),
 `source_frequency` LowCardinality(String), `source_kind` LowCardinality(String),
 `batch_id` String, `audited_at` DateTime('Asia/Shanghai')
) ENGINE = MergeTree ORDER BY (source_year, source_member)"""


def parse_float(value: str) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=BUSINESS_TZ)
        except ValueError:
            pass
    return None


def canonical_code(raw: str) -> str:
    text = str(raw or "").strip().upper()
    match = re.fullmatch(r"(SH|SZ)(\d{6})", text)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    match = re.fullmatch(r"(\d{6})\.(SH|SZ)", text)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return ""


def member_code(member: str) -> str:
    stem = Path(member).stem
    stem = re.sub(r"_20\d{2}$", "", stem, flags=re.IGNORECASE)
    return canonical_code(stem)


def stable_id(code: str, dt: datetime, batch: str) -> int:
    payload = f"{code}|{dt:%Y-%m-%d %H:%M:%S}|15m|{batch}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def write_manifest(payload: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "manifest.json"
    fd, temp = tempfile.mkstemp(prefix="manifest.", suffix=".json", dir=str(OUT))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def exists(c, table: str) -> bool:
    db, name = table.split(".", 1)
    row = c.query(
        "SELECT count() FROM system.tables WHERE database=%(db)s AND name=%(name)s",
        parameters={"db": db, "name": name},
    ).result_rows
    return bool(row and int(row[0][0]))


def parse_member(raw: bytes, info: Any, archive_path: Path, year: int, batch: str) -> tuple[list[tuple], list[Any], dict[str, int]]:
    source_member = str(info.filename).replace("\\", "/")
    source_code = member_code(source_member)
    member_code_raw = Path(source_member).stem
    member_code_raw = re.sub(r"_20\d{2}$", "", member_code_raw, flags=re.IGNORECASE)
    digest = hashlib.sha256(raw).hexdigest()
    rows: list[tuple] = []
    dt_keys: set[tuple[str, datetime]] = set()
    by_day: dict[date, set[str]] = defaultdict(set)
    source_rows = 0
    invalid_rows = 0
    duplicate_keys = 0
    mismatch_rows = 0
    min_dt: datetime | None = None
    max_dt: datetime | None = None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
    reader = csv.reader(text.splitlines())
    next(reader, None)
    created_at = datetime.now(BUSINESS_TZ)
    for row_number, cells in enumerate(reader, start=2):
        if not any(str(cell).strip() for cell in cells):
            continue
        source_rows += 1
        try:
            if len(cells) < 9:
                raise ValueError("short row")
            dt = parse_dt(cells[0])
            row_code = canonical_code(cells[1])
            open_, close, high, low = (parse_float(cells[i]) for i in (3, 4, 5, 6))
            volume = parse_float(cells[7])
            amount = parse_float(cells[8])
            if dt is None or not row_code or row_code != source_code:
                if dt is not None and row_code and row_code != source_code:
                    mismatch_rows += 1
                raise ValueError("invalid datetime/code")
            if any(value is None or value <= 0 for value in (open_, close, high, low)):
                raise ValueError("invalid OHLC")
            if volume is None or amount is None or volume < 0 or amount < 0:
                raise ValueError("invalid volume/amount")
            slot = dt.strftime("%H:%M")
            if slot not in EXPECTED_SLOTS:
                invalid_rows += 1
                continue
            key = (row_code, dt)
            if key in dt_keys:
                duplicate_keys += 1
                invalid_rows += 1
                continue
            dt_keys.add(key)
            by_day[dt.date()].add(slot)
            min_dt = dt if min_dt is None or dt < min_dt else min_dt
            max_dt = dt if max_dt is None or dt > max_dt else max_dt
            row_hash = hashlib.sha256(f"{digest}:{row_number}".encode("ascii")).hexdigest()
            rows.append((
                row_code, dt, float(open_), float(high), float(low), float(close), float(volume), float(amount),
                "lots", "yuan",
                str(archive_path), source_member, "15m", "native", year, row_number, batch,
                "native_wdz_valid", created_at, stable_id(row_code, dt, batch),
            ))
        except (TypeError, ValueError, IndexError):
            invalid_rows += 1
    missing = sum(len(EXPECTED_SLOTS - slots) for slots in by_day.values())
    extra = 0
    complete = sum(1 for slots in by_day.values() if len(slots) == EXPECTED_COUNT)
    incomplete = len(by_day) - complete
    status = "pass" if invalid_rows == 0 and duplicate_keys == 0 and mismatch_rows == 0 and incomplete == 0 else "review"
    audit = (
        year, str(archive_path), source_member, member_code_raw, source_code, source_rows,
        len(rows), invalid_rows, duplicate_keys, len(dt_keys), len(by_day), complete, incomplete,
        missing, extra, mismatch_rows, min_dt, max_dt, info.file_size, digest, status,
        "15m", "native", batch, datetime.now(BUSINESS_TZ),
    )
    stats = {"source_rows": source_rows, "stage_rows": len(rows), "invalid_rows": invalid_rows,
             "duplicate_keys": duplicate_keys, "days": len(by_day), "complete_days": complete,
             "incomplete_days": incomplete, "missing_slots": missing, "mismatch_rows": mismatch_rows}
    return rows, [audit], stats


def insert_batches(c, table: str, rows: list[tuple], columns: list[str], batch_rows: int) -> int:
    total = 0
    for start in range(0, len(rows), batch_rows):
        chunk = rows[start:start + batch_rows]
        for attempt in range(8):
            try:
                c.insert(table, chunk, column_names=columns)
                break
            except Exception as exc:
                text = str(exc)
                if "TOO_MANY_SIMULTANEOUS_QUERIES" not in text and "Too many simultaneous queries" not in text:
                    raise
                if attempt == 7:
                    raise
                delay = min(30, 2 ** attempt)
                print(json.dumps({"phase": "insert_retry", "table": table, "rows": len(chunk), "attempt": attempt + 1, "sleep_seconds": delay}, ensure_ascii=False), flush=True)
                time.sleep(delay)
        total += len(chunk)
    return total


def five_minute_aggregate_diffs(c, year: int) -> dict[str, int]:
    five = "stock.kline_minute_5_pre2010_v20260921_r2_fallback_highvol"
    settings = "SETTINGS max_threads=4, max_memory_usage=12000000000, max_bytes_before_external_group_by=2000000000"
    source = f"""
    SELECT code, bucket
    FROM
    (
      SELECT code, datetime,
        if(toHour(datetime)<12,
           toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),
           toStartOfDay(datetime)+toIntervalHour(13)) AS session_start,
        dateDiff('minute', session_start, datetime) AS elapsed,
        session_start + toIntervalMinute(if(elapsed<=0,15,intDiv(elapsed+14,15)*15)) AS bucket
      FROM {five}
      WHERE toYear(datetime)=%(y)s
    )
    GROUP BY code,bucket
    HAVING count()=3 AND uniqExact(datetime)=3
    """
    native = f"SELECT code, datetime AS bucket FROM {TARGET} WHERE source_year=%(y)s"
    def anti(left: str, right: str) -> int:
        row = c.query(
            f"SELECT count() FROM ({left}) AS l LEFT ANTI JOIN ({right}) AS r ON l.code=r.code AND l.bucket=r.bucket {settings}",
            parameters={"y": year},
        ).result_rows
        return int(row[0][0]) if row else 0
    return {"native_keys_missing_in_5m_aggregate": anti(native, source),
            "five_minute_aggregate_keys_missing_in_native": anti(source, native)}


def existing_stage_members(c, year: int) -> dict[str, int]:
    rows = c.query(
        f"SELECT source_member, count() FROM {TARGET} WHERE source_year=%(y)s GROUP BY source_member",
        parameters={"y": year},
    ).result_rows
    return {str(member): int(count) for member, count in rows}


def existing_stage_keys(c, year: int, member: str) -> set[tuple[str, datetime]]:
    rows = c.query(
        f"SELECT code, datetime FROM {TARGET} WHERE source_year=%(y)s AND source_member=%(m)s",
        parameters={"y": year, "m": member},
    ).result_rows
    return {(str(code), dt) for code, dt in rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--start-year", type=int, default=2000)
    ap.add_argument("--end-year", type=int, default=2010)
    ap.add_argument("--target-prefix", default="wdz_native_15_pre2010_v20260921",
                    help="isolated table prefix without stock.; use a new prefix for purchased-source staging")
    ap.add_argument("--output-dir", type=Path,
                    help="manifest directory; defaults to the standard WDZ directory")
    ap.add_argument("--batch-id", default=DEFAULT_BATCH)
    ap.add_argument("--batch-rows", type=int, default=20000)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--resume", action="store_true", help="resume an existing isolated stage from manifest")
    args = ap.parse_args()
    if args.start_year < 2000 or args.end_year > 2010 or args.start_year > args.end_year:
        raise SystemExit("This stage supports 2000-2010 only")
    global TARGET, AUDIT, OUT
    TARGET = f"stock.{args.target_prefix}_stage"
    AUDIT = f"stock.{args.target_prefix}_file_audit"
    OUT = args.output_dir or (Path(r"F:\AiStock\data\data\runtime") / args.target_prefix)
    archives = [args.source_root / f"{year}_15min.zip" for year in range(args.start_year, args.end_year + 1)]
    missing_archives = [str(path) for path in archives if not path.exists()]
    if missing_archives:
        raise SystemExit(f"missing archives: {missing_archives}")
    c = clickhouse_client()
    if not args.execute:
        print(json.dumps({"dry_run": True, "target": TARGET, "audit": AUDIT, "archives": [str(x) for x in archives]}, ensure_ascii=False))
        return 0
    manifest_path = OUT / "manifest.json"
    old_manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            old_manifest = {}
    if args.resume:
        if not exists(c, TARGET) or not exists(c, AUDIT):
            raise SystemExit("--resume requires both existing isolated target and audit tables")
    elif exists(c, TARGET) or exists(c, AUDIT):
        raise SystemExit(f"refusing existing target/audit table: {TARGET}, {AUDIT}")
    if not exists(c, TARGET):
        c.command(ddl_stage())
    if not exists(c, AUDIT):
        c.command(ddl_audit())
    manifest: dict[str, Any] = {
        "schema_version": "wdz-native-15m-pre2010-v1",
        "scope": [args.start_year, args.end_year], "source_root": str(args.source_root),
        "source_frequency": "15m", "source_kind": "native",
        "expected_slots": sorted(EXPECTED_SLOTS), "expected_bars_per_complete_day": 16,
        "target_table": TARGET, "audit_table": AUDIT, "batch_id": args.batch_id,
        "status": "running", "years": {}, "formal_tables_touched": False,
        "started_at": datetime.now(BUSINESS_TZ).isoformat(),
    }
    if args.resume and old_manifest:
        manifest = old_manifest
        manifest["status"] = "resuming"
        manifest["resumed_at"] = datetime.now(BUSINESS_TZ).isoformat()
    write_manifest(manifest)
    total_rows = 0
    total_audits = 0
    if args.resume:
        total_rows = int(c.query(f"SELECT count() FROM {TARGET}").result_rows[0][0])
        total_audits = int(c.query(f"SELECT count() FROM {AUDIT}").result_rows[0][0])
    for year, archive_path in zip(range(args.start_year, args.end_year + 1), archives):
        existing_year = manifest.get("years", {}).get(str(year), {})
        if args.resume and existing_year.get("status") in {"pass", "review"}:
            print(json.dumps({"year": year, "phase": "resume_skip", "status": existing_year.get("status")}, ensure_ascii=False), flush=True)
            continue
        year_rows = 0
        year_audits = 0
        pending_rows: list[tuple] = []
        pending_audits: list[tuple] = []
        year_stats = {"archive": str(archive_path), "members": 0, "source_rows": 0, "stage_rows": 0,
                      "invalid_rows": 0, "complete_days": 0, "incomplete_days": 0, "missing_slots": 0,
                      "duplicate_keys": 0, "status": "running"}
        resume_counts = existing_stage_members(c, year) if args.resume else {}
        audit_members = set()
        if args.resume:
            audit_members = {str(row[0]) for row in c.query(
                f"SELECT source_member FROM {AUDIT} WHERE source_year=%(y)s", parameters={"y": year}
            ).result_rows}
        with ZipFile(archive_path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir() and Path(info.filename).suffix.lower() == ".csv"]
            year_stats["members"] = len(infos)
            for index, info in enumerate(infos, start=1):
                rows, audits, stats = parse_member(archive.read(info), info, archive_path, year, args.batch_id)
                if args.resume and info.filename in resume_counts:
                    existing_keys = existing_stage_keys(c, year, info.filename)
                    rows = [row for row in rows if (row[0], row[1]) not in existing_keys]
                if rows:
                    pending_rows.extend(rows)
                    while len(pending_rows) >= args.batch_rows:
                        chunk = pending_rows[:args.batch_rows]
                        del pending_rows[:args.batch_rows]
                        year_rows += insert_batches(c, TARGET, chunk, STAGE_COLUMNS, args.batch_rows)
                if audits and (not args.resume or info.filename not in audit_members):
                    pending_audits.extend(audits)
                for key in ("source_rows", "stage_rows", "invalid_rows", "complete_days", "incomplete_days", "missing_slots", "duplicate_keys"):
                    year_stats[key] += stats.get(key, 0)
                if index % 100 == 0 or index == len(infos):
                    print(json.dumps({"year": year, "member": index, "members": len(infos), "stage_rows": year_rows}, ensure_ascii=False), flush=True)
        if pending_rows:
            year_rows += insert_batches(c, TARGET, pending_rows, STAGE_COLUMNS, args.batch_rows)
            pending_rows.clear()
        if pending_audits:
            for attempt in range(8):
                try:
                    c.insert(AUDIT, pending_audits, column_names=AUDIT_COLUMNS)
                    break
                except Exception as exc:
                    text = str(exc)
                    if "TOO_MANY_SIMULTANEOUS_QUERIES" not in text and "Too many simultaneous queries" not in text:
                        raise
                    if attempt == 7:
                        raise
                    delay = min(30, 2 ** attempt)
                    print(json.dumps({"phase": "audit_insert_retry", "year": year, "attempt": attempt + 1, "sleep_seconds": delay}, ensure_ascii=False), flush=True)
                    time.sleep(delay)
            year_audits += len(pending_audits)
            pending_audits.clear()
        year_stats["status"] = "pass" if year_stats["invalid_rows"] == 0 and year_stats["duplicate_keys"] == 0 and year_stats["incomplete_days"] == 0 else "review"
        manifest["years"][str(year)] = year_stats
        manifest["processed_years"] = year + 1 - args.start_year
        manifest["stage_rows"] = total_rows + year_rows
        manifest["audit_rows"] = total_audits + year_audits
        write_manifest(manifest)
        total_rows += year_rows
        total_audits += year_audits
        print(json.dumps({"year": year, **year_stats, "memory_limit_bytes": 12000000000}, ensure_ascii=False), flush=True)
    manifest["status"] = "loaded"
    manifest["stage_rows"] = total_rows
    manifest["audit_rows"] = total_audits
    manifest["finished_at"] = datetime.now(BUSINESS_TZ).isoformat()
    manifest["five_minute_aggregate_diffs"] = {}
    for year in range(args.start_year, args.end_year + 1):
        print(json.dumps({"year": year, "phase": "5m_aggregate_diff_start"}, ensure_ascii=False), flush=True)
        manifest["five_minute_aggregate_diffs"][str(year)] = five_minute_aggregate_diffs(c, year)
        write_manifest(manifest)
        print(json.dumps({"year": year, "five_minute_aggregate_diffs": manifest["five_minute_aggregate_diffs"][str(year)]}, ensure_ascii=False), flush=True)
    manifest["status"] = "complete"
    write_manifest(manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


from __future__ import annotations

"""Stage native 30m/60m bars from the purchased intraday archive.

The source is the immutable archive under
``E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深``.  Each frequency is
processed independently and writes only an isolated evidence table plus a
file/member audit table.  No formal ``kline_minute_*`` table is touched.

The stage deliberately treats a source member/day with missing slots as
``review``.  A later resolver may fill only the missing logical keys from a
complete 5m/1m aggregation; this script never silently mixes the sources.
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
SOURCE_BASE = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深")
DEFAULT_BATCH_PREFIX = "purchased_native_{freq}_pre2010_v20260921"
OUT_BASE = Path(r"F:\AiStock\data\data\runtime")

SLOTS = {
    30: {"10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"},
    60: {"10:30", "11:30", "14:00", "15:00"},
}
EXPECTED = {30: 8, 60: 4}

STAGE_COLUMNS = [
    "code", "datetime", "open", "high", "low", "close", "volume", "amount",
    "volume_unit", "amount_unit", "source_file", "source_member", "source_frequency",
    "source_kind", "source_year", "source_row_number", "batch_id", "quality_status",
    "created_at", "id",
]
AUDIT_COLUMNS = [
    "source_year", "source_file", "source_member", "source_code", "code", "source_rows",
    "valid_stage_rows", "invalid_rows", "duplicate_keys", "unique_datetime_count", "day_count",
    "complete_days", "incomplete_days", "missing_slot_count", "extra_slot_count",
    "code_mismatch_rows", "min_datetime", "max_datetime", "file_size", "member_crc32",
    "member_sha256", "archive_sha256", "crc_verified", "audit_status", "source_frequency",
    "source_kind", "batch_id", "audited_at",
]


def table_names(freq: int) -> tuple[str, str]:
    suffix = f"{freq}_pre2010_v20260921"
    return f"stock.purchased_native_{suffix}_stage", f"stock.purchased_native_{suffix}_file_audit"


def output_dir(freq: int) -> Path:
    return OUT_BASE / f"purchased_native_{freq}_pre2010_v20260921"


def ddl_stage(table: str) -> str:
    return f"""CREATE TABLE {table}
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


def ddl_audit(table: str) -> str:
    return f"""CREATE TABLE {table}
(
 `source_year` UInt16, `source_file` String, `source_member` String,
 `source_code` String, `code` String, `source_rows` UInt32, `valid_stage_rows` UInt32,
 `invalid_rows` UInt32, `duplicate_keys` UInt32, `unique_datetime_count` UInt32,
 `day_count` UInt32, `complete_days` UInt32, `incomplete_days` UInt32,
 `missing_slot_count` UInt32, `extra_slot_count` UInt32, `code_mismatch_rows` UInt32,
 `min_datetime` Nullable(DateTime('Asia/Shanghai')), `max_datetime` Nullable(DateTime('Asia/Shanghai')),
 `file_size` UInt64, `member_crc32` UInt64, `member_sha256` String, `archive_sha256` String,
 `crc_verified` UInt8, `audit_status` LowCardinality(String),
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
    stem = re.sub(r"_20\d{2}$", "", Path(member).stem, flags=re.IGNORECASE)
    return canonical_code(stem)


def stable_id(code: str, dt: datetime, freq: int, batch: str) -> int:
    payload = f"{code}|{dt:%Y-%m-%d %H:%M:%S}|{freq}m|{batch}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="manifest.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def exists(c: Any, table: str) -> bool:
    db, name = table.split(".", 1)
    rows = c.query(
        "SELECT count() FROM system.tables WHERE database=%(db)s AND name=%(name)s",
        parameters={"db": db, "name": name},
    ).result_rows
    return bool(rows and int(rows[0][0]))


def archive_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_member(raw: bytes, info: Any, archive_path: Path, archive_digest: str, year: int, freq: int, batch: str) -> tuple[list[tuple], tuple, dict[str, int]]:
    source_member = str(info.filename).replace("\\", "/")
    source_code = member_code(source_member)
    source_code_raw = re.sub(r"_20\d{2}$", "", Path(source_member).stem, flags=re.IGNORECASE)
    member_digest = hashlib.sha256(raw).hexdigest()
    rows: list[tuple] = []
    keys: set[tuple[str, datetime]] = set()
    by_day: dict[date, set[str]] = defaultdict(set)
    source_rows = invalid_rows = duplicate_keys = mismatch_rows = 0
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
            if any(v is None or v <= 0 for v in (open_, close, high, low)):
                raise ValueError("invalid OHLC")
            if volume is None or amount is None or volume < 0 or amount < 0:
                raise ValueError("invalid volume/amount")
            slot = dt.strftime("%H:%M")
            if slot not in SLOTS[freq]:
                raise ValueError("non-standard time slot")
            key = (row_code, dt)
            if key in keys:
                duplicate_keys += 1
                raise ValueError("duplicate key")
            keys.add(key)
            by_day[dt.date()].add(slot)
            min_dt = dt if min_dt is None or dt < min_dt else min_dt
            max_dt = dt if max_dt is None or dt > max_dt else max_dt
            rows.append((
                row_code, dt, float(open_), float(high), float(low), float(close), float(volume), float(amount),
                "lots", "yuan", str(archive_path), source_member, f"{freq}m", "native", year,
                row_number, batch, "native_purchased_valid", created_at, stable_id(row_code, dt, freq, batch),
            ))
        except (TypeError, ValueError, IndexError):
            invalid_rows += 1
    missing = sum(len(SLOTS[freq] - slots) for slots in by_day.values())
    extra = 0
    complete = sum(1 for slots in by_day.values() if len(slots) == EXPECTED[freq])
    incomplete = len(by_day) - complete
    status = "pass" if invalid_rows == 0 and duplicate_keys == 0 and mismatch_rows == 0 and incomplete == 0 else "review"
    audit = (
        year, str(archive_path), source_member, source_code_raw, source_code, source_rows, len(rows),
        invalid_rows, duplicate_keys, len(keys), len(by_day), complete, incomplete, missing, extra,
        mismatch_rows, min_dt, max_dt, info.file_size, int(info.CRC), member_digest, archive_digest, 1,
        status, f"{freq}m", "native", batch, datetime.now(BUSINESS_TZ),
    )
    stats = {"source_rows": source_rows, "stage_rows": len(rows), "invalid_rows": invalid_rows,
             "duplicate_keys": duplicate_keys, "days": len(by_day), "complete_days": complete,
             "incomplete_days": incomplete, "missing_slots": missing, "mismatch_rows": mismatch_rows}
    return rows, audit, stats


def insert_batches(c: Any, table: str, rows: list[tuple], batch_rows: int) -> int:
    total = 0
    for start in range(0, len(rows), batch_rows):
        chunk = rows[start:start + batch_rows]
        for attempt in range(6):
            try:
                c.insert(table, chunk, column_names=STAGE_COLUMNS)
                break
            except Exception as exc:
                if "TOO_MANY_SIMULTANEOUS_QUERIES" not in str(exc) or attempt >= 5:
                    raise
                time.sleep(5 * (attempt + 1))
        total += len(chunk)
    return total


def five_minute_aggregate_diffs(c: Any, target: str, freq: int, year: int) -> dict[str, int]:
    five = "stock.kline_minute_5_pre2010_v20260921_r2_fallback_highvol"
    settings = "SETTINGS max_threads=2, max_memory_usage=12000000000, max_bytes_before_external_group_by=1000000000"
    source = f"""
    SELECT code, bucket FROM
    (
      SELECT code, datetime,
        if(toHour(datetime)<12,
           toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),
           toStartOfDay(datetime)+toIntervalHour(13)) AS session_start,
        dateDiff('minute', session_start, datetime) AS elapsed,
        session_start + toIntervalMinute(if(elapsed<=0,{freq},intDiv(elapsed+{freq}-1,{freq})*{freq})) AS bucket
      FROM {five}
      WHERE toYear(datetime)=%(y)s
    )
    GROUP BY code,bucket
    HAVING count()={freq // 5} AND uniqExact(datetime)={freq // 5}
    """
    native = f"SELECT code, datetime AS bucket FROM {target} WHERE source_year=%(y)s"

    def anti(left: str, right: str) -> int:
        rows = c.query(
            f"SELECT count() FROM ({left}) AS l LEFT ANTI JOIN ({right}) AS r ON l.code=r.code AND l.bucket=r.bucket {settings}",
            parameters={"y": year},
        ).result_rows
        return int(rows[0][0]) if rows else 0

    return {
        "native_keys_missing_in_5m_aggregate": anti(native, source),
        "five_minute_aggregate_keys_missing_in_native": anti(source, native),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frequency", type=int, choices=(30, 60), required=True)
    ap.add_argument("--source-root", type=Path)
    ap.add_argument("--start-year", type=int, default=2000)
    ap.add_argument("--end-year", type=int, default=2010)
    ap.add_argument("--batch-id")
    ap.add_argument("--batch-rows", type=int, default=20000)
    ap.add_argument("--resume", action="store_true", help="resume an existing isolated stage by logical member/time key")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    if args.start_year < 2000 or args.end_year > 2010 or args.start_year > args.end_year:
        raise SystemExit("This stage supports 2000-2010 only")
    freq = args.frequency
    source_root = args.source_root or (SOURCE_BASE / f"{freq}分钟_按年汇总")
    batch = args.batch_id or DEFAULT_BATCH_PREFIX.format(freq=freq)
    target, audit = table_names(freq)
    archives = [source_root / f"{year}_{freq}min.zip" for year in range(args.start_year, args.end_year + 1)]
    missing = [str(p) for p in archives if not p.exists()]
    if missing:
        raise SystemExit(f"missing archives: {missing}")
    if not args.execute:
        print(json.dumps({"dry_run": True, "frequency": freq, "target": target, "audit": audit, "archives": [str(x) for x in archives]}, ensure_ascii=False))
        return 0
    c = clickhouse_client()
    already_target = exists(c, target)
    already_audit = exists(c, audit)
    if args.resume:
        if not already_target or not already_audit:
            raise SystemExit(f"--resume requires both existing isolated tables: {target}, {audit}")
    else:
        if already_target or already_audit:
            raise SystemExit(f"refusing existing target/audit table: {target}, {audit}")
        c.command(ddl_stage(target))
        c.command(ddl_audit(audit))
    out = output_dir(freq)
    manifest_path = out / "manifest.json"
    if args.resume and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "resuming"
        manifest["resumed_at"] = datetime.now(BUSINESS_TZ).isoformat()
        manifest["resume_years"] = [args.start_year, args.end_year]
        manifest["resume_safe_key"] = "(source_member, code, datetime)"
    else:
        manifest = {
        "schema_version": f"purchased-native-{freq}m-pre2010-v1", "scope": [2000, 2010],
        "source_root": str(source_root), "source_frequency": f"{freq}m", "source_kind": "native_purchased",
        "expected_slots": sorted(SLOTS[freq]), "expected_bars_per_complete_day": EXPECTED[freq],
        "target_table": target, "audit_table": audit, "batch_id": batch, "status": "running",
        "years": {}, "formal_tables_touched": False, "started_at": datetime.now(BUSINESS_TZ).isoformat(),
        }
    write_manifest(manifest_path, manifest)
    total_rows = total_audits = 0
    existing_members: set[str] = set()
    existing_audits: set[str] = set()
    if args.resume:
        existing_members = {str(row[0]) for row in c.query(
            f"SELECT DISTINCT source_member FROM {target} WHERE source_year BETWEEN %(start)s AND %(end)s",
            parameters={"start": args.start_year, "end": args.end_year},
        ).result_rows}
        existing_audits = {str(row[0]) for row in c.query(
            f"SELECT DISTINCT source_member FROM {audit} WHERE source_year BETWEEN %(start)s AND %(end)s",
            parameters={"start": args.start_year, "end": args.end_year},
        ).result_rows}
        manifest["existing_members_at_resume"] = len(existing_members)
        manifest["existing_audits_at_resume"] = len(existing_audits)
    for year, archive_path in zip(range(args.start_year, args.end_year + 1), archives):
        archive_digest = archive_sha256(archive_path)
        year_rows = year_audits = 0
        pending_rows: list[tuple] = []
        pending_audits: list[tuple] = []
        stats_year = {"archive": str(archive_path), "archive_sha256": archive_digest, "members": 0,
                      "crc_bad_members": 0, "source_rows": 0, "stage_rows": 0, "invalid_rows": 0,
                      "complete_days": 0, "incomplete_days": 0, "missing_slots": 0, "duplicate_keys": 0,
                      "status": "running"}
        with ZipFile(archive_path) as archive:
            bad_member = archive.testzip()
            if bad_member:
                stats_year["crc_bad_members"] = 1
                raise RuntimeError(f"ZIP CRC failure: {archive_path} member={bad_member}")
            infos = [i for i in archive.infolist() if not i.is_dir() and Path(i.filename).suffix.lower() == ".csv"]
            stats_year["members"] = len(infos)
            for index, info in enumerate(infos, start=1):
                raw = archive.read(info)  # reads to EOF and lets zipfile validate CRC
                rows, audit_row, member_stats = parse_member(raw, info, archive_path, archive_digest, year, freq, batch)
                member_name = str(info.filename).replace("\\", "/")
                if args.resume and member_name in existing_members:
                    # A previous run may have stopped in the middle of a member.
                    # Insert only logical keys not already present; never append
                    # a duplicate copy of a partially inserted member.
                    old_rows = c.query(
                        f"SELECT datetime FROM {target} WHERE source_member=%(member)s",
                        parameters={"member": member_name},
                    ).result_rows
                    old_keys = {row[0] for row in old_rows}
                    rows = [row for row in rows if row[1] not in old_keys]
                if args.resume and member_name in existing_audits:
                    audit_row = None
                if rows:
                    pending_rows.extend(rows)
                    while len(pending_rows) >= args.batch_rows:
                        chunk = pending_rows[:args.batch_rows]
                        del pending_rows[:args.batch_rows]
                        year_rows += insert_batches(c, target, chunk, args.batch_rows)
                if audit_row is not None:
                    pending_audits.append(audit_row)
                for key in ("source_rows", "stage_rows", "invalid_rows", "complete_days", "incomplete_days", "missing_slots", "duplicate_keys"):
                    stats_year[key] += member_stats.get(key, 0)
                if index % 100 == 0 or index == len(infos):
                    print(json.dumps({"frequency": freq, "year": year, "member": index, "members": len(infos), "stage_rows": year_rows}, ensure_ascii=False), flush=True)
            if pending_rows:
                year_rows += insert_batches(c, target, pending_rows, args.batch_rows)
            if pending_audits:
                c.insert(audit, pending_audits, column_names=AUDIT_COLUMNS)
                year_audits += len(pending_audits)
        stats_year["status"] = "pass" if stats_year["crc_bad_members"] == 0 and stats_year["invalid_rows"] == 0 and stats_year["duplicate_keys"] == 0 and stats_year["incomplete_days"] == 0 else "review"
        manifest["years"][str(year)] = stats_year
        total_rows += year_rows
        total_audits += year_audits
        manifest.update({"processed_years": year - args.start_year + 1, "stage_rows": total_rows, "audit_rows": total_audits})
        write_manifest(manifest_path, manifest)
        print(json.dumps({"frequency": freq, "year": year, **stats_year}, ensure_ascii=False), flush=True)
    final_counts = c.query(
        f"SELECT count(), uniqExact(tuple(code, datetime)) FROM {target} WHERE source_year BETWEEN %(start)s AND %(end)s",
        parameters={"start": args.start_year, "end": args.end_year},
    ).result_rows[0]
    final_audits = c.query(
        f"SELECT count() FROM {audit} WHERE source_year BETWEEN %(start)s AND %(end)s",
        parameters={"start": args.start_year, "end": args.end_year},
    ).result_rows[0][0]
    manifest["status"] = "loaded"
    manifest["stage_rows_in_resume_scope"] = int(final_counts[0])
    manifest["unique_keys_in_resume_scope"] = int(final_counts[1])
    manifest["audit_rows_in_resume_scope"] = int(final_audits)
    manifest["stage_rows_added_this_run"] = total_rows
    manifest["audit_rows_added_this_run"] = total_audits
    manifest["five_minute_aggregate_diffs"] = {}
    write_manifest(manifest_path, manifest)
    for year in range(args.start_year, args.end_year + 1):
        print(json.dumps({"frequency": freq, "year": year, "phase": "5m_aggregate_diff_start"}, ensure_ascii=False), flush=True)
        manifest["five_minute_aggregate_diffs"][str(year)] = five_minute_aggregate_diffs(c, target, freq, year)
        write_manifest(manifest_path, manifest)
        print(json.dumps({"frequency": freq, "year": year, "five_minute_aggregate_diffs": manifest["five_minute_aggregate_diffs"][str(year)]}, ensure_ascii=False), flush=True)
    manifest["status"] = "complete"
    manifest["finished_at"] = datetime.now(BUSINESS_TZ).isoformat()
    write_manifest(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


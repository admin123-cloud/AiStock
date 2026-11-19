from __future__ import annotations

"""Import one or more purchased 5m daily ZIPs into an isolated stage.

Only current ordinary stocks or explicitly mapped indices are accepted.  The
stage keeps the purchased source contract (stock volume in lots/amount in
yuan; index volume unavailable; prices raw/unadjusted).  Formal minute tables
are never modified by this script.
"""

import argparse
import csv
import hashlib
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zipfile import ZipFile
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_STAGE = "stock.purchased_history_minute_5_stage"
SOURCE_AUDIT = "stock.purchased_history_minute_5_source_audit_v2"
DEFAULT_INDEX_LIST = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_指数\指数列表_沪深京.csv")
DEFAULT_BATCH = "purchased_5m_stage_20000630_20260916"

STAGE_COLUMNS = [
    "source",
    "source_category",
    "source_archive",
    "source_entry",
    "batch_id",
    "source_code",
    "code",
    "security_type",
    "universe_decision",
    "period",
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "source_volume_lots",
    "source_amount_yuan",
    "price_adjustment",
    "row_hash",
    "ingested_at",
    "id",
]

AUDIT_COLUMNS = [
    "source_category",
    "source_archive",
    "source_entry",
    "batch_id",
    "source_code",
    "code",
    "security_type",
    "universe_decision",
    "reason",
    "source_row_count",
    "valid_ohlc_row_count",
    "expected_bar_count",
    "unique_datetime_count",
    "invalid_row_count",
    "min_datetime",
    "max_datetime",
    "file_size",
    "file_sha256",
    "audited_at",
]


def parse_float(value: str) -> float | None:
    value = str(value or "").strip()
    if not value:
        return None
    parsed = float(value.replace(",", ""))
    return parsed if math.isfinite(parsed) else None


def parse_datetime(value: str) -> datetime:
    text = value.strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=BUSINESS_TZ)
        except ValueError:
            continue
    raise ValueError(f"unsupported purchased 5m timestamp: {text!r}")


def source_code_from_entry(source_entry: str) -> str:
    """Return the source symbol from daily or annual purchased entry names."""
    stem = Path(source_entry).stem
    return re.sub(r"_(?:19|20)\d{2}$", "", stem)


def canonical_stock(source_code: str) -> str | None:
    match = re.fullmatch(r"(sh|sz|bj)(\d{6})", source_code.strip(), re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(2)}.{match.group(1).upper()}"


def load_index_map(path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("symbol_num") or "").strip()
            code = str(row.get("ts_code") or "").strip()
            if symbol and re.fullmatch(r"\d{6}", symbol) and re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
                mapping[symbol] = code
    return mapping


def load_master(client: Any) -> dict[str, str]:
    return {str(code): str(kind) for code, kind in client.query("SELECT code,type FROM stock.stocks").result_rows}


def classify(source_code: str, category: str, master: dict[str, str], index_map: dict[str, str]) -> tuple[str | None, str, str, str]:
    if category == "index":
        code = index_map.get(source_code.strip())
        if code:
            return code, "index", "accepted_index", "index list symbol_num mapping"
        return None, "unknown", "rejected_unclassified", "index symbol_num absent from index list"

    code = canonical_stock(source_code)
    if not code:
        return None, "unknown", "rejected_unclassified", "source code is not sh/sz/bj canonical form"
    if code in master:
        kind = master[code]
        if kind == "stock":
            return code, "stock", "accepted_stock", "stocks.type=stock"
        return code, kind, "rejected_non_stock", f"stocks.type={kind}"
    return code, "stock", "historical_stock_pending", "ordinary stock absent from current stocks"


def source_info(path: Path) -> tuple[str, str]:
    text = str(path).replace("/", "\\")
    if "A股_分时数据_指数" in text:
        return "index", "purchased_index_5m"
    if "A股_分时数据_京市" in text:
        return "stock_bj", "purchased_bj_5m"
    return "stock_hs", "purchased_hs_5m"


def process_entry(
    archive: ZipFile,
    info: Any,
    archive_path: Path,
    batch_id: str,
    category: str,
    source_category: str,
    master: dict[str, str],
    index_map: dict[str, str],
    include_pending: bool,
) -> tuple[list[Any], list[list[Any]]]:
    raw = archive.read(info)
    digest = hashlib.sha256(raw).hexdigest()
    source_entry = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
    lines = raw.decode("utf-8-sig").splitlines()
    reader = csv.reader(lines)
    header = next(reader, [])
    source_code = source_code_from_entry(source_entry)
    if category == "index":
        source_code = source_code.strip()
    code, security_type, decision, reason = classify(source_code, category, master, index_map)
    stage_rows: list[list[Any]] = []
    datetimes: list[datetime] = []
    valid_ohlc = 0
    invalid = 0
    ingested_at = datetime.now(BUSINESS_TZ)
    for ordinal, cells in enumerate(reader, start=1):
        try:
            if len(cells) not in (10, 11):
                invalid += 1
                continue
            dt = parse_datetime(cells[0])
            open_, close, high, low = (parse_float(cells[i]) for i in (3, 4, 5, 6))
            if any(value is None or value <= 0 for value in (open_, close, high, low)):
                invalid += 1
                continue
            datetimes.append(dt)
            valid_ohlc += 1
            if decision not in ("accepted_stock", "accepted_index") and not (
                include_pending and decision == "historical_stock_pending"
            ):
                continue
            volume = parse_float(cells[7]) if category != "index" else None
            amount_index = 8 if category != "index" else 7
            amount = parse_float(cells[amount_index])
            row_hash = hashlib.sha256(f"{digest}:{ordinal}".encode("ascii")).hexdigest()
            stage_rows.append(
                [
                    "purchased_a_share_history",
                    source_category,
                    str(archive_path),
                    source_entry,
                    batch_id,
                    cells[1].strip(),
                    code,
                    security_type,
                    decision,
                    "5m",
                    dt,
                    open_,
                    high,
                    low,
                    close,
                    volume,
                    amount,
                    "none",
                    row_hash,
                    ingested_at,
                    int.from_bytes(bytes.fromhex(row_hash[:16]), "big"),
                ]
            )
        except (TypeError, ValueError, IndexError):
            invalid += 1
    expected = 48
    audit = [
        category,
        str(archive_path),
        source_entry,
        batch_id,
        source_code,
        code or "",
        security_type,
        decision,
        reason,
        len(lines) - 1 if header else len(lines),
        valid_ohlc,
        expected,
        len(set(datetimes)),
        invalid,
        min(datetimes) if datetimes else None,
        max(datetimes) if datetimes else None,
        info.file_size,
        digest,
        ingested_at,
    ]
    return audit, stage_rows


def create_tables(client: Any) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {SOURCE_AUDIT}
        (
            `source_category` LowCardinality(String), `source_archive` String,
            `source_entry` String, `batch_id` String, `source_code` String,
            `code` String, `security_type` LowCardinality(String),
            `universe_decision` LowCardinality(String), `reason` String,
            `source_row_count` UInt32, `valid_ohlc_row_count` UInt32,
            `expected_bar_count` UInt16, `unique_datetime_count` UInt16,
            `invalid_row_count` UInt32, `min_datetime` Nullable(DateTime('Asia/Shanghai')),
            `max_datetime` Nullable(DateTime('Asia/Shanghai')), `file_size` UInt64,
            `file_sha256` String, `audited_at` DateTime('Asia/Shanghai')
        ) ENGINE = MergeTree
        ORDER BY (batch_id, source_category, source_archive, source_entry)
        """
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", action="append", type=Path, default=[], help="One daily ZIP; repeatable")
    parser.add_argument("--archive-dir", action="append", type=Path, default=[], help="Directory of daily ZIPs; repeatable")
    parser.add_argument("--batch-id", default=DEFAULT_BATCH)
    parser.add_argument("--index-list", type=Path, default=DEFAULT_INDEX_LIST)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--audit-only", action="store_true", help="Write file audits only; do not insert stage rows")
    parser.add_argument("--include-pending", action="store_true")
    parser.add_argument("--batch-rows", type=int, default=5000)
    parser.add_argument("--limit-entries", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    archives = list(args.archive)
    for directory in args.archive_dir:
        if not directory.is_dir():
            print(f"archive_dir_missing={directory}", file=sys.stderr)
            return 2
        archives.extend(sorted(directory.glob("*_5min.zip")))
    archives = list(dict.fromkeys(archives))
    if not archives:
        print("no_archives", file=sys.stderr)
        return 2
    for path in archives:
        if not path.exists():
            print(f"archive_missing={path}", file=sys.stderr)
            return 2
    if not args.index_list.exists():
        print(f"index_list_missing={args.index_list}", file=sys.stderr)
        return 2
    client = clickhouse_client()
    master = load_master(client)
    index_map = load_index_map(args.index_list)
    if args.write:
        create_tables(client)
        if args.audit_only:
            existing = client.query(
                f"SELECT count() FROM {SOURCE_AUDIT} WHERE batch_id=%(batch)s", parameters={"batch": args.batch_id}
            ).result_rows[0][0]
            if existing:
                print(f"existing_audit_rows={existing}; refusing duplicate batch", file=sys.stderr)
                return 3
        else:
            existing = client.query(
                f"SELECT count() FROM {MINUTE_STAGE} WHERE batch_id=%(batch)s", parameters={"batch": args.batch_id}
            ).result_rows[0][0]
            if existing:
                print(f"existing_stage_rows={existing}; refusing duplicate batch", file=sys.stderr)
                return 3

    decisions: Counter[str] = Counter()
    audit_rows: list[list[Any]] = []
    stage_rows: list[list[Any]] = []
    total_entries = 0
    total_source_rows = 0
    total_valid_ohlc = 0
    total_invalid = 0
    written_rows = 0
    for archive_path in archives:
        category, source_category = source_info(archive_path)
        with ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if not info.filename.lower().endswith(".csv"):
                    continue
                if args.limit_entries and total_entries >= args.limit_entries:
                    break
                audit, rows = process_entry(
                    archive,
                    info,
                    archive_path,
                    args.batch_id,
                    "index" if category == "index" else "stock",
                    source_category,
                    master,
                    index_map,
                    args.include_pending,
                )
                total_entries += 1
                decisions[audit[7]] += 1
                total_source_rows += audit[9]
                total_valid_ohlc += audit[10]
                total_invalid += audit[13]
                audit_rows.append(audit)
                if args.write and not args.audit_only:
                    stage_rows.extend(rows)
                    while len(stage_rows) >= args.batch_rows:
                        block, stage_rows = stage_rows[: args.batch_rows], stage_rows[args.batch_rows :]
                        client.insert(MINUTE_STAGE, block, column_names=STAGE_COLUMNS)
                        written_rows += len(block)
                if total_entries % 500 == 0:
                    print(f"progress entries={total_entries} source_rows={total_source_rows} stage_rows={written_rows + len(stage_rows)}", flush=True)
    if args.write:
        if stage_rows and not args.audit_only:
            client.insert(MINUTE_STAGE, stage_rows, column_names=STAGE_COLUMNS)
            written_rows += len(stage_rows)
        if audit_rows:
            client.insert(SOURCE_AUDIT, audit_rows, column_names=AUDIT_COLUMNS)
    print("--- purchased 5m stage audit ---")
    print(f"entries={total_entries}")
    print(f"source_rows={total_source_rows}")
    print(f"valid_ohlc_rows={total_valid_ohlc}")
    print(f"invalid_rows={total_invalid}")
    print(f"decisions={dict(decisions)}")
    print(f"write={args.write}")
    print(f"stage_rows_written={written_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

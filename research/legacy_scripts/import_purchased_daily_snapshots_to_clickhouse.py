from __future__ import annotations

"""Import the purchased daily snapshot fields into an isolated ClickHouse stage.

The source ZIP is read in-place and never extracted.  The default mode is a
read-only audit; ``--write`` is required to create/populate the stage tables.
Only securities classified as ordinary stocks are written.  Historical or
unknown entries are recorded in the universe audit and are not auto-promoted.
"""

import argparse
import csv
import hashlib
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from zipfile import ZipFile
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_SOURCE = Path(r"E:\AStockHistory\A股数据\股票日线\股票全量数据.zip")
DEFAULT_BATCH = "purchased_daily_snapshot_20260916"
DAILY_STAGE = "stock.purchased_history_daily_snapshot_stage"
UNIVERSE_AUDIT = "stock.purchased_history_daily_universe_audit"

EXPECTED_HEADER = [
    "股票代码",
    "股票名称",
    "交易日",
    "开盘价",
    "最高价",
    "最低价",
    "收盘价",
    "前收盘价",
    "涨跌额",
    "涨跌幅（%）",
    "成交量（手）",
    "成交额（千元）",
    "换手率（%）",
    "换手率（%，自由流通股）",
    "量比",
    "市盈率（亏损为空）",
    "市盈率（TTM，亏损为空）",
    "市净率",
    "市销率",
    "市销率（TTM）",
    "股息率（%）",
    "股息率（%，TTM）",
    "总股本（万股）",
    "流通股本（万股）",
    "自由流通股本（万）",
    "总市值（万元）",
    "流通市值（万元）",
    "复权因子",
    "当日涨停价",
    "当日跌停价",
]

ETF_PREFIXES = ("159", "510", "511", "512", "513", "515", "516", "517", "518", "588")
FUND_PREFIXES = (
    "150",
    "151",
    "160",
    "161",
    "162",
    "163",
    "164",
    "165",
    "166",
    "167",
    "168",
    "169",
    "500",
    "501",
    "502",
    "503",
    "505",
    "506",
    "507",
    "508",
    "509",
)
ORDINARY_PREFIXES = (
    "000",
    "001",
    "002",
    "003",
    "300",
    "301",
    "430",
    "600",
    "601",
    "603",
    "605",
    "688",
    "689",
    "831",
    "832",
    "833",
    "834",
    "835",
    "836",
    "837",
    "838",
    "839",
    "870",
    "871",
    "872",
    "873",
    "874",
    "875",
    "876",
    "877",
    "920",
)

STAGE_COLUMNS = [
    "source",
    "source_archive",
    "source_entry",
    "batch_id",
    "source_code",
    "code",
    "security_type",
    "universe_decision",
    "quality_status",
    "trade_date",
    "stock_name",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "change_amount",
    "change_pct",
    "source_volume_lots",
    "source_amount_thousand_yuan",
    "volume_lots",
    "amount_yuan",
    "turnover_rate",
    "free_float_turnover_rate",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dividend_yield",
    "dividend_yield_ttm",
    "total_share_10k",
    "float_share_10k",
    "free_float_share_10k",
    "total_market_cap_10k",
    "float_market_cap_10k",
    "adjustment_factor",
    "limit_up",
    "limit_down",
    "row_hash",
    "ingested_at",
    "id",
]

AUDIT_COLUMNS = [
    "source",
    "source_archive",
    "source_entry",
    "batch_id",
    "source_code",
    "code",
    "security_type",
    "universe_decision",
    "reason",
    "source_row_count",
    "valid_row_count",
    "blank_ohlc_row_count",
    "min_trade_date",
    "max_trade_date",
    "file_size",
    "row_hash",
    "audited_at",
]


def parse_float(value: str) -> float | None:
    value = str(value or "").strip()
    if not value:
        return None
    if "," in value:
        value = value.replace(",", "")
    return float(value)


def parse_trade_date(value: str) -> date | None:
    value = str(value or "").strip()
    if not value:
        return None
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date()
    return datetime.strptime(value, "%Y-%m-%d").date()


def canonical_from_filename(name: str) -> str | None:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    match = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)\.csv", base, re.IGNORECASE)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2).upper()}"


def classify_code(code: str, master: dict[str, str]) -> tuple[str, str, str]:
    """Return (security_type, universe_decision, reason)."""
    if code in master:
        kind = master[code]
        if kind == "stock":
            return "stock", "accepted_stock", "stocks.type=stock"
        return kind, "rejected_non_stock", f"stocks.type={kind}"

    digits = code[:6]
    if digits.startswith(ETF_PREFIXES):
        return "etf", "rejected_non_target", "ETF code prefix"
    if digits.startswith(FUND_PREFIXES):
        return "fund", "rejected_non_target", "fund code prefix"
    if digits.startswith(ORDINARY_PREFIXES):
        return "stock", "historical_stock_pending", "ordinary-stock prefix but absent from current stocks"
    return "unknown", "rejected_unclassified", "not canonical and no ordinary-stock mapping"


def create_tables(client: Any) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {DAILY_STAGE}
        (
            `source` LowCardinality(String),
            `source_archive` String,
            `source_entry` String,
            `batch_id` String,
            `source_code` String,
            `code` String,
            `security_type` LowCardinality(String),
            `universe_decision` LowCardinality(String),
            `quality_status` LowCardinality(String),
            `trade_date` Date,
            `stock_name` String,
            `open` Nullable(Float64),
            `high` Nullable(Float64),
            `low` Nullable(Float64),
            `close` Nullable(Float64),
            `prev_close` Nullable(Float64),
            `change_amount` Nullable(Float64),
            `change_pct` Nullable(Float64),
            `source_volume_lots` Nullable(Float64),
            `source_amount_thousand_yuan` Nullable(Float64),
            `volume_lots` Nullable(Float64),
            `amount_yuan` Nullable(Float64),
            `turnover_rate` Nullable(Float64),
            `free_float_turnover_rate` Nullable(Float64),
            `volume_ratio` Nullable(Float64),
            `pe` Nullable(Float64),
            `pe_ttm` Nullable(Float64),
            `pb` Nullable(Float64),
            `ps` Nullable(Float64),
            `ps_ttm` Nullable(Float64),
            `dividend_yield` Nullable(Float64),
            `dividend_yield_ttm` Nullable(Float64),
            `total_share_10k` Nullable(Float64),
            `float_share_10k` Nullable(Float64),
            `free_float_share_10k` Nullable(Float64),
            `total_market_cap_10k` Nullable(Float64),
            `float_market_cap_10k` Nullable(Float64),
            `adjustment_factor` Nullable(Float64),
            `limit_up` Nullable(Float64),
            `limit_down` Nullable(Float64),
            `row_hash` String,
            `ingested_at` DateTime('Asia/Shanghai'),
            `id` UInt64
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(trade_date)
        ORDER BY (code, trade_date, source)
        SETTINGS index_granularity = 8192
        """
    )
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {UNIVERSE_AUDIT}
        (
            `source` LowCardinality(String),
            `source_archive` String,
            `source_entry` String,
            `batch_id` String,
            `source_code` String,
            `code` String,
            `security_type` LowCardinality(String),
            `universe_decision` LowCardinality(String),
            `reason` String,
            `source_row_count` UInt32,
            `valid_row_count` UInt32,
            `blank_ohlc_row_count` UInt32,
            `min_trade_date` Nullable(Date),
            `max_trade_date` Nullable(Date),
            `file_size` UInt64,
            `row_hash` String,
            `audited_at` DateTime('Asia/Shanghai')
        )
        ENGINE = ReplacingMergeTree(audited_at)
        ORDER BY (batch_id, source_entry)
        SETTINGS index_granularity = 8192
        """
    )


def load_master(client: Any) -> dict[str, str]:
    return {str(code): str(kind) for code, kind in client.query("SELECT code, type FROM stock.stocks").result_rows}


class DirectorySource:
    """Small ZipFile-compatible reader for an already extracted source."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def __enter__(self) -> "DirectorySource":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def read(self, info: Any) -> bytes:
        return Path(info.path).read_bytes()

    def getinfo(self, name: str) -> Any:
        path = Path(name)
        return SimpleNamespace(filename=str(path), file_size=path.stat().st_size, path=path)


def iter_entries(source: Any) -> Iterable[Any]:
    if isinstance(source, ZipFile):
        for info in source.infolist():
            code = canonical_from_filename(info.filename)
            if code:
                yield info
        return
    for path in source.root.rglob("*.csv"):
        if canonical_from_filename(path.name):
            yield SimpleNamespace(filename=str(path), file_size=path.stat().st_size, path=path)


def process_entry(
    z: ZipFile,
    info: Any,
    source_path: Path,
    batch_id: str,
    master: dict[str, str],
    include_pending: bool = False,
    audit_only: bool = False,
    start_year: int | None = None,
    end_year: int | None = None,
) -> tuple[dict[str, Any], list[list[Any]]]:
    code = canonical_from_filename(info.filename)
    assert code is not None
    security_type, decision, reason = classify_code(code, master)
    raw = z.read(info)
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8-sig")
    reader = csv.reader(text.splitlines())
    header = next(reader, [])
    if header != EXPECTED_HEADER:
        reason = "daily header mismatch"
        decision = "rejected_schema"
    rows: list[list[Any]] = []
    source_row_count = 0
    valid_row_count = 0
    blank_ohlc_row_count = 0
    dates: list[date] = []
    now = datetime.now(BUSINESS_TZ).replace(tzinfo=None)
    for cells in reader:
        source_row_count += 1
        if len(cells) != len(EXPECTED_HEADER):
            continue
        try:
            trade_date = parse_trade_date(cells[2])
            if trade_date is None:
                continue
            if start_year is not None and trade_date.year < start_year:
                continue
            if end_year is not None and trade_date.year > end_year:
                continue
            blank_ohlc = all(not str(value or "").strip() for value in cells[3:7])
            if blank_ohlc:
                blank_ohlc_row_count += 1
            dates.append(trade_date)
            valid_row_count += 1
            if audit_only:
                continue
            nums = [parse_float(value) for value in cells[3:]]
            open_, high, low, close = nums[0:4]
            if decision != "accepted_stock" and not (include_pending and decision == "historical_stock_pending"):
                continue
            quality = "missing_ohlc" if blank_ohlc else "valid"
            source_volume_lots = nums[7]
            source_amount_thousand_yuan = nums[8]
            normalized = [
                "purchased_a_share_history",
                str(source_path),
                info.filename.replace("\\", "/").rsplit("/", 1)[-1],
                batch_id,
                code,
                code,
                security_type,
                decision,
                quality,
                trade_date,
                cells[1].strip(),
                open_,
                high,
                low,
                close,
                nums[4],
                nums[5],
                nums[6],
                source_volume_lots,
                source_amount_thousand_yuan,
                source_volume_lots,
                None if source_amount_thousand_yuan is None else source_amount_thousand_yuan * 1000.0,
                *nums[9:27],
            ]
            # The archive digest plus source row ordinal is a stable record
            # identity and avoids re-serializing all 43 stage fields for every
            # row.  The complete source file remains available via
            # ``source_archive``/``source_entry`` and its audit SHA-256.
            row_hash = hashlib.sha256(f"{digest}:{source_row_count}".encode("ascii")).hexdigest()
            normalized.extend([row_hash, now, int.from_bytes(bytes.fromhex(row_hash[:16]), "big")])
            rows.append(normalized)
        except (TypeError, ValueError, IndexError):
            continue
    audit = {
        "source": "purchased_a_share_history",
        "source_archive": str(source_path),
        "source_entry": info.filename.replace("\\", "/").rsplit("/", 1)[-1],
        "batch_id": batch_id,
        "source_code": code,
        "code": code,
        "security_type": security_type,
        "universe_decision": decision,
        "reason": reason,
        "source_row_count": source_row_count,
        "valid_row_count": valid_row_count,
        "blank_ohlc_row_count": blank_ohlc_row_count,
        "min_trade_date": min(dates) if dates else None,
        "max_trade_date": max(dates) if dates else None,
        "file_size": info.file_size,
        "row_hash": digest,
        "audited_at": now,
    }
    return audit, rows


_WORKER_SOURCE_READER: Any = None
_WORKER_SOURCE: Path | None = None
_WORKER_BATCH_ID = ""
_WORKER_MASTER: dict[str, str] = {}
_WORKER_INCLUDE_PENDING = False
_WORKER_START_YEAR: int | None = None
_WORKER_END_YEAR: int | None = None


def init_worker(source_path: str, batch_id: str, master: dict[str, str], include_pending: bool, start_year: int | None, end_year: int | None) -> None:
    global _WORKER_SOURCE_READER, _WORKER_SOURCE, _WORKER_BATCH_ID, _WORKER_MASTER, _WORKER_INCLUDE_PENDING, _WORKER_START_YEAR, _WORKER_END_YEAR
    _WORKER_SOURCE = Path(source_path)
    _WORKER_SOURCE_READER = DirectorySource(_WORKER_SOURCE) if _WORKER_SOURCE.is_dir() else ZipFile(_WORKER_SOURCE)
    _WORKER_BATCH_ID = batch_id
    _WORKER_MASTER = master
    _WORKER_INCLUDE_PENDING = include_pending
    _WORKER_START_YEAR = start_year
    _WORKER_END_YEAR = end_year


def process_entry_worker(entry_name: str) -> tuple[dict[str, Any], list[list[Any]]]:
    assert _WORKER_SOURCE_READER is not None
    assert _WORKER_SOURCE is not None
    info = _WORKER_SOURCE_READER.getinfo(entry_name)
    return process_entry(
        _WORKER_SOURCE_READER,
        info,
        _WORKER_SOURCE,
        _WORKER_BATCH_ID,
        _WORKER_MASTER,
        include_pending=_WORKER_INCLUDE_PENDING,
        audit_only=False,
        start_year=_WORKER_START_YEAR,
        end_year=_WORKER_END_YEAR,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--batch-id", default=DEFAULT_BATCH)
    parser.add_argument("--write", action="store_true", help="Create tables and write accepted stock rows")
    parser.add_argument("--include-pending", action="store_true", help="Also stage historical-stock-pending rows; never promotes them")
    parser.add_argument("--limit-files", type=int, default=0, help="Read only the first N code files; 0 means all")
    parser.add_argument(
        "--batch-rows",
        type=int,
        default=5000,
        help="Maximum rows per month-homogeneous INSERT block; kept small for wide rows and ClickHouse memory",
    )
    parser.add_argument("--workers", type=int, default=1, help="Parallel ZIP parsers for write mode; 1 is the stable sequential default")
    parser.add_argument("--start-year", type=int, default=None, help="Optional inclusive trade-date year filter")
    parser.add_argument("--end-year", type=int, default=None, help="Optional inclusive trade-date year filter")
    return parser.parse_args()


def insert_stage_rows(client: Any, rows: list[list[Any]], batch_rows: int) -> int:
    """Insert rows in partition-homogeneous blocks.

    The stage is partitioned by month.  ClickHouse rejects an INSERT block
    spanning more than ``max_partitions_per_insert_block`` partitions, so a
    normal row-count batch must be split by ``trade_date`` month first.
    """
    if not rows:
        return 0
    by_partition: dict[str, list[list[Any]]] = {}
    for row in rows:
        trade_date = row[9]
        partition = trade_date.strftime("%Y%m")
        by_partition.setdefault(partition, []).append(row)
    written = 0
    for partition_rows in by_partition.values():
        for start in range(0, len(partition_rows), batch_rows):
            block = partition_rows[start : start + batch_rows]
            # Other history validation jobs may temporarily consume all
            # ClickHouse query slots.  Retry only that explicit, transient
            # capacity error; schema/data/connection failures still surface.
            for attempt in range(60):
                try:
                    client.insert(DAILY_STAGE, block, column_names=STAGE_COLUMNS)
                    break
                except Exception as exc:
                    if "Too many simultaneous queries" not in str(exc) or attempt == 59:
                        raise
                    time.sleep(min(1.0 + attempt * 0.25, 10.0))
            written += len(block)
    return written


def main() -> int:
    args = parse_args()
    if not args.source.exists():
        print(f"source_missing={args.source}", file=sys.stderr)
        return 2
    client = clickhouse_client()
    master = load_master(client)
    if args.write:
        create_tables(client)
        existing = client.query(
            f"SELECT count() FROM {DAILY_STAGE} WHERE batch_id = %(batch)s",
            parameters={"batch": args.batch_id},
        ).result_rows[0][0]
        if existing:
            print(f"existing_stage_rows={existing}; refusing duplicate batch", file=sys.stderr)
            return 3

    audit_rows: list[list[Any]] = []
    stage_rows: list[list[Any]] = []
    decisions: Counter[str] = Counter()
    total_entries = 0
    total_source_rows = 0
    total_valid_rows = 0
    total_blank_ohlc = 0
    written_rows = 0

    def consume_result(audit: dict[str, Any], rows: list[list[Any]]) -> None:
        nonlocal total_entries, total_source_rows, total_valid_rows, total_blank_ohlc
        nonlocal stage_rows, written_rows
        total_entries += 1
        decisions[audit["universe_decision"]] += 1
        total_source_rows += audit["source_row_count"]
        total_valid_rows += audit["valid_row_count"]
        total_blank_ohlc += audit["blank_ohlc_row_count"]
        audit_rows.append([audit[column] for column in AUDIT_COLUMNS])
        if args.write:
            stage_rows.extend(rows)
            while len(stage_rows) >= args.batch_rows:
                block, stage_rows = stage_rows[: args.batch_rows], stage_rows[args.batch_rows :]
                written_rows += insert_stage_rows(client, block, args.batch_rows)
        if total_entries % 500 == 0:
            print(
                f"progress entries={total_entries} source_rows={total_source_rows} stage_rows={written_rows + len(stage_rows)}",
                flush=True,
            )

    source_reader = DirectorySource(args.source) if args.source.is_dir() else ZipFile(args.source)
    with source_reader as z:
        if args.write and args.workers > 1:
            entry_names = [info.filename for info in iter_entries(z)]
            if args.limit_files:
                entry_names = entry_names[: args.limit_files]
            with ProcessPoolExecutor(
                max_workers=args.workers,
                initializer=init_worker,
                initargs=(str(args.source), args.batch_id, master, args.include_pending, args.start_year, args.end_year),
            ) as pool:
                for audit, rows in pool.map(process_entry_worker, entry_names, chunksize=1):
                    consume_result(audit, rows)
        else:
            for info in iter_entries(z):
                if args.limit_files and total_entries >= args.limit_files:
                    break
                audit, rows = process_entry(
                    z,
                    info,
                    args.source,
                    args.batch_id,
                    master,
                    include_pending=args.include_pending,
                    audit_only=not args.write,
                    start_year=args.start_year,
                    end_year=args.end_year,
                )
                consume_result(audit, rows)
    if args.write:
        if stage_rows:
            written_rows += insert_stage_rows(client, stage_rows, args.batch_rows)
        if audit_rows:
            client.insert(UNIVERSE_AUDIT, audit_rows, column_names=AUDIT_COLUMNS)

    print("--- purchased daily snapshot audit ---")
    print(f"source={args.source}")
    print(f"entries={total_entries}")
    print(f"source_rows={total_source_rows}")
    print(f"valid_rows={total_valid_rows}")
    print(f"blank_ohlc_rows={total_blank_ohlc}")
    print(f"decisions={dict(decisions)}")
    print(f"write={args.write}")
    print(f"stage_rows_written={written_rows}")
    if args.write:
        print("stage_summary=")
        for row in client.query(
            f"""
            SELECT universe_decision, count(), uniqExact((code, trade_date)), min(trade_date), max(trade_date)
            FROM {DAILY_STAGE} WHERE batch_id = '{args.batch_id}'
            GROUP BY universe_decision ORDER BY universe_decision
            """
        ).result_rows:
            print(row)
        print("audit_summary=")
        for row in client.query(
            f"SELECT universe_decision, count() FROM {UNIVERSE_AUDIT} WHERE batch_id = '{args.batch_id}' GROUP BY universe_decision ORDER BY universe_decision"
        ).result_rows:
            print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build 5/15/30/60 minute bars from local TDX lc5 files.

The script is intentionally file-driven instead of API-driven:
  - local TDX vipdoc/*/fzline/*.lc5 is the source of truth for raw 5m bars
  - derived periods are built in memory per symbol, so memory stays bounded
  - qfq mode writes separate *_qfq tables by default

QFQ adjustment is calculated per symbol/trade day as:
    factor = qfq_daily_close / raw_5m_day_close

If qfq daily close is missing, the script falls back to factor=1 for that
day and reports the missing count.  This makes incomplete factor coverage
visible instead of silently producing misleading adjusted data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_client  # noqa: E402
from utils.kline_store import filter_trading_day_tuples  # noqa: E402


LC5_RECORD = struct.Struct("<HHfffffII")
MARKET_SUFFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
BASE_TABLES = {
    "5m": "kline_minute_5",
    "15m": "kline_minute_15",
    "30m": "kline_minute_30",
    "60m": "kline_minute_60",
}
COLUMNS = ["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]
PERIOD_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}
A_SHARE_5M_ENDPOINTS = {
    "09:35",
    "09:40",
    "09:45",
    "09:50",
    "09:55",
    "10:00",
    "10:05",
    "10:10",
    "10:15",
    "10:20",
    "10:25",
    "10:30",
    "10:35",
    "10:40",
    "10:45",
    "10:50",
    "10:55",
    "11:00",
    "11:05",
    "11:10",
    "11:15",
    "11:20",
    "11:25",
    "11:30",
    "13:05",
    "13:10",
    "13:15",
    "13:20",
    "13:25",
    "13:30",
    "13:35",
    "13:40",
    "13:45",
    "13:50",
    "13:55",
    "14:00",
    "14:05",
    "14:10",
    "14:15",
    "14:20",
    "14:25",
    "14:30",
    "14:35",
    "14:40",
    "14:45",
    "14:50",
    "14:55",
    "15:00",
}
CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Bar:
    code: str
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float

    @property
    def trade_date(self) -> date:
        return self.dt.date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build ClickHouse minute bars from local TDX lc5 files.")
    parser.add_argument("--tdx-root", default=r"D:\TDX\vipdoc")
    parser.add_argument("--markets", default="sh,sz,bj")
    parser.add_argument("--codes", default="", help="Optional full codes: 000001.SZ,600000.SH")
    parser.add_argument("--codes-file", default="", help="Optional UTF-8 file with one full code per line.")
    parser.add_argument("--canonical-only", action="store_true", help="Only import codes present in the QMT canonical stocks table.")
    parser.add_argument("--periods", default="5m,15m,30m,60m")
    parser.add_argument("--start-date", required=True, help="Inclusive local trade date YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="Inclusive local trade date YYYY-MM-DD")
    parser.add_argument("--adjust", choices=["none", "qfq"], default="none")
    parser.add_argument("--daily-table", default="kline_daily", help="Daily qfq close source used by --adjust qfq.")
    parser.add_argument(
        "--target-suffix",
        default=None,
        help="Default: '' for none, '_qfq' for qfq. Use '' to overwrite standard tables.",
    )
    parser.add_argument("--batch-rows", type=int, default=200_000)
    parser.add_argument("--limit-files", type=int, default=0)
    parser.add_argument("--delete-range", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--report-path", default="")
    return parser.parse_args()


def _parse_day(value: str, end: bool = False) -> datetime:
    base = datetime.strptime(value, "%Y-%m-%d")
    if end:
        return base + timedelta(days=1) - timedelta(microseconds=1)
    return base


def _full_code_from_path(path: Path) -> Optional[str]:
    market = path.name[:2].lower()
    suffix = MARKET_SUFFIX.get(market)
    raw = path.stem[2:]
    if suffix and raw.isdigit() and len(raw) == 6:
        return f"{raw}.{suffix}"
    return None


def _tdx_volume_to_lots(code: str, volume: int) -> float:
    """TDX lc5 stores stock turnover in shares; AiStock minute rows use lots."""
    if code.endswith(".SH") and code.startswith("000"):
        return float(volume)
    if code.endswith(".SZ") and code.startswith("399"):
        return float(volume)
    return float(volume) / 100.0


def _decode_tdx_datetime(raw_date: int, raw_time: int) -> Optional[datetime]:
    year = raw_date // 2048 + 2004
    remainder = raw_date % 2048
    month = remainder // 100
    day = remainder % 100
    hour = raw_time // 60
    minute = raw_time % 60
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def _stable_id(code: str, dt: datetime, period: str, adjust: str) -> int:
    key = f"{code}|{dt:%Y-%m-%d %H:%M:%S}|{period}|{adjust}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False)


def _find_files(root: Path, markets: set[str], codes: set[str], limit_files: int) -> list[Path]:
    out: list[Path] = []
    for market in sorted(markets):
        folder = root / market / "fzline"
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.lc5")):
            code = _full_code_from_path(path)
            if codes and code not in codes:
                continue
            out.append(path)
            if limit_files and len(out) >= limit_files:
                return out
    return out


def _load_codes_file(path_value: str) -> set[str]:
    if not path_value:
        return set()
    path = Path(path_value)
    if not path.exists():
        raise SystemExit(f"Codes file not found: {path}")
    codes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.strip().upper()
        if code and not code.startswith("#"):
            codes.add(code)
    return codes


def _load_canonical_codes(client) -> set[str]:
    rows = client.query(
        """
        SELECT code
        FROM stocks
        WHERE type IN ('stock', 'index')
        """
    ).result_rows
    return {str(row[0]).strip().upper() for row in rows if row and row[0]}


def _iter_lc5(path: Path, start_dt: datetime, end_dt: datetime) -> Iterator[Bar]:
    code = _full_code_from_path(path)
    if not code:
        return
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(LC5_RECORD.size)
            if not chunk or len(chunk) < LC5_RECORD.size:
                break
            raw_date, raw_time, open_, high, low, close, amount_raw, volume_raw, _reserved = LC5_RECORD.unpack(chunk)
            local_dt = _decode_tdx_datetime(raw_date, raw_time)
            if local_dt is None or local_dt < start_dt or local_dt > end_dt:
                continue
            if local_dt.hour == 13 and local_dt.minute == 0:
                local_dt = local_dt.replace(hour=11, minute=30)
            if open_ <= 0 or high <= 0 or low <= 0 or close <= 0:
                continue
            yield Bar(
                code=code,
                dt=local_dt,
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=_tdx_volume_to_lots(code, int(volume_raw or 0)),
                amount=float(amount_raw or 0.0),
            )


def _target_table(period: str, adjust: str, target_suffix: Optional[str]) -> str:
    suffix = target_suffix
    if suffix is None:
        suffix = "_qfq" if adjust == "qfq" else ""
    return BASE_TABLES[period] + suffix


def _create_table(client, table: str) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {table}
        (
            code Nullable(String),
            datetime Nullable(DateTime),
            open Nullable(Float64),
            high Nullable(Float64),
            low Nullable(Float64),
            close Nullable(Float64),
            volume Nullable(Float64),
            amount Nullable(Float64),
            created_at Nullable(DateTime),
            id UInt64
        )
        ENGINE = ReplacingMergeTree
        PARTITION BY toYYYYMM(datetime)
        ORDER BY (code, datetime)
        SETTINGS allow_nullable_key = 1
        """
    )


def _delete_range(client, table: str, start_dt: datetime, end_dt: datetime, codes: set[str], dry_run: bool) -> None:
    partition_key = client.query(
        "SELECT partition_key FROM system.tables WHERE database = currentDatabase() AND name = %(table)s",
        parameters={"table": table},
    ).result_rows
    if not partition_key or "toYYYYMM(datetime)" not in str(partition_key[0][0] or ""):
        raise RuntimeError(
            f"refusing DELETE on unpartitioned minute table {table}; use append-only repair or rebuild into a monthly-partitioned table"
        )
    code_filter = ""
    if codes:
        quoted = ", ".join("'" + code.replace("'", "''") + "'" for code in sorted(codes))
        code_filter = f" AND code IN ({quoted})"
    sql = (
        f"ALTER TABLE {table} DELETE WHERE datetime >= toDateTime('{start_dt:%Y-%m-%d %H:%M:%S}') "
        f"AND datetime <= toDateTime('{end_dt:%Y-%m-%d %H:%M:%S}'){code_filter} SETTINGS mutations_sync = 1"
    )
    print(f"delete_range table={table} start={start_dt} end={end_dt} codes={len(codes)} dry_run={dry_run}", flush=True)
    if not dry_run:
        client.command(sql)


def _daily_qfq_closes(client, daily_table: str, code: str, days: set[date]) -> dict[date, float]:
    if not days:
        return {}
    start_day = min(days)
    end_day = max(days)
    rows = client.query(
        f"""
        SELECT trade_date, close
        FROM {daily_table}
        WHERE code = %(code)s
          AND trade_date >= %(start)s
          AND trade_date <= %(end)s
        """,
        parameters={"code": code, "start": start_day, "end": end_day},
    ).result_rows
    return {row[0]: float(row[1]) for row in rows if row[1] and float(row[1]) > 0}


def _apply_qfq(client, daily_table: str, code: str, rows: list[Bar]) -> tuple[list[Bar], dict[str, int]]:
    by_day: dict[date, list[Bar]] = defaultdict(list)
    for row in rows:
        by_day[row.trade_date].append(row)
    raw_close_by_day: dict[date, float] = {}
    for day, items in by_day.items():
        items.sort(key=lambda item: item.dt)
        raw_close_by_day[day] = items[-1].close

    qfq_closes = _daily_qfq_closes(client, daily_table, code, set(by_day))
    factors: dict[date, float] = {}
    missing_days = 0
    for day, raw_close in raw_close_by_day.items():
        qfq_close = qfq_closes.get(day)
        if qfq_close and raw_close > 0:
            factors[day] = qfq_close / raw_close
        else:
            factors[day] = 1.0
            missing_days += 1

    adjusted: list[Bar] = []
    for row in rows:
        factor = factors[row.trade_date]
        adjusted.append(
            Bar(
                code=row.code,
                dt=row.dt,
                open=round(row.open * factor, 4),
                high=round(row.high * factor, 4),
                low=round(row.low * factor, 4),
                close=round(row.close * factor, 4),
                volume=row.volume,
                amount=row.amount,
            )
        )
    return adjusted, {"factor_days": len(factors), "missing_factor_days": missing_days}


def _bucket_end(dt: datetime, period_minutes: int) -> datetime:
    if dt.hour < 12:
        session_start = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    else:
        session_start = dt.replace(hour=13, minute=0, second=0, microsecond=0)
    offset = int((dt - session_start).total_seconds() // 60)
    if offset <= 0:
        steps = 1
    else:
        steps = (offset + period_minutes - 1) // period_minutes
    return session_start + timedelta(minutes=steps * period_minutes)


def _is_regular_a_share_5m_bar(dt: datetime) -> bool:
    return dt.strftime("%H:%M") in A_SHARE_5M_ENDPOINTS


def _aggregate(rows: Iterable[Bar], period: str) -> list[Bar]:
    source_rows = [row for row in rows if _is_regular_a_share_5m_bar(row.dt)]
    if period == "5m":
        return source_rows
    minutes = PERIOD_MINUTES[period]
    groups: dict[tuple[str, datetime], list[Bar]] = defaultdict(list)
    for row in source_rows:
        groups[(row.code, _bucket_end(row.dt, minutes))].append(row)
    out: list[Bar] = []
    for (code, bucket_dt), items in groups.items():
        items.sort(key=lambda item: item.dt)
        out.append(
            Bar(
                code=code,
                dt=bucket_dt,
                open=items[0].open,
                high=max(item.high for item in items),
                low=min(item.low for item in items),
                close=items[-1].close,
                volume=sum(item.volume for item in items),
                amount=round(sum(item.amount for item in items), 4),
            )
        )
    out.sort(key=lambda item: (item.code, item.dt))
    return out


def _to_insert_rows(rows: Iterable[Bar], period: str, adjust: str, created_at: datetime) -> list[tuple]:
    out: list[tuple] = []
    for row in rows:
        out.append(
            (
                row.code,
                row.dt.replace(tzinfo=CN_TZ),
                row.open,
                row.high,
                row.low,
                row.close,
                row.volume,
                row.amount,
                created_at,
                _stable_id(row.code, row.dt, period, adjust),
            )
        )
    return out


def _flush(client, table: str, rows: list[tuple], dry_run: bool) -> int:
    if not rows:
        return 0
    period = next((key for key, value in BASE_TABLES.items() if value == table), "")
    if period:
        before_rows = len(rows)
        rows = filter_trading_day_tuples(period, rows, COLUMNS)
        if not rows:
            print(f"{table} write blocked by trade_calendar guard: dropped={before_rows}", flush=True)
            return 0
    if not dry_run:
        client.insert(table, rows, column_names=COLUMNS)
    return len(rows)


def main() -> int:
    args = parse_args()
    start_ts = _parse_day(args.start_date)
    end_ts = _parse_day(args.end_date, end=True)
    root = Path(args.tdx_root)
    markets = {item.strip().lower() for item in args.markets.split(",") if item.strip()}
    codes = {item.strip().upper() for item in args.codes.split(",") if item.strip()}
    codes.update(_load_codes_file(args.codes_file))
    periods = [item.strip().lower() for item in args.periods.split(",") if item.strip()]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.daily_table):
        raise SystemExit(f"Invalid daily table name: {args.daily_table}")
    unknown = [period for period in periods if period not in BASE_TABLES]
    if unknown:
        raise SystemExit(f"Unsupported periods: {unknown}")
    if not root.exists():
        raise SystemExit(f"TDX root not found: {root}")

    client = clickhouse_client()
    if args.canonical_only:
        canonical_codes = _load_canonical_codes(client)
        codes = (codes & canonical_codes) if codes else canonical_codes
        print(f"canonical_only codes={len(codes)}", flush=True)
    tables = {period: _target_table(period, args.adjust, args.target_suffix) for period in periods}
    for table in tables.values():
        if not args.dry_run:
            _create_table(client, table)
    if args.delete_range:
        for table in tables.values():
            _delete_range(client, table, start_ts, end_ts, codes, args.dry_run)

    files = _find_files(root, markets, codes, args.limit_files)
    print(
        f"start files={len(files)} periods={periods} adjust={args.adjust} tables={tables} "
        f"start_date={args.start_date} end_date={args.end_date} dry_run={args.dry_run}",
        flush=True,
    )

    pending: dict[str, list[tuple]] = {period: [] for period in periods}
    written = {period: 0 for period in periods}
    stats = {
        "files": len(files),
        "files_with_rows": 0,
        "source_5m_rows": 0,
        "qfq_factor_days": 0,
        "qfq_missing_factor_days": 0,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    started = time.time()
    created_at = datetime.now()

    for idx, path in enumerate(files, start=1):
        raw_rows = list(_iter_lc5(path, start_ts, end_ts))
        if not raw_rows:
            if args.progress_every and idx % args.progress_every == 0:
                print(f"progress files={idx}/{len(files)} written={written}", flush=True)
            continue
        stats["files_with_rows"] += 1
        stats["source_5m_rows"] += len(raw_rows)

        source_rows = raw_rows
        if args.adjust == "qfq":
            source_rows, factor_stats = _apply_qfq(client, args.daily_table, raw_rows[0].code, raw_rows)
            stats["qfq_factor_days"] += factor_stats["factor_days"]
            stats["qfq_missing_factor_days"] += factor_stats["missing_factor_days"]

        for period in periods:
            bars = _aggregate(source_rows, period)
            pending[period].extend(_to_insert_rows(bars, period, args.adjust, created_at))
            if len(pending[period]) >= args.batch_rows:
                written[period] += _flush(client, tables[period], pending[period], args.dry_run)
                pending[period].clear()

        if args.progress_every and idx % args.progress_every == 0:
            elapsed = time.time() - started
            print(
                f"progress files={idx}/{len(files)} source_5m={stats['source_5m_rows']} "
                f"written={written} missing_factor_days={stats['qfq_missing_factor_days']} "
                f"elapsed={elapsed:.1f}s",
                flush=True,
            )

    for period in periods:
        written[period] += _flush(client, tables[period], pending[period], args.dry_run)
        pending[period].clear()

    stats["written"] = written
    stats["tables"] = tables
    stats["finished_at"] = datetime.now().isoformat(timespec="seconds")
    stats["elapsed_seconds"] = round(time.time() - started, 3)
    print("done " + json.dumps(stats, ensure_ascii=False, default=str), flush=True)
    if args.report_path:
        report_path = Path(args.report_path)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

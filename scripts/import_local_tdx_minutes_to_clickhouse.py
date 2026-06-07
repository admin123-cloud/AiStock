"""Import local TDX vipdoc minute bars into ClickHouse.

This script reads TongDaXin binary minute files:
  - vipdoc/*/fzline/*.lc5 -> 5 minute bars

It can also aggregate 15 minute bars from the decoded 5 minute bars.  Minute
bar timestamps are stored as China market wall-clock time.
"""

from __future__ import annotations

import argparse
import gc
import struct
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_client  # noqa: E402


LC_RECORD = struct.Struct("<HHfffffII")
MARKET_SUFFIX = {
    "sh": "SH",
    "sz": "SZ",
    "bj": "BJ",
}
TABLE_BY_PERIOD = {
    "5m": "kline_minute_5",
    "15m": "kline_minute_15",
}
COLUMNS = ["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import local TDX lc5 minutes to ClickHouse")
    parser.add_argument("--tdx-root", default=r"D:\TDX\vipdoc", help="TDX vipdoc directory")
    parser.add_argument("--periods", default="5m,15m", help="Comma separated periods: 5m,15m")
    parser.add_argument("--markets", default="sh,sz,bj", help="Comma separated market folders")
    parser.add_argument("--codes", default="", help="Optional comma separated full codes, e.g. 000001.SZ,600000.SH")
    parser.add_argument("--start-date", default="", help="Optional inclusive local trade date YYYY-MM-DD")
    parser.add_argument("--end-date", default="", help="Optional inclusive local trade date YYYY-MM-DD")
    parser.add_argument("--batch-rows", type=int, default=200_000)
    parser.add_argument("--limit-files", type=int, default=0)
    parser.add_argument("--delete-range", action="store_true", help="Delete target rows in date range before insert")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--progress-every", type=int, default=100)
    return parser.parse_args()


def _full_code_from_path(path: Path) -> Optional[str]:
    market = path.name[:2].lower()
    suffix = MARKET_SUFFIX.get(market)
    if not suffix:
        return None
    raw = path.stem[2:]
    if not raw.isdigit() or len(raw) != 6:
        return None
    return f"{raw}.{suffix}"


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


def _parse_date(value: str, end: bool = False) -> Optional[datetime]:
    if not value:
        return None
    base = datetime.strptime(value, "%Y-%m-%d")
    if end:
        return base + timedelta(days=1) - timedelta(microseconds=1)
    return base


def _iter_lc5_rows(path: Path, start_local: Optional[datetime], end_local: Optional[datetime]) -> Iterator[tuple]:
    code = _full_code_from_path(path)
    if not code:
        return
    now = datetime.now()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(LC_RECORD.size)
            if not chunk or len(chunk) < LC_RECORD.size:
                break
            raw_date, raw_time, open_, high, low, close, amount_raw, volume_raw, _reserved = LC_RECORD.unpack(chunk)
            local_dt = _decode_tdx_datetime(raw_date, raw_time)
            if local_dt is None:
                continue
            if start_local and local_dt < start_local:
                continue
            if end_local and local_dt > end_local:
                continue
            if open_ <= 0 or high <= 0 or low <= 0 or close <= 0:
                continue
            # Keep amount unit consistent with existing TdxQuant rows in ClickHouse.
            amount = round(float(amount_raw) / 10000.0, 4)
            volume = int(volume_raw or 0)
            row_id = abs(hash((code, int(local_dt.timestamp()), "5m"))) % (2**63)
            yield (code, local_dt, float(open_), float(high), float(low), float(close), volume, amount, now, row_id)


def _aggregate_15m(rows: Iterable[tuple]) -> list[tuple]:
    groups: dict[tuple[str, datetime], list[tuple]] = {}
    for row in rows:
        code, dt = row[0], row[1]
        local_dt = dt
        minute = local_dt.hour * 60 + local_dt.minute
        # A-share 15m bar endpoints: 09:45, 10:00, ..., 11:30, 13:15, ..., 15:00.
        if local_dt.hour < 12:
            session_start = local_dt.replace(hour=9, minute=30, second=0, microsecond=0)
        else:
            session_start = local_dt.replace(hour=13, minute=0, second=0, microsecond=0)
        offset = int((local_dt - session_start).total_seconds() // 60)
        if offset <= 0:
            bucket_end = session_start + timedelta(minutes=15)
        else:
            bucket_end = session_start + timedelta(minutes=((offset + 14) // 15) * 15)
        groups.setdefault((code, bucket_end), []).append(row)

    out: list[tuple] = []
    now = datetime.now()
    for (code, bucket_dt), items in groups.items():
        items.sort(key=lambda r: r[1])
        if not items:
            continue
        open_ = float(items[0][2])
        high = max(float(r[3]) for r in items)
        low = min(float(r[4]) for r in items)
        close = float(items[-1][5])
        volume = int(sum(int(r[6] or 0) for r in items))
        amount = round(sum(float(r[7] or 0.0) for r in items), 4)
        row_id = abs(hash((code, int(bucket_dt.timestamp()), "15m"))) % (2**63)
        out.append((code, bucket_dt, open_, high, low, close, volume, amount, now, row_id))
    out.sort(key=lambda r: (r[0], r[1]))
    return out


def _find_files(root: Path, markets: set[str], codes: set[str], limit_files: int) -> list[Path]:
    files: list[Path] = []
    for market in sorted(markets):
        folder = root / market / "fzline"
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.lc5")):
            code = _full_code_from_path(path)
            if codes and code not in codes:
                continue
            files.append(path)
            if limit_files and len(files) >= limit_files:
                return files
    return files


def _flush(client, table: str, rows: list[tuple], dry_run: bool) -> int:
    if not rows:
        return 0
    if dry_run:
        return len(rows)
    client.insert(table, rows, column_names=COLUMNS)
    return len(rows)


def _delete_range(
    client,
    table: str,
    start_local: Optional[datetime],
    end_local: Optional[datetime],
    dry_run: bool,
    codes: set[str],
) -> None:
    if not start_local or not end_local:
        raise SystemExit("--delete-range requires --start-date and --end-date")
    start_dt = start_local
    end_dt = end_local
    code_filter = ""
    if codes:
        quoted = ", ".join(f"'{code}'" for code in sorted(codes))
        code_filter = f" AND code IN ({quoted})"
    sql = (
        f"ALTER TABLE {table} DELETE WHERE datetime >= toDateTime('{start_dt:%Y-%m-%d %H:%M:%S}') "
        f"AND datetime <= toDateTime('{end_dt:%Y-%m-%d %H:%M:%S}')" + code_filter + " SETTINGS mutations_sync = 1"
    )
    print(f"delete_range table={table} start={start_dt} end={end_dt} codes={len(codes)} dry_run={dry_run}", flush=True)
    if not dry_run:
        client.command(sql)


def main() -> int:
    args = parse_args()
    root = Path(args.tdx_root)
    periods = {p.strip().lower() for p in args.periods.split(",") if p.strip()}
    markets = {m.strip().lower() for m in args.markets.split(",") if m.strip()}
    codes = {c.strip().upper() for c in args.codes.split(",") if c.strip()}
    start_local = _parse_date(args.start_date)
    end_local = _parse_date(args.end_date, end=True)

    unknown = periods - set(TABLE_BY_PERIOD)
    if unknown:
        raise SystemExit(f"Unsupported periods: {sorted(unknown)}")
    if not root.exists():
        raise SystemExit(f"TDX root not found: {root}")

    files = _find_files(root, markets, codes, args.limit_files)
    print(
        f"local_tdx_import start files={len(files)} periods={sorted(periods)} "
        f"start={args.start_date or '-'} end={args.end_date or '-'} dry_run={args.dry_run}",
        flush=True,
    )
    if not files:
        return 0

    client = clickhouse_client()
    if args.delete_range:
        for period in sorted(periods):
            _delete_range(client, TABLE_BY_PERIOD[period], start_local, end_local, args.dry_run, codes)

    buffers = {period: [] for period in periods}
    written = {period: 0 for period in periods}
    parsed_files = 0
    parsed_5m_rows = 0
    started = time.perf_counter()

    for path in files:
        five_rows = list(_iter_lc5_rows(path, start_local, end_local))
        parsed_files += 1
        parsed_5m_rows += len(five_rows)

        if "5m" in periods:
            buffers["5m"].extend(five_rows)
        if "15m" in periods:
            buffers["15m"].extend(_aggregate_15m(five_rows))

        for period, rows in buffers.items():
            if len(rows) >= args.batch_rows:
                written[period] += _flush(client, TABLE_BY_PERIOD[period], rows, args.dry_run)
                rows.clear()
                gc.collect()

        if parsed_files % max(1, args.progress_every) == 0:
            elapsed = time.perf_counter() - started
            print(
                f"progress files={parsed_files}/{len(files)} parsed_5m={parsed_5m_rows} "
                f"written={written} elapsed={elapsed:.1f}s",
                flush=True,
            )

    for period, rows in buffers.items():
        written[period] += _flush(client, TABLE_BY_PERIOD[period], rows, args.dry_run)
        rows.clear()

    elapsed = time.perf_counter() - started
    print(
        f"local_tdx_import done files={parsed_files} parsed_5m={parsed_5m_rows} "
        f"written={written} elapsed={elapsed:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

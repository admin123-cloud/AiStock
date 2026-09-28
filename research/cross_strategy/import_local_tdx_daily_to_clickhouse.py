"""Import missing daily bars from local TDX vipdoc lday files.

This is a controlled fallback path:
  - QMT canonical codes remain the universe when --canonical-only is used
  - existing kline_daily rows are not overwritten
  - local TDX *.day files only fill missing code + trade_date keys
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_client  # noqa: E402
from utils.kline_store import filter_trading_day_tuples  # noqa: E402
from utils.paths import report_path  # noqa: E402


DAY_RECORD = struct.Struct("<IIIIIfII")
MARKET_SUFFIX = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
TABLE = "kline_daily"
COLUMNS = [
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "change_pct",
    "change_amount",
    "turnover_rate",
    "created_at",
    "id",
]


@dataclass(frozen=True)
class DailyBar:
    code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    prev_close: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import missing local TDX daily bars into ClickHouse kline_daily.")
    parser.add_argument("--tdx-root", default=r"D:\TDX\vipdoc")
    parser.add_argument("--markets", default="sh,sz,bj")
    parser.add_argument("--codes", default="", help="Optional full codes: 000001.SZ,600000.SH")
    parser.add_argument("--codes-file", default="", help="Optional UTF-8 file with one full code per line.")
    parser.add_argument("--canonical-only", action="store_true", help="Only import QMT canonical stocks/index codes.")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--batch-rows", type=int, default=200_000)
    parser.add_argument("--limit-files", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--progress-every", type=int, default=500)
    parser.add_argument("--report-path", default="")
    return parser.parse_args()


def _parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _safe_sql_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid SQL name: {value}")
    return value


def _stable_id(code: str, trade_day: date) -> int:
    key = f"{code}|{trade_day:%Y-%m-%d}|local_tdx_day".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False)


def _full_code_from_path(path: Path) -> Optional[str]:
    market = path.name[:2].lower()
    suffix = MARKET_SUFFIX.get(market)
    raw = path.stem[2:]
    if suffix and raw.isdigit() and len(raw) == 6:
        return f"{raw}.{suffix}"
    return None


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


def _find_files(root: Path, markets: set[str], codes: set[str], limit_files: int) -> list[Path]:
    files: list[Path] = []
    for market in sorted(markets):
        folder = root / market / "lday"
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.day")):
            code = _full_code_from_path(path)
            if codes and code not in codes:
                continue
            files.append(path)
            if limit_files and len(files) >= limit_files:
                return files
    return files


def _decode_ymd(raw: int) -> Optional[date]:
    text = str(int(raw))
    if len(text) != 8:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _iter_day_bars(path: Path, start_day: date, end_day: date) -> list[DailyBar]:
    code = _full_code_from_path(path)
    if not code:
        return []
    parsed: list[tuple[date, float, float, float, float, float, float]] = []
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(DAY_RECORD.size)
            if not chunk or len(chunk) < DAY_RECORD.size:
                break
            raw_day, open_i, high_i, low_i, close_i, amount_f, volume_i, _reserved = DAY_RECORD.unpack(chunk)
            trade_day = _decode_ymd(raw_day)
            if trade_day is None:
                continue
            open_ = float(open_i) / 100.0
            high = float(high_i) / 100.0
            low = float(low_i) / 100.0
            close = float(close_i) / 100.0
            if open_ <= 0 or high <= 0 or low <= 0 or close <= 0:
                continue
            volume = round(float(volume_i or 0) / 100.0, 0)
            amount = float(amount_f or 0.0)
            parsed.append((trade_day, open_, high, low, close, volume, amount))
    parsed.sort(key=lambda row: row[0])
    out: list[DailyBar] = []
    prev_close = 0.0
    for trade_day, open_, high, low, close, volume, amount in parsed:
        if start_day <= trade_day <= end_day:
            out.append(DailyBar(code, trade_day, open_, high, low, close, volume, amount, prev_close))
        prev_close = close
    return out


def _load_existing_keys(client, start_day: date, end_day: date, codes: set[str]) -> set[tuple[str, date]]:
    if not codes:
        return set()
    existing: set[tuple[str, date]] = set()
    code_list = sorted(codes)
    chunk_size = 800
    for offset in range(0, len(code_list), chunk_size):
        chunk = code_list[offset : offset + chunk_size]
        quoted = ",".join("'" + code.replace("'", "\\'") + "'" for code in chunk)
        rows = client.query(
            f"""
            SELECT code, trade_date
            FROM {_safe_sql_name(TABLE)}
            WHERE code IN ({quoted})
              AND trade_date >= %(start)s
              AND trade_date <= %(end)s
            """,
            parameters={"start": start_day, "end": end_day},
        ).result_rows
        existing.update((str(row[0]), row[1]) for row in rows)
    return existing


def _to_insert_row(bar: DailyBar, created_at: datetime) -> tuple:
    change_amount = bar.close - bar.prev_close if bar.prev_close > 0 else 0.0
    change_pct = (change_amount / bar.prev_close * 100.0) if bar.prev_close > 0 else 0.0
    amplitude = ((bar.high - bar.low) / bar.prev_close * 100.0) if bar.prev_close > 0 else 0.0
    return (
        bar.code,
        bar.trade_date,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.amount,
        amplitude,
        change_pct,
        change_amount,
        0.0,
        created_at,
        _stable_id(bar.code, bar.trade_date),
    )


def _flush(client, rows: list[tuple], dry_run: bool) -> int:
    if not rows:
        return 0
    before_rows = len(rows)
    rows = filter_trading_day_tuples("1d", rows, COLUMNS)
    if not rows:
        print(f"{TABLE} write blocked by trade_calendar guard: dropped={before_rows}", flush=True)
        return 0
    if not dry_run:
        client.insert(TABLE, rows, column_names=COLUMNS)
    return len(rows)


def main() -> int:
    args = parse_args()
    start_day = _parse_day(args.start_date)
    end_day = _parse_day(args.end_date)
    root = Path(args.tdx_root)
    markets = {item.strip().lower() for item in args.markets.split(",") if item.strip()}
    codes = {item.strip().upper() for item in args.codes.split(",") if item.strip()}
    codes.update(_load_codes_file(args.codes_file))
    if not root.exists():
        raise SystemExit(f"TDX root not found: {root}")

    client = clickhouse_client()
    if args.canonical_only:
        canonical_codes = _load_canonical_codes(client)
        codes = (codes & canonical_codes) if codes else canonical_codes
        print(f"canonical_only codes={len(codes)}", flush=True)

    files = _find_files(root, markets, codes, args.limit_files)
    existing_keys = _load_existing_keys(client, start_day, end_day, codes or {_full_code_from_path(p) for p in files if _full_code_from_path(p)})
    print(
        f"local_tdx_daily start files={len(files)} existing_keys={len(existing_keys)} "
        f"start_date={args.start_date} end_date={args.end_date} dry_run={args.dry_run}",
        flush=True,
    )

    created_at = datetime.now()
    pending: list[tuple] = []
    stats = {
        "files": len(files),
        "files_with_rows": 0,
        "source_rows": 0,
        "missing_rows_written": 0,
        "skipped_existing": 0,
        "started_at": created_at.isoformat(timespec="seconds"),
    }
    started = time.time()

    for idx, path in enumerate(files, start=1):
        bars = _iter_day_bars(path, start_day, end_day)
        if bars:
            stats["files_with_rows"] += 1
            stats["source_rows"] += len(bars)
        for bar in bars:
            key = (bar.code, bar.trade_date)
            if key in existing_keys:
                stats["skipped_existing"] += 1
                continue
            pending.append(_to_insert_row(bar, created_at))
            existing_keys.add(key)
            if len(pending) >= args.batch_rows:
                stats["missing_rows_written"] += _flush(client, pending, args.dry_run)
                pending.clear()
        if args.progress_every and idx % args.progress_every == 0:
            print(
                f"progress files={idx}/{len(files)} source_rows={stats['source_rows']} "
                f"written={stats['missing_rows_written']} skipped_existing={stats['skipped_existing']} "
                f"elapsed={time.time() - started:.1f}s",
                flush=True,
            )

    stats["missing_rows_written"] += _flush(client, pending, args.dry_run)
    pending.clear()
    stats["finished_at"] = datetime.now().isoformat(timespec="seconds")
    stats["elapsed_seconds"] = round(time.time() - started, 3)
    print("done " + json.dumps(stats, ensure_ascii=False, default=str), flush=True)

    out = Path(args.report_path) if args.report_path else report_path(
        "tdx_fallback_repair",
        f"{datetime.now():%Y%m%d_%H%M%S}_local_tdx_daily_{args.start_date}_{args.end_date}.json",
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(stats, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"report_path={out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

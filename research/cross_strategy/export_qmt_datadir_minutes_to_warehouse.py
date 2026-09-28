from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from scripts.qmtmini_daily_backfill_validate import ch_client
from utils.paths import report_path, warehouse_path


DEFAULT_QMT_DATADIR = Path(r"D:\国金QMT\国金证券QMT交易端\datadir")
DEFAULT_OUTPUT_ROOT = warehouse_path("qmt_datadir_minute_v1")
DEFAULT_REPORT_DIR = report_path("qmt_datadir_minute_export_20260708")
DEFAULT_PERIODS = ("5m", "15m", "30m", "60m")
DAT_HEADER_BYTES = 8
DAT_RECORD_BYTES = 64
SH_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class StockCode:
    code: str
    market: str
    qmt_market: str
    raw_code: str
    list_date: str | None
    quit: int


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def chunked(items: list[StockCode], size: int) -> Iterable[list[StockCode]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def load_stock_codes(active_only: bool) -> list[StockCode]:
    where = "type = 'stock'"
    if active_only:
        where += " AND quit = 0"
    rows = ch_client().query(
        f"""
        SELECT code, market, list_date, quit
        FROM stock.stocks
        WHERE {where}
        ORDER BY code
        """
    ).result_rows
    out: list[StockCode] = []
    for code, market, list_date, quit in rows:
        text = str(code).upper()
        if "." not in text:
            continue
        raw, suffix = text.split(".", 1)
        if suffix not in {"SH", "SZ", "BJ"}:
            continue
        out.append(
            StockCode(
                code=text,
                market=str(market or suffix).upper(),
                qmt_market=suffix,
                raw_code=raw,
                list_date=str(list_date) if list_date else None,
                quit=int(quit or 0),
            )
        )
    return out


def _dat_path(qmt_datadir: Path, item: StockCode) -> Path:
    return qmt_datadir / item.qmt_market / "300" / f"{item.raw_code}.DAT"


def parse_qmt_5m_dat(path: Path, code: str, market: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size <= DAT_HEADER_BYTES:
        return pd.DataFrame()
    payload_bytes = path.stat().st_size - DAT_HEADER_BYTES
    records = payload_bytes // DAT_RECORD_BYTES
    if records <= 0:
        return pd.DataFrame()

    raw = np.fromfile(path, dtype="<u4", offset=DAT_HEADER_BYTES, count=records * 16)
    if raw.size < 16:
        return pd.DataFrame()
    raw = raw.reshape((-1, 16))
    dt = pd.to_datetime(raw[:, 0].astype("int64"), unit="s", utc=True).tz_convert(SH_TZ).tz_localize(None)
    frame = pd.DataFrame(
        {
            "period": "5m",
            "code": code,
            "market": market,
            "datetime": dt,
            "open": raw[:, 1].astype("float64") / 1000.0,
            "high": raw[:, 2].astype("float64") / 1000.0,
            "low": raw[:, 3].astype("float64") / 1000.0,
            "close": raw[:, 4].astype("float64") / 1000.0,
            "volume": raw[:, 6].astype("float64"),
            "amount": raw[:, 8].astype("float64"),
        }
    )
    frame = frame[(frame["datetime"] >= start) & (frame["datetime"] <= end)].copy()
    if frame.empty:
        return frame
    frame["trade_date"] = frame["datetime"].dt.date.astype(str)
    frame = frame.drop_duplicates(["code", "datetime"], keep="last").sort_values(["code", "datetime"])
    return frame


def aggregate_from_5m(frame: pd.DataFrame, period: str) -> pd.DataFrame:
    group_size = {"15m": 3, "30m": 6, "60m": 12}.get(period)
    if frame.empty or not group_size:
        return pd.DataFrame()
    work = frame.copy()
    minute_of_day = work["datetime"].dt.hour * 60 + work["datetime"].dt.minute
    am = (minute_of_day > 9 * 60 + 30) & (minute_of_day <= 11 * 60 + 30)
    pm = (minute_of_day > 13 * 60) & (minute_of_day <= 15 * 60)
    work = work[am | pm].copy()
    if work.empty:
        return pd.DataFrame()
    minute_of_day = work["datetime"].dt.hour * 60 + work["datetime"].dt.minute
    session_start = np.where(minute_of_day <= 11 * 60 + 30, 9 * 60 + 30, 13 * 60)
    slot = ((minute_of_day - session_start) // 5).astype("int64")
    work["_session"] = np.where(minute_of_day <= 11 * 60 + 30, "am", "pm")
    work["_bucket"] = ((slot - 1) // group_size).astype("int64")
    grouped = work.groupby(["code", "market", "trade_date", "_session", "_bucket"], sort=True)
    out = grouped.agg(
        datetime=("datetime", "max"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
        rows=("close", "size"),
    ).reset_index()
    out = out[out["rows"] == group_size].copy()
    if out.empty:
        return pd.DataFrame()
    out.insert(0, "period", period)
    out = out.drop(columns=["_session", "_bucket", "rows"]).sort_values(["code", "datetime"])
    return out[["period", "code", "market", "datetime", "trade_date", "open", "high", "low", "close", "volume", "amount"]]


def write_partitioned_parts(frame: pd.DataFrame, output_root: Path, counters: dict[str, int]) -> dict[str, int]:
    written: dict[str, int] = {}
    if frame.empty:
        return written
    work = frame.copy()
    work["year"] = work["datetime"].dt.year.astype("int16")
    work["month"] = work["datetime"].dt.month.astype("int8")
    for (period, year, month), part in work.groupby(["period", "year", "month"], sort=True):
        counters[period] = counters.get(period, 0) + 1
        part_dir = output_root / f"period={period}" / f"year={int(year):04d}" / f"month={int(month):02d}"
        part_dir.mkdir(parents=True, exist_ok=True)
        out = part_dir / f"part-{counters[period]:06d}.parquet"
        payload = part.drop(columns=["year", "month"]).copy()
        payload.to_parquet(out, index=False)
        written[period] = written.get(period, 0) + len(payload)
    return written


def ensure_clean_output(output_root: Path, overwrite: bool) -> None:
    if not overwrite:
        output_root.mkdir(parents=True, exist_ok=True)
        return
    resolved = output_root.resolve()
    warehouse = warehouse_path().resolve()
    if warehouse not in resolved.parents and resolved != warehouse:
        raise RuntimeError(f"refuse to overwrite non-warehouse path: {resolved}")
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)


def export(args: argparse.Namespace) -> dict:
    qmt_datadir = Path(args.qmt_datadir)
    output_root = Path(args.output_root)
    report_dir = Path(args.report_dir)
    periods = [item.strip() for item in args.periods.split(",") if item.strip()]
    unsupported = sorted(set(periods) - set(DEFAULT_PERIODS))
    if unsupported:
        raise ValueError(f"unsupported periods: {unsupported}")
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    ensure_clean_output(output_root, args.overwrite)
    report_dir.mkdir(parents=True, exist_ok=True)

    codes = load_stock_codes(active_only=args.active_only)
    if args.limit > 0:
        codes = codes[: args.limit]
    log(f"开始解析 QMT DAT：codes={len(codes)} root={qmt_datadir} output={output_root}")
    counters: dict[str, int] = {}
    rows_by_period = {period: 0 for period in periods}
    files_seen = 0
    files_missing = 0
    files_empty = 0
    code_summaries: list[dict] = []

    for batch_no, batch in enumerate(chunked(codes, args.batch_size), start=1):
        five_frames: list[pd.DataFrame] = []
        for item in batch:
            path = _dat_path(qmt_datadir, item)
            if not path.exists():
                files_missing += 1
                code_summaries.append({"code": item.code, "status": "missing", "path": str(path), "rows_5m": 0})
                continue
            files_seen += 1
            frame = parse_qmt_5m_dat(path, item.code, item.qmt_market, start, end)
            if frame.empty:
                files_empty += 1
                code_summaries.append({"code": item.code, "status": "empty", "path": str(path), "rows_5m": 0})
                continue
            five_frames.append(frame)
            code_summaries.append(
                {
                    "code": item.code,
                    "status": "ok",
                    "path": str(path),
                    "rows_5m": int(len(frame)),
                    "first": str(frame["datetime"].min()),
                    "last": str(frame["datetime"].max()),
                }
            )
        if not five_frames:
            log(f"batch {batch_no}: 无可写数据")
            continue
        five = pd.concat(five_frames, ignore_index=True)
        if "5m" in periods:
            written = write_partitioned_parts(five, output_root, counters)
            rows_by_period["5m"] += written.get("5m", 0)
        for period in periods:
            if period == "5m":
                continue
            higher = aggregate_from_5m(five, period)
            written = write_partitioned_parts(higher, output_root, counters)
            rows_by_period[period] += written.get(period, 0)
        log(
            "batch "
            f"{batch_no}/{math.ceil(len(codes) / args.batch_size)}: "
            f"5m_rows={len(five):,} total_5m={rows_by_period.get('5m', 0):,}"
        )

    summary = {
        "qmt_datadir": str(qmt_datadir),
        "output_root": str(output_root),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "active_only": args.active_only,
        "code_count": len(codes),
        "files_seen": files_seen,
        "files_missing": files_missing,
        "files_empty": files_empty,
        "rows_by_period": rows_by_period,
        "parts_by_period": counters,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (report_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame(code_summaries).to_csv(report_dir / "code_summary.csv", index=False, encoding="utf-8-sig")
    log(f"完成：summary={report_dir / 'summary.json'} code_summary={report_dir / 'code_summary.csv'}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse QMT local 5m DAT files into partitioned local parquet warehouse.")
    parser.add_argument("--qmt-datadir", default=str(DEFAULT_QMT_DATADIR))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--start-date", default="2025-07-04")
    parser.add_argument("--end-date", default="2026-07-07")
    parser.add_argument("--periods", default="5m,15m,30m,60m")
    parser.add_argument("--batch-size", type=int, default=80)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--active-only", action="store_true", help="Only export stocks with quit=0.")
    parser.add_argument("--overwrite", action="store_true", help="Remove output root before export.")
    args = parser.parse_args()
    export(args)


if __name__ == "__main__":
    main()

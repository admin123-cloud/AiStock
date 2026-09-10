from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
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

from scripts.qmtmini_daily_backfill_validate import ch_client, quote_sql
from utils.kline_units import normalize_qmt_intraday_units
from utils.paths import report_path


DEFAULT_QMT_DATADIR = Path("D:\\\u56fd\u91d1QMT\\\u56fd\u91d1\u8bc1\u5238QMT\u4ea4\u6613\u7aef\\datadir")
DEFAULT_STAGE_TABLE = "qmt_datadir_intraday_minute_stage"
DEFAULT_PERIODS = ("5m", "15m", "30m")
PERIOD_TO_TABLE = {
    "5m": "kline_minute_5",
    "15m": "kline_minute_15",
    "30m": "kline_minute_30",
}
DAT_HEADER_BYTES = 8
DAT_RECORD_BYTES = 64
SH_TZ = ZoneInfo("Asia/Shanghai")
STAGE_COLUMNS = ["period", "code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]
MINUTE_TARGET_COLUMNS = ["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]
DAILY_TARGET_COLUMNS = [
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
class Security:
    code: str
    market: str
    raw_code: str
    type_: str


def log(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def stable_id(period: str, code: str, dt: Any) -> int:
    ts = pd.Timestamp(dt).to_pydatetime().replace(tzinfo=None)
    key = f"{period}|{code}|{ts:%Y-%m-%d %H:%M:%S}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False)


def daily_id(code: str, trade_date: Any) -> int:
    day = pd.Timestamp(trade_date).date().isoformat()
    key = f"1d|{code}|{day}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False)


def chunked(items: list[Security], size: int) -> Iterable[list[Security]]:
    for idx in range(0, len(items), size):
        yield items[idx : idx + size]


def parse_periods(text: str) -> list[str]:
    periods = [item.strip().lower() for item in text.split(",") if item.strip()]
    unknown = sorted(set(periods) - set(PERIOD_TO_TABLE))
    if unknown:
        raise ValueError(f"unsupported periods: {unknown}")
    return periods


def load_securities(args: argparse.Namespace) -> list[Security]:
    client = ch_client()
    if args.codes.strip():
        requested = [item.strip().upper() for item in args.codes.split(",") if item.strip()]
        code_sql = ",".join(quote_sql(code) for code in requested)
        rows = client.query(
            f"""
            SELECT code, market, type
            FROM stock.stocks
            WHERE code IN ({code_sql})
            ORDER BY code
            """
        ).result_rows
    else:
        types = ["stock"]
        if args.include_index:
            types.append("index")
        type_sql = ",".join(quote_sql(item) for item in types)
        rows = client.query(
            f"""
            SELECT code, market, type
            FROM stock.stocks
            WHERE type IN ({type_sql})
              AND (type = 'index' OR quit = 0)
              AND ifNull(listing_status, 'active') = 'active'
              AND (type = 'index' OR list_date IS NULL OR list_date <= toDate({quote_sql(args.end_date)}))
            ORDER BY type, code
            """
        ).result_rows

    securities: list[Security] = []
    for code, market, type_ in rows:
        text = str(code).upper()
        if "." not in text:
            continue
        raw, suffix = text.split(".", 1)
        if suffix not in {"SH", "SZ", "BJ"}:
            continue
        securities.append(Security(code=text, market=suffix, raw_code=raw, type_=str(type_ or "")))

    offset = max(0, args.code_offset)
    if offset:
        securities = securities[offset:]
    if args.limit > 0:
        securities = securities[: args.limit]
    return securities


def dat_path(root: Path, item: Security) -> Path:
    return root / item.market / "300" / f"{item.raw_code}.DAT"


def parse_5m_dat(path: Path, item: Security, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size <= DAT_HEADER_BYTES:
        return pd.DataFrame()
    records = (path.stat().st_size - DAT_HEADER_BYTES) // DAT_RECORD_BYTES
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
            "code": item.code,
            "datetime": dt,
            "open": raw[:, 1].astype("float64") / 1000.0,
            "high": raw[:, 2].astype("float64") / 1000.0,
            "low": raw[:, 3].astype("float64") / 1000.0,
            "close": raw[:, 4].astype("float64") / 1000.0,
            "volume": raw[:, 6].astype("float64"),
            "amount": raw[:, 8].astype("float64"),
        }
    )
    # QMT DAT uses lots/yuan for stocks but shares/yuan for index records.
    # Normalize before staging so the same frame can safely feed both minute
    # tables and the derived formal daily table.
    frame = normalize_qmt_intraday_units(frame, instrument_type=item.type_)
    frame = frame[(frame["datetime"] >= start) & (frame["datetime"] <= end)].copy()
    if frame.empty:
        return frame
    frame["trade_date"] = frame["datetime"].dt.date.astype(str)
    return frame.drop_duplicates(["code", "datetime"], keep="last").sort_values(["code", "datetime"])


def aggregate_from_5m(frame: pd.DataFrame, period: str) -> pd.DataFrame:
    group_size = {"15m": 3, "30m": 6}.get(period)
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
    grouped = work.groupby(["code", "trade_date", "_session", "_bucket"], sort=True)
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
    return out.drop(columns=["_session", "_bucket", "rows"]).sort_values(["code", "datetime"])


def daily_from_5m(frame: pd.DataFrame, created_at: datetime) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=DAILY_TARGET_COLUMNS)
    grouped = frame.groupby(["code", "trade_date"], sort=True)
    out = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
    ).reset_index()
    out["amplitude"] = np.where(out["open"] != 0, (out["high"] - out["low"]) / out["open"] * 100.0, 0.0)
    out["change_pct"] = np.where(out["open"] != 0, (out["close"] - out["open"]) / out["open"] * 100.0, 0.0)
    out["change_amount"] = out["close"] - out["open"]
    out["turnover_rate"] = 0.0
    out["created_at"] = created_at
    out["id"] = [daily_id(code, day) for code, day in zip(out["code"], out["trade_date"])]
    return out[DAILY_TARGET_COLUMNS]


def ensure_stage_table(client, stage_table: str, reset: bool) -> None:
    if reset:
        client.command(f"DROP TABLE IF EXISTS {stage_table}")
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {stage_table}
        (
            period String,
            code String,
            datetime DateTime,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume Float64,
            amount Float64,
            created_at DateTime,
            id UInt64
        )
        ENGINE = ReplacingMergeTree(created_at)
        ORDER BY (period, code, datetime)
        SETTINGS index_granularity = 8192
        """
    )


def insert_stage(client, stage_table: str, frame: pd.DataFrame, created_at: datetime) -> dict[str, Any]:
    if frame.empty:
        return {"rows": 0, "codes": 0}
    payload = frame.copy()
    payload["created_at"] = created_at
    payload["id"] = [stable_id(period, code, dt) for period, code, dt in zip(payload["period"], payload["code"], payload["datetime"])]
    payload = payload[STAGE_COLUMNS]
    client.insert(stage_table, [tuple(row) for row in payload.itertuples(index=False, name=None)], column_names=STAGE_COLUMNS)
    return {"rows": int(len(payload)), "codes": int(payload["code"].nunique())}


def apply_minutes(client, stage_table: str, periods: list[str], start_date: str, end_next_date: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for period in periods:
        target = PERIOD_TO_TABLE[period]
        before = client.query(
            f"""
            SELECT count()
            FROM {target}
            WHERE datetime >= toDateTime({quote_sql(start_date + ' 00:00:00')})
              AND datetime < toDateTime({quote_sql(end_next_date + ' 00:00:00')})
            """
        ).first_row[0]
        client.command(
            f"""
            INSERT INTO {target} ({", ".join(MINUTE_TARGET_COLUMNS)})
            SELECT
                s.code,
                s.datetime,
                s.open,
                s.high,
                s.low,
                s.close,
                {'toInt64(round(s.volume))' if period == '5m' else 's.volume'} AS volume,
                s.amount,
                s.created_at,
                s.id
            FROM
            (
                SELECT period, code, datetime, open, high, low, close, volume, amount, created_at, id
                FROM {stage_table}
                WHERE period = {quote_sql(period)}
            ) AS s
            ANY LEFT JOIN
            (
                SELECT code, datetime
                FROM {target}
                WHERE datetime >= toDateTime({quote_sql(start_date + ' 00:00:00')})
                  AND datetime < toDateTime({quote_sql(end_next_date + ' 00:00:00')})
            ) AS t
            ON s.code = t.code AND s.datetime = t.datetime
            WHERE t.datetime IS NULL
            SETTINGS max_threads = 2, join_algorithm = 'grace_hash'
            """
        )
        after = client.query(
            f"""
            SELECT count()
            FROM {target}
            WHERE datetime >= toDateTime({quote_sql(start_date + ' 00:00:00')})
              AND datetime < toDateTime({quote_sql(end_next_date + ' 00:00:00')})
            """
        ).first_row[0]
        stage = client.query(
            f"""
            SELECT count(), uniqExact(code), min(datetime), max(datetime)
            FROM {stage_table}
            WHERE period = {quote_sql(period)}
            """
        ).first_row
        result[period] = {
            "stage_rows": int(stage[0] or 0),
            "stage_codes": int(stage[1] or 0),
            "stage_min": str(stage[2]) if stage[2] is not None else None,
            "stage_max": str(stage[3]) if stage[3] is not None else None,
            "inserted_missing_rows": int(after or 0) - int(before or 0),
        }
    return result


def apply_daily(client, daily: pd.DataFrame, trade_date: str, delete_chunk_size: int) -> dict[str, Any]:
    if daily.empty:
        return {"rows": 0, "codes": 0}
    codes = sorted(daily["code"].astype(str).unique())
    for chunk in [codes[idx : idx + delete_chunk_size] for idx in range(0, len(codes), delete_chunk_size)]:
        code_sql = ",".join(quote_sql(code) for code in chunk)
        client.command(
            f"""
            ALTER TABLE kline_daily
            DELETE WHERE trade_date = toDate({quote_sql(trade_date)})
              AND code IN ({code_sql})
            SETTINGS mutations_sync = 1
            """
        )
    client.insert("kline_daily", [tuple(row) for row in daily.itertuples(index=False, name=None)], column_names=DAILY_TARGET_COLUMNS)
    return {"rows": int(len(daily)), "codes": int(len(codes))}


def validate_targets(client, periods: list[str], trade_date: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    daily = client.query(
        f"""
        SELECT count(), uniqExact(code), min(trade_date), max(trade_date)
        FROM kline_daily
        WHERE trade_date = toDate({quote_sql(trade_date)})
        """
    ).first_row
    out["1d"] = {
        "rows": int(daily[0] or 0),
        "codes": int(daily[1] or 0),
        "min": str(daily[2]) if daily[2] is not None else None,
        "max": str(daily[3]) if daily[3] is not None else None,
    }
    for period in periods:
        target = PERIOD_TO_TABLE[period]
        row = client.query(
            f"""
            SELECT count(), uniqExact(code), min(datetime), max(datetime)
            FROM {target}
            WHERE toDate(datetime) = toDate({quote_sql(trade_date)})
            """
        ).first_row
        out[period] = {
            "rows": int(row[0] or 0),
            "codes": int(row[1] or 0),
            "min": str(row[2]) if row[2] is not None else None,
            "max": str(row[3]) if row[3] is not None else None,
        }
    return out


def main() -> int:
    today = datetime.now(SH_TZ).strftime("%Y-%m-%d")
    default_report = report_path("qmt_datadir_intraday_import", f"intraday_{today.replace('-', '')}.json")
    parser = argparse.ArgumentParser(description="Import same-day QMT datadir 5m/15m/30m and derived daily bars to ClickHouse.")
    parser.add_argument("--qmt-datadir", default=str(DEFAULT_QMT_DATADIR))
    parser.add_argument("--stage-table", default=DEFAULT_STAGE_TABLE)
    parser.add_argument("--periods", default=",".join(DEFAULT_PERIODS))
    parser.add_argument("--start-date", default=today)
    parser.add_argument("--end-date", default=today)
    parser.add_argument("--end-next-date", default="")
    parser.add_argument("--codes", default="")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--code-offset", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--include-index", action="store_true")
    parser.add_argument("--reset-stage", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-daily", action="store_true")
    parser.add_argument("--delete-chunk-size", type=int, default=300)
    parser.add_argument("--report", default=str(default_report))
    args = parser.parse_args()

    periods = parse_periods(args.periods)
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    end_next_date = args.end_next_date or str((pd.Timestamp(args.end_date) + pd.Timedelta(days=1)).date())
    created_at = datetime.now(SH_TZ).replace(tzinfo=None, microsecond=0)
    client = ch_client()
    securities = load_securities(args)
    qmt_datadir = Path(args.qmt_datadir)
    ensure_stage_table(client, args.stage_table, reset=args.reset_stage)

    rows_by_period = {period: 0 for period in periods}
    files_seen = 0
    files_missing = 0
    files_empty = 0
    daily_frames: list[pd.DataFrame] = []
    started = time.perf_counter()
    batches = math.ceil(len(securities) / max(1, args.batch_size))
    log(f"start qmt datadir intraday import codes={len(securities)} periods={periods} root={qmt_datadir}")

    for batch_no, batch in enumerate(chunked(securities, max(1, args.batch_size)), start=1):
        five_frames: list[pd.DataFrame] = []
        for item in batch:
            path = dat_path(qmt_datadir, item)
            if not path.exists():
                files_missing += 1
                continue
            files_seen += 1
            frame = parse_5m_dat(path, item, start, end)
            if frame.empty:
                files_empty += 1
                continue
            five_frames.append(frame)
        if not five_frames:
            log(f"batch {batch_no}/{batches}: no rows")
            continue
        five = pd.concat(five_frames, ignore_index=True)
        if not args.skip_daily:
            daily_frames.append(daily_from_5m(five, created_at))
        frames: list[pd.DataFrame] = []
        if "5m" in periods:
            frames.append(five.drop(columns=["trade_date"]))
        for period in periods:
            if period != "5m":
                frames.append(aggregate_from_5m(five, period).drop(columns=["trade_date"], errors="ignore"))
        merged = pd.concat([frame for frame in frames if not frame.empty], ignore_index=True) if frames else pd.DataFrame()
        stage_summary = insert_stage(client, args.stage_table, merged, created_at)
        for period, count in merged.groupby("period").size().items() if not merged.empty else []:
            rows_by_period[str(period)] += int(count)
        log(f"batch {batch_no}/{batches}: stage_rows={stage_summary['rows']:,} codes={stage_summary['codes']}")

    minute_apply = apply_minutes(client, args.stage_table, periods, args.start_date, end_next_date)
    daily_apply = {"skipped": True}
    if not args.skip_daily and daily_frames:
        daily_payload = pd.concat(daily_frames, ignore_index=True)
        daily_payload = daily_payload.drop_duplicates(["code", "trade_date"], keep="last")
        daily_apply = apply_daily(client, daily_payload, args.start_date, args.delete_chunk_size)
    validation = validate_targets(client, periods, args.start_date)
    report = {
        "ok": True,
        "source": "qmt_datadir",
        "qmt_datadir": str(qmt_datadir),
        "stage_table": args.stage_table,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "code_offset": args.code_offset,
        "limit": args.limit,
        "selected_codes": len(securities),
        "files_seen": files_seen,
        "files_missing": files_missing,
        "files_empty": files_empty,
        "stage_rows_by_period": rows_by_period,
        "minute_apply": minute_apply,
        "daily_apply": daily_apply,
        "validation": validation,
        "elapsed_sec": round(time.perf_counter() - started, 3),
        "created_at": created_at.isoformat(sep=" "),
    }
    report_path_obj = Path(args.report)
    report_path_obj.parent.mkdir(parents=True, exist_ok=True)
    report_path_obj.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    log("done " + json.dumps({"report": str(report_path_obj), "validation": validation}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

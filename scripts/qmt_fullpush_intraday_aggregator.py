from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time as dt_time
from pathlib import Path
from threading import Lock, Thread
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from services.operations.ingestion_store import clickhouse_client, clickhouse_query_df, observe
from utils.paths import runtime_path
from utils.qmt_universe import qmt_universe_filter_sql


SH_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_TABLES = {
    "5m": "kline_minute_5",
    "15m": "kline_minute_15",
    "30m": "kline_minute_30",
    "60m": "kline_minute_60",
}
from utils.kline_units import qmt_tick_lots_yuan, MINUTE_UNIT_CONTRACT_ID

MINUTE_COLUMNS = ["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]
INTRADAY_DAILY_COLUMNS = [
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "previous_close",
    "amplitude",
    "change_pct",
    "change_amount",
    "turnover_rate",
    "snapshot_at",
    "source",
    "is_provisional",
]
MARKET_SNAPSHOT_COLUMNS = [
    "trade_date", "snapshot_at", "is_provisional", "covered_count", "expected_count",
    "up_count", "down_count", "unchanged_count", "up_5_count", "down_5_count",
    "limit_up_count", "limit_down_count", "avg_change_percent", "total_amount",
    "sh_amount", "sz_amount", "bj_amount", "bucket_up_7", "bucket_up_5_7",
    "bucket_up_3_5", "bucket_up_0_3", "bucket_zero", "bucket_down_0_3",
    "bucket_down_3_5", "bucket_down_5_7", "bucket_down_7", "source",
]
MIN_SNAPSHOT_COVERAGE = 0.90
DEFAULT_INDEX_CODES = "000001.SH,399001.SZ,399006.SZ,000300.SH,000905.SH,000852.SH"


def log(message: str) -> None:
    print(f"[{datetime.now(SH_TZ).strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def write_report(path: str | Path, payload: dict[str, Any]) -> None:
    report = Path(path)
    report.parent.mkdir(parents=True, exist_ok=True)
    temporary = report.with_suffix(report.suffix + ".tmp")
    temporary.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
    temporary.replace(report)


def quote_sql(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def stable_id(*parts: Any) -> int:
    key = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(), "big", signed=False)


def ch_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(SH_TZ).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def parse_tick_time(value: Any, fallback: datetime | None = None) -> datetime:
    fallback = fallback or datetime.now(SH_TZ).replace(tzinfo=None)
    if value is None or value == "":
        return fallback
    if isinstance(value, pd.Timestamp):
        return value.tz_localize(None).to_pydatetime()
    if isinstance(value, datetime):
        return value.astimezone(SH_TZ).replace(tzinfo=None) if value.tzinfo else value
    try:
        number = int(float(value))
        text = str(number)
        if len(text) >= 14:
            parsed = datetime.strptime(text[:14], "%Y%m%d%H%M%S")
            return parsed.replace(tzinfo=None)
        if number > 10_000_000_000:
            return datetime.fromtimestamp(number / 1000, SH_TZ).replace(tzinfo=None)
    except Exception:
        pass
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return fallback
    return pd.Timestamp(parsed).tz_localize(None).to_pydatetime()


def tick_number(tick: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        value = tick.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except Exception:
            continue
    return float(default)


def normalize_tick_code(code: Any) -> str:
    return str(code or "").strip().upper()


def bucket_end_5m(ts: datetime) -> datetime | None:
    sessions = [
        (dt_time(9, 30), dt_time(11, 30)),
        (dt_time(13, 0), dt_time(15, 0)),
    ]
    current_time = ts.time().replace(microsecond=0)
    for start_t, end_t in sessions:
        if current_time < start_t or current_time > end_t:
            continue
        session_start = datetime.combine(ts.date(), start_t)
        session_end = datetime.combine(ts.date(), end_t)
        if ts <= session_start:
            return session_start + pd.Timedelta(minutes=5)
        elapsed = (ts - session_start).total_seconds() / 60
        bucket_no = int((elapsed + 4.999999) // 5)
        bucket_no = max(1, bucket_no)
        end_ts = session_start + pd.Timedelta(minutes=bucket_no * 5)
        if end_ts > session_end:
            end_ts = session_end
        return end_ts.to_pydatetime() if isinstance(end_ts, pd.Timestamp) else end_ts
    return None


def completed_bucket_cutoff(now: datetime) -> datetime:
    end_ts = bucket_end_5m(now)
    if end_ts is None:
        return now
    return end_ts if now >= end_ts else end_ts - pd.Timedelta(minutes=5)


def is_regular_stock_code(code: str) -> bool:
    return code.endswith((".SH", ".SZ", ".BJ")) and len(code) >= 9


def load_codes(universe: str, codes_arg: str, limit: int, include_index: bool, index_codes: str) -> list[str]:
    if codes_arg.strip():
        codes = [normalize_tick_code(item) for item in codes_arg.split(",") if item.strip()]
    else:
        active_universe = "stock,index" if include_index or "index" in universe.lower() else "stock"
        type_filter = qmt_universe_filter_sql(active_universe)
        current_day = datetime.now(SH_TZ).date().isoformat()
        df = clickhouse_query_df(
            f"""
            SELECT code
            FROM stocks FINAL
            WHERE {type_filter}
              AND (quit = 0 OR quit IS NULL)
              AND (list_date IS NULL OR list_date <= toDate('{current_day}'))
              AND (delist_date IS NULL OR delist_date > toDate('{current_day}'))
            ORDER BY type, code
            """
        )
        codes = [normalize_tick_code(code) for code in df.get("code", [])]
        if include_index and "index" not in universe.lower():
            codes.extend(normalize_tick_code(item) for item in index_codes.split(",") if item.strip())
    codes = [code for code in dict.fromkeys(codes) if is_regular_stock_code(code)]
    return codes[:limit] if limit > 0 else codes


@dataclass
class BarState:
    code: str
    end_ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0
    last_update: datetime = field(default_factory=lambda: datetime.now(SH_TZ).replace(tzinfo=None))

    def update(self, price: float, delta_volume: float, delta_amount: float, ts: datetime) -> None:
        if price > 0:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
            self.close = price
        self.volume += max(0.0, float(delta_volume or 0))
        self.amount += max(0.0, float(delta_amount or 0))
        self.last_update = ts


class FullPushAggregator:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.allowed_codes: set[str] = set()
        self.lock = Lock()
        self.latest_ticks: dict[str, dict[str, Any]] = {}
        # ``subscribe_whole_quote`` and ``get_full_tick`` are both useful for
        # minute bars, but the latter carries the canonical last-close field
        # needed by homepage breadth.  Keep it separately so a malformed
        # subscription callback cannot overwrite an otherwise valid snapshot.
        self.full_tick_ticks: dict[str, dict[str, Any]] = {}
        self.current_bars: dict[str, BarState] = {}
        self.closed_bars: list[BarState] = []
        self.prev_volume: dict[str, float] = {}
        self.prev_amount: dict[str, float] = {}
        self.received_events = 0
        self.received_codes: set[str] = set()
        self.last_callback_at: datetime | None = None
        self.volume_field = "volume"
        self.unit_errors: dict[str, str] = {}
        self.stop_requested = False

    def on_data(self, datas: Any, *, update_bars: bool = True) -> None:
        now = datetime.now(SH_TZ).replace(tzinfo=None)
        if not isinstance(datas, dict):
            return
        with self.lock:
            self.received_events += 1
            for raw_code, raw_tick in datas.items():
                code = normalize_tick_code(raw_code)
                if not code or not isinstance(raw_tick, dict):
                    continue
                if self.allowed_codes and code not in self.allowed_codes:
                    continue
                tick = dict(raw_tick)
                tick_ts = parse_tick_time(tick.get("time"), now)
                price = tick_number(tick, "lastPrice", "last", "price", "close")
                if price <= 0:
                    continue
                if update_bars:
                    self.last_callback_at = now
                self.latest_ticks[code] = tick
                self.received_codes.add(code)
                if not update_bars:
                    continue
                try:
                    cum_volume, cum_amount = qmt_tick_lots_yuan(tick)
                except ValueError as exc:
                    self.unit_errors[code] = str(exc)
                    continue
                self.unit_errors.pop(code, None)
                prev_volume = self.prev_volume.get(code)
                prev_amount = self.prev_amount.get(code)
                end_ts = bucket_end_5m(tick_ts)
                first_open_bucket = bool(end_ts and end_ts.time() <= dt_time(9, 35))
                delta_volume = cum_volume if prev_volume is None and first_open_bucket else 0.0 if prev_volume is None else max(0.0, cum_volume - prev_volume)
                delta_amount = cum_amount if prev_amount is None and first_open_bucket else 0.0 if prev_amount is None else max(0.0, cum_amount - prev_amount)
                self.prev_volume[code] = cum_volume
                self.prev_amount[code] = cum_amount
                if end_ts is None:
                    continue
                existing = self.current_bars.get(code)
                if existing is not None and existing.end_ts != end_ts:
                    self.closed_bars.append(existing)
                    existing = None
                if existing is None:
                    existing = BarState(code=code, end_ts=end_ts, open=price, high=price, low=price, close=price)
                    self.current_bars[code] = existing
                existing.update(price, delta_volume, delta_amount, tick_ts)

    def poll_full_tick(self, xtdata: Any, codes: list[str], batch_size: int) -> int:
        total = 0
        for idx in range(0, len(codes), max(1, batch_size)):
            batch = codes[idx : idx + max(1, batch_size)]
            try:
                data = xtdata.get_full_tick(batch)
            except Exception as exc:
                log(f"get_full_tick skipped batch={idx // max(1, batch_size) + 1}: {type(exc).__name__}: {exc}")
                continue
            if isinstance(data, dict):
                total += len(data)
                self.on_data(data, update_bars=False)
                with self.lock:
                    for raw_code, raw_tick in data.items():
                        code = normalize_tick_code(raw_code)
                        if not code or not isinstance(raw_tick, dict):
                            continue
                        if self.allowed_codes and code not in self.allowed_codes:
                            continue
                        if tick_number(raw_tick, "lastPrice", "last", "price", "close") > 0:
                            self.full_tick_ticks[code] = dict(raw_tick)
        return total

    def drain_closed_bars(self, include_open: bool = False) -> list[BarState]:
        now = datetime.now(SH_TZ).replace(tzinfo=None)
        cutoff = completed_bucket_cutoff(now)
        with self.lock:
            ready = list(self.closed_bars)
            self.closed_bars.clear()
            for code, bar in list(self.current_bars.items()):
                if include_open or bar.end_ts <= cutoff:
                    ready.append(bar)
                    if not include_open:
                        self.current_bars.pop(code, None)
            return ready

    def daily_rows(self) -> list[tuple]:
        created_at = datetime.now(SH_TZ).replace(tzinfo=None)
        rows: list[tuple] = []
        with self.lock:
            items = list(self.latest_ticks.items())
            full_tick_items = dict(self.full_tick_ticks)
        for code, tick in items:
            polled = full_tick_items.get(code)
            if polled and parse_tick_time(polled.get("time"), datetime.min) > parse_tick_time(tick.get("time"), datetime.min):
                tick = polled
            last_price = tick_number(tick, "lastPrice", "last", "price", "close")
            open_price = tick_number(tick, "open", default=last_price)
            high_price = tick_number(tick, "high", default=max(open_price, last_price))
            low_price = tick_number(tick, "low", default=min(open_price, last_price))
            if last_price <= 0 or open_price <= 0 or high_price <= 0 or low_price <= 0:
                continue
            prev_close = tick_number(tick, "lastClose", "preClose", default=0.0)
            try:
                volume, amount = qmt_tick_lots_yuan(tick)
            except ValueError as exc:
                with self.lock:
                    self.unit_errors[code] = str(exc)
                continue
            with self.lock:
                self.unit_errors.pop(code, None)
            change_amount = last_price - prev_close if prev_close > 0 else 0.0
            change_pct = change_amount / prev_close * 100 if prev_close > 0 else 0.0
            amplitude = (high_price - low_price) / prev_close * 100 if prev_close > 0 else 0.0
            trade_dt = parse_tick_time(tick.get("time"), created_at).date()
            rows.append(
                (
                    code,
                    trade_dt,
                    float(open_price),
                    float(high_price),
                    float(low_price),
                    float(last_price),
                    float(volume),
                    float(amount),
                    float(prev_close) if prev_close > 0 else None,
                    float(amplitude),
                    float(change_pct),
                    float(change_amount),
                    0.0,
                    # ``kline_daily_intraday.snapshot_at`` is already a
                    # DateTime('Asia/Shanghai') column.  Unlike legacy minute
                    # tables, do not reinterpret the China wall clock as UTC.
                    created_at,
                    "qmt_fullpush",
                    1,
                )
            )
        return rows


def aggregate_bars(source: list[BarState], target: str) -> list[BarState]:
    # A-share has two independent sessions. Twelve 5m bars form each 60m bar
    # (10:30 / 11:30 / 14:00 / 15:00); never aggregate across the noon break.
    size = {"15m": 3, "30m": 6, "60m": 12}.get(target)
    if not size:
        return []
    grouped: dict[tuple[str, date, str], list[BarState]] = {}
    for bar in source:
        session = "am" if bar.end_ts.time() <= dt_time(11, 30) else "pm"
        grouped.setdefault((bar.code, bar.end_ts.date(), session), []).append(bar)
    out: list[BarState] = []
    for (_code, _day, _session), bars in grouped.items():
        bars = sorted(bars, key=lambda item: item.end_ts)
        for idx in range(0, len(bars), size):
            bucket = bars[idx : idx + size]
            if len(bucket) < size:
                continue
            out.append(
                BarState(
                    code=bucket[0].code,
                    end_ts=bucket[-1].end_ts,
                    open=bucket[0].open,
                    high=max(item.high for item in bucket),
                    low=min(item.low for item in bucket),
                    close=bucket[-1].close,
                    volume=sum(item.volume for item in bucket),
                    amount=sum(item.amount for item in bucket),
                    last_update=bucket[-1].last_update,
                )
            )
    return out


def session_name(ts: datetime) -> str | None:
    if dt_time(9, 30) < ts.time() <= dt_time(11, 30):
        return "am"
    if dt_time(13, 0) < ts.time() <= dt_time(15, 0):
        return "pm"
    return None


def is_target_boundary(ts: datetime, target: str) -> bool:
    minutes = {"15m": 15, "30m": 30, "60m": 60}.get(target)
    if not minutes:
        return False
    # ClickHouse may return timezone-aware timestamps while the session
    # boundaries below are intentionally Asia/Shanghai wall-clock values.
    if ts.tzinfo is not None:
        ts = ts.astimezone(SH_TZ).replace(tzinfo=None)
    session = session_name(ts)
    if session is None:
        return False
    start = datetime.combine(ts.date(), dt_time(9, 30) if session == "am" else dt_time(13, 0))
    elapsed = int((ts - start).total_seconds() // 60)
    return elapsed > 0 and elapsed % minutes == 0


def derive_higher_rows_from_clickhouse(target: str, trade_day: date) -> list[tuple]:
    group_size = {"15m": 3, "30m": 6, "60m": 12}.get(target)
    if not group_size:
        return []
    df = clickhouse_query_df(
        """
        SELECT code, datetime, open, high, low, close, volume, amount, created_at
        FROM kline_minute_5
        WHERE toDate(datetime) = ?
        ORDER BY code, datetime, created_at
        """,
        [trade_day],
    )
    if df.empty:
        return []
    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    work = work.dropna(subset=["code", "datetime", "open", "high", "low", "close"]).copy()
    if work.empty:
        return []
    work = work.drop_duplicates(subset=["code", "datetime"], keep="last")
    target_table = MINUTE_TABLES[target]
    existing_df = clickhouse_query_df(
        f"""
        SELECT code, datetime
        FROM {target_table}
        WHERE toDate(datetime) = ?
        """,
        [trade_day],
    )
    existing_keys: set[tuple[str, datetime]] = set()
    if not existing_df.empty:
        existing_df = existing_df.copy()
        existing_df["datetime"] = pd.to_datetime(existing_df["datetime"], errors="coerce")
        existing_df = existing_df.dropna(subset=["code", "datetime"])
        existing_keys = {
            (str(row.code), row.datetime.to_pydatetime())
            for row in existing_df.itertuples(index=False)
        }
    rows: list[tuple] = []
    created_at = ch_datetime(datetime.now(SH_TZ).replace(tzinfo=None))
    for code, code_df in work.groupby("code", dropna=True):
        code_df = code_df.sort_values("datetime")
        by_dt = {row.datetime.to_pydatetime(): row for row in code_df.itertuples(index=False)}
        for end_ts in sorted(by_dt):
            if not is_target_boundary(end_ts, target):
                continue
            required = [end_ts - pd.Timedelta(minutes=5 * idx) for idx in range(group_size - 1, -1, -1)]
            required_dt = [item.to_pydatetime() if isinstance(item, pd.Timestamp) else item for item in required]
            if any(session_name(item) != session_name(end_ts) or item not in by_dt for item in required_dt):
                continue
            if (str(code), end_ts) in existing_keys:
                continue
            bucket = [by_dt[item] for item in required_dt]
            rows.append(
                (
                    str(code),
                    ch_datetime(end_ts),
                    float(bucket[0].open),
                    float(max(float(item.high) for item in bucket)),
                    float(min(float(item.low) for item in bucket)),
                    float(bucket[-1].close),
                    float(sum(float(item.volume or 0) for item in bucket)),
                    float(sum(float(item.amount or 0) for item in bucket)),
                    created_at,
                    stable_id(target, code, end_ts.strftime("%Y-%m-%d %H:%M:%S")),
                )
            )
    return rows


def validate_pending_unit_contract(saved):
    if saved.get('bars') and saved.get('minute_unit_contract') != MINUTE_UNIT_CONTRACT_ID:
        raise RuntimeError('pending_minute_units_unverified; preserve journal for manual review')


def minute_rows(bars: list[BarState], period: str) -> list[tuple]:
    created_at = datetime.now(SH_TZ).replace(tzinfo=None)
    return [
        (
            bar.code,
            ch_datetime(bar.end_ts),
            float(bar.open),
            float(bar.high),
            float(bar.low),
            float(bar.close),
            float(bar.volume),
            float(bar.amount),
            ch_datetime(created_at),
            stable_id(period, bar.code, bar.end_ts.strftime("%Y-%m-%d %H:%M:%S")),
        )
        for bar in bars
        if bar.open > 0 and bar.high > 0 and bar.low > 0 and bar.close > 0 and (bar.volume > 0 or bar.amount > 0)
    ]


def insert_rows(table: str, rows: list[tuple], columns: list[str], dry_run: bool) -> int:
    if not rows:
        return 0
    if dry_run:
        return len(rows)
    started = time.monotonic()
    try:
        clickhouse_client().insert(table, rows, column_names=columns)
    except Exception:
        observe(source='clickhouse', operation='insert:'+table, duration_seconds=time.monotonic()-started,
                outcome='failed', requested=len(rows), received=0)
        raise
    observe(source='clickhouse', operation='insert:'+table, duration_seconds=time.monotonic()-started,
            outcome='success', requested=len(rows), received=len(rows), persisted_at=datetime.now(SH_TZ).isoformat())
    return len(rows)


def publish_market_snapshot(daily_rows: list[tuple], *, dry_run: bool) -> dict[str, Any]:
    """Publish the homepage fact from the same full-market QMT tick batch.

    This producer-side write is intentionally independent from the web
    scheduler.  The backend may reconcile it, but it must never be the only
    mechanism that creates intraday turnover or breadth.
    """
    if not daily_rows:
        return {"written": False, "reason": "no_daily_rows", "covered_count": 0, "expected_count": 0}
    stocks = clickhouse_query_df("SELECT code, market FROM stocks FINAL WHERE type = 'stock'")
    market_by_code = {
        str(row.code): str(row.market)
        for row in stocks.itertuples(index=False)
        if str(row.market) in {"SH", "SZ", "BJ"}
    }
    expected = len(market_by_code)
    records: list[dict[str, Any]] = []
    for row in daily_rows:
        code, trade_date, _open, _high, _low, close, _volume, amount, previous_close, _amplitude, change_pct, *_tail = row
        market = market_by_code.get(str(code))
        if not market or not (math.isfinite(float(close)) and float(close) > 0):
            continue
        previous = float(previous_close or 0)
        pct = float(change_pct or 0)
        if previous <= 0 or not math.isfinite(pct):
            continue
        records.append({"trade_date": trade_date, "market": market, "amount": float(amount) if math.isfinite(float(amount or 0)) else 0.0, "pct": pct})
    if not records or expected <= 0:
        return {"written": False, "reason": "no_valid_stock_ticks", "covered_count": len(records), "expected_count": expected}
    trade_dates = {item["trade_date"] for item in records}
    if len(trade_dates) != 1:
        return {"written": False, "reason": "mixed_trade_dates", "covered_count": len(records), "expected_count": expected}
    covered = len(records)
    if covered < expected * MIN_SNAPSHOT_COVERAGE:
        return {"written": False, "reason": "coverage_below_threshold", "covered_count": covered, "expected_count": expected}
    def count(predicate): return sum(1 for item in records if predicate(item["pct"]))
    def amount(market: str | None = None): return sum(item["amount"] for item in records if market is None or item["market"] == market)
    now = datetime.now(SH_TZ).replace(tzinfo=None)
    is_final = now.time() >= dt_time(15, 5)
    row = (
        records[0]["trade_date"], now, int(not is_final), covered, expected,
        count(lambda pct: pct > 0), count(lambda pct: pct < 0), count(lambda pct: pct == 0),
        count(lambda pct: pct >= 5), count(lambda pct: pct <= -5), count(lambda pct: pct >= 9.9), count(lambda pct: pct <= -9.9),
        sum(item["pct"] for item in records) / covered, amount(), amount("SH"), amount("SZ"), amount("BJ"),
        count(lambda pct: pct >= 7), count(lambda pct: 5 <= pct < 7), count(lambda pct: 3 <= pct < 5), count(lambda pct: 0 < pct < 3),
        count(lambda pct: pct == 0), count(lambda pct: -3 < pct < 0), count(lambda pct: -5 < pct <= -3),
        count(lambda pct: -7 < pct <= -5), count(lambda pct: pct <= -7),
        "qmt_fullpush_tick:after_close_final" if is_final else "qmt_fullpush_tick",
    )
    if not dry_run:
        clickhouse_client().insert("market_sentiment_snapshot", [row], column_names=MARKET_SNAPSHOT_COLUMNS)
    return {"written": True, "final": is_final, "covered_count": covered, "expected_count": expected}


def latest_closed_boundary(now, minutes=5):
    from datetime import timedelta
    candidates = []
    for hour, minute in ((9, 30), (13, 0)):
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for offset in range(minutes, 121, minutes):
            value = start + timedelta(minutes=offset)
            if value <= now:
                candidates.append(value)
    return max(candidates) if candidates else None


def flush_snapshot(aggregator):
    started = time.monotonic()
    daily = aggregator.daily_rows()
    dry_run = bool(aggregator.args.dry_run)
    written = insert_rows("kline_daily_intraday", daily, INTRADAY_DAILY_COLUMNS, dry_run) if aggregator.args.write_daily else 0
    market = publish_market_snapshot(daily, dry_run=dry_run) if aggregator.args.write_daily else {"written": False}
    now = datetime.now(SH_TZ).replace(tzinfo=None)
    with aggregator.lock:
        ticks = dict(aggregator.latest_ticks)
        for code, tick in aggregator.full_tick_ticks.items():
            if code not in ticks or parse_tick_time(tick.get("time"), datetime.min) > parse_tick_time(ticks[code].get("time"), datetime.min):
                ticks[code] = tick
    expected = set(aggregator.allowed_codes)
    fresh = {code for code, tick in ticks.items() if code in expected and
             0 <= (now-parse_tick_time(tick.get("time"), datetime.min)).total_seconds() <= 300}
    from services.operations.intraday_coverage import evaluate_coverage
    coverage = evaluate_coverage(expected, ticks, {row[0] for row in daily} if written and not dry_run else set(),
                                 now=now, parse_time=parse_tick_time, metadata=getattr(aggregator, 'metadata', {}))
    persisted_at = time.monotonic() if written and not dry_run else None
    previous = getattr(aggregator, 'last_persisted_monotonic', None)
    interval = round(persisted_at-previous, 3) if previous is not None and persisted_at is not None else None
    if persisted_at is not None:
        aggregator.last_persisted_monotonic = persisted_at
    return {"daily_rows": written, "market_snapshot": market,
            "minute_unit_contract": MINUTE_UNIT_CONTRACT_ID, "unit_errors": dict(aggregator.unit_errors),
            "coverage": coverage, "persisted_at": now.isoformat() if persisted_at else None,
            "previous_successful_write_gap_seconds": interval,
            "interval_slo_300s": 'unknown' if interval is None else 'passed' if interval <= 300 else 'failed',
            "duration_seconds": round(time.monotonic()-started, 3),
            "universe_count": len(expected), "source_fresh_300s": len(fresh),
            "unverified_codes": sorted(expected-fresh),
            "freshness_note": "Includes indices; suspension/session exemptions require separate evidence",
            "at": now.isoformat()}


def flush_minutes(aggregator, *, include_open_bars=False):
    # Keep failed 5m writes available for retry; persisted journal survives restarts.
    pending = getattr(aggregator, "pending_bars", [])
    pending.extend(aggregator.drain_closed_bars(include_open=include_open_bars))
    aggregator.pending_bars = pending
    journal = getattr(aggregator, "minute_journal", None)
    if journal and pending:
        write_report(journal, {"minute_unit_contract":MINUTE_UNIT_CONTRACT_ID, "bars": [asdict(bar) for bar in pending]})
    summary = {"minute_rows": {}, "errors": {}}
    periods = {x.strip() for x in aggregator.args.periods.split(",")}
    if "5m" in periods or periods.intersection({"15m", "30m", "60m"}):
        summary["minute_rows"]["5m"] = insert_rows(MINUTE_TABLES["5m"], minute_rows(pending, "5m"), MINUTE_COLUMNS, aggregator.args.dry_run)
    aggregator.pending_bars = []
    if journal:
        write_report(journal, {"minute_unit_contract":MINUTE_UNIT_CONTRACT_ID, "bars": []})
    now = datetime.now(SH_TZ).replace(tzinfo=None)
    for target in ("15m", "30m", "60m"):
        if target not in periods:
            continue
        # Only query the derived period after one of its boundaries has closed.
        boundary = latest_closed_boundary(now, int(target[:-1]))
        completed = getattr(aggregator, "derived_completed", {})
        if boundary is None or completed.get(target) == boundary:
            continue
        blocked = getattr(aggregator, "blocked_derived", {})
        if target in blocked:
            summary["errors"][target] = blocked[target]
            continue
        try:
            rows = aggregate_bars(pending, target) if aggregator.args.dry_run else derive_higher_rows_from_clickhouse(target, now.date())
            if aggregator.args.dry_run:
                rows = minute_rows(rows, target)
            summary["minute_rows"][target] = insert_rows(MINUTE_TABLES[target], rows, MINUTE_COLUMNS, aggregator.args.dry_run)
            completed[target] = boundary
            aggregator.derived_completed = completed
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            summary["errors"][target] = message
            from services.operations.ingestion_checkpoint import storage_failure
            if storage_failure(message):
                blocked[target] = message
                aggregator.blocked_derived = blocked
    return summary


def flush_once(aggregator, *, include_open_bars=False, write_daily=True):
    summary = flush_snapshot(aggregator) if write_daily else {}
    summary.update(flush_minutes(aggregator, include_open_bars=include_open_bars))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="QMT full-push intraday daily/minute aggregator.")
    parser.add_argument("--markets", default="SH,SZ,BJ")
    parser.add_argument("--universe", default="stock,index")
    parser.add_argument("--codes", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--include-index", action="store_true")
    parser.add_argument("--index-codes", default=DEFAULT_INDEX_CODES)
    parser.add_argument("--periods", default="5m,15m,30m,60m")
    parser.add_argument("--duration-sec", type=int, default=0)
    parser.add_argument("--flush-interval-sec", type=int, default=60)
    parser.add_argument("--full-tick-batch-size", type=int, default=500)
    parser.add_argument("--poll-full-tick", action="store_true")
    parser.add_argument("--write-daily", action="store_true", help="write provisional intraday daily snapshots; never writes kline_daily")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--flush-open-bars-on-exit", action="store_true")
    parser.add_argument("--connect-retry-sec", type=int, default=30)
    parser.add_argument("--connect-deadline", default="", help="Asia/Shanghai HH:MM; retry QMT until this session deadline")
    parser.add_argument("--report", default=str(runtime_path("qmt_fullpush_intraday_aggregator.json")))
    args = parser.parse_args()
    from services.operations.lifecycle import InstanceLock
    scope = hashlib.sha256(args.codes.encode()).hexdigest()[:12] if args.codes else "full-market"
    lock = InstanceLock(runtime_path("operations", f"intraday-{scope}.lock"))
    lock.acquire()
    try:
        return run_collector(args)
    finally:
        lock.release()


def run_collector(args):
    os.environ.setdefault("AISTOCK_QMT_QUOTE_HOST", "127.0.0.1")
    os.environ.setdefault("AISTOCK_QMT_QUOTE_PORT", "58610")
    from xtquant import xtdata  # type: ignore

    host = os.getenv("AISTOCK_QMT_QUOTE_HOST", "127.0.0.1")
    port = int(os.getenv("AISTOCK_QMT_QUOTE_PORT", "58610"))
    deadline = None
    if args.connect_deadline:
        try:
            end_time = datetime.strptime(args.connect_deadline, "%H:%M").time()
            deadline = datetime.combine(datetime.now(SH_TZ).date(), end_time)
        except ValueError as exc:
            raise ValueError(f"invalid --connect-deadline: {args.connect_deadline}") from exc
    while True:
        try:
            xtdata.connect(host, port)
            break
        except Exception as exc:
            if deadline is not None and datetime.now(SH_TZ).replace(tzinfo=None) >= deadline:
                log(f"QMT connection deadline reached: {type(exc).__name__}: {exc}")
                return 2
            log(f"QMT connection unavailable; retrying in {max(5, args.connect_retry_sec)}s: {type(exc).__name__}: {exc}")
            time.sleep(max(5, args.connect_retry_sec))
    codes = load_codes(args.universe, args.codes, args.limit, args.include_index, args.index_codes)
    markets = [item.strip().upper() for item in args.markets.split(",") if item.strip()]
    cache_seed: dict[str, Any] = {"attempted": False, "ok": None, "error": None}
    # Holding-T repairs pass an explicit small code set. Refresh QMT's closed
    # 5m history first, because a fresh full-push subscription cannot produce
    # a closed bar until the next bucket boundary.
    if args.codes.strip() and "5m" in {item.strip() for item in args.periods.split(",")}:
        cache_seed["attempted"] = True
        try:
            now = datetime.now(SH_TZ)
            from services.operations.qmt_download_queue import coordinated_download
            coordinated_download(codes, "5m", now.strftime("%Y%m%d000000"), now.strftime("%Y%m%d%H%M%S"),
                lambda: xtdata.download_history_data2(codes, "5m", now.strftime("%Y%m%d000000"), now.strftime("%Y%m%d%H%M%S")))
            cache_seed["ok"] = True
            log(f"seeded QMT 5m history cache for {len(codes)} targeted codes")
        except Exception as exc:
            cache_seed["ok"] = False
            cache_seed["error"] = f"{type(exc).__name__}: {exc}"
            log(f"QMT 5m history cache seed failed: {cache_seed['error']}")
    aggregator = FullPushAggregator(args)
    aggregator.allowed_codes = set(codes)
    try:
        frame = clickhouse_query_df('SELECT code, type FROM stocks FINAL')
        aggregator.metadata = {row['code']: {'type': row['type']} for row in frame.to_dict('records')}
    except Exception as exc:
        aggregator.metadata = {}
        log(f'Universe classification unavailable: {type(exc).__name__}')
    stopped = {"value": False}

    def _handle_stop(_signum, _frame) -> None:
        stopped["value"] = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    journal = Path(args.report).with_suffix(".pending.json")
    aggregator.minute_journal = journal
    aggregator.pending_bars = []
    if journal.exists():
        saved = json.loads(journal.read_text(encoding="utf-8"))
        validate_pending_unit_contract(saved)
        for value in saved.get("bars", []):
            for key in ("end_ts", "last_update"):
                if value.get(key):
                    value[key] = datetime.fromisoformat(value[key])
            aggregator.pending_bars.append(BarState(**value))

    seq = xtdata.subscribe_whole_quote(markets, aggregator.on_data)
    log(f"subscribed full-push markets={markets} seq={seq} codes={len(codes)} dry_run={args.dry_run}")
    started = time.monotonic()
    from services.operations.ingestion_lanes import IngestionLane
    flushes = []
    full_tick_polls = 0
    exit_code = 0

    def snapshot_action():
        nonlocal full_tick_polls
        poll_started = time.monotonic()
        count = 0
        errors = {}
        if args.poll_full_tick:
            try:
                from services.operations.qmt_snapshot import read_full_snapshot
                response = read_full_snapshot(codes, args.full_tick_batch_size)
                ticks = response['ticks']
                count = len(ticks)
                aggregator.on_data(ticks, update_bars=False)
                with aggregator.lock:
                    aggregator.full_tick_ticks.update(ticks)
                if response.get('failed_batches'):
                    errors['poll'] = response['failed_batches']
                full_tick_polls += 1
            except Exception as exc:
                errors['poll'] = f"{type(exc).__name__}: {exc}"
        poll_seconds = time.monotonic()-poll_started
        # Subscription remains available if a disposable polling worker fails.
        result = flush_snapshot(aggregator)
        with aggregator.lock:
            last_push = aggregator.last_callback_at
        push_age = (datetime.now(SH_TZ).replace(tzinfo=None)-last_push).total_seconds() if last_push else None
        active_session = session_name(datetime.now(SH_TZ).replace(tzinfo=None)) is not None
        if active_session and (push_age is None or push_age > 120):
            errors['subscription'] = 'No verified subscription callback in 120 seconds; polling cannot certify minute bars'
        result['subscription_age_seconds'] = round(push_age, 3) if push_age is not None else None
        total_seconds = round(time.monotonic()-poll_started, 3)
        if result['coverage']['slo_300s'] != 'passed':
            errors['coverage'] = f"{len(result['coverage']['unverified'])} securities not verified fresh and persisted"
        if result['interval_slo_300s'] == 'failed' or total_seconds > 300:
            errors['slo'] = 'full-market persistence exceeded 300 seconds'
        result.update(poll_seconds=round(poll_seconds, 3), end_to_end_seconds=total_seconds,
                      polled_codes=count, errors=errors)
        log(f"snapshot {result['daily_rows']} rows poll={poll_seconds:.3f}s write={result['duration_seconds']}s fresh={result['source_fresh_300s']}/{result['universe_count']}")
        return result

    snapshot_lane = IngestionLane("snapshot", snapshot_action)
    minute_lane = IngestionLane("minutes", lambda: flush_minutes(aggregator))
    lanes = [snapshot_lane, minute_lane]
    next_snapshot = started
    last_boundary = None
    heartbeat = 0
    try:
        while not stopped["value"]:
            clock = time.monotonic()
            now = datetime.now(SH_TZ).replace(tzinfo=None)
            if clock >= next_snapshot and snapshot_lane.start():
                next_snapshot = clock + max(1, args.flush_interval_sec)
            boundary = latest_closed_boundary(now)
            if boundary is not None and boundary != last_boundary and (now-boundary).total_seconds() >= 15:
                if minute_lane.start():
                    last_boundary = boundary
            # Retry failed 5m writes without waiting for a new candle.
            if minute_lane.state["status"] in ("failed", "degraded") and clock-(minute_lane.started or clock) >= 60:
                minute_lane.start()
            if clock >= heartbeat:
                write_report(runtime_path("operations", "intraday_ingestion.json") if not args.codes else Path(args.report).with_suffix(".live.json"),
                             {"generated_at": now.isoformat()+"+08:00", "lanes": [lane.snapshot() for lane in lanes],
                              "snapshot_target_seconds": args.flush_interval_sec, "phase": "running"})
                heartbeat = clock+10
            if args.duration_sec > 0 and clock-started >= args.duration_sec:
                break
            time.sleep(0.5)
    except Exception as exc:
        exit_code = 2
        log(f"aggregator failed: {type(exc).__name__}: {exc}")
    finally:
        try:
            if seq and int(seq) > 0:
                xtdata.unsubscribe_quote(seq)
        except Exception:
            pass
        for lane in lanes:
            if not lane.join(10):
                exit_code = 2
        if minute_lane.join():
            try:
                flushes.append(flush_minutes(aggregator, include_open_bars=args.flush_open_bars_on_exit))
            except Exception as exc:
                exit_code = 2
                log(f"final minute flush failed: {exc}")
        flushes.extend(lane.snapshot() for lane in lanes)
        if aggregator.unit_errors and exit_code == 0:
            exit_code = 2
        report = {
            "ok": exit_code == 0,
            "started_at": datetime.fromtimestamp(time.time() - (time.monotonic() - started), SH_TZ).replace(tzinfo=None),
            "finished_at": datetime.now(SH_TZ).replace(tzinfo=None),
            "quote_host": host,
            "quote_port": port,
            "markets": markets,
            "loaded_codes": len(codes),
            "subscribe_seq": seq,
            "received_events": aggregator.received_events,
            "received_codes": len(aggregator.received_codes),
            "last_callback_at": aggregator.last_callback_at,
            "current_open_bars": len(aggregator.current_bars),
            "closed_bars_pending": len(aggregator.closed_bars),
            "volume_field": aggregator.volume_field,
            "minute_unit_contract": MINUTE_UNIT_CONTRACT_ID,
            "unit_errors": dict(aggregator.unit_errors),
            "full_tick_polls": full_tick_polls,
            "cache_seed": cache_seed,
            "flushes": flushes,
            "dry_run": bool(args.dry_run),
        }
        write_report(runtime_path("operations", "intraday_ingestion.json") if not args.codes else Path(args.report).with_suffix(".live.json"),
                     {"generated_at": datetime.now(SH_TZ).isoformat(), "phase": "stopped",
                      "snapshot_target_seconds": args.flush_interval_sec, "lanes": [lane.snapshot() for lane in lanes]})
        write_report(args.report, report)
        log(f"report={args.report} ok={report['ok']} events={report['received_events']} codes={report['received_codes']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Small, read-optimized snapshots for the home-page market sentiment card."""

from __future__ import annotations

from datetime import datetime, timedelta, time as dt_time
from math import isfinite
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from utils.market_warehouse import (
    clickhouse_available,
    clickhouse_client,
    clickhouse_query_df,
    clickhouse_scalar,
    clickhouse_table_exists,
)
from services.market_turnover_forecast import (
    build_fast_intraday_turnover_fallback,
    get_cached_intraday_turnover_forecast,
    get_intraday_turnover_forecast,
)
from utils.logger import get_logger


TABLE = "market_sentiment_snapshot"
SH_TZ = ZoneInfo("Asia/Shanghai")
CONFIRMED_MARKETS = ("SH", "SZ", "BJ")
MIN_CONFIRMED_MARKET_COVERAGE = 0.90
logger = get_logger("market_sentiment_cache")

_COLUMNS = [
    "trade_date", "snapshot_at", "is_provisional", "covered_count", "expected_count",
    "up_count", "down_count", "unchanged_count", "up_5_count", "down_5_count",
    "limit_up_count", "limit_down_count", "avg_change_percent", "total_amount",
    "sh_amount", "sz_amount", "bj_amount", "bucket_up_7", "bucket_up_5_7",
    "bucket_up_3_5", "bucket_up_0_3", "bucket_zero", "bucket_down_0_3",
    "bucket_down_3_5", "bucket_down_5_7", "bucket_down_7", "source",
]


def _ready() -> bool:
    return clickhouse_available() and clickhouse_table_exists(TABLE)


def _as_int(row: Any, key: str) -> int:
    return int(row.get(key) or 0)


def _as_float(row: Any, key: str) -> float:
    value = float(row.get(key) or 0.0)
    # QMT daily rows can contain NaN amount fields for a small number of
    # suspended/invalid records.  A single NaN poisons ClickHouse ``sum`` and
    # then makes FastAPI reject the whole homepage JSON response.
    return value if isfinite(value) else 0.0


def _row_tuple(trade_date: Any, row: Any, *, provisional: bool, expected_count: int, source: str) -> tuple:
    now = datetime.now(SH_TZ).replace(tzinfo=None)
    return (
        trade_date, now, int(provisional), _as_int(row, "covered_count") or expected_count, expected_count,
        _as_int(row, "up_count"), _as_int(row, "down_count"), _as_int(row, "unchanged_count"),
        _as_int(row, "up_5_count"), _as_int(row, "down_5_count"),
        _as_int(row, "limit_up_count"), _as_int(row, "limit_down_count"),
        _as_float(row, "avg_change_percent"), _as_float(row, "total_amount"),
        _as_float(row, "sh_amount"), _as_float(row, "sz_amount"), _as_float(row, "bj_amount"),
        _as_int(row, "bucket_up_7"), _as_int(row, "bucket_up_5_7"), _as_int(row, "bucket_up_3_5"),
        _as_int(row, "bucket_up_0_3"), _as_int(row, "bucket_zero"), _as_int(row, "bucket_down_0_3"),
        _as_int(row, "bucket_down_3_5"), _as_int(row, "bucket_down_5_7"), _as_int(row, "bucket_down_7"), source,
    )


def _normalize_series(values: list[int]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    if low == high:
        return [50.0 for _ in values]
    return [round((value - low) / (high - low) * 100, 2) for value in values]


def _confirmed_daily_row_is_complete(row: Any, baseline_by_market: Dict[str, int]) -> bool:
    """Reject a partially written daily batch before it reaches the homepage."""
    for market in CONFIRMED_MARKETS:
        baseline = int(baseline_by_market.get(market) or 0)
        actual = _as_int(row, f"{market.lower()}_covered_count")
        if baseline <= 0 or actual < baseline * MIN_CONFIRMED_MARKET_COVERAGE:
            return False
    return True


def _intraday_frame_is_complete(frame: Any, expected_count: int) -> bool:
    return frame is not None and not frame.empty and _as_int(frame.iloc[0], "covered_count") >= expected_count * MIN_CONFIRMED_MARKET_COVERAGE


def refresh_daily_history(days: int = 30, *, force: bool = False) -> int:
    """Publish only complete daily snapshots; ``force`` also corrects old cache rows."""
    if not _ready():
        return 0
    safe_days = max(1, min(int(days), 60))
    expected_df = clickhouse_query_df(
        """
        SELECT DISTINCT k.trade_date
        FROM kline_daily k JOIN stocks s ON s.code = k.code
        WHERE s.type = 'stock' AND k.open > 0
        ORDER BY k.trade_date DESC LIMIT ?
        """,
        [safe_days],
    )
    if expected_df is None or expected_df.empty:
        return 0
    expected_dates = {str(value)[:10] for value in expected_df["trade_date"].tolist()}
    cached_df = clickhouse_query_df(
        f"""SELECT DISTINCT trade_date FROM {TABLE}
        WHERE is_provisional = 0 AND trade_date IN ({','.join(['?'] * len(expected_dates))})""",
        sorted(expected_dates),
    )
    cached_dates = {str(value)[:10] for value in cached_df.get("trade_date", [])} if cached_df is not None else set()
    # Do not use only row count and max date here: a middle trading-day gap
    # can satisfy both checks and silently disappear from a line chart.
    if not force and expected_dates == cached_dates:
        return 0
    df = clickhouse_query_df(
        """
        WITH dates AS (
            SELECT DISTINCT k.trade_date
            FROM kline_daily k JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock' AND k.open > 0
            ORDER BY k.trade_date DESC LIMIT {days}
        ), base AS (
            SELECT k.trade_date, s.market, k.amount,
                COALESCE(k.change_pct, (k.close - k.open) / NULLIF(k.open, 0) * 100) AS pct
            FROM kline_daily k JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock' AND k.open > 0 AND k.trade_date IN dates
        )
        SELECT trade_date, count() AS covered_count,
            countIf(market = 'SH') AS sh_covered_count,
            countIf(market = 'SZ') AS sz_covered_count,
            countIf(market = 'BJ') AS bj_covered_count,
            sum(pct > 0) AS up_count, sum(pct < 0) AS down_count, sum(pct = 0) AS unchanged_count,
            sum(pct >= 5) AS up_5_count, sum(pct <= -5) AS down_5_count,
            sum(pct >= 9.9) AS limit_up_count, sum(pct <= -9.9) AS limit_down_count,
            avgIf(pct, isFinite(pct)) AS avg_change_percent,
            sumIf(amount, isFinite(amount)) AS total_amount,
            sumIf(amount, market = 'SH' AND isFinite(amount)) AS sh_amount,
            sumIf(amount, market = 'SZ' AND isFinite(amount)) AS sz_amount,
            sumIf(amount, market = 'BJ' AND isFinite(amount)) AS bj_amount,
            sum(pct >= 7) AS bucket_up_7, sum(pct >= 5 AND pct < 7) AS bucket_up_5_7,
            sum(pct >= 3 AND pct < 5) AS bucket_up_3_5, sum(pct > 0 AND pct < 3) AS bucket_up_0_3,
            sum(pct = 0) AS bucket_zero, sum(pct < 0 AND pct > -3) AS bucket_down_0_3,
            sum(pct <= -3 AND pct > -5) AS bucket_down_3_5, sum(pct <= -5 AND pct > -7) AS bucket_down_5_7,
            sum(pct <= -7) AS bucket_down_7
        FROM base GROUP BY trade_date
        """.format(days=safe_days)
    )
    if df is None or df.empty:
        return 0
    expected = int(clickhouse_scalar("SELECT count() FROM stocks WHERE type = 'stock'") or 0)
    records = [item._asdict() for item in df.itertuples(index=False)]
    baseline_by_market = {
        market: max((_as_int(item, f"{market.lower()}_covered_count") for item in records), default=0)
        for market in CONFIRMED_MARKETS
    }
    complete_records = [
        item for item in records
        if _confirmed_daily_row_is_complete(item, baseline_by_market)
    ]
    rejected = len(records) - len(complete_records)
    if rejected:
        logger.warning(
            "market sentiment daily cache rejected incomplete dates: rejected=%s baselines=%s",
            rejected,
            baseline_by_market,
        )
    rows = [
        _row_tuple(item["trade_date"], item, provisional=False, expected_count=expected, source="kline_daily")
        for item in complete_records
    ]
    if not rows:
        return 0
    clickhouse_client().insert(TABLE, rows, column_names=_COLUMNS)
    return len(rows)


def refresh_intraday_snapshot(*, finalize_if_closed: bool = False) -> bool:
    """Write one full-market snapshot; never runs in a request.

    The QMT full-push collector writes ``kline_daily_intraday`` as its
    producer-side fact.  Read that compact, per-code snapshot first.  Raw 5m
    bars remain a controlled fallback for the short interval before a newly
    deployed collector has emitted its first daily fact.
    """
    if not _ready():
        return False
    now = datetime.now(SH_TZ)
    trade_date = now.date()
    expected = int(clickhouse_scalar("SELECT count() FROM stocks WHERE type = 'stock'") or 0)
    previous = clickhouse_scalar("SELECT max(trade_date) FROM kline_daily WHERE trade_date < ?", [trade_date])
    if expected <= 0 or not previous:
        return False
    # Once the after-close task has published a confirmed row, startup
    # recovery and late intraday jobs must never append a newer provisional
    # row and make the homepage appear stale again.
    if not finalize_if_closed and now.time() >= dt_time(15, 5):
        confirmed = clickhouse_scalar(
            f"SELECT count() FROM {TABLE} WHERE trade_date = ? AND is_provisional = 0",
            [trade_date],
        )
        if int(confirmed or 0) > 0:
            return True
    df = clickhouse_query_df(
        """
        WITH intraday_latest AS (
            SELECT code, argMax(close, snapshot_at) AS close,
                argMax(previous_close, snapshot_at) AS previous_close,
                argMax(change_pct, snapshot_at) AS change_pct,
                argMax(amount, snapshot_at) AS amount,
                max(snapshot_at) AS as_of
            FROM kline_daily_intraday FINAL
            WHERE trade_date = ?
            GROUP BY code
        ), base AS (
            SELECT s.market, i.close, i.as_of, i.previous_close, i.amount,
                COALESCE(i.change_pct, (i.close - i.previous_close) / nullIf(i.previous_close, 0) * 100) AS pct
            FROM stocks AS s FINAL INNER JOIN intraday_latest i ON i.code = s.code
            WHERE s.type = 'stock' AND i.close > 0 AND i.previous_close > 0
        )
        SELECT count() AS covered_count, max(as_of) AS as_of, sum(pct > 0) AS up_count, sum(pct < 0) AS down_count,
            sum(pct = 0) AS unchanged_count, sum(pct >= 5) AS up_5_count, sum(pct <= -5) AS down_5_count,
            sum(pct >= 9.9) AS limit_up_count, sum(pct <= -9.9) AS limit_down_count, avgIf(pct, isFinite(pct)) AS avg_change_percent,
            sumIf(amount, isFinite(amount)) AS total_amount, sumIf(amount, market = 'SH' AND isFinite(amount)) AS sh_amount,
            sumIf(amount, market = 'SZ' AND isFinite(amount)) AS sz_amount, sumIf(amount, market = 'BJ' AND isFinite(amount)) AS bj_amount,
            sum(pct >= 7) AS bucket_up_7, sum(pct >= 5 AND pct < 7) AS bucket_up_5_7,
            sum(pct >= 3 AND pct < 5) AS bucket_up_3_5, sum(pct > 0 AND pct < 3) AS bucket_up_0_3,
            sum(pct = 0) AS bucket_zero, sum(pct < 0 AND pct > -3) AS bucket_down_0_3,
            sum(pct <= -3 AND pct > -5) AS bucket_down_3_5, sum(pct <= -5 AND pct > -7) AS bucket_down_5_7,
            sum(pct <= -7) AS bucket_down_7
        FROM base
        """, [trade_date]
    )
    source = "qmt:kline_daily_intraday"
    # A newly started collector has no fact before its first full-market tick.
    # Only then may the backend use the expensive raw-minute fallback.
    using_intraday_fact = _intraday_frame_is_complete(df, expected)
    if not using_intraday_fact:
        df = clickhouse_query_df(
        """
        WITH minute_snapshot AS (
            SELECT code, argMax(close, datetime) AS close, max(datetime) AS as_of,
                sumIf(amount, isFinite(amount)) AS amount
            FROM kline_minute_5
            WHERE toDate(datetime) = ?
            GROUP BY code
        ), previous_close AS (
            SELECT code, argMax(close, trade_date) AS close FROM kline_daily
            WHERE trade_date = ? GROUP BY code
        ), base AS (
            SELECT s.market, m.close, m.as_of, p.close AS previous_close, m.amount,
                (m.close - p.close) / nullIf(p.close, 0) * 100 AS pct
            FROM stocks s INNER JOIN minute_snapshot m ON m.code = s.code
            INNER JOIN previous_close p ON p.code = s.code
            WHERE s.type = 'stock' AND m.close > 0 AND p.close > 0
        )
        SELECT count() AS covered_count, max(as_of) AS as_of, sum(pct > 0) AS up_count, sum(pct < 0) AS down_count,
            sum(pct = 0) AS unchanged_count, sum(pct >= 5) AS up_5_count, sum(pct <= -5) AS down_5_count,
            sum(pct >= 9.9) AS limit_up_count, sum(pct <= -9.9) AS limit_down_count, avg(pct) AS avg_change_percent,
            sum(amount) AS total_amount, sumIf(amount, market = 'SH') AS sh_amount, sumIf(amount, market = 'SZ') AS sz_amount,
            sumIf(amount, market = 'BJ') AS bj_amount, sum(pct >= 7) AS bucket_up_7,
            sum(pct >= 5 AND pct < 7) AS bucket_up_5_7, sum(pct >= 3 AND pct < 5) AS bucket_up_3_5,
            sum(pct > 0 AND pct < 3) AS bucket_up_0_3, sum(pct = 0) AS bucket_zero,
            sum(pct < 0 AND pct > -3) AS bucket_down_0_3, sum(pct <= -3 AND pct > -5) AS bucket_down_3_5,
            sum(pct <= -5 AND pct > -7) AS bucket_down_5_7, sum(pct <= -7) AS bucket_down_7
        FROM base
        """, [trade_date, previous]
        )
        source = "qmt:kline_minute_5"
    if not _intraday_frame_is_complete(df, expected):
        return False
    snapshot = df.iloc[0].to_dict()
    raw_as_of = snapshot.get("as_of")
    final_minute_bar = False
    # No later job may downgrade a completed close back to provisional.  This
    # includes startup recovery, which normally calls the intraday refresher.
    if now.time() >= dt_time(15, 5) and (finalize_if_closed or raw_as_of is not None):
        try:
            business_as_of = raw_as_of.to_pydatetime().replace(tzinfo=None) if raw_as_of is not None else now.replace(tzinfo=None)
            # Legacy minute bars have an eight-hour persisted-clock offset;
            # producer-side daily facts use an Asia/Shanghai snapshot time.
            if not using_intraday_fact:
                business_as_of -= timedelta(hours=8)
            final_minute_bar = finalize_if_closed or business_as_of.time().hour >= 15
        except Exception:
            final_minute_bar = False
    source = f"{source}:after_close_final" if final_minute_bar else source
    clickhouse_client().insert(
        TABLE,
        [_row_tuple(trade_date, snapshot, provisional=not final_minute_bar, expected_count=expected, source=source)],
        column_names=_COLUMNS,
    )
    if not final_minute_bar:
        get_intraday_turnover_forecast(current_snapshot={
            "as_of": raw_as_of,
            "total_turnover": snapshot.get("total_amount"),
            "sh_amount": snapshot.get("sh_amount"),
            "sz_amount": snapshot.get("sz_amount"),
            "bj_amount": snapshot.get("bj_amount"),
            "covered_count": snapshot.get("covered_count"),
        })
    return True


def load_homepage_sentiment() -> Optional[Dict[str, Any]]:
    """Return only tiny cached rows for the homepage, or ``None`` during bootstrap."""
    if not _ready():
        return None
    df = clickhouse_query_df(
        f"""SELECT trade_date, argMax(is_provisional, snapshot_at) AS is_provisional,
            argMax(covered_count, snapshot_at) AS covered_count, argMax(expected_count, snapshot_at) AS expected_count,
            argMax(up_count, snapshot_at) AS up_count, argMax(down_count, snapshot_at) AS down_count,
            argMax(unchanged_count, snapshot_at) AS unchanged_count, argMax(up_5_count, snapshot_at) AS up_5_count,
            argMax(down_5_count, snapshot_at) AS down_5_count, argMax(limit_up_count, snapshot_at) AS limit_up_count,
            argMax(limit_down_count, snapshot_at) AS limit_down_count, argMax(avg_change_percent, snapshot_at) AS avg_change_percent,
            argMax(total_amount, snapshot_at) AS total_amount, argMax(sh_amount, snapshot_at) AS sh_amount,
            argMax(sz_amount, snapshot_at) AS sz_amount, argMax(bj_amount, snapshot_at) AS bj_amount,
            argMax(bucket_up_7, snapshot_at) AS bucket_up_7, argMax(bucket_up_5_7, snapshot_at) AS bucket_up_5_7,
            argMax(bucket_up_3_5, snapshot_at) AS bucket_up_3_5, argMax(bucket_up_0_3, snapshot_at) AS bucket_up_0_3,
            argMax(bucket_zero, snapshot_at) AS bucket_zero, argMax(bucket_down_0_3, snapshot_at) AS bucket_down_0_3,
            argMax(bucket_down_3_5, snapshot_at) AS bucket_down_3_5, argMax(bucket_down_5_7, snapshot_at) AS bucket_down_5_7,
            argMax(bucket_down_7, snapshot_at) AS bucket_down_7, argMax(source, snapshot_at) AS source
        FROM {TABLE} GROUP BY trade_date ORDER BY trade_date DESC LIMIT 31"""
    )
    if df is None or len(df) < 30:
        return None
    rows = list(reversed([item._asdict() for item in df.itertuples(index=False)]))
    latest = rows[-1]
    total = _as_int(latest, "up_count") + _as_int(latest, "down_count") + _as_int(latest, "unchanged_count")
    score = 50 + (_as_int(latest, "up_count") - _as_int(latest, "down_count")) / total * 50 if total else 50
    dates = [str(item["trade_date"])[:10] for item in rows]
    keys = ("up_count", "down_count", "limit_up_count", "limit_down_count", "up_5_count", "down_5_count")
    raw = {f"{key}_series": [_as_int(item, key) for item in rows] for key in keys}
    normalized = {key: _normalize_series(value) for key, value in raw.items()}
    distribution = [(">=7", "bucket_up_7", "up"), ("5~7", "bucket_up_5_7", "up"), ("3~5", "bucket_up_3_5", "up"), ("0~3", "bucket_up_0_3", "up"), ("0", "bucket_zero", "flat"), ("-3~0", "bucket_down_0_3", "down"), ("-5~-3", "bucket_down_3_5", "down"), ("-7~-5", "bucket_down_5_7", "down"), ("<=-7", "bucket_down_7", "down")]
    forecast = get_cached_intraday_turnover_forecast()
    if not _as_int(latest, "is_provisional"):
        forecast = {"available": False, "reason": "当日成交额已收盘确认", "source": latest.get("source")}
    if not forecast.get("available") and _as_int(latest, "is_provisional"):
        forecast = build_fast_intraday_turnover_fallback({
            "total": _as_float(latest, "total_amount"), "sh": _as_float(latest, "sh_amount"),
            "sz": _as_float(latest, "sz_amount"), "bj": _as_float(latest, "bj_amount"),
        })
    return {"date": dates[-1], "up_count": _as_int(latest, "up_count"), "down_count": _as_int(latest, "down_count"), "unchanged_count": _as_int(latest, "unchanged_count"), "up_5_percent_count": _as_int(latest, "up_5_count"), "down_5_percent_count": _as_int(latest, "down_5_count"), "limit_up_count": _as_int(latest, "limit_up_count"), "limit_down_count": _as_int(latest, "limit_down_count"), "avg_change_percent": round(_as_float(latest, "avg_change_percent"), 3), "total_turnover": _as_float(latest, "total_amount"), "sh_amount": _as_float(latest, "sh_amount"), "sz_amount": _as_float(latest, "sz_amount"), "turnover_source": "market_sentiment_snapshot", "change_distribution": [{"label": label, "count": _as_int(latest, key), "side": side} for label, key, side in distribution], "up_down_5_trend_7d": {"trade_dates": dates[-7:], "up_5_counts": raw["up_5_count_series"][-7:], "down_5_counts": raw["down_5_count_series"][-7:], "latest_date": dates[-1]}, "emotion_curve_30d": {"trade_dates": dates[-30:], "raw_series": {key: value[-30:] for key, value in raw.items()}, "normalized_series": {key: value[-30:] for key, value in normalized.items()}, "latest_snapshot": {key: _as_int(latest, key) for key in keys}, "latest_date": dates[-1]}, "turnover_trend_30d": {"trade_dates": dates[-30:], "amounts": [_as_float(item, "total_amount") for item in rows[-30:]], "sh_amounts": [_as_float(item, "sh_amount") for item in rows[-30:]], "sz_amounts": [_as_float(item, "sz_amount") for item in rows[-30:]], "bj_amounts": [_as_float(item, "bj_amount") for item in rows[-30:]], "threshold_amount": 2_000_000_000_000, "unit": "CNY", "latest_date": dates[-1], "latest_is_provisional": bool(_as_int(latest, "is_provisional"))}, "turnover_forecast_intraday": forecast, "sentiment_score": round(score, 2), "source": "market_sentiment_snapshot", "intraday_status": {"mode": "provisional_5m", "covered_count": _as_int(latest, "covered_count"), "expected_count": _as_int(latest, "expected_count"), "coverage_ratio": round(_as_int(latest, "covered_count") / _as_int(latest, "expected_count"), 4) if _as_int(latest, "expected_count") else 0.0} if _as_int(latest, "is_provisional") else None}

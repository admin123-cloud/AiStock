"""Intraday full-market turnover forecast based on QMT 5-minute bars.

The forecast deliberately aggregates the stock universe, never broker accounts.
Different broker QMT feeds are therefore alternative market-data sources for
validation/failover, not values which can be added together.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict
import time
from zoneinfo import ZoneInfo

import pandas as pd

from utils.market_warehouse import clickhouse_available, clickhouse_query_df, clickhouse_table_exists
from utils.paths import runtime_path


SH_TZ = ZoneInfo("Asia/Shanghai")
MIN_HISTORY_DAYS = 20
HISTORY_DAYS = 60
_CACHE: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
_PERSISTED_FORECAST_PATH = runtime_path("market_turnover_forecast", "latest.json")


def _is_current_trading_day(payload: Any) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("available")
        and str(payload.get("trade_date") or "") == datetime.now(SH_TZ).date().isoformat()
    )


def _persist(payload: Dict[str, Any]) -> None:
    if not _is_current_trading_day(payload):
        return
    try:
        _PERSISTED_FORECAST_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = _PERSISTED_FORECAST_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(_PERSISTED_FORECAST_PATH)
    except Exception:
        # Forecast persistence must never make the real-time path unavailable.
        pass


def _load_persisted() -> Dict[str, Any] | None:
    try:
        payload = json.loads(_PERSISTED_FORECAST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if _is_current_trading_day(payload) else None


def get_cached_intraday_turnover_forecast() -> Dict[str, Any]:
    """Return a fresh forecast, or the last same-day value while a refresh runs."""
    payload = _CACHE.get("payload")
    if payload is not None and time.monotonic() < float(_CACHE.get("expires_at") or 0):
        return payload
    if _is_current_trading_day(payload):
        return {**payload, "stale": True, "stale_reason": "等待下一次5分钟快照刷新"}
    persisted = _load_persisted()
    if persisted is not None:
        _CACHE["payload"] = persisted
        _CACHE["expires_at"] = time.monotonic() + 60
        return {**persisted, "stale": True, "stale_reason": "服务重启后已恢复最近一次预测"}
    return _empty("盘中成交额预测等待下一次5分钟快照刷新")


def _empty(reason: str, *, as_of: str | None = None) -> Dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "as_of": as_of,
        "source": "qmt:kline_minute_5",
        "refresh_interval_minutes": 5,
        "aggregation_scope": "全市场股票成交额（非券商账户成交额）",
    }


def _market_amounts(row: pd.Series) -> Dict[str, float]:
    return {
        "total": float(row.get("total_amount") or 0.0),
        "sh": float(row.get("sh_amount") or 0.0),
        "sz": float(row.get("sz_amount") or 0.0),
        "bj": float(row.get("bj_amount") or 0.0),
    }


def build_fast_intraday_turnover_fallback(current_amounts: Dict[str, Any]) -> Dict[str, Any]:
    """Always show a bounded estimate while the data-driven profile is warming up."""
    now = datetime.now(SH_TZ)
    minutes = now.hour * 60 + now.minute
    anchors = ((9 * 60 + 35, 0.08), (9 * 60 + 45, 0.21), (10 * 60, 0.28),
               (10 * 60 + 30, 0.40), (11 * 60, 0.52), (11 * 60 + 30, 0.61),
               (13 * 60 + 5, 0.63), (14 * 60, 0.78), (14 * 60 + 30, 0.88), (15 * 60, 1.0))
    ratio = anchors[0][1]
    for start, value in anchors:
        if minutes >= start:
            ratio = value
        else:
            break
    amounts = {key: float(current_amounts.get(key) or 0.0) for key in ("total", "sh", "sz", "bj")}
    if amounts["total"] <= 0:
        return _empty("当前交易日成交额为空")
    forecast = amounts["total"] / ratio
    return {
        "available": True,
        "trade_date": now.date().isoformat(),
        "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
        "time_bucket": now.strftime("%H:%M"),
        "source": "qmt:kline_minute_5:profile_fallback",
        "refresh_interval_minutes": 5,
        "aggregation_scope": "全市场股票成交额（非券商账户成交额）",
        "current_amounts": amounts,
        "forecast_amount": forecast,
        "forecast_interval": {"lower": forecast * 0.85, "upper": forecast * 1.15},
        "completion_ratio": {"p25": ratio * 0.93, "p50": ratio, "p75": ratio * 1.07},
        "sample_days": 0,
        "coverage": {"code_count": 0, "reference_code_count": 0, "ratio": 0.0},
        "confidence": 0.45,
        "fallback": True,
        "multi_broker_policy": "多券商行情源用于一致性校验和故障切换，不叠加成交额。",
    }


def get_intraday_turnover_forecast(
    *,
    current_snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Estimate today's close turnover from the historical same-time profile.

    For every completed historical trading day, calculate the share of its
    turnover that had occurred by the latest completed 5-minute bucket today.
    P25/P50/P75 of that share give a robust forecast interval without looking
    at today's close.
    """
    cached = _CACHE.get("payload")
    if cached is not None and time.monotonic() < float(_CACHE.get("expires_at") or 0):
        return cached

    def finish(payload: Dict[str, Any]) -> Dict[str, Any]:
        _CACHE["payload"] = payload
        # The minute source is refreshed on a five-minute cadence; caching avoids
        # expensive repeated history scans when several users open the homepage.
        _CACHE["expires_at"] = time.monotonic() + 240
        _persist(payload)
        return payload

    if not clickhouse_available() or not clickhouse_table_exists("kline_minute_5"):
        return finish(_empty("5分钟行情表不可用"))

    today = datetime.now(SH_TZ).date()
    try:
        from scheduler.trading_calendar import TradingCalendar

        if not TradingCalendar.is_trading_day(datetime.now(SH_TZ)):
            return finish(_empty("非交易日，不生成盘中成交额预测"))
    except Exception:
        # Calendar availability must not make the homepage endpoint fail.
        pass
    if current_snapshot:
        # QMT minute timestamps in ClickHouse are stored as a naive value
        # shifted by +8h.  Convert them back to the Asia/Shanghai trading
        # clock before matching historical same-time progress.
        as_of = pd.Timestamp(current_snapshot.get("as_of")).to_pydatetime() - timedelta(hours=8)
        current_amounts = {
            "total": float(current_snapshot.get("total_turnover") or 0.0),
            "sh": float(current_snapshot.get("sh_amount") or 0.0),
            "sz": float(current_snapshot.get("sz_amount") or 0.0),
            "bj": float(current_snapshot.get("bj_amount") or 0.0),
        }
        code_count = int(current_snapshot.get("covered_count") or 0)
    else:
        try:
            current_df = clickhouse_query_df(
                """
                WITH current_rows AS (
                    SELECT k.code, k.datetime, k.amount, s.market
                    FROM kline_minute_5 AS k FINAL
                    INNER JOIN stocks AS s ON s.code = k.code
                    WHERE toDate(k.datetime) = ?
                      AND s.type = 'stock'
                      AND k.amount > 0
                      AND ((toHour(k.datetime - INTERVAL 8 HOUR) BETWEEN 9 AND 14)
                        OR (toHour(k.datetime - INTERVAL 8 HOUR) = 15 AND toMinute(k.datetime - INTERVAL 8 HOUR) = 0))
                ), latest AS (SELECT max(datetime) AS as_of FROM current_rows)
                SELECT
                    max(r.datetime) AS as_of,
                    countDistinct(r.code) AS code_count,
                    sum(r.amount) AS total_amount,
                    sumIf(r.amount, r.market = 'SH') AS sh_amount,
                    sumIf(r.amount, r.market = 'SZ') AS sz_amount,
                    sumIf(r.amount, r.market = 'BJ') AS bj_amount
                FROM current_rows AS r
                CROSS JOIN latest
                WHERE r.datetime <= latest.as_of
                """,
                [today],
            )
        except Exception:
            return finish(_empty("盘中5分钟行情查询失败"))
        if current_df is None or current_df.empty or pd.isna(current_df.iloc[0].get("as_of")):
            return finish(_empty("当前交易日尚无全市场5分钟行情"))

        current = current_df.iloc[0]
        as_of = pd.Timestamp(current["as_of"]).to_pydatetime() - timedelta(hours=8)
        current_amounts = _market_amounts(current)
        code_count = int(current.get("code_count") or 0)

    time_text = as_of.strftime("%H:%M:%S")
    if current_amounts["total"] <= 0 or code_count <= 0:
        return finish(_empty("当前交易日成交额为空", as_of=as_of.strftime("%Y-%m-%d %H:%M:%S")))

    try:
        history_df = clickhouse_query_df(
            """
            WITH daily AS (
                SELECT
                    toDate(k.datetime) AS trade_date,
                    countDistinct(k.code) AS code_count,
                    sum(k.amount) AS day_total,
                    sumIf(k.amount, formatDateTime(k.datetime - INTERVAL 8 HOUR, '%%H:%%i:%%s') <= ?) AS cumulative_at_time
                FROM kline_minute_5 AS k
                INNER JOIN stocks AS s ON s.code = k.code
                WHERE s.type = 'stock'
                  AND toDate(k.datetime) < ?
                  AND toDate(k.datetime) >= subtractDays(?, ?)
                  AND k.amount > 0
                  AND ((toHour(k.datetime - INTERVAL 8 HOUR) BETWEEN 9 AND 14)
                    OR (toHour(k.datetime - INTERVAL 8 HOUR) = 15 AND toMinute(k.datetime - INTERVAL 8 HOUR) = 0))
                GROUP BY trade_date
            )
            SELECT trade_date, code_count, day_total, cumulative_at_time,
                cumulative_at_time / nullIf(day_total, 0) AS completion_ratio
            FROM daily
            WHERE day_total > 0 AND cumulative_at_time > 0
            ORDER BY trade_date DESC
            """,
            [time_text, today, today, HISTORY_DAYS],
        )
    except Exception as exc:
        return finish(_empty(f"历史成交进度查询失败：{exc}", as_of=as_of.strftime("%Y-%m-%d %H:%M:%S")))

    if history_df is None or len(history_df) < MIN_HISTORY_DAYS:
        return finish(_empty(
            f"历史5分钟成交进度样本不足（需要{MIN_HISTORY_DAYS}日）",
            as_of=as_of.strftime("%Y-%m-%d %H:%M:%S"),
        ))

    ratios = pd.to_numeric(history_df["completion_ratio"], errors="coerce").dropna()
    ratios = ratios[(ratios > 0.02) & (ratios <= 1.05)]
    if len(ratios) < MIN_HISTORY_DAYS:
        return finish(_empty("有效历史成交进度样本不足", as_of=as_of.strftime("%Y-%m-%d %H:%M:%S")))

    p25, p50, p75 = (float(ratios.quantile(q)) for q in (0.25, 0.50, 0.75))
    if p25 <= 0 or p50 <= 0 or p75 <= 0:
        return finish(_empty("历史成交进度无效", as_of=as_of.strftime("%Y-%m-%d %H:%M:%S")))

    median_code_count = float(pd.to_numeric(history_df["code_count"], errors="coerce").median() or 0)
    coverage = min(1.0, code_count / median_code_count) if median_code_count else 0.0
    dispersion = max(0.0, p75 - p25)
    confidence = max(0.0, min(1.0, coverage * (1.0 - min(0.55, dispersion))))
    forecast = current_amounts["total"] / p50
    lower = current_amounts["total"] / p75
    upper = current_amounts["total"] / p25

    return finish({
        "available": True,
        "trade_date": today.isoformat(),
        "as_of": as_of.strftime("%Y-%m-%d %H:%M:%S"),
        "time_bucket": time_text[:5],
        "source": "qmt:kline_minute_5",
        "refresh_interval_minutes": 5,
        "aggregation_scope": "全市场股票成交额（非券商账户成交额）",
        "current_amounts": current_amounts,
        "forecast_amount": forecast,
        "forecast_interval": {"lower": lower, "upper": upper},
        "completion_ratio": {"p25": p25, "p50": p50, "p75": p75},
        "sample_days": int(len(ratios)),
        "coverage": {"code_count": code_count, "reference_code_count": round(median_code_count), "ratio": coverage},
        "confidence": confidence,
        "multi_broker_policy": "多券商行情源用于一致性校验和故障切换，不叠加成交额。",
    })

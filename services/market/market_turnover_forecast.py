"""Intraday full-market turnover forecast based on QMT 5-minute bars.

The forecast deliberately aggregates the stock universe, never broker accounts.
Different broker QMT feeds are therefore alternative market-data sources for
validation/failover, not values which can be added together.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
import time
from zoneinfo import ZoneInfo

import pandas as pd

from utils.market_warehouse import clickhouse_available, clickhouse_query_df, clickhouse_table_exists
from utils.paths import runtime_path


logger = logging.getLogger(__name__)
SH_TZ = ZoneInfo("Asia/Shanghai")
MIN_HISTORY_DAYS = 20
HISTORY_DAYS = 60
_SUCCESS_CACHE_TTL_SECONDS = 240
_ERROR_CACHE_TTL_SECONDS = 30
_CACHE: Dict[str, Any] = {"expires_at": 0.0, "payload": None}
_REFRESH_LOCK = threading.Lock()
_PERSISTED_FORECAST_PATH = runtime_path("market_turnover_forecast", "latest.json")


def _now_bj() -> datetime:
    return datetime.now(SH_TZ)


def _is_current_trading_day(payload: Any) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("available")
        and str(payload.get("trade_date") or "") == _now_bj().date().isoformat()
    )


def _persist(payload: Dict[str, Any]) -> None:
    if not _is_current_trading_day(payload):
        return
    try:
        from services.intraday_phase_gate import is_before_postclose_cutoff

        if not is_before_postclose_cutoff(_now_bj()):
            # S4 硬截断：15:05:00 起禁止再把盘中成交额预测落盘为当天缓存
            return
    except Exception:
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


_POSTCLOSE_CONTROL_ROOT: Optional[Path] = None


def _is_postclose_rebuilding(today: date) -> bool:
    try:
        from services.postclose_publication import PublicationStore

        return PublicationStore(_POSTCLOSE_CONTROL_ROOT).is_rebuilding(today)
    except Exception:
        return False


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
    # S9 发布代际门控：必须位于暖缓存命中之前，重建期绝不返回旧缓存或混代数据
    if _is_postclose_rebuilding(_now_bj().date()):
        return _empty("盘后重建中（not_ready），暂不提供盘中成交额预测")

    cached = _CACHE.get("payload")
    if cached is not None and time.monotonic() < float(_CACHE.get("expires_at") or 0):
        return cached

    with _REFRESH_LOCK:
        # 进程内单飞保护：并发请求缓存过期时只允许首个线程打向 ClickHouse，
        # 避免多个 60 日重查询叠加触发 ClickHouse total memory limit (241)。
        cached = _CACHE.get("payload")
        if cached is not None and time.monotonic() < float(_CACHE.get("expires_at") or 0):
            return cached
        return _compute_intraday_turnover_forecast(current_snapshot=current_snapshot)


def _compute_intraday_turnover_forecast(
    *,
    current_snapshot: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    def finish(payload: Dict[str, Any]) -> Dict[str, Any]:
        _CACHE["payload"] = payload
        # 成功预测缓存 4 分钟（与 5 分钟快照节奏对齐）；失败态仅短缓存 30 秒，
        # 既防雪崩重试，也避免瞬时波动令前端长时间挂错。
        ttl = _SUCCESS_CACHE_TTL_SECONDS if payload.get("available") else _ERROR_CACHE_TTL_SECONDS
        _CACHE["expires_at"] = time.monotonic() + ttl
        _persist(payload)
        return payload

    if not clickhouse_available() or not clickhouse_table_exists("kline_minute_5"):
        return finish(_empty("5分钟行情表不可用"))

    now_bj = _now_bj()
    today = now_bj.date()
    try:
        from scheduler.trading_calendar import TradingCalendar

        cal_res = TradingCalendar.resolve_trading_day(now_bj)
        cal_state = getattr(cal_res, "state", "unknown")
        if cal_state == "closed":
            return finish(_empty("非交易日，不生成盘中成交额预测"))
        if cal_state != "trading":
            return finish(_empty("交易日历未知（fail-closed），不生成盘中成交额预测"))
    except Exception:
        # 三态日历异常或不可用时严格 fail-closed，拒绝生成并落盘预测
        return finish(_empty("交易日历不可用（fail-closed），不生成盘中成交额预测"))
    if current_snapshot:
        # R27b：kline_minute_5 datetime 已标注 Asia/Shanghai，as_of 读回即
        # 北京墙钟（aware），规整为 naive 北京墙后再做同刻进度匹配。
        as_of_dt = pd.Timestamp(current_snapshot.get("as_of")).to_pydatetime()
        as_of = (
            as_of_dt.astimezone(SH_TZ).replace(tzinfo=None)
            if as_of_dt.tzinfo is not None
            else as_of_dt
        )
        current_amounts = {
            "total": float(current_snapshot.get("total_turnover") or 0.0),
            "sh": float(current_snapshot.get("sh_amount") or 0.0),
            "sz": float(current_snapshot.get("sz_amount") or 0.0),
            "bj": float(current_snapshot.get("bj_amount") or 0.0),
        }
        code_count = int(current_snapshot.get("covered_count") or 0)
    else:
        try:
            # R23（2026-11-09，任务书 20261109_samefamily_fixes_r23）：raw 基表
            # 直查 + 内层 (code, datetime) 级 argMax(col, created_at) 等价去重
            # （镜像 R20b/R14 三件套）。原版经视图网关重写为无界视图（真库实测
            # 冷启 55GiB/126~223s），toDate 谓词对 toYYYYMM 分区亦不裁剪；改
            # datetime 范围谓词后只在当日分区内去重一次。
            current_df = clickhouse_query_df(
                """
                WITH current_rows AS (
                    SELECT
                        k.code AS code,
                        k.datetime AS bar_time,
                        argMax(k.amount, k.created_at) AS amount,
                        any(s.market) AS market
                    FROM kline_minute_5 AS k
                    INNER JOIN stocks AS s ON s.code = k.code
                    WHERE k.datetime >= ? AND k.datetime < ? + INTERVAL 1 DAY
                      AND s.type = 'stock'
                      AND k.amount > 0
                      AND toHour(k.datetime) BETWEEN 9 AND 15
                    GROUP BY k.code, k.datetime
                ), latest AS (SELECT max(bar_time) AS as_of FROM current_rows)
                SELECT
                    max(r.bar_time) AS as_of,
                    countDistinct(r.code) AS code_count,
                    sum(r.amount) AS total_amount,
                    sumIf(r.amount, r.market = 'SH') AS sh_amount,
                    sumIf(r.amount, r.market = 'SZ') AS sz_amount,
                    sumIf(r.amount, r.market = 'BJ') AS bj_amount
                FROM current_rows AS r
                CROSS JOIN latest
                WHERE r.bar_time <= latest.as_of
                SETTINGS max_bytes_before_external_group_by = 268435456
                """,
                [today, today],
                raw=True,
            )
        except Exception:
            return finish(_empty("盘中5分钟行情查询失败"))
        if current_df is None or current_df.empty or pd.isna(current_df.iloc[0].get("as_of")):
            return finish(_empty("当前交易日尚无全市场5分钟行情"))

        current = current_df.iloc[0]
        as_of_raw = pd.Timestamp(current["as_of"]).to_pydatetime()
        as_of = (
            as_of_raw.astimezone(SH_TZ).replace(tzinfo=None)
            if as_of_raw.tzinfo is not None
            else as_of_raw
        )
        current_amounts = _market_amounts(current)
        code_count = int(current.get("code_count") or 0)

    time_text = as_of.strftime("%H:%M:%S")
    if current_amounts["total"] <= 0 or code_count <= 0:
        return finish(_empty("当前交易日成交额为空", as_of=as_of.strftime("%Y-%m-%d %H:%M:%S")))

    try:
        # R23 + 2026-11-26 OOM 根治：
        # 1) 保留 raw 基表直查、datetime 范围分区裁剪与 (code, datetime) argMax 去重；
        # 2) 用 IN 子查询替代 INNER JOIN stocks，保留 kline_minute_5 的物理
        #    ORDER BY (code, datetime) 顺序；
        # 3) 开启 optimize_aggregation_in_order=1 流式顺序聚合 + 256MiB 外部归并兜底，
        #    将 60 日千万行聚合内存由 >1.88GiB（并发易触发 10GiB MEMORY_LIMIT_EXCEEDED）
        #    降至 ~300MiB 零落盘。
        start_date = today - timedelta(days=HISTORY_DAYS)
        history_df = clickhouse_query_df(
            """
            WITH daily AS (
                SELECT
                    toDate(b.bar_time) AS trade_date,
                    countDistinct(b.code) AS code_count,
                    sum(b.amount) AS day_total,
                    sumIf(b.amount, formatDateTime(b.bar_time, '%%H:%%i:%%s') <= ?) AS cumulative_at_time
                FROM (
                    SELECT
                        k.code AS code,
                        k.datetime AS bar_time,
                        argMax(k.amount, k.created_at) AS amount
                    FROM kline_minute_5 AS k
                    WHERE k.code IN (SELECT code FROM stocks WHERE type = 'stock')
                      AND k.datetime >= ? AND k.datetime < ?
                      AND k.amount > 0
                      AND toHour(k.datetime) BETWEEN 9 AND 15
                    GROUP BY k.code, k.datetime
                ) AS b
                GROUP BY trade_date
            )
            SELECT trade_date, code_count, day_total, cumulative_at_time,
                cumulative_at_time / nullIf(day_total, 0) AS completion_ratio
            FROM daily
            WHERE day_total > 0 AND cumulative_at_time > 0
            ORDER BY trade_date DESC
            SETTINGS optimize_aggregation_in_order = 1, max_bytes_before_external_group_by = 268435456
            """,
            [time_text, start_date, today],
            raw=True,
        )
    except Exception as exc:
        logger.error("历史成交进度查询失败: %s", exc, exc_info=True)
        reason_detail = str(exc).split(" (for url ", 1)[0].strip()
        return finish(_empty(f"历史成交进度查询失败：{reason_detail}", as_of=as_of.strftime("%Y-%m-%d %H:%M:%S")))

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

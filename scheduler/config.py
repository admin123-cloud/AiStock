"""Scheduler configuration."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional


SCHEDULER_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "jobs": {
        "full_sync_kline": {
            "enabled": True,
            "cron_expression": "0 1 * * 6",
            "description": "Weekly full sync for all kline periods",
            "timeout": 7200,
        },
        "daily_kline_pipeline": {
            "enabled": True,
            "time": "16:15",
            "timezone": "Asia/Shanghai",
            "description": "Daily post-close kline pipeline (16:15 Asia/Shanghai)",
        },
        "realtime_quotes": {
            "enabled": False,
            "description": "Realtime watchlist quote refresh during trading hours",
            "trading_days_only": True,
            "trading_hours": {"start": "09:30", "end": "15:00"},
            "interval": 60,
        },
        "realtime_indices": {
            "enabled": True,
            "description": "Realtime major indices refresh during trading hours",
            "trading_days_only": True,
            "trading_hours": {"start": "09:30", "end": "15:00"},
            "interval": 120,
        },
        "intraday_sentiment": {
            "enabled": True,
            "description": "Intraday market sentiment refresh and post-close consolidation",
            "trading_days_only": True,
            "trading_hours": {"start": "09:30", "end": "18:01"},
            "interval": 120,
        },
        "minute_kline": {
            "enabled": True,
            "description": "Fetch minute-kline snapshots for tracked symbols",
            "trading_days_only": True,
            "trading_hours": {"start": "09:30", "end": "15:00"},
            "interval": 900,
        },
        "cls_news": {
            "enabled": True,
            "description": "Hourly CLS telegraph news refresh and event signal scoring",
            "trading_days_only": False,
            "interval": 3600,
            "pages": 1,
            "rn": 50,
        },
        "t_trade_monitor": {
            "enabled": True,
            "description": "L2做T盘中监控：东财实时行情30s轮询+RSI计算+信号→弹窗→下单",
            "trading_days_only": True,
            "trading_hours": {"start": "09:25", "end": "15:00"},
            "interval": 30,
            "hearbeats_per_cron": 60,
        },
    },
    "sync": {
        "kline": {
            "periods": ["1d", "1w", "1M", "1Q", "1Y"],
            "count": 5000,
            "adjust": "forward",
            "batch_size": 50,
            "delay_between_batches": 1.0,
        },
        "minute_kline": {
            "batch_size": 200,
            "chunk_days": 2,
            "memory_limit_gb": 8,
            "hard_memory_limit_gb": 12,
            "delay_between_batches": 2.0,
            "delay_between_stocks": 0.5,
        },
        "quotes": {
            "max_retries": 3,
            "timeout": 10,
        },
    },
    "notification": {
        "enabled": False,
        "email": {
            "enabled": False,
            "recipients": [],
        },
    },
}


# Asia/Shanghai has kept a fixed +08:00 offset since 1991 (no DST); a static
# timezone keeps the policy independent of the tzdata database.
_BEIJING_TZINFO = timezone(timedelta(hours=8), "Asia/Shanghai")
_DAILY_KLINE_PIPELINE_DUE_TIME = time(16, 15)


def daily_kline_pipeline_due(
    now: datetime,
    last_successful_run_date: Optional[date],
) -> bool:
    """Return True when the daily kline pipeline is due at ``now``.

    Owner-approved policy (2026-11-05):

    * ``now`` must be an aware datetime; naive input raises ``ValueError``.
    * ``now`` is evaluated in Asia/Shanghai local time: the pipeline is due
      from 16:15 (inclusive) onwards on every natural calendar day.
    * Once a run has succeeded on the current Beijing calendar date it is
      not due again that day.

    There is deliberately no trading-day/weekend/holiday filtering here, and
    this module must not import or invoke the pipeline, databases, network
    sources, or scheduler runtime/task code.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            "daily_kline_pipeline_due() requires an aware datetime; "
            "naive datetimes are rejected"
        )
    beijing_now = now.astimezone(_BEIJING_TZINFO)
    if beijing_now.time() < _DAILY_KLINE_PIPELINE_DUE_TIME:
        return False
    if (
        last_successful_run_date is not None
        and last_successful_run_date == beijing_now.date()
    ):
        return False
    return True

"""Scheduler configuration."""

from typing import Any, Dict


SCHEDULER_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "jobs": {
        "full_sync_kline": {
            "enabled": True,
            "cron_expression": "0 1 * * 6",
            "description": "Weekly full sync for all kline periods",
            "timeout": 7200,
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

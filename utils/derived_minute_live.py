"""Names and schema for the safe, current-day derived-minute materialization."""

from __future__ import annotations

from typing import Any


_PERIODS = {"15m": 15, "30m": 30, "60m": 60}


def live_derived_table(period: str) -> str:
    """Return the controlled table name for a current-day derived period."""
    try:
        minutes = _PERIODS[period]
    except KeyError as exc:
        raise ValueError(f"unsupported live derived period: {period!r}") from exc
    return f"kline_minute_{minutes}_live_derived"


def is_live_derived_period(period: str) -> bool:
    return period.lower() in _PERIODS


def ensure_live_derived_table(client: Any, period: str) -> str:
    """Create the append-only, month-partitioned live table if needed."""
    table = live_derived_table(period)
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {table}
        (
            code Nullable(String),
            datetime Nullable(DateTime),
            open Nullable(Float64),
            high Nullable(Float64),
            low Nullable(Float64),
            close Nullable(Float64),
            volume Nullable(Float64),
            amount Nullable(Float64),
            created_at DateTime,
            id UInt64
        )
        ENGINE = ReplacingMergeTree(created_at)
        PARTITION BY toYYYYMM(datetime)
        ORDER BY (code, datetime)
        SETTINGS allow_nullable_key = 1
        """
    )
    return table

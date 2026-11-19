"""Shared history boundary for maintained stock and index K-lines."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd


KLINE_HISTORY_START_DATE = date(2000, 1, 1)


class KlineHistoryPolicyError(ValueError):
    """Raised when a K-line write falls outside the maintained date range."""


def _period_kind(period: str) -> str:
    value = str(period or "").strip().lower()
    if value in {"1d", "daily", "day", "sector_daily"}:
        return "daily"
    if value in {"1m", "1min", "5m", "5min", "15m", "15min", "30m", "30min", "60m", "60min", "1h"}:
        return "minute"
    if value in {"1w", "weekly", "week"}:
        return "weekly"
    if value in {"1mon", "monthly", "month"}:
        return "monthly"
    if value in {"1q", "quarterly", "quarter"}:
        return "quarterly"
    if value in {"1y", "yearly", "year"}:
        return "yearly"
    return "unknown"


def _date_column(period: str, columns) -> str | None:
    present = set(columns)
    kind = _period_kind(period)
    choices = {
        "daily": ("trade_date", "date", "datetime"),
        "minute": ("datetime", "date", "trade_date"),
        "weekly": ("week_start_date", "week_end_date", "date", "datetime"),
        "monthly": ("month_start_date", "month_end_date", "date", "datetime"),
        "quarterly": ("year", "quarter_start_date", "quarter_end_date", "date", "trade_date"),
        "yearly": ("year", "date", "trade_date"),
        "unknown": ("trade_date", "datetime", "date", "week_end_date", "month_end_date", "year"),
    }
    return next((name for name in choices[kind] if name in present), None)


def _business_date(value: Any, *, year_column: bool = False) -> date | None:
    if value is None or value is pd.NA:
        return None
    if year_column:
        try:
            return date(int(value), 1, 1)
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        value = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(value):
        return None
    if value.tzinfo is not None:
        value = value.tz_convert("Asia/Shanghai")
    return value.date()


def kline_history_mask(period: str, frame) -> pd.Series:
    """Return rows whose period business date is on/after 2000-01-01."""
    if frame is None or frame.empty:
        return pd.Series([], dtype=bool, index=getattr(frame, "index", None))
    column = _date_column(period, frame.columns)
    if column is None:
        raise KlineHistoryPolicyError(
            f"cannot determine K-line date for period={period}; columns={list(frame.columns)}"
        )
    year_column = column == "year" and _period_kind(period) in {"quarterly", "yearly"}
    dates = frame[column].map(lambda value: _business_date(value, year_column=year_column))
    return dates.map(lambda value: value is not None and value >= KLINE_HISTORY_START_DATE)


def filter_kline_history_rows(period: str, frame):
    """Keep only maintained K-line rows, logging any pre-2000 input dropped."""
    if frame is None or frame.empty:
        return frame
    mask = kline_history_mask(period, frame)
    removed = int((~mask).sum())
    if removed:
        from utils.logger import get_logger

        logger = get_logger("KlineHistoryPolicy")
        logger.warning(
            "K-line rows before maintenance start or with invalid dates dropped: "
            f"period={period}, cutoff={KLINE_HISTORY_START_DATE.isoformat()}, rows={removed}"
        )
    return frame.loc[mask].copy()


def assert_kline_history_rows(period: str, frame) -> None:
    """Reject an entire write batch if any K-line row predates the policy."""
    if frame is None or frame.empty:
        return
    mask = kline_history_mask(period, frame)
    if not bool(mask.all()):
        column = _date_column(period, frame.columns)
        bad = frame.loc[~mask, column]
        samples = [str(value) for value in bad.head(5).tolist()]
        raise KlineHistoryPolicyError(
            f"blocked K-line write before {KLINE_HISTORY_START_DATE.isoformat()}: "
            f"period={period}, rows={len(bad)}, date_column={column}, samples={samples}"
        )

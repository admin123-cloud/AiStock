"""
AkShare/Sina fallback for recent A-share minute bars.

This module is intentionally narrow: fetch 5-minute bars from Sina via
AkShare, filter the requested dates, and derive 15/30/60 minute bars locally.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from utils.logger import get_logger

logger = get_logger("AkShareMinuteFallback")


_PERIOD_TO_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60}
_INTRADAY_BAR_DELAY_SECONDS = 120
_MARKET_TZ = ZoneInfo("Asia/Shanghai")


def _to_sina_symbol(code: str) -> Optional[str]:
    text = str(code or "").strip().upper()
    if not text:
        return None
    if text.startswith(("SH", "SZ", "BJ")) and len(text) >= 8:
        return text[:2].lower() + text[2:8]
    if "." in text:
        bare, suffix = text.split(".", 1)
        bare = bare.zfill(6)
        suffix = suffix.upper()
    else:
        bare = text.zfill(6)
        if bare.startswith(("6", "5", "9")):
            suffix = "SH"
        elif bare.startswith(("4", "8")):
            suffix = "BJ"
        else:
            suffix = "SZ"

    if bare == "999999" and suffix == "SH":
        bare = "000001"
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix)
    return f"{prefix}{bare}" if prefix else None


def _filter_dates(df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"])
    start_day = pd.to_datetime(str(start_date)[:10], errors="coerce")
    end_day = pd.to_datetime(str(end_date)[:10], errors="coerce")
    if pd.isna(start_day) or pd.isna(end_day):
        return pd.DataFrame()
    mask = (work["date"].dt.date >= start_day.date()) & (work["date"].dt.date <= end_day.date())
    return work.loc[mask].sort_values("date").reset_index(drop=True)


def _filter_market_minutes(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    ts = pd.to_datetime(df["date"], errors="coerce")
    mins = ts.dt.hour * 60 + ts.dt.minute
    mask = ((mins > 9 * 60 + 30) & (mins <= 11 * 60 + 30)) | (
        (mins > 13 * 60) & (mins <= 15 * 60)
    )
    return df.loc[mask].sort_values("date").reset_index(drop=True)


def _normalize_sina_frame(raw: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    rename = {"day": "date", "vol": "volume"}
    work = raw.rename(columns=rename).copy()
    required = ["date", "open", "high", "low", "close", "volume", "amount"]
    if any(col not in work.columns for col in required):
        return pd.DataFrame()
    work = work[required]
    work = _filter_dates(work, start_date, end_date)
    work = _filter_market_minutes(work)
    if work.empty:
        return pd.DataFrame()
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["date", "open", "high", "low", "close"])
    work = work[(work["open"] > 0) & (work["high"] > 0) & (work["low"] > 0) & (work["close"] > 0)]
    if work.empty:
        return pd.DataFrame()
    # Match existing ClickHouse minute-table convention: volume in lots, amount in 10k CNY.
    work["volume"] = (work["volume"].fillna(0) / 100.0).round(0)
    work["amount"] = (work["amount"].fillna(0) / 10000.0).round(4)
    return work.sort_values("date").reset_index(drop=True)


def _filter_confirmed_intraday_bars(df: pd.DataFrame, end_date: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    now = datetime.now(_MARKET_TZ)
    end_day = pd.to_datetime(str(end_date)[:10], errors="coerce")
    if pd.isna(end_day) or end_day.date() != now.date():
        return df
    cutoff = (now - timedelta(seconds=_INTRADAY_BAR_DELAY_SECONDS)).replace(tzinfo=None)
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    return work.loc[work["date"] <= cutoff].sort_values("date").reset_index(drop=True)


def _aggregate_5m(df_5m: pd.DataFrame, target_period: str) -> pd.DataFrame:
    minutes = _PERIOD_TO_MINUTES.get(str(target_period or "").lower())
    if not minutes or minutes == 5:
        return df_5m
    group_size = minutes // 5
    if group_size <= 1 or df_5m is None or df_5m.empty:
        return pd.DataFrame()

    work = df_5m.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    rows = []
    for _, day_df in work.groupby(work["date"].dt.strftime("%Y-%m-%d")):
        day_df = day_df.reset_index(drop=True)
        for start_idx in range(0, len(day_df), group_size):
            chunk = day_df.iloc[start_idx:start_idx + group_size]
            if len(chunk) < group_size:
                continue
            rows.append(
                {
                    "date": chunk.iloc[-1]["date"],
                    "open": float(chunk.iloc[0]["open"]),
                    "high": float(chunk["high"].max()),
                    "low": float(chunk["low"].min()),
                    "close": float(chunk.iloc[-1]["close"]),
                    "volume": float(pd.to_numeric(chunk["volume"], errors="coerce").fillna(0).sum()),
                    "amount": float(pd.to_numeric(chunk["amount"], errors="coerce").fillna(0).sum()),
                }
            )
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True) if rows else pd.DataFrame()


def fetch_recent_minute(
    code: str,
    start_date: str,
    end_date: str,
    period: str,
) -> Optional[pd.DataFrame]:
    period_key = str(period or "").strip().lower()
    if period_key not in _PERIOD_TO_MINUTES:
        return None
    symbol = _to_sina_symbol(code)
    if not symbol:
        return None
    try:
        import akshare as ak

        raw = ak.stock_zh_a_minute(symbol=symbol, period="5", adjust="")
        df_5m = _normalize_sina_frame(raw, start_date, end_date)
        df_5m = _filter_confirmed_intraday_bars(df_5m, end_date)
        if df_5m.empty:
            return None
        result = df_5m if period_key == "5m" else _aggregate_5m(df_5m, period_key)
        if result is None or result.empty:
            return None
        logger.info(
            f"akshare minute fallback fetched {code} {period_key} rows={len(result)} "
            f"range={result['date'].min()}~{result['date'].max()}"
        )
        return result
    except Exception as exc:
        msg = str(exc)
        log = logger.debug if "list index out of range" in msg else logger.warning
        log(
            f"akshare minute fallback failed code={code} period={period_key} "
            f"start={start_date} end={end_date} error={exc}"
        )
        return None

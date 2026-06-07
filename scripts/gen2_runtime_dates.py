from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def _query_latest_trade_date_from_clickhouse() -> str:
    from utils.market_warehouse import clickhouse_client

    ch = clickhouse_client()
    df = ch.query_df(
        """
        SELECT MAX(k.trade_date) AS latest_date
        FROM kline_daily k
        JOIN stocks s ON s.code = k.code
        WHERE s.type = 'stock'
          AND coalesce(s.quit, 0) = 0
          AND coalesce(s.st, 0) = 0
        """
    )
    if df.empty or pd.isna(df.iloc[0].get("latest_date")):
        raise RuntimeError("Cannot resolve latest stock trade date from ClickHouse.")
    return pd.Timestamp(df.iloc[0]["latest_date"]).strftime("%Y-%m-%d")


def resolve_latest_stock_trade_date() -> str:
    return _query_latest_trade_date_from_clickhouse()


def resolve_end_date(end_date: str | None = None) -> str:
    text = str(end_date or "").strip()
    if text:
        return pd.Timestamp(text).strftime("%Y-%m-%d")
    return resolve_latest_stock_trade_date()


def latest_date_from_frame(df: pd.DataFrame, column: str) -> str:
    if column not in df.columns:
        raise RuntimeError(f"Column not found for latest-date resolution: {column}")
    dates = pd.to_datetime(df[column], errors="coerce").dropna()
    if dates.empty:
        raise RuntimeError(f"No valid dates found in column: {column}")
    return dates.max().strftime("%Y-%m-%d")


def latest_date_from_parquet(path: Path, column: str) -> str:
    df = pd.read_parquet(path, columns=[column])
    return latest_date_from_frame(df, column)


def resolve_window_ends(
    latest_end_date: str,
    *,
    train_end: str = "2025-03-31",
    valid_end: str = "2025-12-31",
    blind_start: str = "2026-01-01",
) -> dict[str, tuple[str, str]]:
    latest_ts = pd.Timestamp(resolve_end_date(latest_end_date))
    windows: dict[str, tuple[str, str]] = {}

    def _append(name: str, start: str, end: str) -> None:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if start_ts <= end_ts and start_ts <= latest_ts:
            windows[name] = (start_ts.strftime("%Y-%m-%d"), end_ts.strftime("%Y-%m-%d"))

    _append("full", "2024-07-09", latest_ts.strftime("%Y-%m-%d"))
    _append("train", "2024-07-09", min(latest_ts, pd.Timestamp(train_end)).strftime("%Y-%m-%d"))
    _append("valid", "2025-04-01", min(latest_ts, pd.Timestamp(valid_end)).strftime("%Y-%m-%d"))
    if latest_ts >= pd.Timestamp(blind_start):
        _append("blind_2026ytd", blind_start, latest_ts.strftime("%Y-%m-%d"))
    return windows


def add_end_date_argument(parser: Any, help_text: str | None = None) -> None:
    parser.add_argument(
        "--end-date",
        default="",
        help=help_text or "Optional end date in YYYY-MM-DD; blank means latest stock trade date.",
    )

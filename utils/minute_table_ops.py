"""Shared minute-table write and aggregation helpers.

These helpers are source-neutral.  Source collectors should use them to write
the formal ``kline_minute_*`` tables without importing another provider's
collector module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from utils.kline_store import filter_trading_day_rows
from utils.market_warehouse import clickhouse_client


FREQ_TABLE_MAP = {
    "5": "kline_minute_5",
    "15": "kline_minute_15",
    "30": "kline_minute_30",
    "60": "kline_minute_60",
}


def _ch() -> Any:
    return clickhouse_client()


def upsert_to_ch(table: str, df: pd.DataFrame, replace: bool = False) -> int:
    """Insert minute bars into a formal ClickHouse minute table."""
    if df.empty:
        return 0
    period = table.replace("kline_minute_", "") + "m"
    before_rows = len(df)
    df = filter_trading_day_rows(period, df)
    if df.empty:
        print(f"{table} write blocked by trade_calendar guard: dropped={before_rows}", flush=True)
        return 0
    ch = _ch()
    if replace:
        pairs = df[["code", "datetime"]].drop_duplicates()
        for _, row in pairs.iterrows():
            ch.command(
                f"ALTER TABLE {table} DELETE WHERE code = %(code)s AND datetime = %(datetime)s",
                parameters={"code": row["code"], "datetime": row["datetime"]},
            )
    ch.insert_df(table, df)
    return int(len(df))


def filter_missing_rows(table: str, df: pd.DataFrame) -> pd.DataFrame:
    """Remove minute keys already present in the target table."""
    if df.empty:
        return df
    ch = _ch()
    code = str(df["code"].iloc[0])
    min_dt = df["datetime"].min()
    max_dt = df["datetime"].max()
    existing = ch.query_df(
        f"SELECT datetime FROM {table} "
        "WHERE code = %(code)s AND datetime >= %(min_dt)s AND datetime <= %(max_dt)s",
        parameters={"code": code, "min_dt": min_dt, "max_dt": max_dt},
    )
    if existing.empty:
        return df
    existing_keys = set(pd.to_datetime(existing["datetime"]).tolist())
    work = df.copy()
    work["_dt_key"] = pd.to_datetime(work["datetime"])
    return work[~work["_dt_key"].isin(existing_keys)].drop(columns=["_dt_key"])


def aggregate_from_5m(base: pd.DataFrame, target_minutes: int) -> pd.DataFrame:
    if base.empty:
        return pd.DataFrame()
    group_size = target_minutes // 5
    work = base.sort_values("datetime").copy()
    work["_day"] = pd.to_datetime(work["datetime"]).dt.date
    work["_grp"] = work.groupby("_day").cumcount() // group_size
    grouped = work.groupby(["_day", "_grp"], as_index=False)
    return grouped.agg(
        datetime=("datetime", "max"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
    )


def derive_higher_frame(df_5m: pd.DataFrame, code: str, target_minutes: int) -> pd.DataFrame:
    if df_5m.empty:
        return pd.DataFrame()
    result = aggregate_from_5m(df_5m, target_minutes)
    if result.empty:
        return pd.DataFrame()
    result["code"] = code
    result["created_at"] = datetime.now()
    result["id"] = [
        abs(hash((row.code, int(pd.Timestamp(row.datetime).timestamp()), f"{target_minutes}m", "minute.derived5m"))) % (2**63)
        for row in result.itertuples(index=False)
    ]
    return result[["code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]]

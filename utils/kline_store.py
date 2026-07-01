from __future__ import annotations

from datetime import date
from typing import Any, Optional

import pandas as pd

from utils.logger import get_logger
from utils.market_warehouse import (
    clean_minute_bars_df,
    clickhouse_client,
    clickhouse_query_df,
    clickhouse_scalar,
    clickhouse_table_exists,
)

logger = get_logger("KlineStore")


KLINE_TABLES = {
    "1d": "kline_daily",
    "daily": "kline_daily",
    "1w": "kline_weekly",
    "weekly": "kline_weekly",
    "1mon": "kline_monthly",
    "monthly": "kline_monthly",
    "1q": "kline_quarterly",
    "quarterly": "kline_quarterly",
    "1y": "kline_yearly",
    "yearly": "kline_yearly",
    "1m": "kline_minute_1",
    "1min": "kline_minute_1",
    "5m": "kline_minute_5",
    "5min": "kline_minute_5",
    "15m": "kline_minute_15",
    "15min": "kline_minute_15",
    "30m": "kline_minute_30",
    "30min": "kline_minute_30",
    "60m": "kline_minute_60",
    "60min": "kline_minute_60",
}

TIME_COLUMNS = {
    "1d": "trade_date",
    "daily": "trade_date",
    "1w": "week_start_date",
    "weekly": "week_start_date",
    "1mon": "month_start_date",
    "monthly": "month_start_date",
    "1q": "quarter_start_date",
    "quarterly": "quarter_start_date",
    "1y": "year",
    "yearly": "year",
    "1m": "datetime",
    "1min": "datetime",
    "5m": "datetime",
    "5min": "datetime",
    "15m": "datetime",
    "15min": "datetime",
    "30m": "datetime",
    "30min": "datetime",
    "60m": "datetime",
    "60min": "datetime",
}

MINUTE_PERIODS = {"1m", "1min", "5m", "5min", "15m", "15min", "30m", "30min", "60m", "60min"}


def _get_table_name(period: str) -> str:
    period_key = period.lower()
    if period_key not in KLINE_TABLES:
        raise ValueError(f"Unsupported K-line period: {period}")
    return KLINE_TABLES[period_key]


def _get_time_column(period: str) -> str:
    return TIME_COLUMNS.get(period.lower(), "trade_date")


def _quote(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _to_clickhouse_value(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    if pd.isna(value):
        return None
    return value


def _is_minute_period(period: str) -> bool:
    return period.lower() in MINUTE_PERIODS


def query_kline(
    code: str,
    period: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    limit: Optional[int] = None,
) -> pd.DataFrame:
    table = _get_table_name(period)
    time_col = _get_time_column(period)
    sql = f"SELECT * FROM {table} WHERE code = ?"
    params: list[Any] = [code]
    if start_date is not None:
        sql += f" AND {time_col} >= ?"
        params.append(start_date)
    if end_date is not None:
        sql += f" AND {time_col} <= ?"
        params.append(end_date)
    sql += f" ORDER BY {time_col} ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    df = clickhouse_query_df(sql, params)
    if _is_minute_period(period):
        return clean_minute_bars_df(df, period, datetime_col=time_col, code_col="code")
    return df


def query_kline_batch(
    codes: list[str],
    period: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame()

    table = _get_table_name(period)
    time_col = _get_time_column(period)
    placeholders = ", ".join(["?"] * len(codes))
    sql = f"SELECT * FROM {table} WHERE code IN ({placeholders})"
    params: list[Any] = list(codes)
    if start_date is not None:
        sql += f" AND {time_col} >= ?"
        params.append(start_date)
    if end_date is not None:
        sql += f" AND {time_col} <= ?"
        params.append(end_date)
    sql += f" ORDER BY code, {time_col} ASC"

    df = clickhouse_query_df(sql, params)
    if _is_minute_period(period):
        return clean_minute_bars_df(df, period, datetime_col=time_col, code_col="code")
    return df


def get_latest_trade_date(period: str = "1d") -> Optional[date]:
    table = _get_table_name(period)
    time_col = _get_time_column(period)
    try:
        if clickhouse_table_exists("trade_calendar"):
            if _is_minute_period(period):
                return clickhouse_scalar(
                    f"""
                    SELECT max(toDate(k.{time_col}))
                    FROM {table} k
                    JOIN trade_calendar c
                      ON c.trade_date = toDate(k.{time_col})
                     AND c.market = 'SH'
                     AND c.is_trading = 1
                    """
                )
            return clickhouse_scalar(
                f"""
                SELECT max(k.{time_col})
                FROM {table} k
                JOIN trade_calendar c
                  ON c.trade_date = k.{time_col}
                 AND c.market = 'SH'
                 AND c.is_trading = 1
                """
            )
    except Exception as exc:
        logger.warning(f"latest trade date calendar filter failed, fallback to raw max: {exc}")
    return clickhouse_scalar(f"SELECT max({time_col}) FROM {table}")


def get_kline_date_range(code: str, period: str) -> tuple[Optional[date], Optional[date]]:
    table = _get_table_name(period)
    time_col = _get_time_column(period)
    df = clickhouse_query_df(
        f"SELECT min({time_col}) AS min_date, max({time_col}) AS max_date FROM {table} WHERE code = ?",
        [code],
    )
    if df.empty:
        return None, None
    return df.iloc[0]["min_date"], df.iloc[0]["max_date"]


def check_kline_exists(code: str, period: str, trade_date: date) -> bool:
    table = _get_table_name(period)
    time_col = _get_time_column(period)
    value = clickhouse_scalar(
        f"SELECT count() FROM {table} WHERE code = ? AND {time_col} = ?",
        [code, trade_date],
    )
    return int(value or 0) > 0


def insert_kline(code: str, period: str, data: dict[str, Any]) -> bool:
    payload = dict(data)
    payload.setdefault("code", code)
    return insert_kline_batch(period, [payload]) == 1


def insert_kline_batch(period: str, data: list[dict[str, Any]], on_conflict: str = "ignore") -> int:
    if not data:
        return 0
    return insert_kline_dataframe(period, pd.DataFrame(data), on_conflict=on_conflict)


def insert_kline_dataframe(period: str, df: pd.DataFrame, on_conflict: str = "ignore") -> int:
    if df.empty:
        return 0

    table = _get_table_name(period)
    client = clickhouse_client()

    if on_conflict == "upsert":
        time_col = _get_time_column(period)
        if {"code", time_col}.issubset(df.columns):
            for code, code_df in df.groupby("code", dropna=True):
                values = pd.to_datetime(code_df[time_col], errors="coerce").dropna()
                if values.empty:
                    continue
                min_value = values.min().strftime("%Y-%m-%d %H:%M:%S")
                max_value = values.max().strftime("%Y-%m-%d %H:%M:%S")
                client.command(
                    f"""
                    ALTER TABLE {table}
                    DELETE WHERE code = {_quote(code)}
                      AND {time_col} >= toDateTime({_quote(min_value)})
                      AND {time_col} <= toDateTime({_quote(max_value)})
                    SETTINGS mutations_sync = 1
                    """
                )

    rows = [
        tuple(_to_clickhouse_value(value) for value in row)
        for row in df.itertuples(index=False, name=None)
    ]
    client.insert(table, rows, column_names=list(df.columns))
    logger.info(f"ClickHouse K-line insert: table={table}, rows={len(rows)}")
    return len(rows)


def delete_kline(
    code: str,
    period: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> int:
    table = _get_table_name(period)
    time_col = _get_time_column(period)
    where = ["code = ?"]
    params: list[Any] = [code]
    if start_date is not None:
        where.append(f"{time_col} >= ?")
        params.append(start_date)
    if end_date is not None:
        where.append(f"{time_col} <= ?")
        params.append(end_date)

    before = int(clickhouse_scalar(f"SELECT count() FROM {table} WHERE {' AND '.join(where)}", params) or 0)
    sql, named = _delete_sql(table, where, params)
    clickhouse_client().command(sql, parameters=named)
    return before


def _delete_sql(table: str, where: list[str], params: list[Any]) -> tuple[str, dict[str, Any]]:
    conditions: list[str] = []
    named: dict[str, Any] = {}
    for idx, condition in enumerate(where):
        key = f"p{idx}"
        conditions.append(condition.replace("?", f"%({key})s", 1))
        named[key] = params[idx]
    return (
        f"ALTER TABLE {table} DELETE WHERE {' AND '.join(conditions)} SETTINGS mutations_sync = 1",
        named,
    )


def get_kline_stats(period: str = "1d") -> dict[str, Any]:
    table = _get_table_name(period)
    time_col = _get_time_column(period)
    df = clickhouse_query_df(
        f"""
        SELECT
            count() AS total_count,
            uniqExact(code) AS stock_count,
            min({time_col}) AS min_date,
            max({time_col}) AS max_date
        FROM {table}
        """
    )
    if df.empty:
        return {"total_count": 0, "stock_count": 0, "min_date": None, "max_date": None, "period": period, "table": table}
    row = df.iloc[0]
    return {
        "total_count": int(row["total_count"] or 0),
        "stock_count": int(row["stock_count"] or 0),
        "min_date": row["min_date"],
        "max_date": row["max_date"],
        "period": period,
        "table": table,
    }


def check_kline_completeness(
    code: str,
    period: str,
    expected_dates: list[date],
) -> tuple[int, int, list[date]]:
    if not expected_dates:
        return 0, 0, []
    table = _get_table_name(period)
    time_col = _get_time_column(period)
    placeholders = ", ".join(["?"] * len(expected_dates))
    df = clickhouse_query_df(
        f"SELECT {time_col} AS value FROM {table} WHERE code = ? AND {time_col} IN ({placeholders})",
        [code, *expected_dates],
    )
    actual = set(df["value"].tolist()) if not df.empty else set()
    expected = set(expected_dates)
    return len(actual), len(expected), sorted(expected - actual)


def ensure_kline_tables_exist() -> None:
    missing = [table for table in sorted(set(KLINE_TABLES.values())) if not clickhouse_table_exists(table)]
    if missing:
        raise RuntimeError(f"Missing ClickHouse K-line tables: {', '.join(missing)}")


def create_kline_indexes() -> None:
    return None

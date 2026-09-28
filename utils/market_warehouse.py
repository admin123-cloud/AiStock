from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from utils.paths import warehouse_path

REPO_ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_ROOT = warehouse_path()
_CH_LOCAL = threading.local()
_CH_LOCK = threading.Lock()


def market_source() -> str:
    return "clickhouse"


def clickhouse_available() -> bool:
    if market_source() != "clickhouse":
        return False
    try:
        import clickhouse_connect  # noqa: F401
    except Exception:
        return False
    return True


def _new_clickhouse_client():
    import clickhouse_connect

    host = os.getenv("AISTOCK_CLICKHOUSE_HOST", "127.0.0.1")
    port = int(os.getenv("AISTOCK_CLICKHOUSE_PORT", "8123"))
    database = os.getenv("AISTOCK_CLICKHOUSE_DATABASE", "stock")
    username = os.getenv("AISTOCK_CLICKHOUSE_USER", "default")
    password = os.getenv("AISTOCK_CLICKHOUSE_PASSWORD", "")
    return clickhouse_connect.get_client(
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
    )


def clickhouse_client():
    """
    Thread-local ClickHouse client reuse.
    - Avoids creating a new HTTP connection per query.
    - Auto recreates client if ping failed.
    """
    client = getattr(_CH_LOCAL, "client", None)
    if client is not None:
        try:
            client.command("SELECT 1")
            return client
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            _CH_LOCAL.client = None

    with _CH_LOCK:
        client = getattr(_CH_LOCAL, "client", None)
        if client is not None:
            try:
                client.command("SELECT 1")
                return client
            except Exception:
                try:
                    client.close()
                except Exception:
                    pass
                _CH_LOCAL.client = None

        client = _new_clickhouse_client()
        _CH_LOCAL.client = client
        return client


def _adapt_clickhouse_query(sql: str, params: Optional[Any]):
    """
    Adapt legacy qmark-style SQL (`?`) + positional params to ClickHouse
    named parameters expected by clickhouse-connect.
    """
    if not params:
        return sql, None
    sh_tz = ZoneInfo("Asia/Shanghai")

    def _normalize_param(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, pd.Timestamp):
            if value.tzinfo is None:
                value = value.tz_localize(sh_tz)
            else:
                value = value.tz_convert(sh_tz)
            return value.to_pydatetime()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=sh_tz)
            return value.astimezone(sh_tz)
        return value

    if isinstance(params, Mapping):
        return sql, {k: _normalize_param(v) for k, v in dict(params).items()}
    if not isinstance(params, Sequence) or isinstance(params, (str, bytes, bytearray)):
        return sql, params

    values = [_normalize_param(v) for v in list(params)]
    if "?" not in sql:
        return sql, values

    adapted_sql = sql
    named: dict[str, Any] = {}
    for idx, value in enumerate(values):
        key = f"p{idx}"
        adapted_sql = adapted_sql.replace("?", f"%({key})s", 1)
        named[key] = value
    return adapted_sql, named


def clickhouse_query_df(sql: str, params: Optional[list] = None) -> pd.DataFrame:
    client = clickhouse_client()
    adapted_sql, adapted_params = _adapt_clickhouse_query(sql, params)
    return client.query_df(adapted_sql, parameters=adapted_params)


def clickhouse_scalar(sql: str, params: Optional[list] = None):
    client = clickhouse_client()
    adapted_sql, adapted_params = _adapt_clickhouse_query(sql, params)
    result = client.query(adapted_sql, parameters=adapted_params)
    if not result.result_rows:
        return None
    return result.result_rows[0][0]


def clickhouse_table_exists(table_name: str) -> bool:
    if not table_name:
        return False
    try:
        value = clickhouse_scalar(
            """
            SELECT count()
            FROM system.tables
            WHERE database = currentDatabase()
              AND name = ?
            """,
            [str(table_name)],
        )
        return int(value or 0) > 0
    except Exception:
        return False


_MINUTE_PERIOD_ALIASES = {
    "1m": 1,
    "1min": 1,
    "5m": 5,
    "5min": 5,
    "15m": 15,
    "15min": 15,
    "30m": 30,
    "30min": 30,
    "60m": 60,
    "60min": 60,
}


def _normalize_minute_period(period: Any) -> Optional[int]:
    if period is None:
        return None
    if isinstance(period, int):
        return period if period in {1, 5, 15, 30, 60} else None
    text = str(period).strip().lower()
    if text.isdigit():
        value = int(text)
        return value if value in {1, 5, 15, 30, 60} else None
    return _MINUTE_PERIOD_ALIASES.get(text)


def _valid_minute_bar_times(period_minutes: int) -> set[str]:
    local_sessions = [
        ("09:30", "11:30"),
        ("13:00", "15:00"),
    ]
    valid: set[str] = set()
    for start_text, end_text in local_sessions:
        start_ts = pd.Timestamp(f"2000-01-01 {start_text}")
        end_ts = pd.Timestamp(f"2000-01-01 {end_text}")
        current = start_ts + pd.Timedelta(minutes=period_minutes)
        while current <= end_ts:
            valid.add(current.strftime("%H:%M"))
            current += pd.Timedelta(minutes=period_minutes)
    return valid


def clean_minute_bars_df(
    df: Optional[pd.DataFrame],
    period: Any,
    *,
    datetime_col: str = "datetime",
    code_col: str = "code",
    keep: str = "last",
) -> pd.DataFrame:
    minute_period = _normalize_minute_period(period)
    if df is None:
        return pd.DataFrame()
    if minute_period is None or df.empty or datetime_col not in df.columns:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

    work = df.copy()
    work[datetime_col] = pd.to_datetime(work[datetime_col], errors="coerce")
    work = work.dropna(subset=[datetime_col]).copy()
    if work.empty:
        return work

    valid_times = _valid_minute_bar_times(minute_period)
    work["_hhmm"] = work[datetime_col].dt.strftime("%H:%M")
    work = work[work["_hhmm"].isin(valid_times)].copy()
    if work.empty:
        return work.drop(columns=["_hhmm"], errors="ignore")

    subset = [datetime_col]
    if code_col in work.columns:
        subset = [code_col, datetime_col]
    work = work.drop_duplicates(subset=subset, keep=keep).copy()
    sort_cols = [code_col, datetime_col] if code_col in work.columns else [datetime_col]
    work = work.sort_values(sort_cols).reset_index(drop=True)
    return work.drop(columns=["_hhmm"], errors="ignore")

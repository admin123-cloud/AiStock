"""Post-close A-share margin-financing sentiment facts.

QMT exposes quotes and account-level credit trading, but not exchange-wide
margin statistics.  This module persists the official SSE/SZSE daily summaries
through AkShare so the homepage can treat them as confirmed daily facts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from utils.logger import get_logger
from utils.market_warehouse import clickhouse_available, clickhouse_client, clickhouse_query_df


TABLE = "market_margin_sentiment"
SH_TZ = ZoneInfo("Asia/Shanghai")
_COLUMNS = [
    "trade_date",
    "financing_balance",
    "securities_lending_balance",
    "margin_balance",
    "financing_buy",
    "financing_net_buy",
    "source",
    "updated_at",
]
logger = get_logger("margin_sentiment")

_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE}
(
    trade_date Date,
    financing_balance Float64,
    securities_lending_balance Float64,
    margin_balance Float64,
    financing_buy Float64,
    financing_net_buy Float64,
    source String,
    updated_at DateTime('Asia/Shanghai')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY trade_date
"""


def ensure_table() -> bool:
    if not clickhouse_available():
        return False
    clickhouse_client().command(_TABLE_DDL)
    return True


def _number(value: Any) -> float:
    number = pd.to_numeric(value, errors="coerce")
    return float(number) if pd.notna(number) else 0.0


def _sse_frame(start_date: str, end_date: str) -> pd.DataFrame:
    import akshare as ak

    raw = ak.stock_margin_sse(start_date=start_date, end_date=end_date)
    if raw is None or raw.empty:
        return pd.DataFrame()
    frame = raw.rename(
        columns={
            "信用交易日期": "trade_date",
            "融资余额": "financing_balance",
            "融资买入额": "financing_buy",
            "融券余量金额": "securities_lending_balance",
            "融资融券余额": "margin_balance",
        }
    ).copy()
    return frame[["trade_date", "financing_balance", "financing_buy", "securities_lending_balance", "margin_balance"]]


def _szse_row(trade_date: str) -> dict[str, float]:
    import akshare as ak

    raw = ak.stock_margin_szse(date=trade_date.replace("-", ""))
    if raw is None or raw.empty:
        raise RuntimeError(f"SZSE margin summary is empty for {trade_date}")
    row = raw.iloc[0]
    # SZSE reports this aggregate in 100 million CNY; SSE reports CNY.
    return {
        "financing_balance": _number(row.get("融资余额")) * 100_000_000,
        "financing_buy": _number(row.get("融资买入额")) * 100_000_000,
        "securities_lending_balance": _number(row.get("融券余额")) * 100_000_000,
        "margin_balance": _number(row.get("融资融券余额")) * 100_000_000,
    }


def refresh_margin_sentiment(days: int = 10) -> dict[str, Any]:
    """Fetch recent official SSE/SZSE summaries and upsert confirmed rows."""
    if not ensure_table():
        return {"written": 0, "reason": "clickhouse_unavailable"}

    window_days = max(2, min(int(days), 90))
    end = datetime.now(SH_TZ).date()
    start = end - timedelta(days=window_days * 2)
    sse = _sse_frame(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if sse.empty:
        return {"written": 0, "reason": "sse_empty"}

    sse["trade_date"] = pd.to_datetime(sse["trade_date"], errors="coerce").dt.date
    sse = sse.dropna(subset=["trade_date"]).sort_values("trade_date").tail(window_days)
    if sse.empty:
        return {"written": 0, "reason": "sse_dates_invalid"}

    records: list[dict[str, Any]] = []
    failed_dates: list[str] = []
    for item in sse.to_dict("records"):
        trade_date = item["trade_date"]
        try:
            szse = _szse_row(trade_date.isoformat())
        except Exception as exc:
            failed_dates.append(trade_date.isoformat())
            logger.warning("margin SZSE summary unavailable for %s: %s", trade_date, exc)
            continue
        records.append(
            {
                "trade_date": trade_date,
                "financing_balance": _number(item["financing_balance"]) + szse["financing_balance"],
                "securities_lending_balance": _number(item["securities_lending_balance"]) + szse["securities_lending_balance"],
                "margin_balance": _number(item["margin_balance"]) + szse["margin_balance"],
                "financing_buy": _number(item["financing_buy"]) + szse["financing_buy"],
            }
        )

    if not records:
        return {"written": 0, "reason": "szse_empty", "failed_dates": failed_dates}

    prior = clickhouse_query_df(
        f"SELECT financing_balance FROM {TABLE} FINAL WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1",
        [records[0]["trade_date"]],
    )
    previous_balance = _number(prior.iloc[0]["financing_balance"]) if prior is not None and not prior.empty else None
    updated_at = datetime.now(SH_TZ).replace(tzinfo=None)
    rows = []
    for item in records:
        financing_balance = item["financing_balance"]
        net_buy = financing_balance - previous_balance if previous_balance is not None else 0.0
        previous_balance = financing_balance
        rows.append(
            (
                item["trade_date"],
                financing_balance,
                item["securities_lending_balance"],
                item["margin_balance"],
                item["financing_buy"],
                net_buy,
                "sse_szse_margin_summary_via_akshare",
                updated_at,
            )
        )
    clickhouse_client().insert(TABLE, rows, column_names=_COLUMNS)
    latest_date = records[-1]["trade_date"].isoformat()
    expected_date = None
    try:
        expected = clickhouse_query_df(
            """
            SELECT max(trade_date) AS trade_date
            FROM trade_calendar
            WHERE market = 'SH' AND is_trading = 1 AND trade_date < ?
            """,
            [end],
        )
        if expected is not None and not expected.empty and pd.notna(expected.iloc[0].get("trade_date")):
            expected_date = str(expected.iloc[0]["trade_date"])[:10]
    except Exception:
        expected_date = None
    return {
        "written": len(rows),
        "latest_date": latest_date,
        "expected_latest_date": expected_date,
        "stale": bool(expected_date and latest_date < expected_date),
        "failed_dates": failed_dates,
    }


def load_margin_sentiment(days: int = 30) -> dict[str, Any]:
    """Return a compact, confirmed daily series for the homepage."""
    if not clickhouse_available():
        return {"available": False, "reason": "clickhouse_unavailable", "dates": []}
    try:
        frame = clickhouse_query_df(
            f"""
            SELECT trade_date, financing_balance, securities_lending_balance,
                   margin_balance, financing_buy, financing_net_buy, source
            FROM {TABLE} FINAL
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [max(3, min(int(days), 90) + 1)],
        )
    except Exception as exc:
        logger.info("margin sentiment is not ready: %s", exc)
        return {"available": False, "reason": "not_initialized", "dates": []}
    if frame is None or frame.empty:
        return {"available": False, "reason": "no_confirmed_margin_data", "dates": []}

    frame = frame.iloc[::-1].copy()
    frame["securities_lending_balance_change"] = frame["securities_lending_balance"].diff().fillna(0.0)
    if len(frame) > int(days):
        frame = frame.iloc[-int(days):].copy()
    dates = [str(value)[:10] for value in frame["trade_date"].tolist()]
    values = lambda name: [round(_number(value), 2) for value in frame[name].tolist()]
    latest = frame.iloc[-1]
    return {
        "available": True,
        "dates": dates,
        "financing_balance": values("financing_balance"),
        "securities_lending_balance": values("securities_lending_balance"),
        "margin_balance": values("margin_balance"),
        "financing_buy": values("financing_buy"),
        "financing_net_buy": values("financing_net_buy"),
        "securities_lending_balance_change": values("securities_lending_balance_change"),
        "latest": {
            "date": dates[-1],
            "financing_balance": round(_number(latest["financing_balance"]), 2),
            "securities_lending_balance": round(_number(latest["securities_lending_balance"]), 2),
            "margin_balance": round(_number(latest["margin_balance"]), 2),
            "financing_net_buy": round(_number(latest["financing_net_buy"]), 2),
            "securities_lending_balance_change": round(_number(latest["securities_lending_balance_change"]), 2),
        },
        "source": str(latest["source"]),
        "is_confirmed": True,
    }

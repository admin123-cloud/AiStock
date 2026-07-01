"""Sync A-share trading calendar into ClickHouse."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo


project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from config import config as app_config
from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
from utils.logger import get_logger
from utils.market_warehouse import clickhouse_client


logger = get_logger("trade_calendar")
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


def _normalize_trade_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y%m%d")
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit() and len(text) >= 8 and text[:2] in {"19", "20"}:
        return text[:8]
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            return datetime.fromtimestamp(number / 1000, tz=BUSINESS_TZ).strftime("%Y%m%d")
        if number > 100_000_000:
            return datetime.fromtimestamp(number, tz=BUSINESS_TZ).strftime("%Y%m%d")
        if len(text) >= 8:
            return text[:8]
    return text.replace("-", "")[:8]


def _dedupe_dates(values: list[Any]) -> list[str]:
    return sorted({item for item in (_normalize_trade_date(value) for value in values) if item})


def _load_qmt_trade_dates(start_time: str = "19900101", end_time: str = "") -> list[str]:
    client = QmtMiniMarketClient()
    client.connect()
    trade_dates = _dedupe_dates(client.get_trading_dates("SH", start_time, end_time, -1))
    if not trade_dates:
        raise RuntimeError("QMT returned empty trading dates")
    return trade_dates


def _load_akshare_trade_dates() -> list[str]:
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    trade_dates = _dedupe_dates(df["trade_date"].tolist())
    if not trade_dates:
        raise RuntimeError("AkShare/Sina returned empty trading dates")
    return trade_dates


def _preferred_trade_calendar_source() -> str:
    preferred = str(app_config.get("data_sync.preferred_source", "qmt_xtquant") or "qmt_xtquant").strip().lower()
    if preferred in {"qmt", "qmtmini", "qmt_xtquant", "xtquant"}:
        return "qmt_xtquant"
    return "qmt_xtquant"


def sync_trade_calendar() -> int:
    """Sync trading dates into the existing trade_calendar table."""
    source = _preferred_trade_calendar_source()
    try:
        try:
            logger.info("start syncing trade_calendar from QMT/xtquant")
            trade_dates = _load_qmt_trade_dates()
        except Exception as primary_exc:
            logger.warning(f"primary trade calendar source failed, fallback to AkShare/Sina: {primary_exc}")
            source = "akshare_sina"
            trade_dates = _load_akshare_trade_dates()

        logger.info(f"trade calendar dates loaded: count={len(trade_dates)}, source={source}")

        insert_rows = []
        skipped_dates = []
        min_trade_date = datetime.strptime("19900101", "%Y%m%d").date()
        for raw_date in trade_dates:
            date_str = _normalize_trade_date(raw_date)[:8]
            try:
                trade_date = datetime.strptime(date_str, "%Y%m%d").date()
            except ValueError:
                skipped_dates.append(raw_date)
                continue
            if trade_date < min_trade_date:
                skipped_dates.append(raw_date)
                continue
            insert_rows.append((trade_date, "SH", 1))
            insert_rows.append((trade_date, "SZ", 1))
        if skipped_dates:
            logger.warning(f"skipped invalid trade calendar dates: count={len(skipped_dates)}, sample={skipped_dates[:5]}")

        if not insert_rows:
            logger.warning("no trade calendar rows to write")
            return 0

        client = clickhouse_client()
        client.command(
            """
            CREATE TABLE IF NOT EXISTS trade_calendar
            (
                trade_date Date,
                market String,
                is_trading UInt8
            )
            ENGINE = ReplacingMergeTree()
            ORDER BY (trade_date, market)
            """
        )

        tmp_table = "trade_calendar_sync_tmp"
        backup_table = "trade_calendar_sync_backup"
        client.command(f"DROP TABLE IF EXISTS {tmp_table}")
        client.command(f"DROP TABLE IF EXISTS {backup_table}")
        client.command(f"CREATE TABLE {tmp_table} AS trade_calendar")
        client.insert(tmp_table, insert_rows, column_names=["trade_date", "market", "is_trading"])
        client.command(f"RENAME TABLE trade_calendar TO {backup_table}, {tmp_table} TO trade_calendar")
        client.command(f"DROP TABLE IF EXISTS {backup_table}")

        logger.info(f"trade calendar write complete: rows={len(insert_rows)}, source={source}")
        return len(insert_rows)
    except Exception as exc:
        logger.error(f"sync trade calendar failed: {exc}")
        raise


def get_trading_dates(start_date: str, end_date: str, market: str = "SH") -> list[str]:
    client = clickhouse_client()
    rs = client.query(
        """
        SELECT trade_date
        FROM trade_calendar
        WHERE trade_date >= %(start)s
          AND trade_date <= %(end)s
          AND market = %(market)s
          AND is_trading = 1
        ORDER BY trade_date
        """,
        parameters={"start": start_date, "end": end_date, "market": market},
    )
    return [str(r[0]) for r in rs.result_rows]


if __name__ == "__main__":
    sync_trade_calendar()

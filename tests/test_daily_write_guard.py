from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from services.data_storage import DataStorageService
from utils.daily_write_guard import DailyWriteBlockedError
from utils.kline_store import delete_kline, insert_kline_dataframe


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": "000001.SZ",
                "trade_date": "2026-10-20",
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
                "amount": 1.0,
            }
        ]
    )


def test_kline_store_blocks_formal_daily_insert_before_filter_or_client(monkeypatch):
    monkeypatch.setattr(
        "utils.kline_store.filter_trading_day_rows",
        lambda *args, **kwargs: pytest.fail("daily gate must run first"),
    )
    monkeypatch.setattr(
        "utils.kline_store.clickhouse_client",
        lambda: pytest.fail("daily gate must run before ClickHouse client creation"),
    )

    with pytest.raises(DailyWriteBlockedError, match="kline_daily"):
        insert_kline_dataframe("daily", _daily_frame())


def test_kline_store_blocks_formal_daily_delete_before_count_query(monkeypatch):
    monkeypatch.setattr(
        "utils.kline_store.clickhouse_scalar",
        lambda *args, **kwargs: pytest.fail("daily gate must run before count query"),
    )

    with pytest.raises(DailyWriteBlockedError, match="delete"):
        delete_kline("000001.SZ", "daily")


def test_data_storage_reraises_daily_write_block():
    with pytest.raises(DailyWriteBlockedError, match="DataStorageService"):
        DataStorageService()._batch_insert_daily_klines(None, [{"code": "000001.SZ"}])


def test_data_storage_blocks_before_opening_a_session(monkeypatch):
    monkeypatch.setattr(
        "services.data_storage.get_db",
        lambda: pytest.fail("daily gate must run before opening a session"),
    )

    with pytest.raises(DailyWriteBlockedError, match="batch_insert_klines"):
        DataStorageService().batch_insert_klines([{"code": "000001.SZ"}], period="daily")


def test_qmt_stage_is_blocked_before_client_creation(monkeypatch):
    import scripts.qmtmini_daily_backfill_validate as module

    monkeypatch.setattr(module, "ch_client", lambda: pytest.fail("must block before client"))
    args = SimpleNamespace(stage_table="stage", start_date="2026-10-20", end_date="2026-10-20")

    with pytest.raises(DailyWriteBlockedError, match="stage-to-formal"):
        module.apply_stage(args)

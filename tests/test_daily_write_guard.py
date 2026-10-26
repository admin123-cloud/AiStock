from __future__ import annotations

from datetime import date
import sys
from types import SimpleNamespace

import pandas as pd
import pytest

from services.data_storage import DataStorageService
from utils.daily_write_guard import DailyWriteBlockedError
from utils.daily_write_guard import (
    load_daily_write_scope_for_keys,
    make_daily_write_scope,
)
from utils.daily_date_universe import build_date_universe_payload
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


def _permit_payload():
    return build_date_universe_payload(
        code="000001.SZ",
        security_type="stock",
        identity_status="verified",
        source_kind="accepted_test_source",
        source_ref="fixture://000001.SZ",
        source_sha256="source-sha",
        dates=[date(2026, 10, 20)],
    )


class _Result:
    def __init__(self, names, rows):
        self.column_names = names
        self.result_rows = rows


class _DateUniverseClient:
    def __init__(self, payload, *, include_member=True):
        self.payload = payload
        self.include_member = include_member
        self.calls = []
        self.inserts = []
        self.commands = []

    def query(self, sql, settings=None, **kwargs):
        self.calls.append((sql, settings))
        if "FROM stocks" in sql:
            return _Result(["code"], [["000001.SZ"]])
        if "daily_date_universe_registry" in sql:
            row = self.payload.registry.registry_row()
            names = ["dataset_id", "code", "completeness_state", "date_set_sha256", "is_active"]
            return _Result(names, [[row[name] for name in names]])
        if "daily_date_universe_member" in sql and self.include_member:
            row = self.payload.member_rows[0]
            names = ["dataset_id", "code", "trade_date", "date_set_sha256"]
            return _Result(names, [[row[name] for name in names]])
        if "daily_date_universe_member" in sql:
            return _Result(["dataset_id", "code", "trade_date", "date_set_sha256"], [])
        raise AssertionError(f"unexpected query: {sql}")

    def insert(self, table, rows, column_names=None):
        self.inserts.append((table, rows, column_names))

    def command(self, sql, parameters=None):
        self.commands.append((sql, parameters))


def test_kline_store_reads_registry_member_before_filter_when_permit_missing(monkeypatch):
    monkeypatch.setattr(
        "utils.kline_store.filter_trading_day_rows",
        lambda *args, **kwargs: pytest.fail("daily gate must run first"),
    )
    monkeypatch.setattr(
        "utils.kline_store.clickhouse_client",
        lambda: _DateUniverseClient(_permit_payload(), include_member=False),
    )

    with pytest.raises(DailyWriteBlockedError, match="registry/member"):
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

    with pytest.raises(DailyWriteBlockedError, match="trade_date/date"):
        DataStorageService().batch_insert_klines([{"code": "000001.SZ"}], period="daily")


def test_daily_scope_reads_current_stock_registry_and_member_as_readonly():
    payload = _permit_payload()
    client = _DateUniverseClient(payload)

    scope = load_daily_write_scope_for_keys(
        [("000001.SZ", date(2026, 10, 20))],
        source="test-reader-injection",
        client=client,
    )

    assert scope.exact_key_permit is not None
    assert scope.exact_key_permit.keys == {
        ("000001.SZ", date(2026, 10, 20))
    }
    assert len(client.calls) == 3
    assert all(settings == {"readonly": 1} for _, settings in client.calls)


def test_kline_store_injects_exact_key_permit_before_formal_insert(monkeypatch):
    payload = _permit_payload()
    client = _DateUniverseClient(payload)
    monkeypatch.setattr("utils.kline_store.clickhouse_client", lambda: client)
    monkeypatch.setattr("utils.kline_store.filter_trading_day_rows", lambda period, frame: frame)

    inserted = insert_kline_dataframe("daily", _daily_frame())

    assert inserted == 1
    assert client.inserts[0][0] == "kline_daily"
    assert all(settings == {"readonly": 1} for _, settings in client.calls)


def test_qmt_intraday_daily_apply_injects_exact_key_permit(monkeypatch):
    from scripts.import_qmt_datadir_intraday_to_clickhouse import apply_daily

    payload = _permit_payload()
    client = _DateUniverseClient(payload)
    columns = [
        "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
        "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id",
    ]
    daily = pd.DataFrame(
        [{column: None for column in columns}]
    )
    daily.loc[0, "code"] = "000001.SZ"
    daily.loc[0, "trade_date"] = date(2026, 10, 20)
    daily.loc[0, "close"] = 1.0

    result = apply_daily(client, daily, "2026-10-20", delete_chunk_size=10)

    assert result == {"rows": 1, "codes": 1}
    assert client.inserts[0][0] == "kline_daily"
    assert all(settings == {"readonly": 1} for _, settings in client.calls)


def test_data_storage_injects_exact_key_permit_before_formal_session(monkeypatch):
    payload = _permit_payload()
    client = _DateUniverseClient(payload)
    monkeypatch.setattr("utils.market_warehouse.clickhouse_client", lambda: client)

    class Session:
        def __init__(self):
            self.executed = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, rows):
            self.executed.append((statement, rows))

        def commit(self):
            return None

    session = Session()
    monkeypatch.setattr("services.data_storage.get_db", lambda: iter([session]))

    inserted = DataStorageService().batch_insert_klines(
        _daily_frame().to_dict("records"), period="daily"
    )

    assert inserted == 1
    assert len(session.executed) == 1
    assert client.calls and all(settings == {"readonly": 1} for _, settings in client.calls)


def test_scheduler_reads_registry_member_before_opening_session(monkeypatch):
    payload = _permit_payload()
    client = _DateUniverseClient(payload, include_member=False)
    monkeypatch.setattr("utils.market_warehouse.clickhouse_client", lambda: client)

    class DB:
        def get_session(self):
            pytest.fail("scheduler must read registry/member before opening the write session")

    monkeypatch.setattr("utils.database.db", DB())
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(Process=lambda: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=0))),
    )
    from scheduler.tasks.full_sync_kline_task import FullSyncKlineTask

    klines = pd.DataFrame(
        [{
            "datetime": pd.Timestamp("2026-10-20"),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
            "amount": 1.0,
        }]
    )
    with pytest.raises(DailyWriteBlockedError, match="registry/member"):
        FullSyncKlineTask()._save_kline_data("000001.SZ", "1d", klines)


def test_scheduler_injects_exact_key_permit_before_formal_session(monkeypatch):
    payload = _permit_payload()
    client = _DateUniverseClient(payload)
    monkeypatch.setattr("utils.market_warehouse.clickhouse_client", lambda: client)
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        SimpleNamespace(Process=lambda: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=0))),
    )

    class Query:
        def filter(self, *args):
            return self

        def all(self):
            return []

    class Session:
        def __init__(self):
            self.added = []
            self.committed = False

        def query(self, *args):
            return Query()

        def add_all(self, rows):
            self.added.extend(rows)

        def flush(self):
            return None

        def commit(self):
            self.committed = True

        def close(self):
            return None

        def rollback(self):
            return None

    session = Session()

    class DB:
        def get_session(self):
            return iter([session])

    monkeypatch.setattr("utils.database.db", DB())
    from scheduler.tasks.full_sync_kline_task import FullSyncKlineTask

    klines = pd.DataFrame(
        [{
            "datetime": pd.Timestamp("2026-10-20"),
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
            "amount": 1.0,
        }]
    )

    FullSyncKlineTask()._save_kline_data("000001.SZ", "1d", klines)

    assert session.committed is True
    assert len(session.added) == 1
    assert all(settings == {"readonly": 1} for _, settings in client.calls)


def test_qmt_stage_is_blocked_before_client_creation(monkeypatch):
    import scripts.qmtmini_daily_backfill_validate as module

    monkeypatch.setattr(module, "ch_client", lambda: pytest.fail("must block before client"))
    args = SimpleNamespace(stage_table="stage", start_date="2026-10-20", end_date="2026-10-20")

    with pytest.raises(DailyWriteBlockedError, match="stage-to-formal"):
        module.apply_stage(args)


def test_tqcenter_stage_is_blocked_before_client_creation(monkeypatch):
    import scripts.backfill_tqcenter_daily_to_clickhouse as module

    monkeypatch.setattr(module, "ch_client", lambda: pytest.fail("must block before client"))
    args = SimpleNamespace(codes="000001.SZ", limit=1, start_date="2026-10-20", delete_chunk_size=1)

    with pytest.raises(DailyWriteBlockedError, match="stage-to-formal"):
        module.apply_stage(args)


def test_daily_metrics_apply_is_blocked_after_read_only_recalculation(monkeypatch):
    import scripts.repair_kline_daily_metrics as module

    row = {column: 0 for column in module.COLUMNS}
    row.update({"code": "000001.SH", "trade_date": "2026-10-20", "close": 1.0, "open": 1.0, "high": 1.0, "low": 1.0})
    monkeypatch.setattr(module, "clickhouse_scalar", lambda *args, **kwargs: "2026-10-19")
    monkeypatch.setattr(module, "clickhouse_query_df", lambda *args, **kwargs: pd.DataFrame([row]))

    with pytest.raises(DailyWriteBlockedError, match="daily metrics repair"):
        module.repair_metrics("2026-10-20", "2026-10-20", dry_run=False)

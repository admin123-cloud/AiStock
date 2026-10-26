from __future__ import annotations

from dataclasses import dataclass

import pytest

from utils.clickhouse_schema import (
    ClickHouseSchemaContractError,
    STARTUP_TABLE_CONTRACTS,
    startup_bootstrap_table_names,
    verify_startup_schema_contract,
)


@dataclass
class _Result:
    rows: list[tuple]

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, *, incompatible: bool = False):
        self.incompatible = incompatible
        self.calls: list[str] = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append(sql)
        if sql.strip() == "SELECT 1":
            return _Result([(1,)])
        if "SELECT name FROM system.tables" in sql:
            return _Result([(name,) for name in startup_bootstrap_table_names()])
        if "SELECT engine, partition_key, sorting_key" in sql:
            table = (parameters or {}).get("table_name")
            if table == "stocks":
                return _Result([("ReplacingMergeTree", "", "code")])
            if table == "kline_daily":
                partition = "toYYYYMM(trade_date)"
                if self.incompatible:
                    partition = ""
                return _Result([("ReplacingMergeTree(created_at)", partition, "code, trade_date")])
            if table == "daily_date_universe_registry":
                return _Result([("ReplacingMergeTree(published_at)", "", "code, dataset_id")])
            if table == "daily_date_universe_member":
                return _Result([("MergeTree", "toYYYYMM(trade_date)", "dataset_id, code, trade_date")])
            if table == "daily_coverage_audit":
                return _Result([("ReplacingMergeTree(observed_at)", "toYYYYMM(trade_date)", "code, trade_date, audit_id")])
        if "SELECT name FROM system.columns" in sql:
            table = (parameters or {}).get("table_name")
            contract = next(item for item in STARTUP_TABLE_CONTRACTS if item.name == table)
            columns = sorted(contract.required_columns)
            if self.incompatible and table == "kline_daily":
                columns.remove("created_at")
            return _Result([(name,) for name in columns])
        raise AssertionError(f"unexpected SQL: {sql}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


def test_startup_contract_reads_only_schema_metadata():
    connection = _Connection()

    verify_startup_schema_contract(connection)

    assert any("system.tables" in call for call in connection.calls)
    assert any("system.columns" in call for call in connection.calls)
    assert not any("CREATE" in call.upper() or "ALTER" in call.upper() for call in connection.calls)


def test_startup_contract_fails_closed_for_incompatible_kline_daily():
    connection = _Connection(incompatible=True)

    with pytest.raises(ClickHouseSchemaContractError, match="kline_daily"):
        verify_startup_schema_contract(connection)

    assert not any("CREATE" in call.upper() or "ALTER" in call.upper() for call in connection.calls)


def test_database_startup_readiness_does_not_bootstrap_ddl(monkeypatch):
    from utils.database import DatabaseManager

    connection = _Connection()
    manager = object.__new__(DatabaseManager)
    manager._engine = _Engine(connection)
    monkeypatch.setattr(manager, "_startup_retry_config", lambda: (1, 0.0))
    monkeypatch.setattr(
        manager,
        "create_tables",
        lambda: pytest.fail("startup readiness must not issue bootstrap DDL"),
    )

    manager.ensure_ready_for_startup()

    assert connection.calls[0].strip() == "SELECT 1"

from __future__ import annotations

from dataclasses import dataclass

import pytest

from utils.clickhouse_schema import (
    ClickHouseSchemaContractError,
    STARTUP_TABLE_CONTRACTS,
    ensure_kline_history_constraints,
    ensure_neeq_security_constraints,
    startup_bootstrap_table_names,
    verify_startup_schema_contract,
)


@dataclass
class _Result:
    rows: list[tuple]

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(
        self,
        *,
        incompatible: bool = False,
        unversioned_kline: bool = False,
        nullable_created_at: bool = False,
        constraint_ddl_overrides: dict[str, str] | None = None,
        column_type_overrides: dict[tuple[str, str], str] | None = None,
    ):
        self.incompatible = incompatible
        self.unversioned_kline = unversioned_kline
        self.nullable_created_at = nullable_created_at
        self.constraint_ddl_overrides = dict(constraint_ddl_overrides or {})
        self.column_type_overrides = dict(column_type_overrides or {})
        self.calls: list[str] = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append(sql)
        if sql.strip() == "SELECT 1":
            return _Result([(1,)])
        if "SELECT name FROM system.tables" in sql:
            return _Result([(name,) for name in startup_bootstrap_table_names()])
        if "FROM system.tables" in sql and "table_name" in sql:
            table = (parameters or {}).get("table_name")
            if table == "stocks":
                row = ("ReplacingMergeTree", "ReplacingMergeTree(updated_at) ORDER BY code", "", "code")
            elif table == "kline_daily":
                partition = "" if self.incompatible else "toYYYYMM(trade_date)"
                engine_full = "ReplacingMergeTree(created_at) PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)"
                if self.unversioned_kline:
                    engine_full = "ReplacingMergeTree PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)"
                row = ("ReplacingMergeTree", engine_full, partition, "code, trade_date")
            elif table == "daily_date_universe_registry":
                row = ("ReplacingMergeTree", "ReplacingMergeTree(published_at) ORDER BY (code, dataset_id)", "", "code, dataset_id")
            elif table == "daily_date_universe_member":
                row = ("MergeTree", "MergeTree PARTITION BY toYYYYMM(trade_date) ORDER BY (dataset_id, code, trade_date)", "toYYYYMM(trade_date)", "dataset_id, code, trade_date")
            elif table == "daily_coverage_audit":
                row = ("ReplacingMergeTree", "ReplacingMergeTree(observed_at) PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date, audit_id)", "toYYYYMM(trade_date)", "code, trade_date, audit_id")
            else:
                raise AssertionError(f"unexpected table: {table}")
            if "create_table_query" in sql.lower():
                from utils.clickhouse_schema import MARKET_TABLE_DDL

                ddl = self.constraint_ddl_overrides.get(table)
                if ddl is None:
                    ddl = next(
                        statement
                        for statement in MARKET_TABLE_DDL
                        if f"CREATE TABLE IF NOT EXISTS {table}" in statement
                    )
                # ClickHouse re-serialises string literals with backslashes
                # doubled when rendering create_table_query.
                ddl = ddl.replace("\\", "\\\\")
                row = (*row, ddl)
            return _Result([row] if "engine_full" in sql else [(row[0], row[2], row[3])])
        if "FROM system.columns" in sql and "table_name" in sql:
            table = (parameters or {}).get("table_name")
            contract = next(item for item in STARTUP_TABLE_CONTRACTS if item.name == table)
            columns = sorted(contract.required_columns)
            if self.incompatible and table == "kline_daily":
                columns.remove("created_at")
            types = {
                "code": "String", "name": "String", "market": "String", "type": "String",
                "listing_status": "LowCardinality(String)", "industry": "Nullable(String)",
                "region": "Nullable(String)", "list_date": "Nullable(Date)",
                "delist_date": "Nullable(Date)", "quit": "UInt8", "st": "UInt8",
                "industry_code": "Nullable(String)", "self_selected": "UInt8", "holding": "UInt8",
                "float_share": "Nullable(Float64)", "total_share": "Nullable(Float64)",
                "created_at": "DateTime", "updated_at": "DateTime", "trade_date": "Date",
                "open": "Float64", "high": "Float64", "low": "Float64", "close": "Float64",
                "volume": "Float64", "amount": "Float64", "amplitude": "Float64",
                "change_pct": "Float64", "change_amount": "Float64", "turnover_rate": "Float64",
                "id": "UInt64", "dataset_id": "String", "security_type": "LowCardinality(String)",
                "identity_status": "String", "source_kind": "String", "source_ref": "String",
                "source_sha256": "String", "coverage_start": "Nullable(Date)",
                "coverage_end": "Nullable(Date)", "completeness_state": "LowCardinality(String)",
                "date_set_sha256": "String", "member_count": "UInt64",
                "published_at": "DateTime('Asia/Shanghai')", "is_active": "UInt8",
                "member_ordinal": "UInt32", "audit_id": "String", "date_universe_id": "String",
                "audit_state": "LowCardinality(String)", "status_audit_ref": "String",
                "minute_reference_required": "UInt8", "evidence_json": "String",
                "observed_at": "DateTime('Asia/Shanghai')",
            }
            if table == "daily_coverage_audit":
                types["coverage_start"] = "Date"
                types["coverage_end"] = "Date"
            for (override_table, column_name), type_name in self.column_type_overrides.items():
                if override_table == table:
                    types[column_name] = type_name
            if self.nullable_created_at and table == "kline_daily":
                types["created_at"] = "Nullable(DateTime)"
            if self.incompatible and table == "kline_daily":
                types.pop("created_at", None)
            if "type" in sql.lower():
                return _Result([(name, types.get(name, "String")) for name in columns])
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


class _HistoryConstraintConnection:
    def __init__(self, ddl="CREATE TABLE stock.kline_daily (...)"):
        self.ddl = ddl
        self.calls = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append(sql)
        if "FROM system.tables" in sql:
            return _Result([("kline_daily", "MergeTree", self.ddl)])
        if "FROM system.columns" in sql:
            rows = [
                ("kline_daily", "trade_date", "Date"),
                ("kline_daily", "open", "Float64"),
                ("kline_daily", "high", "Float64"),
                ("kline_daily", "low", "Float64"),
                ("kline_daily", "close", "Float64"),
            ]
            if "SELECT table, name, type" in sql:
                return _Result(rows)
            return _Result([(table, name) for table, name, _ in rows])
        if "SELECT currentDatabase()" in sql:
            return _Result([("stock",)])
        return _Result([])


class _NeeqConstraintConnection:
    def __init__(self, ddl=None):
        self.ddl = ddl or "CREATE TABLE stock.kline_daily (code String, CONSTRAINT codex_no_neeq_code CHECK 1)"
        self.calls = []

    def execute(self, statement, parameters=None):
        sql = str(statement)
        self.calls.append(sql)
        if "FROM system.tables" in sql:
            return _Result([("kline_daily", "MergeTree", self.ddl)])
        if "FROM system.columns" in sql:
            return _Result([("kline_daily", "code", "String")])
        if "SELECT currentDatabase()" in sql:
            return _Result([("stock",)])
        return _Result([])


def test_startup_contract_reads_only_schema_metadata():
    connection = _Connection()

    verify_startup_schema_contract(connection)

    assert any("system.tables" in call for call in connection.calls)
    assert any("system.columns" in call for call in connection.calls)
    assert not any(call.strip().upper().startswith(("CREATE", "ALTER", "DROP")) for call in connection.calls)


def test_startup_contract_fails_closed_for_incompatible_kline_daily():
    connection = _Connection(incompatible=True)

    with pytest.raises(ClickHouseSchemaContractError, match="kline_daily"):
        verify_startup_schema_contract(connection)

    assert not any(call.strip().upper().startswith(("CREATE", "ALTER", "DROP")) for call in connection.calls)


def test_startup_contract_rejects_unversioned_kline_daily():
    connection = _Connection(unversioned_kline=True)

    with pytest.raises(ClickHouseSchemaContractError, match="engine_full"):
        verify_startup_schema_contract(connection)


def test_startup_contract_rejects_nullable_kline_created_at():
    connection = _Connection(nullable_created_at=True)

    with pytest.raises(ClickHouseSchemaContractError, match="created_at"):
        verify_startup_schema_contract(connection)


def test_startup_contract_rejects_missing_neeq_check_constraint():
    from utils.clickhouse_schema import MARKET_TABLE_DDL

    ddl = next(statement for statement in MARKET_TABLE_DDL if "CREATE TABLE IF NOT EXISTS kline_daily" in statement)
    ddl = "\n".join(line for line in ddl.splitlines() if "codex_no_neeq_code" not in line)
    connection = _Connection(constraint_ddl_overrides={"kline_daily": ddl})

    with pytest.raises(ClickHouseSchemaContractError, match="codex_no_neeq_code"):
        verify_startup_schema_contract(connection)

    assert not any(call.strip().upper().startswith(("CREATE", "ALTER", "DROP")) for call in connection.calls)


def test_startup_contract_rejects_same_name_neeq_constraint_with_wrong_expression():
    connection = _Connection(
        constraint_ddl_overrides={
            "kline_daily": "CREATE TABLE stock.kline_daily (code String, CONSTRAINT codex_no_neeq_code CHECK 1)"
        }
    )

    with pytest.raises(ClickHouseSchemaContractError, match="codex_no_neeq_code"):
        verify_startup_schema_contract(connection)

    assert not any(call.strip().upper().startswith(("CREATE", "ALTER", "DROP")) for call in connection.calls)


@pytest.mark.parametrize(
    "table_name",
    [
        "stocks",
        "kline_daily",
        "daily_date_universe_registry",
        "daily_date_universe_member",
        "daily_coverage_audit",
        "kline_daily_intraday",
    ],
)
def test_bootstrap_neeq_checks_normalize_case_and_whitespace_before_matching(table_name):
    from utils.clickhouse_schema import MARKET_TABLE_DDL

    ddl = next(statement for statement in MARKET_TABLE_DDL if f"CREATE TABLE IF NOT EXISTS {table_name}" in statement)

    assert "match(upper(trimBoth(ifNull(toString(code), '')))," in ddl


@pytest.mark.parametrize(
    ("table_name", "constraint_name"),
    [
        (table, constraint)
        for table in ("stocks", "daily_date_universe_registry", "daily_date_universe_member", "daily_coverage_audit")
        for constraint in ("codex_no_neeq_code", "codex_no_neeq_legacy_bj_code")
    ],
)
def test_startup_contract_rejects_missing_neeq_check_on_all_guarded_tables(table_name, constraint_name):
    from utils.clickhouse_schema import MARKET_TABLE_DDL

    ddl = next(statement for statement in MARKET_TABLE_DDL if f"CREATE TABLE IF NOT EXISTS {table_name}" in statement)
    ddl = "\n".join(line for line in ddl.splitlines() if constraint_name not in line)
    connection = _Connection(constraint_ddl_overrides={table_name: ddl})

    with pytest.raises(ClickHouseSchemaContractError, match=constraint_name):
        verify_startup_schema_contract(connection)

    assert not any(call.strip().upper().startswith(("CREATE", "ALTER", "DROP")) for call in connection.calls)


@pytest.mark.parametrize(
    ("table_name", "constraint_name"),
    [
        (table, constraint)
        for table in ("stocks", "daily_date_universe_registry", "daily_date_universe_member", "daily_coverage_audit")
        for constraint in ("codex_no_neeq_code", "codex_no_neeq_legacy_bj_code")
    ],
)
def test_startup_contract_rejects_wrong_neeq_expression_on_all_guarded_tables(table_name, constraint_name):
    from utils.clickhouse_schema import MARKET_TABLE_DDL

    ddl = next(statement for statement in MARKET_TABLE_DDL if f"CREATE TABLE IF NOT EXISTS {table_name}" in statement)
    ddl = "\n".join(
        line.replace(f"CONSTRAINT {constraint_name} CHECK", f"CONSTRAINT {constraint_name} CHECK 1")
        if constraint_name in line
        else line
        for line in ddl.splitlines()
    )
    connection = _Connection(constraint_ddl_overrides={table_name: ddl})

    with pytest.raises(ClickHouseSchemaContractError, match=constraint_name):
        verify_startup_schema_contract(connection)

    assert not any(call.strip().upper().startswith(("CREATE", "ALTER", "DROP")) for call in connection.calls)


@pytest.mark.parametrize(
    ("table_name", "column_name", "actual_type"),
    [
        ("daily_date_universe_registry", "member_count", "String"),
        ("daily_date_universe_registry", "published_at", "Nullable(DateTime)"),
        ("daily_date_universe_member", "trade_date", "String"),
        ("daily_date_universe_member", "member_ordinal", "String"),
    ],
)
def test_startup_contract_rejects_wrong_registry_member_column_types(table_name, column_name, actual_type):
    connection = _Connection(column_type_overrides={(table_name, column_name): actual_type})

    with pytest.raises(ClickHouseSchemaContractError, match=column_name):
        verify_startup_schema_contract(connection)


def test_startup_contract_rejects_missing_kline_history_guard():
    from utils.clickhouse_schema import MARKET_TABLE_DDL

    ddl = next(statement for statement in MARKET_TABLE_DDL if "CREATE TABLE IF NOT EXISTS kline_daily" in statement)
    ddl = "\n".join(
        line for line in ddl.splitlines() if "codex_kline_maintenance_from_2000" not in line
    )
    connection = _Connection(constraint_ddl_overrides={"kline_daily": ddl})

    with pytest.raises(ClickHouseSchemaContractError, match="codex_kline_maintenance_from_2000"):
        verify_startup_schema_contract(connection)


def test_kline_history_constraint_reads_column_types_before_installing_guard():
    connection = _HistoryConstraintConnection()

    ensure_kline_history_constraints(connection)

    assert any("SELECT table, name, type FROM system.columns" in sql for sql in connection.calls)
    assert any("ADD CONSTRAINT codex_kline_maintenance_from_2000" in sql for sql in connection.calls)


def test_kline_history_constraint_is_not_reinstalled_when_bootstrap_ddl_has_it():
    from utils.clickhouse_schema import MARKET_TABLE_DDL

    ddl = next(statement for statement in MARKET_TABLE_DDL if "CREATE TABLE IF NOT EXISTS kline_daily" in statement)
    connection = _HistoryConstraintConnection(ddl=ddl)

    ensure_kline_history_constraints(connection)

    assert not any("ALTER TABLE" in sql.upper() for sql in connection.calls)


def test_neeq_constraint_with_same_name_but_wrong_expression_fails_closed():
    connection = _NeeqConstraintConnection()

    with pytest.raises(ClickHouseSchemaContractError, match="codex_no_neeq_code"):
        ensure_neeq_security_constraints(connection)

    assert not any("ALTER TABLE" in sql.upper() for sql in connection.calls)


def test_neeq_constraints_matching_bootstrap_ddl_are_not_reinstalled():
    from utils.clickhouse_schema import MARKET_TABLE_DDL

    ddl = next(statement for statement in MARKET_TABLE_DDL if "CREATE TABLE IF NOT EXISTS kline_daily" in statement)
    # system.tables.create_table_query renders string literals with backslashes doubled
    connection = _NeeqConstraintConnection(ddl=ddl.replace("\\", "\\\\"))

    ensure_neeq_security_constraints(connection)

    assert not any("ALTER TABLE" in sql.upper() for sql in connection.calls)


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

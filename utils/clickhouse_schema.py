"""
ClickHouse bootstrap DDL for tables that are managed outside SQLAlchemy create_all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from sqlalchemy import Engine, text


MARKET_TABLE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS stocks
    (
        code String,
        name String,
        market String,
        type String,
        industry Nullable(String),
        region Nullable(String),
        list_date Nullable(Date),
        delist_date Nullable(Date),
        quit UInt8 DEFAULT 0,
        st UInt8 DEFAULT 0,
        industry_code Nullable(String),
        self_selected UInt8 DEFAULT 0,
        holding UInt8 DEFAULT 0,
        float_share Nullable(Float64),
        total_share Nullable(Float64),
        created_at DateTime DEFAULT now(),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY code
    """,
    """
    CREATE TABLE IF NOT EXISTS kline_daily
    (
        code String,
        trade_date Date,
        open Float64,
        high Float64,
        low Float64,
        close Float64,
        volume Float64 DEFAULT 0,
        amount Float64 DEFAULT 0,
        amplitude Nullable(Float64),
        change_pct Nullable(Float64),
        change_amount Nullable(Float64),
        turnover_rate Nullable(Float64),
        created_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(created_at)
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (code, trade_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_date_universe_registry
    (
        dataset_id String,
        code String,
        security_type LowCardinality(String),
        identity_status String,
        source_kind String,
        source_ref String,
        source_sha256 String,
        coverage_start Nullable(Date),
        coverage_end Nullable(Date),
        completeness_state LowCardinality(String),
        date_set_sha256 String,
        member_count UInt64,
        published_at DateTime('Asia/Shanghai'),
        is_active UInt8 DEFAULT 1
    )
    ENGINE = ReplacingMergeTree(published_at)
    ORDER BY (code, dataset_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_date_universe_member
    (
        dataset_id String,
        code String,
        trade_date Date,
        member_ordinal UInt32,
        date_set_sha256 String,
        published_at DateTime('Asia/Shanghai')
    )
    ENGINE = MergeTree
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (dataset_id, code, trade_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_coverage_audit
    (
        audit_id String,
        code String,
        trade_date Date,
        coverage_start Date,
        coverage_end Date,
        date_universe_id String,
        date_set_sha256 String,
        audit_state LowCardinality(String),
        status_audit_ref String,
        minute_reference_required UInt8,
        evidence_json String,
        observed_at DateTime('Asia/Shanghai')
    )
    ENGINE = ReplacingMergeTree(observed_at)
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (code, trade_date, audit_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS kline_daily_intraday
    (
        code String,
        trade_date Date,
        open Float64,
        high Float64,
        low Float64,
        close Float64,
        volume Float64 DEFAULT 0,
        amount Float64 DEFAULT 0,
        previous_close Nullable(Float64),
        amplitude Nullable(Float64),
        change_pct Nullable(Float64),
        change_amount Nullable(Float64),
        turnover_rate Nullable(Float64),
        snapshot_at DateTime('Asia/Shanghai'),
        source String DEFAULT 'qmt_intraday',
        is_provisional UInt8 DEFAULT 1
    )
    ENGINE = ReplacingMergeTree(snapshot_at)
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (code, trade_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS emotion_cycle
    (
        id Int64,
        date Date,
        close_up_rate Nullable(Float64),
        intraday_up_rate Nullable(Float64),
        sh_up_rate Nullable(Float64),
        sz_up_rate Nullable(Float64),
        gem_up_rate Nullable(Float64),
        cyb_up_rate Nullable(Float64),
        strong_up_rate Nullable(Float64),
        limit_up_follow_rate Nullable(Float64),
        weak_up_rate Nullable(Float64),
        yesterday_monster_up_rate Nullable(Float64),
        yesterday_strong_up_rate Nullable(Float64),
        yesterday_weak_up_rate Nullable(Float64),
        total_stocks Int32,
        is_confirmed Int32 DEFAULT 0,
        created_at DateTime DEFAULT now(),
        updated_at DateTime DEFAULT now()
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY date
    """,
    """
    CREATE TABLE IF NOT EXISTS market_sentiment_snapshot
    (
        trade_date Date,
        snapshot_at DateTime('Asia/Shanghai'),
        is_provisional UInt8,
        covered_count UInt32,
        expected_count UInt32,
        up_count UInt32,
        down_count UInt32,
        unchanged_count UInt32,
        up_5_count UInt32,
        down_5_count UInt32,
        limit_up_count UInt32,
        limit_down_count UInt32,
        avg_change_percent Float64,
        total_amount Float64,
        sh_amount Float64,
        sz_amount Float64,
        bj_amount Float64,
        bucket_up_7 UInt32,
        bucket_up_5_7 UInt32,
        bucket_up_3_5 UInt32,
        bucket_up_0_3 UInt32,
        bucket_zero UInt32,
        bucket_down_0_3 UInt32,
        bucket_down_3_5 UInt32,
        bucket_down_5_7 UInt32,
        bucket_down_7 UInt32,
        source String
    )
    ENGINE = ReplacingMergeTree(snapshot_at)
    PARTITION BY toYYYYMM(trade_date)
    ORDER BY (trade_date, is_provisional)
    """,
    """
    CREATE TABLE IF NOT EXISTS market_margin_sentiment
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
    """,
    """
    CREATE TABLE IF NOT EXISTS source_sectors
    (
        source String,
        sector_code String,
        sector_name String,
        sector_type String,
        level Int32 DEFAULT 0,
        canonical_sector_code String DEFAULT '',
        canonical_sector_name String DEFAULT '',
        match_method String DEFAULT '',
        confidence Float64 DEFAULT 0,
        alias_status String DEFAULT '',
        snapshot_date Date,
        snapshot_at DateTime,
        id String
    )
    ENGINE = ReplacingMergeTree(snapshot_at)
    ORDER BY (source, snapshot_date, sector_code, canonical_sector_code)
    """,
    """
    CREATE TABLE IF NOT EXISTS source_sector_stocks
    (
        source String,
        sector_code String,
        sector_name String,
        sector_type String,
        source_stock_code String,
        canonical_code String,
        alias_status String DEFAULT '',
        match_method String DEFAULT '',
        confidence Float64 DEFAULT 0,
        snapshot_date Date,
        snapshot_at DateTime,
        id String
    )
    ENGINE = ReplacingMergeTree(snapshot_at)
    ORDER BY (source, snapshot_date, sector_code, canonical_code, source_stock_code)
    """,
]


STRATEGY_TABLE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS user_stocks
    (
        id Int64,
        user_id Int32,
        code String,
        stock_name Nullable(String),
        group_name String,
        is_holding UInt8,
        position_shares Nullable(Int64),
        position_cost Nullable(Decimal(10, 3)),
        notes Nullable(String),
        sort_order Int32,
        created_at DateTime,
        updated_at DateTime
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (user_id, code)
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_pool_runs
    (
        id Int64,
        strategy_name String,
        trade_date Date,
        run_type String,
        param_version String,
        total_scanned Int32,
        total_candidates Int32,
        notes Nullable(String),
        created_at DateTime
    )
    ENGINE = MergeTree
    ORDER BY id
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_candidates
    (
        id Int64,
        strategy_name String,
        code String,
        stock_name Nullable(String),
        first_detected_date Date,
        last_detected_date Date,
        current_trade_date Date,
        current_state String,
        alert_level Nullable(String),
        is_active UInt8,
        box_top Nullable(Decimal(10, 3)),
        box_bottom Nullable(Decimal(10, 3)),
        last_close Nullable(Decimal(10, 3)),
        trigger_price Nullable(Decimal(10, 3)),
        trigger_reason Nullable(String),
        param_version String,
        notes Nullable(String),
        created_at DateTime,
        updated_at DateTime
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY id
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_state_history
    (
        id Int64,
        strategy_name String,
        candidate_id Nullable(Int64),
        code String,
        stock_name Nullable(String),
        trade_date Date,
        state String,
        alert_level Nullable(String),
        is_confirmed UInt8,
        close_price Nullable(Decimal(10, 3)),
        trigger_price Nullable(Decimal(10, 3)),
        box_top Nullable(Decimal(10, 3)),
        box_bottom Nullable(Decimal(10, 3)),
        trigger_reason Nullable(String),
        created_at DateTime
    )
    ENGINE = MergeTree
    ORDER BY id
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_alerts
    (
        id Int64,
        strategy_name String,
        candidate_id Nullable(Int64),
        code String,
        stock_name Nullable(String),
        trade_date Date,
        state String,
        alert_level String,
        channel String,
        title Nullable(String),
        message Nullable(String),
        trigger_price Nullable(Decimal(10, 3)),
        box_top Nullable(Decimal(10, 3)),
        box_bottom Nullable(Decimal(10, 3)),
        is_sent UInt8,
        sent_at Nullable(DateTime),
        created_at DateTime
    )
    ENGINE = MergeTree
    ORDER BY id
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_backtest_runs
    (
        id Int64,
        strategy_name String,
        task_id String,
        start_date Nullable(Date),
        end_date Nullable(Date),
        status String,
        summary_json Nullable(String),
        error_message Nullable(String),
        created_at DateTime,
        completed_at Nullable(DateTime)
    )
    ENGINE = ReplacingMergeTree(created_at)
    ORDER BY id
    """,
    """
    CREATE TABLE IF NOT EXISTS selector_runs
    (
        id Int64,
        selector_name String,
        run_type String,
        status String,
        params_json Nullable(String),
        summary_json Nullable(String),
        total_items Int32,
        total_signals Int32,
        notes Nullable(String),
        created_at DateTime,
        updated_at DateTime
    )
    ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY id
    """,
    """
    CREATE TABLE IF NOT EXISTS selector_run_records
    (
        id Int64,
        run_id Int64,
        record_type String,
        code Nullable(String),
        stock_name Nullable(String),
        signal_date Nullable(Date),
        payload_json Nullable(String),
        created_at DateTime
    )
    ENGINE = MergeTree
    ORDER BY (run_id, id)
    """,
]


class ClickHouseSchemaContractError(RuntimeError):
    """Raised when the live ClickHouse schema cannot satisfy the app contract."""


@dataclass(frozen=True)
class ClickHouseTableContract:
    name: str
    engine_prefix: str
    partition_key: str
    sorting_key: str
    required_columns: frozenset[str]


_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


STARTUP_TABLE_CONTRACTS = (
    ClickHouseTableContract(
        name="stocks",
        engine_prefix="ReplacingMergeTree",
        partition_key="",
        sorting_key="code",
        required_columns=frozenset(
            {
                "code",
                "name",
                "market",
                "type",
                "list_date",
                "delist_date",
                "created_at",
                "updated_at",
            }
        ),
    ),
    ClickHouseTableContract(
        name="kline_daily",
        engine_prefix="ReplacingMergeTree",
        partition_key="toYYYYMM(trade_date)",
        sorting_key="(code,trade_date)",
        required_columns=frozenset(
            {
                "code",
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "created_at",
            }
        ),
    ),
    ClickHouseTableContract(
        name="daily_date_universe_registry",
        engine_prefix="ReplacingMergeTree",
        partition_key="",
        sorting_key="(code,dataset_id)",
        required_columns=frozenset(
            {
                "dataset_id",
                "code",
                "security_type",
                "identity_status",
                "source_kind",
                "source_ref",
                "source_sha256",
                "coverage_start",
                "coverage_end",
                "completeness_state",
                "date_set_sha256",
                "member_count",
                "published_at",
                "is_active",
            }
        ),
    ),
    ClickHouseTableContract(
        name="daily_date_universe_member",
        engine_prefix="MergeTree",
        partition_key="toYYYYMM(trade_date)",
        sorting_key="(dataset_id,code,trade_date)",
        required_columns=frozenset(
            {
                "dataset_id",
                "code",
                "trade_date",
                "member_ordinal",
                "date_set_sha256",
                "published_at",
            }
        ),
    ),
    ClickHouseTableContract(
        name="daily_coverage_audit",
        engine_prefix="ReplacingMergeTree",
        partition_key="toYYYYMM(trade_date)",
        sorting_key="(code,trade_date,audit_id)",
        required_columns=frozenset(
            {
                "audit_id",
                "code",
                "trade_date",
                "coverage_start",
                "coverage_end",
                "date_universe_id",
                "date_set_sha256",
                "audit_state",
                "status_audit_ref",
                "minute_reference_required",
                "evidence_json",
                "observed_at",
            }
        ),
    ),
)


def _normalize_contract_text(value: Any) -> str:
    return re.sub(r"[\s`()]+", "", str(value or "")).lower()


def _result_rows(result: Any) -> list[Mapping[str, Any] | tuple[Any, ...]]:
    """Read SQLAlchemy and lightweight fake results without changing query shape."""
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        return list(mappings().all())
    fetchall = getattr(result, "fetchall", None)
    if callable(fetchall):
        return list(fetchall())
    rows = getattr(result, "result_rows", None)
    return list(rows or [])


def _row_value(row: Mapping[str, Any] | tuple[Any, ...], key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    return row[index] if len(row) > index else None


def startup_bootstrap_table_names(
    market_ddl: Iterable[str] = MARKET_TABLE_DDL,
    strategy_ddl: Iterable[str] = STRATEGY_TABLE_DDL,
) -> tuple[str, ...]:
    """Return the table names that an explicit bootstrap owns."""
    names = {
        name.lower()
        for ddl in [*market_ddl, *strategy_ddl]
        for name in _CREATE_TABLE_RE.findall(ddl)
    }
    return tuple(sorted(names))


def verify_startup_schema_contract(
    connection: Any,
    *,
    contracts: Iterable[ClickHouseTableContract] = STARTUP_TABLE_CONTRACTS,
) -> None:
    """Verify the minimum live schema without issuing any DDL.

    Startup must fail closed when a managed table is missing or when a critical
    table has a different engine, partition, sorting key, or required column.
    Explicit migration/bootstrap commands remain the only DDL entry point.
    """
    expected_tables = set(startup_bootstrap_table_names())
    table_rows = _result_rows(
        connection.execute(
            text(
                "SELECT name FROM system.tables "
                "WHERE database = currentDatabase()"
            )
        )
    )
    actual_tables = {
        str(_row_value(row, "name", 0) or "").lower() for row in table_rows
    }
    problems: list[str] = []
    missing_tables = sorted(expected_tables - actual_tables)
    if missing_tables:
        problems.append("missing tables: " + ", ".join(missing_tables))

    for contract in contracts:
        if contract.name.lower() not in actual_tables:
            continue
        table_result = connection.execute(
            text(
                "SELECT engine, partition_key, sorting_key "
                "FROM system.tables "
                "WHERE database = currentDatabase() AND name = :table_name"
            ),
            {"table_name": contract.name},
        )
        table_rows = _result_rows(table_result)
        if not table_rows:
            problems.append(f"{contract.name}: metadata row missing")
            continue
        table_row = table_rows[0]
        engine = str(_row_value(table_row, "engine", 0) or "")
        partition_key = _normalize_contract_text(_row_value(table_row, "partition_key", 1))
        sorting_key = _normalize_contract_text(_row_value(table_row, "sorting_key", 2))
        if not engine.lower().startswith(contract.engine_prefix.lower()):
            problems.append(
                f"{contract.name}: engine {engine!r} does not start with "
                f"{contract.engine_prefix!r}"
            )
        if partition_key != _normalize_contract_text(contract.partition_key):
            problems.append(
                f"{contract.name}: partition_key {partition_key!r} != "
                f"{_normalize_contract_text(contract.partition_key)!r}"
            )
        if sorting_key != _normalize_contract_text(contract.sorting_key):
            problems.append(
                f"{contract.name}: sorting_key {sorting_key!r} != "
                f"{_normalize_contract_text(contract.sorting_key)!r}"
            )

        column_result = connection.execute(
            text(
                "SELECT name FROM system.columns "
                "WHERE database = currentDatabase() AND table = :table_name"
            ),
            {"table_name": contract.name},
        )
        column_rows = _result_rows(column_result)
        actual_columns = {
            str(_row_value(row, "name", 0) or "") for row in column_rows
        }
        missing_columns = sorted(contract.required_columns - actual_columns)
        if missing_columns:
            problems.append(
                f"{contract.name}: missing columns: {', '.join(missing_columns)}"
            )

    if problems:
        raise ClickHouseSchemaContractError(
            "ClickHouse startup schema contract failed; no DDL was issued: "
            + "; ".join(problems)
        )


def ensure_clickhouse_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        for ddl in [*MARKET_TABLE_DDL, *STRATEGY_TABLE_DDL]:
            connection.execute(text(ddl))
        # The table predates this field on deployed hosts.  Keep the migration
        # idempotent so existing historical metadata remains usable.
        connection.execute(text("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS delist_date Nullable(Date) AFTER list_date"))

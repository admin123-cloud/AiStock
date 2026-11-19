"""
ClickHouse bootstrap DDL for tables that are managed outside SQLAlchemy create_all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from sqlalchemy import Engine, text

from utils.security_market_policy import clickhouse_normalized_code_expression


_NEEQ_SECURITY_CODE_FIELDS = frozenset(
    {
        "code",
        "stock_code",
        "security_code",
        "canonical_code",
        "source_code",
        "source_stock_code",
        "source_symbol",
    }
)
_NEEQ_SECURITY_CODE_PATTERN = (
    r"^(?:(?:400|420|430)[0-9]{3}|(?:43|83|87)[0-9]{4})"
    r"(?:\.(?:SZ|SH|NQ|NEEQ))?$|^88[0-9]{4}(?:\.(?:SZ|NQ|NEEQ))?$"
)
_NEEQ_LEGACY_BJ_PATTERN = r"^(?:400|420|430)[0-9]{3}\.BJ$"


def _neeq_check_constraints(column: str = "code") -> tuple[tuple[str, str], ...]:
    normalized_code = clickhouse_normalized_code_expression(column)
    return (
        (
            f"codex_no_neeq_{column}",
            f"CHECK NOT match({normalized_code}, '{_NEEQ_SECURITY_CODE_PATTERN}')",
        ),
        (
            f"codex_no_neeq_legacy_bj_{column}",
            f"CHECK NOT match({normalized_code}, '{_NEEQ_LEGACY_BJ_PATTERN}')",
        ),
    )


MARKET_TABLE_DDL = [
    """
    CREATE TABLE IF NOT EXISTS stocks
    (
        code String,
        CONSTRAINT codex_no_neeq_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:(?:400|420|430)[0-9]{3}|(?:43|83|87)[0-9]{4})(?:\\.(?:SZ|SH|NQ|NEEQ))?$|^88[0-9]{4}(?:\\.(?:SZ|NQ|NEEQ))?$'),
        CONSTRAINT codex_no_neeq_legacy_bj_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:400|420|430)[0-9]{3}\\.BJ$'),
        name String,
        market String,
        type String,
        listing_status LowCardinality(String) DEFAULT 'unknown',
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
        CONSTRAINT codex_no_neeq_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:(?:400|420|430)[0-9]{3}|(?:43|83|87)[0-9]{4})(?:\\.(?:SZ|SH|NQ|NEEQ))?$|^88[0-9]{4}(?:\\.(?:SZ|NQ|NEEQ))?$'),
        CONSTRAINT codex_no_neeq_legacy_bj_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:400|420|430)[0-9]{3}\\.BJ$'),
        trade_date Date,
        CONSTRAINT codex_kline_maintenance_from_2000 CHECK trade_date >= toDate('2000-01-01'),
        open Float64,
        high Float64,
        low Float64,
        close Float64,
        volume Float64 DEFAULT 0,
        amount Float64 DEFAULT 0,
        amplitude Float64,
        change_pct Float64,
        change_amount Float64,
        turnover_rate Float64,
        created_at DateTime DEFAULT toDateTime('1970-01-01 00:00:00'),
        id UInt64
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
        CONSTRAINT codex_no_neeq_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:(?:400|420|430)[0-9]{3}|(?:43|83|87)[0-9]{4})(?:\\.(?:SZ|SH|NQ|NEEQ))?$|^88[0-9]{4}(?:\\.(?:SZ|NQ|NEEQ))?$'),
        CONSTRAINT codex_no_neeq_legacy_bj_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:400|420|430)[0-9]{3}\\.BJ$'),
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
        CONSTRAINT codex_no_neeq_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:(?:400|420|430)[0-9]{3}|(?:43|83|87)[0-9]{4})(?:\\.(?:SZ|SH|NQ|NEEQ))?$|^88[0-9]{4}(?:\\.(?:SZ|NQ|NEEQ))?$'),
        CONSTRAINT codex_no_neeq_legacy_bj_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:400|420|430)[0-9]{3}\\.BJ$'),
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
        CONSTRAINT codex_no_neeq_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:(?:400|420|430)[0-9]{3}|(?:43|83|87)[0-9]{4})(?:\\.(?:SZ|SH|NQ|NEEQ))?$|^88[0-9]{4}(?:\\.(?:SZ|NQ|NEEQ))?$'),
        CONSTRAINT codex_no_neeq_legacy_bj_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:400|420|430)[0-9]{3}\\.BJ$'),
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
        CONSTRAINT codex_no_neeq_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:(?:400|420|430)[0-9]{3}|(?:43|83|87)[0-9]{4})(?:\\.(?:SZ|SH|NQ|NEEQ))?$|^88[0-9]{4}(?:\\.(?:SZ|NQ|NEEQ))?$'),
        CONSTRAINT codex_no_neeq_legacy_bj_code CHECK NOT match(upper(trimBoth(ifNull(toString(code), ''))), '^(?:400|420|430)[0-9]{3}\\.BJ$'),
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


KLINE_DAILY_REQUIRED_COLUMN_TYPES: tuple[tuple[str, str], ...] = (
    ("code", "String"),
    ("trade_date", "Date"),
    ("open", "Float64"),
    ("high", "Float64"),
    ("low", "Float64"),
    ("close", "Float64"),
    ("volume", "Float64"),
    ("amount", "Float64"),
    ("amplitude", "Float64"),
    ("change_pct", "Float64"),
    ("change_amount", "Float64"),
    ("turnover_rate", "Float64"),
    ("created_at", "DateTime"),
    ("id", "UInt64"),
)
KLINE_DAILY_ENGINE_FULL_PREFIX = "ReplacingMergeTree(created_at)"
KLINE_DAILY_PARTITION_KEY = "toYYYYMM(trade_date)"
KLINE_DAILY_SORTING_KEY = "(code,trade_date)"


@dataclass(frozen=True)
class ClickHouseTableContract:
    name: str
    engine_prefix: str
    partition_key: str
    sorting_key: str
    required_columns: frozenset[str]
    engine_full_prefix: str | None = None
    required_column_types: tuple[tuple[str, str], ...] = ()
    required_check_constraints: tuple[tuple[str, str], ...] = ()


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
        engine_full_prefix="ReplacingMergeTree(updated_at)",
        required_columns=frozenset(
            {
                "code",
                "name",
                "market",
                "type",
                "listing_status",
                "industry",
                "region",
                "list_date",
                "delist_date",
                "quit",
                "st",
                "industry_code",
                "self_selected",
                "holding",
                "float_share",
                "total_share",
                "created_at",
                "updated_at",
            }
        ),
        required_column_types=(
            ("code", "String"),
            ("name", "String"),
            ("market", "String"),
            ("type", "String"),
            ("listing_status", "LowCardinality(String)"),
            ("industry", "Nullable(String)"),
            ("region", "Nullable(String)"),
            ("list_date", "Nullable(Date)"),
            ("delist_date", "Nullable(Date)"),
            ("quit", "UInt8"),
            ("st", "UInt8"),
            ("industry_code", "Nullable(String)"),
            ("self_selected", "UInt8"),
            ("holding", "UInt8"),
            ("float_share", "Nullable(Float64)"),
            ("total_share", "Nullable(Float64)"),
            ("created_at", "DateTime"),
            ("updated_at", "DateTime"),
        ),
        required_check_constraints=_neeq_check_constraints(),
    ),
    ClickHouseTableContract(
        name="kline_daily",
        engine_prefix="ReplacingMergeTree",
        partition_key=KLINE_DAILY_PARTITION_KEY,
        sorting_key=KLINE_DAILY_SORTING_KEY,
        required_columns=frozenset(name for name, _ in KLINE_DAILY_REQUIRED_COLUMN_TYPES),
        engine_full_prefix=KLINE_DAILY_ENGINE_FULL_PREFIX,
        required_column_types=KLINE_DAILY_REQUIRED_COLUMN_TYPES,
        required_check_constraints=(
            *_neeq_check_constraints(),
            (
                "codex_kline_maintenance_from_2000",
                "CHECK trade_date >= toDate('2000-01-01')",
            ),
        ),
    ),
    ClickHouseTableContract(
        name="daily_date_universe_registry",
        engine_prefix="ReplacingMergeTree",
        engine_full_prefix="ReplacingMergeTree(published_at)",
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
        required_column_types=(
            ("dataset_id", "String"),
            ("code", "String"),
            ("security_type", "LowCardinality(String)"),
            ("identity_status", "String"),
            ("source_kind", "String"),
            ("source_ref", "String"),
            ("source_sha256", "String"),
            ("coverage_start", "Nullable(Date)"),
            ("coverage_end", "Nullable(Date)"),
            ("completeness_state", "LowCardinality(String)"),
            ("date_set_sha256", "String"),
            ("member_count", "UInt64"),
            ("published_at", "DateTime('Asia/Shanghai')"),
            ("is_active", "UInt8"),
        ),
        required_check_constraints=_neeq_check_constraints(),
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
        required_column_types=(
            ("dataset_id", "String"),
            ("code", "String"),
            ("trade_date", "Date"),
            ("member_ordinal", "UInt32"),
            ("date_set_sha256", "String"),
            ("published_at", "DateTime('Asia/Shanghai')"),
        ),
        required_check_constraints=_neeq_check_constraints(),
    ),
    ClickHouseTableContract(
        name="daily_coverage_audit",
        engine_prefix="ReplacingMergeTree",
        engine_full_prefix="ReplacingMergeTree(observed_at)",
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
        required_column_types=(
            ("audit_id", "String"),
            ("code", "String"),
            ("trade_date", "Date"),
            ("coverage_start", "Date"),
            ("coverage_end", "Date"),
            ("date_universe_id", "String"),
            ("date_set_sha256", "String"),
            ("audit_state", "LowCardinality(String)"),
            ("status_audit_ref", "String"),
            ("minute_reference_required", "UInt8"),
            ("evidence_json", "String"),
            ("observed_at", "DateTime('Asia/Shanghai')"),
        ),
        required_check_constraints=_neeq_check_constraints(),
    ),
)


def _normalize_contract_text(value: Any) -> str:
    return re.sub(r"[\s`()]+", "", str(value or "")).lower()


def _compact_sql_fragment(value: Any) -> str:
    """Remove formatting-only whitespace and identifier quoting for comparisons."""
    return re.sub(r"\s+", "", str(value or "")).replace("`", "")


def _server_rendered_constraint_fragment(constraint_name: str, expression: str) -> str:
    """Compact a CHECK constraint the way ClickHouse renders it in
    ``system.tables.create_table_query``.

    The server re-serialises string literals with backslashes doubled, so the
    expected fragment must be escaped the same way; otherwise a semantically
    identical constraint can never match (verified on 24.8.14.39 against a
    table created verbatim from ``MARKET_TABLE_DDL``).
    """
    rendered = f"CONSTRAINT {constraint_name} {expression}".replace("\\", "\\\\")
    return _compact_sql_fragment(rendered)


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
    table has a different engine/version expression, partition, sorting key,
    required column, or required column type. Explicit migration/bootstrap
    commands remain the only DDL entry point.
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
                "SELECT engine, engine_full, partition_key, sorting_key, create_table_query "
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
        engine_full = str(_row_value(table_row, "engine_full", 1) or "")
        partition_key = _normalize_contract_text(_row_value(table_row, "partition_key", 2))
        sorting_key = _normalize_contract_text(_row_value(table_row, "sorting_key", 3))
        create_table_query = str(_row_value(table_row, "create_table_query", 4) or "")
        if not engine.lower().startswith(contract.engine_prefix.lower()):
            problems.append(
                f"{contract.name}: engine {engine!r} does not start with "
                f"{contract.engine_prefix!r}"
            )
        if contract.engine_full_prefix and not engine_full.lower().startswith(contract.engine_full_prefix.lower()):
            problems.append(
                f"{contract.name}: engine_full {engine_full!r} does not start with "
                f"{contract.engine_full_prefix!r}"
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
        compact_create_table_query = _compact_sql_fragment(create_table_query).lower()
        for constraint_name, expression in contract.required_check_constraints:
            expected_constraint = _server_rendered_constraint_fragment(constraint_name, expression).lower()
            if expected_constraint not in compact_create_table_query:
                problems.append(
                    f"{contract.name}: CHECK constraint {constraint_name} missing or expression mismatch"
                )

        column_result = connection.execute(
            text(
                "SELECT name, type FROM system.columns "
                "WHERE database = currentDatabase() AND table = :table_name"
            ),
            {"table_name": contract.name},
        )
        column_rows = _result_rows(column_result)
        actual_column_types = {
            str(_row_value(row, "name", 0) or ""): str(_row_value(row, "type", 1) or "")
            for row in column_rows
        }
        actual_columns = set(actual_column_types)
        missing_columns = sorted(contract.required_columns - actual_columns)
        if missing_columns:
            problems.append(
                f"{contract.name}: missing columns: {', '.join(missing_columns)}"
            )
        for column_name, expected_type in contract.required_column_types:
            actual_type = actual_column_types.get(column_name)
            if actual_type != expected_type:
                problems.append(
                    f"{contract.name}: column {column_name} type {actual_type!r} != {expected_type!r}"
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
        # These columns predate the current canonical table definition on some
        # deployed hosts. Explicit bootstrap adds conservative defaults; an
        # unknown listing state remains ineligible for active-stock writes.
        connection.execute(
            text(
                "ALTER TABLE stocks ADD COLUMN IF NOT EXISTS listing_status "
                "LowCardinality(String) DEFAULT 'unknown' AFTER type"
            )
        )
        connection.execute(text("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS delist_date Nullable(Date) AFTER list_date"))
        ensure_neeq_security_constraints(connection)
        ensure_kline_history_constraints(connection)


def ensure_kline_history_constraints(connection: Any) -> None:
    """Enforce the 2000 start date on physical K-line bar tables."""
    table_rows = _result_rows(
        connection.execute(
            text(
                "SELECT name, engine, create_table_query "
                "FROM system.tables WHERE database = currentDatabase()"
            )
        )
    )
    column_rows = _result_rows(
        connection.execute(
            text(
                "SELECT table, name, type FROM system.columns "
                "WHERE database = currentDatabase()"
            )
        )
    )
    database_rows = _result_rows(
        connection.execute(text("SELECT currentDatabase() AS database_name"))
    )
    database = str(_row_value(database_rows[0], "database_name", 0) or "stock")
    metadata = {
        str(_row_value(row, "name", 0) or ""): {
            "engine": str(_row_value(row, "engine", 1) or ""),
            "ddl": str(_row_value(row, "create_table_query", 2) or ""),
        }
        for row in table_rows
    }
    columns: dict[str, set[str]] = {}
    for row in column_rows:
        table = str(_row_value(row, "table", 0) or "")
        column = str(_row_value(row, "name", 1) or "")
        columns.setdefault(table, set()).add(column)

    column_types: dict[str, dict[str, str]] = {}
    for row in column_rows:
        table = str(_row_value(row, "table", 0) or "")
        column = str(_row_value(row, "name", 1) or "")
        column_type = str(_row_value(row, "type", 2) or "")
        column_types.setdefault(table, {})[column] = column_type

    ignored_engines = {"view", "materializedview", "liveview", "windowview"}
    for table, item in metadata.items():
        if item["engine"].lower() in ignored_engines:
            continue
        types = column_types.get(table, {})
        full_ohlc = {"open", "high", "low", "close"}.issubset(types)
        partial_daily_aggregate = {
            "code", "trade_date", "close", "volume", "amount", "bars"
        }.issubset(types)
        if not full_ohlc and not partial_daily_aggregate:
            continue
        date_column = next(
            (
                column
                for column in (
                    "trade_date", "datetime", "week_start_date", "month_start_date",
                    "year", "source_timestamp", "timestamp", "date",
                )
                if column in types
            ),
            None,
        )
        if date_column is None:
            raise ClickHouseSchemaContractError(
                f"cannot install K-line date constraint on {database}.{table}: "
                "OHLC columns exist but no supported period date column was found"
            )

        quoted_column = f"`{date_column.replace('`', '``')}`"
        column_type = types[date_column]
        nullable = column_type.startswith("Nullable(")
        if date_column == "year":
            expression = (
                f"{'isNotNull(' + quoted_column + ') AND ' if nullable else ''}"
                f"{quoted_column} >= 2000"
            )
            predicate = f"{date_column}>=2000"
            null_guard = f"{date_column}isnotnull" if nullable else ""
        elif date_column == "source_timestamp" and any(
            token in column_type for token in ("Int", "UInt")
        ):
            expression = (
                f"{'isNotNull(' + quoted_column + ') AND ' if nullable else ''}"
                f"toDate(toDateTime({quoted_column}, 'UTC')) >= toDate('2000-01-01')"
            )
            predicate = "todate(todatetime(`source_timestamp`,'utc'))>=todate('2000-01-01')"
            null_guard = "sourcetimestampisnotnull" if nullable else ""
        elif "DateTime" in column_type:
            expression = (
                f"{'isNotNull(' + quoted_column + ') AND ' if nullable else ''}"
                f"{quoted_column} >= toDateTime('2000-01-01 00:00:00', 'Asia/Shanghai')"
            )
            predicate = f"{date_column}>=todatetime('2000-01-0100:00:00','asia/shanghai')"
            null_guard = f"{date_column}isnotnull" if nullable else ""
        elif "Date" in column_type:
            expression = (
                f"{'isNotNull(' + quoted_column + ') AND ' if nullable else ''}"
                f"{quoted_column} >= toDate('2000-01-01')"
            )
            predicate = f"{date_column}>=todate('2000-01-01')"
            null_guard = f"{date_column}isnotnull" if nullable else ""
        else:
            raise ClickHouseSchemaContractError(
                f"cannot install K-line date constraint on {database}.{table}: "
                f"unsupported {date_column} type {column_type}"
            )
        constraint = "codex_kline_maintenance_from_2000"
        compact_ddl = re.sub(r"\s+", "", item["ddl"]).replace("`", "").lower()
        if (
            constraint in item["ddl"]
            and predicate in compact_ddl
            and (not null_guard or null_guard in compact_ddl)
        ):
            continue
        table_id = f"`{database.replace('`', '``')}`.`{table.replace('`', '``')}`"
        if constraint in item["ddl"]:
            connection.execute(
                text(f"ALTER TABLE {table_id} DROP CONSTRAINT {constraint}")
            )
        connection.execute(
            text(
                f"ALTER TABLE {table_id} ADD CONSTRAINT {constraint} "
                f"CHECK {expression}"
            )
        )


def ensure_neeq_security_constraints(connection: Any) -> None:
    """Install fail-closed NEEQ CHECKs on every current/future code table.

    The DB-level checks complement application guards and cover direct INSERTs
    into existing tables. Running this during an explicit schema bootstrap also
    attaches the same checks to newly created code-bearing tables.
    """
    table_rows = _result_rows(
        connection.execute(
            text(
                "SELECT name, engine, create_table_query "
                "FROM system.tables WHERE database = currentDatabase()"
            )
        )
    )
    columns = _result_rows(
        connection.execute(
            text(
                "SELECT table, name, type FROM system.columns "
                "WHERE database = currentDatabase()"
            )
        )
    )
    physical = {
        str(_row_value(row, "name", 0) or ""): {
            "engine": str(_row_value(row, "engine", 1) or ""),
            "ddl": str(_row_value(row, "create_table_query", 2) or ""),
        }
        for row in table_rows
    }
    database_rows = _result_rows(
        connection.execute(text("SELECT currentDatabase() AS database_name"))
    )
    database = str(_row_value(database_rows[0], "database_name", 0) or "stock")
    pending_additions: list[str] = []
    conflicts: list[str] = []
    for row in columns:
        table = str(_row_value(row, "table", 0) or "")
        column = str(_row_value(row, "name", 1) or "")
        column_type = str(_row_value(row, "type", 2) or "")
        metadata = physical.get(table)
        if (
            metadata is None
            or metadata["engine"].lower() in {"view", "materializedview", "liveview", "windowview"}
            or column not in _NEEQ_SECURITY_CODE_FIELDS
            or "String" not in column_type
        ):
            continue

        table_id = f"`{database.replace('`', '``')}`.`{table.replace('`', '``')}`"
        compact_ddl = _compact_sql_fragment(metadata["ddl"])
        normalized_code = clickhouse_normalized_code_expression(column)
        constraints = (
            (f"codex_no_neeq_{column}", _NEEQ_SECURITY_CODE_PATTERN),
            (f"codex_no_neeq_legacy_bj_{column}", _NEEQ_LEGACY_BJ_PATTERN),
        )
        for constraint_name, pattern in constraints:
            expected_fragment = _server_rendered_constraint_fragment(
                constraint_name,
                f"CHECK NOT match({normalized_code}, '{pattern}')",
            )
            if constraint_name.lower() in metadata["ddl"].lower():
                if expected_fragment not in compact_ddl:
                    conflicts.append(f"{database}.{table}.{constraint_name}")
                continue
            pending_additions.append(
                f"ALTER TABLE {table_id} ADD CONSTRAINT {constraint_name} "
                f"CHECK NOT match({normalized_code}, '{pattern}')"
            )

    if conflicts:
        raise ClickHouseSchemaContractError(
            "existing NEEQ security constraint expression mismatch; no DDL was issued: "
            + ", ".join(sorted(set(conflicts)))
        )
    for statement in pending_additions:
        connection.execute(text(statement))

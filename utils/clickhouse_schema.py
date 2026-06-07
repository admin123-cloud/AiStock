"""
ClickHouse bootstrap DDL for tables that are managed outside SQLAlchemy create_all.
"""

from __future__ import annotations

from sqlalchemy import Engine, text


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


def ensure_clickhouse_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        for ddl in STRATEGY_TABLE_DDL:
            connection.execute(text(ddl))

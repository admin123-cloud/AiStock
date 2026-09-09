"""
ClickHouse bootstrap DDL for tables that are managed outside SQLAlchemy create_all.
"""

from __future__ import annotations

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


def ensure_clickhouse_tables(engine: Engine) -> None:
    with engine.begin() as connection:
        for ddl in [*MARKET_TABLE_DDL, *STRATEGY_TABLE_DDL]:
            connection.execute(text(ddl))
        # The table predates this field on deployed hosts.  Keep the migration
        # idempotent so existing historical metadata remains usable.
        connection.execute(text("ALTER TABLE stocks ADD COLUMN IF NOT EXISTS delist_date Nullable(Date) AFTER list_date"))

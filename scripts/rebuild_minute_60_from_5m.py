"""Rebuild the derived 60m table from the canonical 5m table without mutations."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from utils.market_warehouse import clickhouse_client


SOURCE_TABLE = "kline_minute_5"
DEFAULT_TARGET = "kline_minute_60_rebuild_20260831"
VALID_5M_TIMES = ",".join(
    f"'{hour:02d}:{minute:02d}'"
    for start_hour, start_minute, end_hour, end_minute in ((9, 30, 11, 30), (13, 0, 15, 0))
    for hour, minute in (
        divmod(total, 60)
        for total in range(start_hour * 60 + start_minute + 5, end_hour * 60 + end_minute + 1, 5)
    )
)


def quote_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum() or value[0].isdigit():
        raise ValueError(f"unsafe identifier: {value}")
    return value


def create_target(client, target: str) -> None:
    target = quote_identifier(target)
    client.command(f"DROP TABLE IF EXISTS {target}")
    client.command(
        f"""
        CREATE TABLE {target}
        (
          code Nullable(String),
          datetime Nullable(DateTime),
          open Nullable(Float64),
          high Nullable(Float64),
          low Nullable(Float64),
          close Nullable(Float64),
          volume Nullable(Float64),
          amount Nullable(Float64),
          created_at Nullable(DateTime),
          id UInt64
        )
        ENGINE = ReplacingMergeTree
        PARTITION BY toYYYYMM(datetime)
        ORDER BY (code, datetime)
        SETTINGS allow_nullable_key = 1, index_granularity = 8192
        """
    )


def build_target(client, target: str) -> None:
    target = quote_identifier(target)
    client.command(
        f"""
        INSERT INTO {target} (code, datetime, open, high, low, close, volume, amount, created_at, id)
        SELECT
            code,
            bucket,
            argMin(open, datetime),
            max(high),
            min(low),
            argMax(close, datetime),
            sum(volume),
            sum(amount),
            now(),
            cityHash64(code, toString(bucket), '60m')
        FROM
        (
            SELECT
                code,
                datetime,
                open,
                high,
                low,
                close,
                volume,
                amount,
                session_start + toIntervalMinute(
                    if(elapsed_minutes <= 0, 60, intDiv(elapsed_minutes + 59, 60) * 60)
                ) AS bucket
            FROM
            (
                SELECT
                    code,
                    datetime,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    amount,
                    if(
                        toHour(datetime) < 12,
                        toStartOfDay(datetime) + toIntervalHour(9) + toIntervalMinute(30),
                        toStartOfDay(datetime) + toIntervalHour(13)
                    ) AS session_start,
                    dateDiff('minute', session_start, datetime) AS elapsed_minutes
                FROM {SOURCE_TABLE} FINAL
                WHERE code IS NOT NULL
                  AND datetime IS NOT NULL
                  AND formatDateTime(datetime, '%H:%i') IN ({VALID_5M_TIMES})
            )
        )
        GROUP BY code, bucket
        HAVING count() = 12
        SETTINGS
            max_threads = 2,
            max_memory_usage = 8000000000,
            max_bytes_before_external_group_by = 500000000,
            optimize_aggregation_in_order = 1
        """
    )


def validate_target(client, target: str) -> dict[str, object]:
    target = quote_identifier(target)
    row = client.query(
        f"""
        SELECT
            count() AS rows,
            uniqExact(code) AS codes,
            min(datetime) AS min_datetime,
            max(datetime) AS max_datetime,
            countIf(formatDateTime(datetime, '%H:%i') NOT IN ('10:30', '11:30', '14:00', '15:00')) AS invalid_times,
            count() - uniqExact(code, datetime) AS duplicate_rows
        FROM {target} FINAL
        """
    ).result_rows[0]
    return dict(zip(("rows", "codes", "min_datetime", "max_datetime", "invalid_times", "duplicate_rows"), row))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--phase", choices=("create", "build", "validate", "all"), default="all")
    args = parser.parse_args()
    client = clickhouse_client()
    if args.phase in {"create", "all"}:
        create_target(client, args.target)
    if args.phase in {"build", "all"}:
        build_target(client, args.target)
    if args.phase in {"validate", "all"}:
        print(json.dumps({"checked_at": datetime.now().isoformat(), **validate_target(client, args.target)}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Migrate ClickHouse minute K-line timestamps to Asia/Shanghai wall-clock time.

Older imports stored minute bars as ``exchange_time - 8 hours``.  This script
rewrites affected kline_minute_* tables so the persisted ``datetime`` is the
actual A-share business timestamp, e.g. 2026-05-15 15:00:00 for close.

The migration is guarded by an hour/minute distribution check and keeps a
timestamped backup table for each migrated table.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_query_df  # noqa: E402


TABLE_PERIODS = {
    "kline_minute_1": 1,
    "kline_minute_5": 5,
    "kline_minute_15": 15,
    "kline_minute_30": 30,
    "kline_minute_60": 60,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate minute K-line datetime to East-8 business time.")
    parser.add_argument("--tables", default=",".join(TABLE_PERIODS), help="Comma-separated table names.")
    parser.add_argument("--execute", action="store_true", help="Actually migrate tables. Default is dry-run.")
    parser.add_argument("--keep-staging-on-error", action="store_true")
    return parser.parse_args()


def _valid_times(period_minutes: int) -> set[str]:
    sessions = [("09:30", "11:30"), ("13:00", "15:00")]
    out: set[str] = set()
    for start_text, end_text in sessions:
        start_h, start_m = [int(x) for x in start_text.split(":")]
        end_h, end_m = [int(x) for x in end_text.split(":")]
        current = start_h * 60 + start_m + period_minutes
        end = end_h * 60 + end_m
        while current <= end:
            out.add(f"{current // 60:02d}:{current % 60:02d}")
            current += period_minutes
    return out


def _shifted_times(times: Iterable[str], hours: int) -> set[str]:
    shifted: set[str] = set()
    for item in times:
        hour, minute = [int(x) for x in item.split(":")]
        total = (hour * 60 + minute + hours * 60) % (24 * 60)
        shifted.add(f"{total // 60:02d}:{total % 60:02d}")
    return shifted


def _time_tuples(values: Iterable[str]) -> str:
    pairs = []
    for value in sorted(values):
        hour, minute = [int(x) for x in value.split(":")]
        pairs.append(f"({hour},{minute})")
    return ",".join(pairs)


def _table_exists(table: str) -> bool:
    df = clickhouse_query_df(
        """
        SELECT count() AS c
        FROM system.tables
        WHERE database = currentDatabase() AND name = ?
        """,
        [table],
    )
    return bool(df is not None and not df.empty and int(df.iloc[0]["c"] or 0) > 0)


def _table_stats(table: str, period_minutes: int) -> dict:
    local_times = _valid_times(period_minutes)
    legacy_times = _shifted_times(local_times, -8)
    df = clickhouse_query_df(
        f"""
        SELECT
            count() AS rows,
            uniqExact(tuple(code, datetime)) AS key_rows,
            uniqExact(tuple(code, datetime + INTERVAL 8 HOUR)) AS shifted_key_rows,
            min(datetime) AS min_dt,
            max(datetime) AS max_dt,
            countIf((toHour(datetime), toMinute(datetime)) IN ({_time_tuples(local_times)})) AS local_rows,
            countIf((toHour(datetime), toMinute(datetime)) IN ({_time_tuples(legacy_times)})) AS legacy_rows
        FROM {table}
        """
    )
    if df is None or df.empty:
        return {"rows": 0, "local_rows": 0, "legacy_rows": 0, "min_dt": None, "max_dt": None}
    row = df.iloc[0]
    return {
        "rows": int(row["rows"] or 0),
        "key_rows": int(row["key_rows"] or 0),
        "shifted_key_rows": int(row["shifted_key_rows"] or 0),
        "local_rows": int(row["local_rows"] or 0),
        "legacy_rows": int(row["legacy_rows"] or 0),
        "min_dt": str(row["min_dt"]) if row["min_dt"] is not None else None,
        "max_dt": str(row["max_dt"]) if row["max_dt"] is not None else None,
    }


def _create_sql_for_table(table: str, target_table: str) -> str:
    df = clickhouse_query_df(
        """
        SELECT create_table_query
        FROM system.tables
        WHERE database = currentDatabase() AND name = ?
        """,
        [table],
    )
    if df is None or df.empty:
        raise RuntimeError(f"table not found: {table}")
    sql = str(df.iloc[0]["create_table_query"])
    return re.sub(
        rf"CREATE TABLE\s+(?:`?[^.`\s]+`?\.)?`?{re.escape(table)}`?",
        f"CREATE TABLE {target_table}",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )


def _migrate_table(table: str, period_minutes: int, execute: bool, keep_staging_on_error: bool) -> dict:
    before = _table_stats(table, period_minutes)
    if before["rows"] == 0:
        return {"table": table, "status": "empty", "before": before}
    if before["legacy_rows"] <= before["local_rows"]:
        return {"table": table, "status": "already_local_or_mixed", "before": before}

    suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    staging = f"{table}_east8_staging_{suffix}"
    backup = f"{table}_utc_like_backup_{suffix}"
    result = {"table": table, "status": "dry_run", "before": before, "staging": staging, "backup": backup}
    if not execute:
        return result

    ch = clickhouse_client()
    try:
        ch.command(f"DROP TABLE IF EXISTS {staging}")
        ch.command(_create_sql_for_table(table, staging))
        ch.command(
            f"""
            INSERT INTO {staging}
            SELECT
                code,
                datetime + INTERVAL 8 HOUR AS datetime,
                open,
                high,
                low,
                close,
                volume,
                amount,
                created_at,
                id
            FROM {table}
            """
        )
        staged = _table_stats(staging, period_minutes)
        if staged["key_rows"] != before["shifted_key_rows"]:
            raise RuntimeError(
                f"natural key mismatch for {table}: before_shifted_keys={before['shifted_key_rows']} "
                f"staged_keys={staged['key_rows']}"
            )
        if staged["local_rows"] <= staged["legacy_rows"]:
            raise RuntimeError(f"staged distribution still looks legacy for {table}: {staged}")
        ch.command(f"RENAME TABLE {table} TO {backup}, {staging} TO {table}")
        after = _table_stats(table, period_minutes)
        result.update({"status": "migrated", "after": after})
        return result
    except Exception:
        if not keep_staging_on_error:
            try:
                ch.command(f"DROP TABLE IF EXISTS {staging}")
            except Exception:
                pass
        raise


def main() -> int:
    args = parse_args()
    tables = [item.strip() for item in args.tables.split(",") if item.strip()]
    unknown = [table for table in tables if table not in TABLE_PERIODS]
    if unknown:
        raise SystemExit(f"Unsupported tables: {unknown}")

    results = []
    for table in tables:
        if not _table_exists(table):
            results.append({"table": table, "status": "missing"})
            continue
        result = _migrate_table(
            table,
            TABLE_PERIODS[table],
            execute=args.execute,
            keep_staging_on_error=args.keep_staging_on_error,
        )
        results.append(result)
        print(result, flush=True)

    migrated = [item for item in results if item.get("status") == "migrated"]
    if args.execute:
        print(f"done: migrated={len(migrated)} tables", flush=True)
    else:
        print("dry-run only; rerun with --execute to migrate", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

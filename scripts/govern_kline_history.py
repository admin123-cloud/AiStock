"""Audit and repair historical K-line tables in ClickHouse.

Policy used by this project:
  - kline_daily is the daily source used by strategy logic.
  - kline_minute_5 is the raw local TDX minute baseline.
  - kline_minute_15/30/60 should be deterministic aggregations from 5m.

The script is conservative by default: it audits and writes reports only.
Use explicit repair flags to run ClickHouse mutations.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_client, clickhouse_table_exists  # noqa: E402


PERIODS: Dict[str, Dict[str, Any]] = {
    "1m": {"table": "kline_minute_1", "time_col": "datetime", "filter_expr": "toDate(datetime)", "key_cols": ["code", "datetime"], "minute": 1},
    "5m": {"table": "kline_minute_5", "time_col": "datetime", "filter_expr": "toDate(datetime)", "key_cols": ["code", "datetime"], "minute": 5},
    "15m": {"table": "kline_minute_15", "time_col": "datetime", "filter_expr": "toDate(datetime)", "key_cols": ["code", "datetime"], "minute": 15},
    "30m": {"table": "kline_minute_30", "time_col": "datetime", "filter_expr": "toDate(datetime)", "key_cols": ["code", "datetime"], "minute": 30},
    "60m": {"table": "kline_minute_60", "time_col": "datetime", "filter_expr": "toDate(datetime)", "key_cols": ["code", "datetime"], "minute": 60},
    "1d": {"table": "kline_daily", "time_col": "trade_date", "filter_expr": "trade_date", "key_cols": ["code", "trade_date"], "minute": None},
    "1w": {"table": "kline_weekly", "time_col": "week_start_date", "filter_expr": "week_start_date", "key_cols": ["code", "week_start_date"], "minute": None},
    "1mon": {"table": "kline_monthly", "time_col": "month_start_date", "filter_expr": "month_start_date", "key_cols": ["code", "month_start_date"], "minute": None},
    "1q": {"table": "kline_quarterly", "time_col": "year", "filter_expr": "year", "key_cols": ["code", "year", "quarter"], "minute": None},
    "1y": {"table": "kline_yearly", "time_col": "year", "filter_expr": "year", "key_cols": ["code", "year"], "minute": None},
}
DERIVED_PERIODS = ["15m", "30m", "60m"]
CHUNKED_AUDIT_PERIODS = {"1m", "5m", "15m", "30m", "60m", "1d"}
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_MEMORY_USAGE_BYTES = 10_000_000_000
CH_MEMORY_SETTINGS = {
    "max_memory_usage": MAX_MEMORY_USAGE_BYTES,
    "max_memory_usage_for_user": MAX_MEMORY_USAGE_BYTES,
    "max_memory_usage_for_all_queries": MAX_MEMORY_USAGE_BYTES,
    "max_bytes_before_external_group_by": 1_000_000_000,
    "max_bytes_before_external_sort": 1_000_000_000,
    "max_threads": 1,
}


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _quote_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def _valid_times_sql(period_minutes: int) -> str:
    values: List[str] = []
    for hour, minute, end_hour, end_minute in [(9, 30, 11, 30), (13, 0, 15, 0)]:
        cur = datetime(2000, 1, 1, hour, minute) + timedelta(minutes=period_minutes)
        end = datetime(2000, 1, 1, end_hour, end_minute)
        while cur <= end:
            values.append("'" + cur.strftime("%H:%M") + "'")
            cur += timedelta(minutes=period_minutes)
    return ", ".join(values)


def _date_filter(filter_expr: str, start_date: Optional[date], end_date: Optional[date]) -> str:
    parts: List[str] = []
    if filter_expr == "year":
        if start_date:
            parts.append(f"year >= {int(start_date.year)}")
        if end_date:
            parts.append(f"year <= {int(end_date.year)}")
    else:
        if start_date:
            parts.append(f"{filter_expr} >= toDate('{_quote_date(start_date)}')")
        if end_date:
            parts.append(f"{filter_expr} <= toDate('{_quote_date(end_date)}')")
    return " AND ".join(parts) if parts else "1"


def _date_chunks(start_date: Optional[date], end_date: Optional[date], chunk_days: int) -> List[Tuple[Optional[date], Optional[date]]]:
    if chunk_days <= 0 or not start_date or not end_date:
        return [(start_date, end_date)]
    chunks: List[Tuple[Optional[date], Optional[date]]] = []
    cursor = start_date
    while cursor <= end_date:
        chunk_end = min(end_date, cursor + timedelta(days=chunk_days - 1))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _safe_table(name: str) -> str:
    if not IDENT_RE.match(name):
        raise ValueError(f"invalid table name: {name}")
    return name


def _query_one(client: Any, sql: str) -> Tuple[Any, ...]:
    rows = client.query(sql, settings=CH_MEMORY_SETTINGS).result_rows
    return tuple(rows[0]) if rows else tuple()


def _query_rows(client: Any, sql: str) -> List[Tuple[Any, ...]]:
    return list(client.query(sql, settings=CH_MEMORY_SETTINGS).result_rows)


def _audit_period(client: Any, period: str, start_date: Optional[date], end_date: Optional[date], use_final: bool) -> Dict[str, Any]:
    spec = PERIODS[period]
    table = _safe_table(spec["table"])
    col = spec["time_col"]
    filter_expr = spec["filter_expr"]
    key_cols = ", ".join(spec["key_cols"])
    table_expr = f"{table} FINAL" if use_final else table

    if not clickhouse_table_exists(table):
        return {"period": period, "table": table, "exists": False}

    where = _date_filter(filter_expr, start_date, end_date)
    uniq_expr = f"uniqExact(tuple({key_cols}))"
    minute = spec["minute"]

    summary = _query_one(
        client,
        f"""
        SELECT
            count() AS raw_rows,
            {uniq_expr} AS uniq_rows,
            min({col}) AS min_time,
            max({col}) AS max_time,
            countIf(code IS NULL OR code = '') AS bad_code_rows,
            countIf(open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL) AS null_ohlc_rows,
            countIf(open <= 0 OR high <= 0 OR low <= 0 OR close <= 0) AS non_positive_ohlc_rows,
            countIf(high < low OR high < greatest(open, close) OR low > least(open, close)) AS bad_ohlc_rows,
            countDistinct({filter_expr}) AS covered_units
        FROM {table_expr}
        WHERE {where}
        """,
    )
    raw_rows = int(summary[0] or 0)
    uniq_rows = int(summary[1] or 0)
    out: Dict[str, Any] = {
        "period": period,
        "table": table,
        "exists": True,
        "raw_rows": raw_rows,
        "uniq_rows": uniq_rows,
        "duplicate_rows": max(0, raw_rows - uniq_rows),
        "min_time": str(summary[2]) if summary[2] is not None else None,
        "max_time": str(summary[3]) if summary[3] is not None else None,
        "bad_code_rows": int(summary[4] or 0),
        "null_ohlc_rows": int(summary[5] or 0),
        "non_positive_ohlc_rows": int(summary[6] or 0),
        "bad_ohlc_rows": int(summary[7] or 0),
        "covered_days": int(summary[8] or 0),
    }

    dup_groups = _query_one(
        client,
        f"""
        SELECT count()
        FROM (
            SELECT {key_cols}, count() AS cnt
            FROM {table_expr}
            WHERE {where}
            GROUP BY {key_cols}
            HAVING cnt > 1
        )
        """,
    )
    out["duplicate_groups"] = int(dup_groups[0] or 0)

    if minute:
        invalid_time = _query_one(
            client,
            f"""
            SELECT count()
            FROM {table_expr}
            WHERE {where}
              AND formatDateTime(datetime, '%H:%i') NOT IN ({_valid_times_sql(minute)})
            """,
        )
        out["invalid_bar_time_rows"] = int(invalid_time[0] or 0)
    else:
        out["invalid_bar_time_rows"] = 0

    return out


def _bucket_expr(period_minutes: int) -> str:
    return f"""
    session_start + toIntervalMinute(
        if(elapsed_minutes <= 0, {period_minutes}, intDiv(elapsed_minutes + {period_minutes} - 1, {period_minutes}) * {period_minutes})
    )
    """


def _code_filter(codes: Optional[List[str]]) -> str:
    if not codes:
        return ""
    quoted = ", ".join("'" + code.replace("'", "''") + "'" for code in codes)
    return f" AND code IN ({quoted})"


def _source_5m_subquery(start_date: Optional[date], end_date: Optional[date], use_final: bool, codes: Optional[List[str]] = None) -> str:
    table_expr = "kline_minute_5 FINAL" if use_final else "kline_minute_5"
    where = _date_filter("toDate(datetime)", start_date, end_date)
    return f"""
    SELECT
        code,
        datetime AS source_datetime,
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
    FROM {table_expr}
    WHERE {where}
      AND code IS NOT NULL
      AND datetime IS NOT NULL
      AND formatDateTime(datetime, '%H:%i') IN ({_valid_times_sql(5)})
      {_code_filter(codes)}
    """


def _derived_aggregate_sql(
    period: str,
    start_date: Optional[date],
    end_date: Optional[date],
    use_final: bool,
    codes: Optional[List[str]] = None,
) -> str:
    minutes = int(PERIODS[period]["minute"])
    bucket = _bucket_expr(minutes)
    return f"""
    SELECT
        code,
        bucket AS datetime,
        argMin(open, source_datetime) AS open,
        max(high) AS high,
        min(low) AS low,
        argMax(close, source_datetime) AS close,
        sum(volume) AS volume,
        sum(amount) AS amount,
        count() AS source_bars
    FROM (
        SELECT *, {_bucket_expr(minutes)} AS bucket
        FROM ({_source_5m_subquery(start_date, end_date, use_final, codes)})
    )
    GROUP BY code, bucket
    """


def _period_codes(client: Any, target: str, start_date: Optional[date], end_date: Optional[date]) -> List[str]:
    where = _date_filter("toDate(datetime)", start_date, end_date)
    rows = _query_rows(
        client,
        f"""
        SELECT DISTINCT code
        FROM (
            SELECT code FROM kline_minute_5 WHERE {where} AND code IS NOT NULL
            UNION DISTINCT
            SELECT code FROM {target} WHERE {where} AND code IS NOT NULL
        )
        ORDER BY code
        """,
    )
    return [str(row[0]) for row in rows if row and row[0]]


def _check_derived_consistency(
    client: Any,
    period: str,
    start_date: Optional[date],
    end_date: Optional[date],
    use_final: bool,
    tolerance: float,
) -> Dict[str, Any]:
    target = _safe_table(PERIODS[period]["table"])
    target_expr = f"{target} FINAL" if use_final else target
    where = _date_filter("toDate(datetime)", start_date, end_date)
    expected_source_bars = int(PERIODS[period]["minute"]) // 5
    source_rows = _query_rows(client, _derived_aggregate_sql(period, start_date, end_date, use_final))
    target_rows = _query_rows(
        client,
        f"""
        SELECT code, datetime, open, high, low, close, volume, amount
        FROM {target_expr}
        WHERE {where}
          AND code IS NOT NULL
          AND datetime IS NOT NULL
        """,
    )

    def row_key(row: Tuple[Any, ...]) -> Tuple[str, Any]:
        return str(row[0]), row[1]

    def as_number(value: Any) -> float:
        return float(value or 0)

    source_map = {row_key(row): row for row in source_rows}
    target_map = {row_key(row): row for row in target_rows}
    all_keys = set(source_map) | set(target_map)

    missing_target_rows = 0
    incomplete_source_bucket_rows = 0
    value_mismatch_rows = 0
    for key, src in source_map.items():
        tgt = target_map.get(key)
        if tgt is None:
            missing_target_rows += 1
            continue
        if int(src[8] or 0) != expected_source_bars:
            incomplete_source_bucket_rows += 1
        if (
            abs(as_number(src[2]) - as_number(tgt[2])) > tolerance
            or abs(as_number(src[3]) - as_number(tgt[3])) > tolerance
            or abs(as_number(src[4]) - as_number(tgt[4])) > tolerance
            or abs(as_number(src[5]) - as_number(tgt[5])) > tolerance
            or abs(as_number(src[6]) - as_number(tgt[6])) > 0.5
            or abs(as_number(src[7]) - as_number(tgt[7])) > 1.0
        ):
            value_mismatch_rows += 1

    extra_target_rows = len(set(target_map) - set(source_map))
    totals = [
        len(all_keys),
        missing_target_rows,
        extra_target_rows,
        incomplete_source_bucket_rows,
        value_mismatch_rows,
    ]
    return {
        "period": period,
        "target_table": target,
        "code_count": len({key[0] for key in all_keys}),
        "joined_rows": totals[0],
        "missing_target_rows": totals[1],
        "extra_target_rows": totals[2],
        "incomplete_source_bucket_rows": totals[3],
        "value_mismatch_rows": totals[4],
    }


def _optimize_final(client: Any, table: str) -> str:
    table = _safe_table(table)
    return str(client.command(f"OPTIMIZE TABLE {table} FINAL"))


def _delete_range(client: Any, table: str, col: str, start_date: date, end_date: date) -> None:
    table = _safe_table(table)
    expr = f"toDate({col})"
    client.command(
        f"""
        ALTER TABLE {table}
        DELETE WHERE {expr} >= toDate('{_quote_date(start_date)}')
          AND {expr} <= toDate('{_quote_date(end_date)}')
        SETTINGS mutations_sync = 1
        """,
    )


def _is_month_partitioned_minute_table(client: Any, table: str) -> bool:
    row = _query_one(
        client,
        f"SELECT partition_key FROM system.tables WHERE database = currentDatabase() AND name = '{_safe_table(table)}'",
    )
    return bool(row and "toYYYYMM(datetime)" in str(row[0] or ""))


def _repair_bad_ohlc(client: Any, period: str, start_date: date, end_date: date) -> int:
    spec = PERIODS[period]
    table = _safe_table(spec["table"])
    where = _date_filter(spec["filter_expr"], start_date, end_date)
    bad_where = (
        f"{where} AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL "
        "AND (high < low OR high < greatest(open, close) OR low > least(open, close))"
    )
    before = int(_query_one(client, f"SELECT count() FROM {table} WHERE {bad_where}")[0] or 0)
    if before <= 0:
        return 0
    client.command(
        f"""
        ALTER TABLE {table}
        UPDATE
            high = greatest(high, open, close),
            low = least(low, open, close)
        WHERE {bad_where}
        SETTINGS mutations_sync = 1
        """,
    )
    return before


def _partial_zero_source_days(client: Any, start_date: date, end_date: date, max_bars: int) -> List[Dict[str, Any]]:
    rows = _query_rows(
        client,
        f"""
        SELECT
            code,
            toDate(datetime) AS trade_date,
            count() AS bars,
            min(datetime) AS min_datetime,
            max(datetime) AS max_datetime,
            sum(volume) AS total_volume,
            sum(amount) AS total_amount
        FROM kline_minute_5
        WHERE {_date_filter('toDate(datetime)', start_date, end_date)}
          AND code IS NOT NULL
          AND code != ''
        GROUP BY code, trade_date
        HAVING bars < {int(max_bars)}
           AND total_volume = 0
           AND total_amount = 0
        ORDER BY trade_date, code
        """,
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "code": str(row[0]),
                "trade_date": str(row[1]),
                "bars": int(row[2] or 0),
                "min_datetime": str(row[3]) if row[3] is not None else None,
                "max_datetime": str(row[4]) if row[4] is not None else None,
                "total_volume": float(row[5] or 0),
                "total_amount": float(row[6] or 0),
            }
        )
    return out


def _code_day_where(code_days: List[Dict[str, Any]], date_expr: str) -> str:
    parts: List[str] = []
    for item in code_days:
        code = str(item["code"]).replace("'", "''")
        trade_date = str(item["trade_date"])
        parts.append(f"(code = '{code}' AND {date_expr} = toDate('{trade_date}'))")
    return " OR ".join(parts) if parts else "0"


def _delete_partial_zero_source_days(client: Any, start_date: date, end_date: date, max_bars: int) -> List[Dict[str, Any]]:
    code_days = _partial_zero_source_days(client, start_date, end_date, max_bars)
    if not code_days:
        return []

    repair_rows: List[Dict[str, Any]] = [
        {
            "action": "detect_partial_zero_5m_source_day",
            "code": item["code"],
            "trade_date": item["trade_date"],
            "bars": item["bars"],
            "min_datetime": item["min_datetime"],
            "max_datetime": item["max_datetime"],
            "total_volume": item["total_volume"],
            "total_amount": item["total_amount"],
        }
        for item in code_days
    ]

    for period in ["5m", *DERIVED_PERIODS]:
        table = _safe_table(PERIODS[period]["table"])
        if not clickhouse_table_exists(table):
            continue
        date_expr = "toDate(datetime)"
        deleted_rows = 0
        for pos in range(0, len(code_days), 100):
            batch = code_days[pos : pos + 100]
            where = _code_day_where(batch, date_expr)
            before = int(_query_one(client, f"SELECT count() FROM {table} WHERE {where}")[0] or 0)
            if before <= 0:
                continue
            client.command(
                f"""
                ALTER TABLE {table}
                DELETE WHERE {where}
                SETTINGS mutations_sync = 1
                """,
            )
            deleted_rows += before
        repair_rows.append(
            {
                "action": "delete_partial_zero_source_day_rows",
                "period": period,
                "table": table,
                "code_days": len(code_days),
                "rows_deleted": deleted_rows,
            }
        )
    return repair_rows


def _rebuild_derived(client: Any, period: str, start_date: date, end_date: date, use_final: bool) -> int:
    target = _safe_table(PERIODS[period]["table"])
    if not _is_month_partitioned_minute_table(client, target):
        raise RuntimeError(
            f"refusing to mutate unpartitioned {target}; rebuild it into a monthly-partitioned table first"
        )
    _delete_range(client, target, "datetime", start_date, end_date)
    source_sql = _derived_aggregate_sql(period, start_date, end_date, use_final)
    client.command(
        f"""
        INSERT INTO {target}
            (code, datetime, open, high, low, close, volume, amount, created_at, id)
        SELECT
            code,
            datetime,
            open,
            high,
            low,
            close,
            volume,
            amount,
            now(),
            cityHash64(code, toString(datetime), '{period}')
        FROM ({source_sql})
        """,
    )
    return int(_query_one(client, f"SELECT count() FROM {target} WHERE {_date_filter('toDate(datetime)', start_date, end_date)}")[0] or 0)


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    keys: List[str] = []
    for row in rows:
        for key in row.keys():
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _audit_anomalies(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    watched = [
        "duplicate_rows",
        "duplicate_groups",
        "bad_code_rows",
        "null_ohlc_rows",
        "non_positive_ohlc_rows",
        "bad_ohlc_rows",
        "invalid_bar_time_rows",
    ]
    for row in rows:
        reasons = [key for key in watched if int(row.get(key) or 0) > 0]
        if reasons:
            item = dict(row)
            item["anomaly_reasons"] = ",".join(reasons)
            out.append(item)
    return out


def _consistency_anomalies(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    watched = [
        "missing_target_rows",
        "extra_target_rows",
        "incomplete_source_bucket_rows",
        "value_mismatch_rows",
    ]
    for row in rows:
        reasons = [key for key in watched if int(row.get(key) or 0) > 0]
        if reasons:
            item = dict(row)
            item["anomaly_reasons"] = ",".join(reasons)
            out.append(item)
    return out


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit and repair ClickHouse K-line history.")
    parser.add_argument("--start-date", default="", help="Inclusive YYYY-MM-DD. Empty means no lower bound.")
    parser.add_argument("--end-date", default="", help="Inclusive YYYY-MM-DD. Empty means no upper bound.")
    parser.add_argument("--periods", default="1m,5m,15m,30m,60m,1d,1w,1mon,1q,1y", help="Comma separated periods.")
    parser.add_argument("--chunk-days", type=int, default=1, help="Date range chunk size. Use 0 to run as one range.")
    parser.add_argument("--skip-audit", action="store_true", help="Skip the initial audit pass; useful for already-located repairs.")
    parser.add_argument("--use-final", action="store_true", help="Read tables with FINAL during audit.")
    parser.add_argument("--check-derived", action="store_true", help="Compare 15/30/60 against 5m aggregation.")
    parser.add_argument("--optimize-final", action="store_true", help="Run OPTIMIZE TABLE ... FINAL for audited tables.")
    parser.add_argument("--repair-bad-ohlc", action="store_true", help="Clamp high/low to include open/close for invalid OHLC rows.")
    parser.add_argument(
        "--delete-partial-zero-source",
        action="store_true",
        help="Delete 5m code-days with fewer than expected bars and zero volume/amount, plus matching 15/30/60 rows.",
    )
    parser.add_argument("--partial-zero-max-bars", type=int, default=48, help="Maximum expected 5m bars per full trading day.")
    parser.add_argument("--rebuild-derived", action="store_true", help="Rebuild 15/30/60 from 5m for the date range.")
    parser.add_argument("--tolerance", type=float, default=0.0001)
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "reports" / "kline_history_governance"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start_date = _parse_date(args.start_date)
    end_date = _parse_date(args.end_date)
    periods = [p.strip().lower() for p in args.periods.split(",") if p.strip()]
    unknown = [p for p in periods if p not in PERIODS]
    if unknown:
        raise SystemExit(f"Unsupported periods: {unknown}")
    if args.rebuild_derived and (not start_date or not end_date):
        raise SystemExit("--rebuild-derived requires --start-date and --end-date")
    if args.delete_partial_zero_source and (not start_date or not end_date):
        raise SystemExit("--delete-partial-zero-source requires --start-date and --end-date")

    out_dir = Path(args.out_dir) / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    audit_jsonl = out_dir / "audit.jsonl"
    consistency_jsonl = out_dir / "consistency.jsonl"
    repairs_jsonl = out_dir / "repairs.jsonl"

    chunks = _date_chunks(start_date, end_date, int(args.chunk_days))

    audit_rows: List[Dict[str, Any]] = []
    if not args.skip_audit:
        for period in periods:
            period_chunks = chunks if period in CHUNKED_AUDIT_PERIODS else [(start_date, end_date)]
            for chunk_start, chunk_end in period_chunks:
                row = _audit_period(client, period, chunk_start, chunk_end, args.use_final)
                row["chunk_start"] = str(chunk_start) if chunk_start else None
                row["chunk_end"] = str(chunk_end) if chunk_end else None
                audit_rows.append(row)
                _append_jsonl(audit_jsonl, row)

    consistency_rows: List[Dict[str, Any]] = []
    if args.check_derived:
        for chunk_start, chunk_end in chunks:
            for period in DERIVED_PERIODS:
                if period in periods:
                    row = _check_derived_consistency(client, period, chunk_start, chunk_end, args.use_final, args.tolerance)
                    row["chunk_start"] = str(chunk_start) if chunk_start else None
                    row["chunk_end"] = str(chunk_end) if chunk_end else None
                    consistency_rows.append(row)
                    _append_jsonl(consistency_jsonl, row)

    repair_rows: List[Dict[str, Any]] = []
    if args.optimize_final:
        for p in periods:
            table = PERIODS[p]["table"]
            if clickhouse_table_exists(table):
                row = {"action": "optimize_final", "period": p, "table": table, "result": _optimize_final(client, table)}
                repair_rows.append(row)
                _append_jsonl(repairs_jsonl, row)

    if args.repair_bad_ohlc:
        if not start_date or not end_date:
            raise SystemExit("--repair-bad-ohlc requires --start-date and --end-date")
        for period in periods:
            table = PERIODS[period]["table"]
            if clickhouse_table_exists(table):
                repaired = _repair_bad_ohlc(client, period, start_date, end_date)
                if repaired:
                    row = {"action": "repair_bad_ohlc", "period": period, "table": table, "rows_repaired": repaired}
                    repair_rows.append(row)
                    _append_jsonl(repairs_jsonl, row)

    if args.delete_partial_zero_source:
        assert start_date is not None and end_date is not None
        for row in _delete_partial_zero_source_days(client, start_date, end_date, int(args.partial_zero_max_bars)):
            repair_rows.append(row)
            _append_jsonl(repairs_jsonl, row)

    if args.rebuild_derived:
        assert start_date is not None and end_date is not None
        for period in DERIVED_PERIODS:
            if period in periods:
                rows_after = _rebuild_derived(client, period, start_date, end_date, args.use_final)
                row = {"action": "rebuild_from_5m", "period": period, "rows_after": rows_after}
                repair_rows.append(row)
                _append_jsonl(repairs_jsonl, row)

    report = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "range": {"start_date": args.start_date or None, "end_date": args.end_date or None},
        "periods": periods,
        "chunk_days": int(args.chunk_days),
        "chunks": len(chunks),
        "use_final": bool(args.use_final),
        "audit": audit_rows,
        "consistency": consistency_rows,
        "repairs": repair_rows,
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_csv(out_dir / "audit.csv", audit_rows)
    _write_csv(out_dir / "consistency.csv", consistency_rows)
    _write_csv(out_dir / "repairs.csv", repair_rows)
    anomalies = _audit_anomalies(audit_rows)
    consistency_anomalies = _consistency_anomalies(consistency_rows)
    _write_csv(out_dir / "anomalies.csv", anomalies)
    _write_csv(out_dir / "consistency_anomalies.csv", consistency_anomalies)

    print(f"report_dir={out_dir}")
    for row in audit_rows:
        print(
            f"[{row.get('period')} {row.get('chunk_start') or '-'}~{row.get('chunk_end') or '-'}] "
            f"rows={row.get('raw_rows')} uniq={row.get('uniq_rows')} "
            f"dup_rows={row.get('duplicate_rows')} dup_groups={row.get('duplicate_groups')} "
            f"invalid_time={row.get('invalid_bar_time_rows')} bad_ohlc={row.get('bad_ohlc_rows')}"
        )
    for row in consistency_rows:
        print(
            f"[consistency {row['period']} {row.get('chunk_start') or '-'}~{row.get('chunk_end') or '-'}] "
            f"missing={row['missing_target_rows']} extra={row['extra_target_rows']} "
            f"incomplete_src={row['incomplete_source_bucket_rows']} mismatch={row['value_mismatch_rows']}"
        )
    if repair_rows:
        print(f"repairs={len(repair_rows)}")
    if anomalies or consistency_anomalies:
        print(f"anomalies={len(anomalies)}, consistency_anomalies={len(consistency_anomalies)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

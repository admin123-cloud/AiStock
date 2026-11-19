"""Read-only audit for pre-2010 derived K-line candidates.

The candidate 5m table is the only source of derived values.  Existing
15m/30m/60m and daily tables are read only and are compared against the
candidate-derived result.  This script deliberately contains no DDL, INSERT,
UPDATE, DELETE, OPTIMIZE, or mutation path.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


PERIODS = {
    "15m": ("stock.kline_minute_15", 15, 3),
    "30m": ("stock.kline_minute_30", 30, 6),
    "60m": ("stock.kline_minute_60", 60, 12),
}
SHARDS = tuple("0123456789") + ("other",)
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")

# These are intentionally conservative.  They are query settings only; no
# server/table setting is changed by this script.
QUERY_SETTINGS = {
    "max_threads": 1,
    # The ClickHouse container is explicitly capped at 12 GiB for this
    # read-only audit.  Keep headroom for the server/container rather than
    # requesting the full cgroup limit in one query.
    # The running ClickHouse server still advertises an effective ~3.6 GiB
    # overcommit ceiling despite the container cgroup being raised to 12 GiB.
    # Keep each audit query below that ceiling and force spill early so a
    # single annual shard cannot evict another overnight task.
    "max_memory_usage": 6 * 1024 * 1024 * 1024,
    "max_bytes_before_external_group_by": 3 * 1024 * 1024 * 1024,
    "max_bytes_before_external_sort": 2 * 1024 * 1024 * 1024,
    "max_execution_time": 120,
    "join_algorithm": "grace_hash",
}
PRICE_TOLERANCE = 0.005
QUANTITY_TOLERANCE = 0.001


def ident(value: str) -> str:
    if not IDENT_RE.fullmatch(value):
        raise ValueError(f"unsafe identifier: {value}")
    return value


def q(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def shard_filter(shard: str) -> str:
    if shard == "other":
        return "(code IS NULL OR code = '' OR substring(code, 1, 1) NOT IN ('0','1','2','3','4','5','6','7','8','9'))"
    if len(shard) > 1:
        # Allow narrower numeric prefixes (e.g. 600—609) after a first-digit
        # shard exceeds the ClickHouse container's effective memory ceiling.
        if not shard.isdigit():
            raise ValueError(f"unsafe numeric prefix: {shard}")
        return f"startsWith(code, {q(shard)})"
    return f"substring(code, 1, 1) = {q(shard)}"


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def query_rows(client: Any, sql: str) -> list[tuple[Any, ...]]:
    return list(client.query(sql, settings=QUERY_SETTINGS).result_rows)


def source_5m_sql(candidate: str, start: date, end: date, shard: str) -> str:
    return f"""
        SELECT code, datetime AS source_datetime, open, high, low, close, volume, amount
        FROM {candidate}
        WHERE datetime >= toDate({q(start)})
          AND datetime < toDate({q(end)})
          AND {shard_filter(shard)}
          AND code IS NOT NULL AND code != ''
          AND datetime IS NOT NULL
    """


def candidate_duplicate_summary(client: Any, candidate: str, start: date, end: date, shard: str) -> dict[str, int]:
    rows = query_rows(client, f"""
        SELECT count(), sum(cnt - 1)
        FROM (
            SELECT code, datetime, count() AS cnt
            FROM {candidate}
            WHERE datetime >= toDate({q(start)})
              AND datetime < toDate({q(end)})
              AND {shard_filter(shard)}
              AND code IS NOT NULL AND code != ''
            GROUP BY code, datetime
            HAVING cnt > 1
        )
    """)
    row = rows[0] if rows else (0, 0)
    return {"duplicate_key_groups": int(row[0] or 0), "duplicate_extra_rows": int(row[1] or 0)}


def derived_sql(candidate: str, start: date, end: date, shard: str, minutes: int) -> str:
    src = source_5m_sql(candidate, start, end, shard)
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
            SELECT *,
                session_start + toIntervalMinute(
                    if(elapsed_minutes <= 0, {minutes},
                       intDiv(elapsed_minutes + {minutes} - 1, {minutes}) * {minutes})
                ) AS bucket
            FROM (
                SELECT *,
                    if(toHour(source_datetime) < 12,
                       toStartOfDay(source_datetime) + toIntervalHour(9) + toIntervalMinute(30),
                       toStartOfDay(source_datetime) + toIntervalHour(13)) AS session_start,
                    dateDiff('minute', session_start, source_datetime) AS elapsed_minutes
                FROM ({src})
                WHERE formatDateTime(source_datetime, '%H:%i') IN (
                    '09:35','09:40','09:45','09:50','09:55','10:00','10:05','10:10',
                    '10:15','10:20','10:25','10:30','10:35','10:40','10:45','10:50',
                    '10:55','11:00','11:05','11:10','11:15','11:20','11:25','11:30',
                    '13:05','13:10','13:15','13:20','13:25','13:30','13:35','13:40',
                    '13:45','13:50','13:55','14:00','14:05','14:10','14:15','14:20',
                    '14:25','14:30','14:35','14:40','14:45','14:50','14:55','15:00'
                )
            )
        )
        GROUP BY code, bucket
    """


def target_summary_sql(table: str, start: date, end: date, shard: str) -> str:
    return f"""
        SELECT code, datetime,
               argMax(open, tuple(created_at, id)) AS open,
               argMax(high, tuple(created_at, id)) AS high,
               argMax(low, tuple(created_at, id)) AS low,
               argMax(close, tuple(created_at, id)) AS close,
               argMax(volume, tuple(created_at, id)) AS volume,
               argMax(amount, tuple(created_at, id)) AS amount,
               count() AS raw_rows
        FROM {table}
        WHERE datetime >= toDate({q(start)})
          AND datetime < toDate({q(end)})
          AND {shard_filter(shard)}
          AND code IS NOT NULL AND code != ''
        GROUP BY code, datetime
    """


def daily_candidate_sql(candidate: str, start: date, end: date, shard: str) -> str:
    return f"""
        SELECT code, toDate(source_datetime) AS trade_date,
               argMin(open, source_datetime) AS open,
               max(high) AS high,
               min(low) AS low,
               argMax(close, source_datetime) AS close,
               sum(volume) AS volume,
               sum(amount) AS amount,
               count() AS source_bars
        FROM ({source_5m_sql(candidate, start, end, shard)})
        GROUP BY code, trade_date
    """


def daily_target_sql(start: date, end: date, shard: str) -> str:
    return f"""
        SELECT code, trade_date,
               argMax(open, tuple(created_at, id)) AS open,
               argMax(high, tuple(created_at, id)) AS high,
               argMax(low, tuple(created_at, id)) AS low,
               argMax(close, tuple(created_at, id)) AS close,
               argMax(volume, tuple(created_at, id)) AS volume,
               argMax(amount, tuple(created_at, id)) AS amount,
               count() AS raw_rows
        FROM stock.kline_daily
        WHERE trade_date >= toDate({q(start)})
          AND trade_date < toDate({q(end)})
          AND {shard_filter(shard)}
          AND code IS NOT NULL AND code != ''
        GROUP BY code, trade_date
    """


def numeric(value: Any) -> float:
    return float(value or 0.0)


def beyond_tolerance(left: Any, right: Any, *, quantity: bool = False) -> bool:
    lhs = numeric(left)
    rhs = numeric(right)
    tolerance = QUANTITY_TOLERANCE if quantity else PRICE_TOLERANCE
    return abs(lhs - rhs) / max(abs(rhs), 1.0) > tolerance


def compare_maps(source_rows: list[tuple[Any, ...]], target_rows: list[tuple[Any, ...]], *, period: str) -> dict[str, Any]:
    source = {(str(row[0]), str(row[1])): row for row in source_rows}
    target = {(str(row[0]), str(row[1])): row for row in target_rows}
    missing = sorted(set(source) - set(target))
    extra = sorted(set(target) - set(source))
    duplicates = sum(1 for row in target_rows if len(row) > 8 and int(row[8] or 0) > 1)
    differences = 0
    incomplete = 0
    for key, src in source.items():
        tgt = target.get(key)
        if tgt is None:
            continue
        expected_bars = {"1d": 48, "15m": 3, "30m": 6, "60m": 12}.get(period, 48)
        if len(src) > 8 and int(src[8] or 0) != expected_bars:
            incomplete += 1
        # source rows: key + six values + source_bars; target rows: key + six values + raw_rows
        if any(
            beyond_tolerance(src[idx], tgt[idx], quantity=idx >= 6)
            for idx in range(2, 8)
        ):
            differences += 1
    return {
        "source_rows": len(source_rows),
        "target_logical_rows": len(target_rows),
        "missing_target_rows": len(missing),
        "extra_target_rows": len(extra),
        "target_duplicate_groups": duplicates,
        "incomplete_source_buckets": incomplete,
        "value_difference_rows": differences,
        "sample_missing": missing[:20],
        "sample_extra": extra[:20],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-table", default="stock.purchased_history_minute_5_formal_candidate_20260916")
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2009)
    parser.add_argument("--start-month", type=int, default=1)
    parser.add_argument("--end-month", type=int, default=12)
    parser.add_argument("--start-day", type=int, default=1)
    parser.add_argument("--end-day", type=int, default=31)
    parser.add_argument("--shards", default=",".join(SHARDS))
    parser.add_argument("--out-dir", type=Path, default=runtime_path("quality_plan", "pre2010_derived_dryrun"))
    parser.add_argument("--periods", default="15m,30m,60m,1d")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    candidate = ident(args.candidate_table)
    periods = [item.strip() for item in args.periods.split(",") if item.strip()]
    unknown = sorted(set(periods) - set(PERIODS) - {"1d"})
    if unknown:
        raise SystemExit(f"unsupported periods: {unknown}")
    if args.start_year < 1990 or args.end_year < args.start_year:
        raise SystemExit("invalid year range")
    if not 1 <= args.start_month <= 12 or not 1 <= args.end_month <= 12 or args.start_month > args.end_month:
        raise SystemExit("invalid month range")
    if not 1 <= args.start_day <= 31 or not 1 <= args.end_day <= 31 or args.start_day > args.end_day:
        raise SystemExit("invalid day range")
    shards = tuple(item.strip() for item in args.shards.split(",") if item.strip())
    unknown_shards = sorted(item for item in shards if item not in SHARDS and not item.isdigit())
    if unknown_shards:
        raise SystemExit(f"unsupported shards: {unknown_shards}")
    run_id = time.strftime("%Y%m%dT%H%M%S")
    out_dir = args.out_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=False)
    client = clickhouse_client()
    report: dict[str, Any] = {
        "run_id": run_id,
        "mode": "read_only_dry_run",
        "formal_mutation": False,
        "candidate_table": candidate,
        "source_contract": "frozen_5m_candidate_only",
        "years": [args.start_year, args.end_year],
        "months": [args.start_month, args.end_month],
        "shards": list(shards),
        "query_settings": QUERY_SETTINGS,
        "rows": [],
    }
    for year in range(args.start_year, args.end_year + 1):
        for month in range(args.start_month, args.end_month + 1):
            start, month_end = month_bounds(year, month)
            # Clamp a generic ``--end-day 31`` to short months.  The start day
            # must still be valid for every selected month.
            if args.start_day > (month_end - start).days:
                continue
            start = start.replace(day=args.start_day)
            end_day = min(args.end_day, (month_end - start.replace(day=1)).days)
            end = start.replace(day=end_day).fromordinal(start.replace(day=end_day).toordinal() + 1)
            end = min(end, month_end)
            month_label = f"{year:04d}-{month:02d}"
            for shard in shards:
                started = time.perf_counter()
                duplicate_5m = candidate_duplicate_summary(client, candidate, start, end, shard)
                row: dict[str, Any] = {
                    "month": month_label,
                    "code_shard": shard,
                    "candidate_5m_duplicate": duplicate_5m,
                    "periods": {},
                }
                for period in periods:
                    if period == "1d":
                        source_rows = query_rows(client, daily_candidate_sql(candidate, start, end, shard))
                        target_rows = query_rows(client, daily_target_sql(start, end, shard))
                        row["periods"][period] = compare_maps(source_rows, target_rows, period="1d")
                        continue
                    table, minutes, _ = PERIODS[period]
                    source_rows = query_rows(client, derived_sql(candidate, start, end, shard, minutes))
                    target_rows = query_rows(client, target_summary_sql(table, start, end, shard))
                    row["periods"][period] = compare_maps(source_rows, target_rows, period=period)
                row["elapsed_seconds"] = round(time.perf_counter() - started, 3)
                report["rows"].append(row)
                print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with (out_dir / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fields = ["month", "code_shard", "period", "source_rows", "target_logical_rows", "missing_target_rows", "extra_target_rows", "target_duplicate_groups", "incomplete_source_buckets", "value_difference_rows", "elapsed_seconds"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in report["rows"]:
            for period, values in row["periods"].items():
                writer.writerow({"month": row["month"], "code_shard": row["code_shard"], "period": period, "elapsed_seconds": row["elapsed_seconds"], **{key: values.get(key) for key in fields[3:-1]}})
    print(json.dumps({"report_dir": str(out_dir), "rows": len(report["rows"]), "formal_mutation": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

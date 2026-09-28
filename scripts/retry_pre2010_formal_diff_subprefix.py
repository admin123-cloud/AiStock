from __future__ import annotations

"""Retry bounded pre-2010 formal diff partitions by code second digit.

This is a read-only supplement for the three annual partitions that exceeded
ClickHouse's total memory limit. It never issues DDL, INSERT, DELETE, UPDATE,
FINAL or mutation. Each month/second-code-digit query is checkpointed.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

CANDIDATE = "stock.purchased_history_minute_5_formal_candidate_20260916"
FORMAL = "stock.kline_minute_5"
SETTINGS = {
    "max_threads": 1,
    "max_memory_usage": 6 * 1024 * 1024 * 1024,
    "max_bytes_before_external_group_by": 3 * 1024 * 1024 * 1024,
    "max_bytes_before_external_sort": 2 * 1024 * 1024 * 1024,
    "max_execution_time": 120,
    "join_algorithm": "grace_hash",
}


def bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start.isoformat(), end.isoformat()


def query(client: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    return list(client.query(sql, parameters=params, settings=SETTINGS).result_rows)


def where_clause(alias: str, start: str, end: str, prefix: str) -> str:
    col = f"{alias}.code" if alias else "code"
    return (
        f"{alias + '.' if alias else ''}datetime >= toDate(%(start)s) "
        f"AND {alias + '.' if alias else ''}datetime < toDate(%(end)s) "
        f"AND substring({col}, 1, {len(prefix)}) = %(prefix)s "
        f"AND {col} IS NOT NULL AND {col} != ''"
    )


def summary(client: Any, table: str, start: str, end: str, prefix: str) -> dict[str, int]:
    where = where_clause("", start, end, prefix)
    params = {"start": start, "end": end, "prefix": prefix}
    raw = query(client, f"SELECT count() FROM {table} WHERE {where}", params)[0][0]
    uniq = query(client, f"SELECT uniqExact(tuple(code,datetime)) FROM {table} WHERE {where}", params)[0][0]
    dup = query(
        client,
        f"SELECT count(), coalesce(sum(cnt - 1), 0) FROM (SELECT code, datetime, count() AS cnt FROM {table} WHERE {where} GROUP BY code, datetime HAVING cnt > 1)",
        params,
    )[0]
    return {
        "raw_rows": int(raw or 0),
        "unique_keys": int(uniq or 0),
        "duplicate_groups": int(dup[0] or 0),
        "duplicate_extra_rows": int(dup[1] or 0),
    }


def key_diff(client: Any, start: str, end: str, prefix: str, left: str, right: str) -> int:
    wl = where_clause("", start, end, prefix)
    params = {"start": start, "end": end, "prefix": prefix}
    sql = f"""
      SELECT count() FROM
      (SELECT code, datetime FROM {left} WHERE {wl} GROUP BY code, datetime) AS l
      LEFT ANTI JOIN
      (SELECT code, datetime FROM {right} WHERE {wl} GROUP BY code, datetime) AS r
      ON l.code = r.code AND l.datetime = r.datetime
    """
    return int(query(client, sql, params)[0][0] or 0)


def conflicts(client: Any, start: str, end: str, prefix: str) -> dict[str, int]:
    params = {"start": start, "end": end, "prefix": prefix}
    wc = where_clause("", start, end, prefix)
    sql = f"""
      SELECT count(), countIf(ohlc_diff), countIf(quantity_diff AND NOT ohlc_diff),
             countIf(volume_diff AND NOT ohlc_diff), countIf(amount_diff AND NOT ohlc_diff),
             countIf(volume_scale_signal), countIf(amount_scale_signal)
      FROM (
       SELECT
         (c.open != f.open OR c.high != f.high OR c.low != f.low OR c.close != f.close) AS ohlc_diff,
         (c.volume != f.volume OR c.amount != f.amount) AS quantity_diff,
         (c.volume != f.volume) AS volume_diff, (c.amount != f.amount) AS amount_diff,
         (abs(toFloat64(f.volume)/nullIf(toFloat64(c.volume),0)-100) <= 1 OR abs(toFloat64(f.volume)/nullIf(toFloat64(c.volume),0)-0.01) <= 0.0001) AS volume_scale_signal,
         (abs(toFloat64(f.amount)/nullIf(toFloat64(c.amount),0)-10000) <= 100 OR abs(toFloat64(f.amount)/nullIf(toFloat64(c.amount),0)-0.0001) <= 0.000001) AS amount_scale_signal
       FROM (
        SELECT code, datetime, argMax(open, tuple(created_at,id)) AS open,
          argMax(high, tuple(created_at,id)) AS high, argMax(low, tuple(created_at,id)) AS low,
          argMax(close, tuple(created_at,id)) AS close, argMax(volume, tuple(created_at,id)) AS volume,
          argMax(amount, tuple(created_at,id)) AS amount
        FROM {CANDIDATE} WHERE {wc} GROUP BY code, datetime
       ) c INNER JOIN (
        SELECT code, datetime, argMax(open, tuple(created_at,id)) AS open,
          argMax(high, tuple(created_at,id)) AS high, argMax(low, tuple(created_at,id)) AS low,
          argMax(close, tuple(created_at,id)) AS close, argMax(volume, tuple(created_at,id)) AS volume,
          argMax(amount, tuple(created_at,id)) AS amount
        FROM {FORMAL} WHERE {wc} GROUP BY code, datetime
       ) f ON c.code=f.code AND c.datetime=f.datetime
       WHERE (c.open != f.open OR c.high != f.high OR c.low != f.low OR c.close != f.close OR c.volume != f.volume OR c.amount != f.amount)
      )
    """
    row = query(client, sql, params)[0]
    return {
        "conflicts": int(row[0] or 0),
        "value_conflicts": int(row[1] or 0),
        "unit_conflicts": int(row[2] or 0),
        "volume_unit_signals": int(row[5] or 0),
        "amount_unit_signals": int(row[6] or 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--base-prefix", default="60", help="prefix before one additional digit; e.g. 600 for 6000-6009")
    args = parser.parse_args()
    if not (2001 <= args.year <= 2009 and 1 <= args.month <= 12):
        raise SystemExit("scope must remain within frozen 2001-2009")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    start, end = bounds(args.year, args.month)
    report = {
        "schema_version": "pre2010-formal-diff-subprefix-v1",
        "scope": {"year": args.year, "month": args.month, "first_code_digit": "6", "frozen_envelope": [2001, 2009]},
        "formal_mutation": False,
        "rows": [],
    }
    for prefix in (args.base_prefix + digit for digit in "0123456789"):
        t0 = time.perf_counter()
        row: dict[str, Any] = {"year": args.year, "month": args.month, "code_prefix": prefix, "status": "checked"}
        try:
            row["candidate"] = summary(client, CANDIDATE, start, end, prefix)
            row["formal"] = summary(client, FORMAL, start, end, prefix)
            row["missing"] = key_diff(client, start, end, prefix, CANDIDATE, FORMAL)
            row["formal_extra"] = key_diff(client, start, end, prefix, FORMAL, CANDIDATE)
            row["conflicts"] = conflicts(client, start, end, prefix)
        except Exception as exc:
            row = {
                "year": args.year,
                "month": args.month,
                "code_prefix": prefix,
                "status": "not_checked",
                "error": f"{type(exc).__name__}: {exc}",
            }
        row["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
        report["rows"].append(row)
        print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
        (args.out_dir / "report.partial.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (args.out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

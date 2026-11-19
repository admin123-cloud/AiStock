from __future__ import annotations

"""Bounded, read-only candidate-vs-formal 5m key/value audit.

The scan is deliberately partitioned by month and first code character.  It
only issues SELECTs against the isolated candidate and formal tables; no
FINAL, mutation, DDL, INSERT, UPDATE or DELETE is used.  A failed partition
is retained as ``not_checked`` evidence instead of being reported as zero.
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

CANDIDATE = "stock.purchased_history_minute_5_formal_candidate_20260916"
FORMAL = "stock.kline_minute_5"
SHARDS = tuple("0123456789") + ("other",)
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


def shard_sql(shard: str, alias: str = "") -> str:
    col = f"{alias}.code" if alias else "code"
    if shard == "other":
        return f"({col} IS NULL OR {col} = '' OR substring({col}, 1, 1) NOT IN ('0','1','2','3','4','5','6','7','8','9'))"
    return f"substring({col}, 1, 1) = %(shard)s"


def query(client: Any, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
    return list(client.query(sql, parameters=params, settings=SETTINGS).result_rows)


def summary(client: Any, table: str, start: str, end: str, shard: str) -> dict[str, int]:
    where = f"datetime >= toDate(%(start)s) AND datetime < toDate(%(end)s) AND {shard_sql(shard)} AND code IS NOT NULL AND code != ''"
    raw = query(client, f"SELECT count() FROM {table} WHERE {where}", {"start": start, "end": end, "shard": shard})[0][0]
    uniq = query(client, f"SELECT uniqExact(tuple(code,datetime)) FROM {table} WHERE {where}", {"start": start, "end": end, "shard": shard})[0][0]
    dup = query(client, f"""
        SELECT count(), coalesce(sum(cnt - 1), 0)
        FROM (SELECT code, datetime, count() AS cnt FROM {table}
              WHERE {where} GROUP BY code, datetime HAVING cnt > 1)
    """, {"start": start, "end": end, "shard": shard})[0]
    return {"raw_rows": int(raw or 0), "unique_keys": int(uniq or 0), "duplicate_groups": int(dup[0] or 0), "duplicate_extra_rows": int(dup[1] or 0)}


def key_diff(client: Any, start: str, end: str, shard: str, left: str, right: str) -> int:
    where = f"datetime >= toDate(%(start)s) AND datetime < toDate(%(end)s) AND {shard_sql(shard)} AND code IS NOT NULL AND code != ''"
    sql = f"""
        SELECT count() FROM (SELECT code, datetime FROM {left} WHERE {where} GROUP BY code, datetime) AS l
        LEFT ANTI JOIN (SELECT code, datetime FROM {right} WHERE {where} GROUP BY code, datetime) AS r
        ON l.code = r.code AND l.datetime = r.datetime
    """
    return int(query(client, sql, {"start": start, "end": end, "shard": shard})[0][0] or 0)


def conflict_summary(client: Any, start: str, end: str, shard: str) -> dict[str, int]:
    where = f"datetime >= toDate(%(start)s) AND datetime < toDate(%(end)s) AND {shard_sql(shard)} AND code IS NOT NULL AND code != ''"
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
        FROM {CANDIDATE} WHERE {where} GROUP BY code, datetime
      ) c INNER JOIN (
        SELECT code, datetime, argMax(open, tuple(created_at,id)) AS open,
          argMax(high, tuple(created_at,id)) AS high, argMax(low, tuple(created_at,id)) AS low,
          argMax(close, tuple(created_at,id)) AS close, argMax(volume, tuple(created_at,id)) AS volume,
          argMax(amount, tuple(created_at,id)) AS amount
        FROM {FORMAL} WHERE datetime >= toDate(%(start)s) AND datetime < toDate(%(end)s)
          AND {shard_sql(shard)} AND code IS NOT NULL AND code != '' GROUP BY code, datetime
      ) f ON c.code=f.code AND c.datetime=f.datetime
       WHERE (c.open != f.open OR c.high != f.high OR c.low != f.low OR c.close != f.close OR c.volume != f.volume OR c.amount != f.amount)
      )
    """
    row = query(client, sql, {"start": start, "end": end, "shard": shard})[0]
    return {"conflicts": int(row[0] or 0), "value_conflicts": int(row[1] or 0), "unit_conflicts": int(row[2] or 0), "volume_unit_signals": int(row[5] or 0), "amount_unit_signals": int(row[6] or 0)}


def audit_range(client: Any, start: str, end: str, shard: str) -> dict[str, Any]:
    """Audit one bounded range; callers may aggregate disjoint day ranges."""
    cand = summary(client, CANDIDATE, start, end, shard)
    formal = summary(client, FORMAL, start, end, shard)
    conflicts = conflict_summary(client, start, end, shard)
    return {"candidate": cand, "formal": formal,
            "missing": key_diff(client, start, end, shard, CANDIDATE, FORMAL),
            "formal_extra": key_diff(client, start, end, shard, FORMAL, CANDIDATE),
            "conflicts": conflicts}


def add_stats(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("candidate", "formal", "conflicts"):
        out[key] = {}
        for name in set(left[key]) | set(right[key]):
            out[key][name] = int(left[key].get(name, 0)) + int(right[key].get(name, 0))
    out["missing"] = int(left.get("missing", 0)) + int(right.get("missing", 0))
    out["formal_extra"] = int(left.get("formal_extra", 0)) + int(right.get("formal_extra", 0))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-year", type=int, default=2001)
    parser.add_argument("--end-year", type=int, default=2009)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if not (2000 <= args.start_year <= args.end_year <= 2010):
        raise SystemExit("scope must remain within 2000-2010")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    report = {"schema_version": "pre2010-formal-diff-dryrun-v1", "scope": {"start_year": args.start_year, "end_year": args.end_year, "frozen_envelope": [2000, 2010]}, "formal_mutation": False, "rows": []}
    for year in range(args.start_year, args.end_year + 1):
        for month in range(1, 13):
            start, end = bounds(year, month)
            for shard in SHARDS:
                t0 = time.perf_counter()
                row: dict[str, Any] = {"year": year, "month": month, "code_shard": shard, "status": "checked"}
                try:
                    row.update(audit_range(client, start, end, shard))
                except Exception as exc:
                    if "MEMORY_LIMIT_EXCEEDED" in str(exc):
                        # Retry only the failed month as daily shards.  This
                        # is still bounded and read-only, and keeps a single
                        # large part from invalidating the annual report.
                        try:
                            aggregate: dict[str, Any] | None = None
                            cursor = datetime.fromisoformat(start).date()
                            finish = datetime.fromisoformat(end).date()
                            while cursor < finish:
                                nxt = cursor + timedelta(days=1)
                                part = audit_range(client, cursor.isoformat(), nxt.isoformat(), shard)
                                aggregate = part if aggregate is None else add_stats(aggregate, part)
                                cursor = nxt
                            row.update(aggregate or {"candidate": {}, "formal": {}, "missing": 0, "formal_extra": 0, "conflicts": {}})
                            row["fallback"] = "daily"
                        except Exception as retry_exc:
                            row.update({"status": "not_checked", "error": f"daily_fallback_{type(retry_exc).__name__}: {retry_exc}"})
                    else:
                        row.update({"status": "not_checked", "error": f"{type(exc).__name__}: {exc}"})
                row["elapsed_seconds"] = round(time.perf_counter() - t0, 3)
                report["rows"].append(row)
                print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
                # Checkpoint after every bounded partition so a later OOM or
                # connection failure cannot erase already audited years.
                (args.out_dir / "report.partial.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
                )
    (args.out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

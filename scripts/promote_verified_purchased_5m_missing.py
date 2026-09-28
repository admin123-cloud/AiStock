from __future__ import annotations

"""Insert only missing formal 5m keys from a validated candidate table.

The operation is intentionally append-only.  Existing formal keys are never
deleted or updated.  Same-value overlaps are skipped; different-value
overlaps are written to a conflict evidence table and remain isolated.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
CANDIDATE = "stock.purchased_history_minute_5_formal_candidate_20260916"
FORMAL = "stock.kline_minute_5"
CONFLICTS = "stock.ocean_verified_purchased_5m_formal_conflicts"


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def batch_filter(batch_ids: list[str]) -> tuple[str, dict[str, str]]:
    keys = [f"batch{index}" for index in range(len(batch_ids))]
    return ", ".join(f"%({key})s" for key in keys), dict(zip(keys, batch_ids))


def create_conflict_table(client: Any) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {CONFLICTS}
        (
            batch_id String, code String, datetime DateTime('Asia/Shanghai'),
            candidate_open Float64, candidate_high Float64, candidate_low Float64,
            candidate_close Float64, candidate_volume Int64, candidate_amount Float64,
            formal_open Nullable(Float64), formal_high Nullable(Float64),
            formal_low Nullable(Float64), formal_close Nullable(Float64),
            formal_volume Nullable(Int64), formal_amount Nullable(Float64),
            detected_at DateTime('Asia/Shanghai'), reason String
        ) ENGINE = ReplacingMergeTree(detected_at)
        ORDER BY (batch_id, code, datetime)
        """
    )


def day_summary(client: Any, batch_sql: str, params: dict[str, str]) -> list[tuple[Any, ...]]:
    bounds = client.query(
        f"SELECT min(toDate(datetime)), max(toDate(datetime)) FROM {CANDIDATE} WHERE batch_id IN ({batch_sql})",
        parameters=params,
    ).result_rows[0]
    if bounds[0] is None or bounds[1] is None:
        return []
    rows: list[tuple[Any, ...]] = []
    day = bounds[0]
    end = bounds[1]
    while day <= end:
        local = dict(params)
        local["day"] = day
        result = client.query(
            f"""
            SELECT count(), uniqExact(tuple(code, datetime))
            FROM {CANDIDATE}
            WHERE batch_id IN ({batch_sql})
              AND datetime >= toDateTime(%(day)s)
              AND datetime < toDateTime(%(day)s) + INTERVAL 1 DAY
            """,
            parameters=local,
        ).result_rows[0]
        if int(result[0] or 0):
            rows.append((day, int(result[0]), int(result[1])))
        day += timedelta(days=1)
    return rows


def joined_count(client: Any, batch_sql: str, params: dict[str, Any], day: date) -> int:
    local = dict(params)
    local["day"] = day
    return int(client.query(
        f"""
        SELECT count()
        FROM {CANDIDATE} AS c
        INNER JOIN {FORMAL} AS f
          ON f.code = c.code AND f.datetime = c.datetime
        WHERE c.batch_id IN ({batch_sql})
          AND c.datetime >= toDateTime(%(day)s)
          AND c.datetime < toDateTime(%(day)s) + INTERVAL 1 DAY
        """,
        parameters=local,
    ).result_rows[0][0])


def conflict_count(client: Any, batch_sql: str, params: dict[str, Any], day: date) -> int:
    local = dict(params)
    local["day"] = day
    return int(client.query(
        f"""
        SELECT count()
        FROM {CANDIDATE} AS c
        INNER JOIN {FORMAL} AS f
          ON f.code = c.code AND f.datetime = c.datetime
        WHERE c.batch_id IN ({batch_sql})
          AND c.datetime >= toDateTime(%(day)s)
          AND c.datetime < toDateTime(%(day)s) + INTERVAL 1 DAY
          AND (isNull(f.open) OR isNull(f.high) OR isNull(f.low) OR isNull(f.close)
               OR isNull(f.volume) OR isNull(f.amount)
               OR f.open != c.open OR f.high != c.high OR f.low != c.low
               OR f.close != c.close OR f.volume != c.volume OR f.amount != c.amount)
        """,
        parameters=local,
    ).result_rows[0][0])


def record_conflicts(client: Any, batch_sql: str, params: dict[str, Any], day: date) -> None:
    local = dict(params)
    local["day"] = day
    client.command(
        f"""
        INSERT INTO {CONFLICTS}
        (batch_id, code, datetime, candidate_open, candidate_high, candidate_low,
         candidate_close, candidate_volume, candidate_amount, formal_open,
         formal_high, formal_low, formal_close, formal_volume, formal_amount,
         detected_at, reason)
        SELECT c.batch_id, c.code, c.datetime, c.open, c.high, c.low, c.close,
               c.volume, c.amount, f.open, f.high, f.low, f.close, f.volume,
               f.amount, now('Asia/Shanghai'), 'existing_formal_key_value_conflict'
        FROM {CANDIDATE} AS c
        INNER JOIN {FORMAL} AS f
          ON f.code = c.code AND f.datetime = c.datetime
        WHERE c.batch_id IN ({batch_sql})
          AND c.datetime >= toDateTime(%(day)s)
          AND c.datetime < toDateTime(%(day)s) + INTERVAL 1 DAY
          AND (isNull(f.open) OR isNull(f.high) OR isNull(f.low) OR isNull(f.close)
               OR isNull(f.volume) OR isNull(f.amount)
               OR f.open != c.open OR f.high != c.high OR f.low != c.low
               OR f.close != c.close OR f.volume != c.volume OR f.amount != c.amount)
        """,
        parameters=local,
    )


def insert_missing(client: Any, batch_sql: str, params: dict[str, Any], day: date) -> None:
    local = dict(params)
    local["day"] = day
    client.command(
        f"""
        INSERT INTO {FORMAL}
        (code, datetime, open, high, low, close, volume, amount, created_at, id)
        SELECT c.code, c.datetime, c.open, c.high, c.low, c.close, c.volume,
               c.amount, now(), c.id
        FROM {CANDIDATE} AS c
        LEFT ANTI JOIN
        (
            SELECT code, datetime
            FROM {FORMAL}
            WHERE datetime >= toDateTime(%(day)s)
              AND datetime < toDateTime(%(day)s) + INTERVAL 1 DAY
        ) AS f
          ON f.code = c.code AND f.datetime = c.datetime
        WHERE c.batch_id IN ({batch_sql})
          AND c.datetime >= toDateTime(%(day)s)
          AND c.datetime < toDateTime(%(day)s) + INTERVAL 1 DAY
        """,
        parameters=local,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", action="append", required=True)
    parser.add_argument("--report-dir", type=Path, default=runtime_path("ocean_vps_ingest", "reports"))
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if any(not value or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for char in value) for value in args.batch_id):
        raise SystemExit("invalid batch id")
    client = clickhouse_client()
    batch_sql, params = batch_filter(args.batch_id)
    summaries = day_summary(client, batch_sql, params)
    if not summaries:
        print("no_candidate_rows", file=sys.stderr)
        return 3
    invalid = [row for row in summaries if int(row[1]) != int(row[2])]
    if invalid:
        print(f"candidate_duplicate_keys={invalid[:5]}", file=sys.stderr)
        return 4
    if args.write:
        create_conflict_table(client)

    report: dict[str, Any] = {
        "generated_at": datetime.now(BUSINESS_TZ).isoformat(),
        "batch_ids": args.batch_id,
        "formal_table": FORMAL,
        "candidate_table": CANDIDATE,
        "write": bool(args.write),
        "days": [],
    }
    total_candidates = total_existing = total_missing = total_conflicts = 0
    for day, candidate_rows, candidate_unique in summaries:
        candidate_rows = int(candidate_rows)
        existing = joined_count(client, batch_sql, params, day)
        conflicts = conflict_count(client, batch_sql, params, day)
        missing = candidate_rows - existing
        record = {
            "trade_date": str(day),
            "candidate_rows": candidate_rows,
            "candidate_unique": int(candidate_unique),
            "existing_formal_keys_before": existing,
            "missing_formal_keys_before": missing,
            "conflicting_existing_keys": conflicts,
            "inserted_missing_keys": 0,
        }
        if args.write and missing > 0:
            if conflicts:
                record_conflicts(client, batch_sql, params, day)
            insert_missing(client, batch_sql, params, day)
            after = joined_count(client, batch_sql, params, day)
            remaining = candidate_rows - after
            if remaining:
                raise RuntimeError(f"formal_missing_after_insert={day}:{remaining}")
            record["inserted_missing_keys"] = missing
            record["formal_keys_after"] = after
        report["days"].append(record)
        total_candidates += candidate_rows
        total_existing += existing
        total_missing += missing
        total_conflicts += conflicts

    report.update({
        "candidate_rows": total_candidates,
        "existing_formal_keys_before": total_existing,
        "missing_formal_keys_before": total_missing,
        "conflicting_existing_keys": total_conflicts,
        "inserted_missing_keys": sum(int(row["inserted_missing_keys"]) for row in report["days"]),
        "formal_mutation": bool(args.write and total_missing > 0),
    })
    report_path = args.report_dir / f"promotion_{'_'.join(args.batch_id)}.json"
    atomic_json(report_path, report)
    print(json.dumps({key: value for key, value in report.items() if key != "days"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

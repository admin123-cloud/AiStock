from __future__ import annotations

"""Build and optionally promote the purchased 5m 2000--2010 candidate.

The user policy for this run is intentionally explicit:

* one purchased minute source is sufficient; a second minute source is not a
  required gate;
* every complete 48-bar code-day must still reconcile to the purchased daily
  snapshot (volume in lots, amount in yuan);
* the default tolerances are the existing conservative project contract:
  price <= 0.5%, volume <= 0.1%, amount <= 1%;
* formal promotion is append-only. Existing formal keys are never overwritten;
  value conflicts are recorded and left untouched.

The script writes only a versioned candidate/audit table unless ``--promote``
is explicitly supplied. It processes one calendar year at a time so the
ClickHouse work can be resumed and diagnosed without a whole-history query.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


TZ = ZoneInfo("Asia/Shanghai")
SOURCE = "stock.purchased_history_minute_5_formal_candidate_20260916"
DAILY = "stock.purchased_history_daily_snapshot_stage"
FORMAL = "stock.kline_minute_5"
RUN_ID = "purchased_5m_2000_2010_user_policy_20260921"
CANDIDATE = f"stock.{RUN_ID}_candidate"
AUDIT = f"stock.{RUN_ID}_day_audit"
CONFLICTS = f"stock.{RUN_ID}_formal_conflicts"

PRICE_TOLERANCE = 0.005
VOLUME_TOLERANCE = 0.001
AMOUNT_TOLERANCE = 0.01
SETTINGS = (
    "SETTINGS max_threads=1, max_memory_usage=10000000000, "
    "max_bytes_before_external_group_by=1000000000, "
    "max_bytes_before_external_sort=1000000000, join_algorithm='partial_merge'"
)


def ddl() -> tuple[str, str, str]:
    candidate = f"""
    CREATE TABLE {CANDIDATE}
    (
      code String, datetime DateTime('Asia/Shanghai'), trade_date Date,
      open Float64, high Float64, low Float64, close Float64,
      volume Int64, amount Float64, source_batch_id String,
      price_relative_diff Float64, volume_relative_diff Float64,
      amount_relative_diff Float64, daily_source String,
      quality_status LowCardinality(String), run_id String,
      created_at DateTime('Asia/Shanghai'), id UInt64
    ) ENGINE = MergeTree
    PARTITION BY toYYYYMM(datetime)
    ORDER BY (code, datetime)
    """
    audit = f"""
    CREATE TABLE {AUDIT}
    (
      source_year UInt16, code String, trade_date Date,
      minute_rows UInt32, unique_minute_rows UInt32,
      daily_rows UInt32, price_relative_diff Float64,
      volume_relative_diff Float64, amount_relative_diff Float64,
      status LowCardinality(String), reason String,
      daily_source String, run_id String,
      created_at DateTime('Asia/Shanghai')
    ) ENGINE = MergeTree
    ORDER BY (source_year, code, trade_date)
    """
    conflicts = f"""
    CREATE TABLE IF NOT EXISTS {CONFLICTS}
    (
      run_id String, code String, datetime DateTime('Asia/Shanghai'),
      candidate_open Float64, candidate_high Float64, candidate_low Float64,
      candidate_close Float64, candidate_volume Int64, candidate_amount Float64,
      formal_open Nullable(Float64), formal_high Nullable(Float64),
      formal_low Nullable(Float64), formal_close Nullable(Float64),
      formal_volume Nullable(Int64), formal_amount Nullable(Float64),
      detected_at DateTime('Asia/Shanghai'), reason String
    ) ENGINE = MergeTree
    ORDER BY (run_id, code, datetime)
    """
    return candidate, audit, conflicts


def exists(client: Any, table: str) -> bool:
    db, name = table.split(".", 1)
    row = client.query(
        "SELECT count() FROM system.tables WHERE database=%(db)s AND name=%(name)s",
        parameters={"db": db, "name": name},
    ).result_rows[0]
    return bool(row[0])


def accepted_days_sql(year: int) -> str:
    """Return one-year accepted/review code-day relation."""

    return f"""
    WITH src_days AS
    (
      SELECT code, toDate(datetime) AS trade_date,
             count() AS minute_rows, uniqExact(datetime) AS unique_minute_rows
      FROM {SOURCE}
      WHERE toYear(datetime) = %(year)s
      GROUP BY code, trade_date
    ),
    src_agg AS
    (
      SELECT code, toDate(datetime) AS trade_date,
             argMin(open, datetime) AS open,
             max(high) AS high, min(low) AS low,
             argMax(close, datetime) AS close,
             sum(volume) AS volume, sum(amount) AS amount
      FROM {SOURCE}
      WHERE toYear(datetime) = %(year)s
      GROUP BY code, trade_date
    ),
    daily AS
    (
      SELECT code, trade_date,
             argMax(open, ingested_at) AS open,
             argMax(high, ingested_at) AS high,
             argMax(low, ingested_at) AS low,
             argMax(close, ingested_at) AS close,
             argMax(volume_lots, ingested_at) AS volume,
             argMax(amount_yuan, ingested_at) AS amount,
             count() AS daily_rows
      FROM {DAILY}
      WHERE trade_date >= toDate(concat(toString(%(year)s), '-01-01'))
        AND trade_date < toDate(concat(toString(%(year)s + 1), '-01-01'))
        AND security_type = 'stock'
        AND universe_decision = 'accepted_stock'
        AND quality_status = 'valid'
      GROUP BY code, trade_date
    )
    SELECT s.code AS code, s.trade_date AS trade_date, s.minute_rows, s.unique_minute_rows,
           ifNull(d.daily_rows, 0) AS daily_rows,
           if(d.code = '', 0., greatest(
             abs(a.open - d.open) / greatest(abs(d.open), 1.),
             abs(a.high - d.high) / greatest(abs(d.high), 1.),
             abs(a.low - d.low) / greatest(abs(d.low), 1.),
             abs(a.close - d.close) / greatest(abs(d.close), 1.)
           )) AS price_diff,
           if(d.code = '', 0., abs(a.volume - d.volume) / greatest(abs(d.volume), 1.)) AS volume_diff,
           if(d.code = '', 0., abs(a.amount - d.amount) / greatest(abs(d.amount), 1.)) AS amount_diff,
           multiIf(
             s.minute_rows != 48 OR s.unique_minute_rows != 48, 'review',
             d.code = '' OR d.daily_rows != 1, 'review',
             price_diff <= {PRICE_TOLERANCE}
               AND volume_diff <= {VOLUME_TOLERANCE}
               AND amount_diff <= {AMOUNT_TOLERANCE}, 'accepted',
             'review'
           ) AS status,
           multiIf(
             s.minute_rows != 48 OR s.unique_minute_rows != 48, 'incomplete_or_duplicate_5m',
             d.code = '' OR d.daily_rows != 1, 'purchased_daily_missing_or_duplicate',
             price_diff > {PRICE_TOLERANCE}, 'price_over_tolerance',
             volume_diff > {VOLUME_TOLERANCE}, 'volume_over_tolerance',
             amount_diff > {AMOUNT_TOLERANCE}, 'amount_over_tolerance',
             'accepted_5m_vs_purchased_daily'
           ) AS reason,
           if(d.code = '', '', '{DAILY}') AS daily_source
    FROM src_days s
    INNER JOIN src_agg a ON a.code = s.code AND a.trade_date = s.trade_date
    LEFT JOIN daily d ON d.code = s.code AND d.trade_date = s.trade_date
    {SETTINGS}
    """


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2010)
    parser.add_argument("--write-candidate", action="store_true")
    parser.add_argument("--promote", action="store_true")
    args = parser.parse_args()
    if not 2000 <= args.start_year <= args.end_year <= 2010:
        raise SystemExit("scope must remain within 2000-2010")
    if args.promote:
        args.write_candidate = True

    client = clickhouse_client()
    candidate_ddl, audit_ddl, conflict_ddl = ddl()
    if args.promote:
        if not exists(client, CANDIDATE) or not exists(client, AUDIT):
            raise SystemExit("--promote requires a completed candidate and audit table")
    elif args.write_candidate:
        if exists(client, CANDIDATE) or exists(client, AUDIT):
            raise SystemExit(f"refusing existing run tables: {CANDIDATE}, {AUDIT}")
        client.command(candidate_ddl)
        client.command(audit_ddl)
    elif exists(client, CANDIDATE) or exists(client, AUDIT):
        raise SystemExit("run tables already exist; use a new RUN_ID or inspect the existing run")

    summary: dict[str, Any] = {
        "schema_version": "purchased-5m-2000-2010-user-policy-v1",
        "run_id": RUN_ID,
        "scope": [args.start_year, args.end_year],
        "source": SOURCE,
        "daily_source": DAILY,
        "formal_table": FORMAL,
        "single_source_allowed": True,
        "tolerances": {
            "price": PRICE_TOLERANCE,
            "volume": VOLUME_TOLERANCE,
            "amount": AMOUNT_TOLERANCE,
        },
        "write_candidate": bool(args.write_candidate),
        "promote": bool(args.promote),
        "formal_mutation": False,
        "years": {},
    }

    for year in range(args.start_year, args.end_year + 1):
        if args.promote:
            continue
        params = {"year": year, "run_id": RUN_ID}
        relation = accepted_days_sql(year)
        counts = client.query(
            f"SELECT status, count() FROM ({relation}) GROUP BY status ORDER BY status",
            parameters=params,
        ).result_rows
        status_counts = {str(row[0]): int(row[1]) for row in counts}
        accepted_days = status_counts.get("accepted", 0)
        accepted_rows = int(client.query(
            f"SELECT count() FROM ({relation}) WHERE status='accepted'",
            parameters=params,
        ).result_rows[0][0])
        record = {"status_counts": status_counts, "accepted_days": accepted_days, "accepted_rows": accepted_rows}

        if args.write_candidate:
            client.command(
                f"INSERT INTO {AUDIT} SELECT %(year)s, r.code, r.trade_date, r.minute_rows, r.unique_minute_rows, "
                f"r.daily_rows, r.price_diff, r.volume_diff, r.amount_diff, r.status, r.reason, r.daily_source, %(run_id)s, "
                f"now('Asia/Shanghai') FROM ({relation}) AS r",
                parameters=params,
            )
            client.command(
                f"""
                INSERT INTO {CANDIDATE}
                (code, datetime, trade_date, open, high, low, close, volume, amount,
                 source_batch_id, price_relative_diff, volume_relative_diff,
                 amount_relative_diff, daily_source, quality_status, run_id,
                 created_at, id)
                SELECT s.code, s.datetime, toDate(s.datetime), s.open, s.high, s.low, s.close,
                       s.volume, s.amount, s.batch_id,
                       d.price_diff, d.volume_diff, d.amount_diff,
                       d.daily_source, 'accepted', %(run_id)s,
                       now('Asia/Shanghai'), cityHash64(s.code, toString(s.datetime), %(run_id)s)
                FROM {SOURCE} s
                INNER JOIN ({relation}) d
                  ON d.code=s.code AND d.trade_date=toDate(s.datetime) AND d.status='accepted'
                WHERE toYear(s.datetime)=%(year)s
                {SETTINGS}
                """,
                parameters=params,
            )
            actual = client.query(
                f"SELECT count(), uniqExact(tuple(code,datetime)) FROM {CANDIDATE} WHERE toYear(datetime)=%(year)s {SETTINGS}",
                parameters=params,
            ).result_rows[0]
            record["candidate_rows_written"] = int(actual[0])
            record["candidate_unique_keys"] = int(actual[1])
        summary["years"][str(year)] = record
        print(json.dumps({"year": year, **record}, ensure_ascii=False), flush=True)

    if args.promote:
        client.command(conflict_ddl)
        from datetime import date

        total_before = total_after = total_conflicts = total_inserted = 0
        for year in range(args.start_year, args.end_year + 1):
            for month in range(1, 13):
                start = date(year, month, 1)
                end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
                params = {"run_id": RUN_ID, "start": start, "end": end, "ym": year * 100 + month}
                overlap = int(client.query(
                    f"""
                    SELECT count()
                    FROM {CANDIDATE} c INNER JOIN {FORMAL} f
                      ON f.code=c.code AND f.datetime=c.datetime AND toYYYYMM(f.datetime)=%(ym)s
                    WHERE toYYYYMM(c.datetime)=%(ym)s
                      AND (f.open != c.open OR f.high != c.high OR f.low != c.low OR f.close != c.close
                           OR f.volume != c.volume OR f.amount != c.amount)
                    {SETTINGS}
                    """, parameters=params,
                ).result_rows[0][0])
                if overlap:
                    client.command(
                        f"""
                        INSERT INTO {CONFLICTS}
                        SELECT %(run_id)s, c.code, c.datetime, c.open, c.high, c.low, c.close, c.volume, c.amount,
                               f.open, f.high, f.low, f.close, f.volume, f.amount,
                               now('Asia/Shanghai'), 'existing_formal_key_value_conflict'
                        FROM {CANDIDATE} c INNER JOIN {FORMAL} f
                          ON f.code=c.code AND f.datetime=c.datetime AND toYYYYMM(f.datetime)=%(ym)s
                        WHERE toYYYYMM(c.datetime)=%(ym)s
                          AND (f.open != c.open OR f.high != c.high OR f.low != c.low OR f.close != c.close
                               OR f.volume != c.volume OR f.amount != c.amount)
                        {SETTINGS}
                        """, parameters=params,
                    )
                before = int(client.query(
                    f"""
                    SELECT count()
                    FROM {CANDIDATE} c LEFT JOIN {FORMAL} f
                      ON f.code=c.code AND f.datetime=c.datetime AND toYYYYMM(f.datetime)=%(ym)s
                    WHERE toYYYYMM(c.datetime)=%(ym)s
                      AND f.code IS NULL
                    {SETTINGS}
                    """, parameters=params,
                ).result_rows[0][0])
                if before:
                    client.command(
                        f"""
                        INSERT INTO {FORMAL}
                        (code, datetime, open, high, low, close, volume, amount, created_at, id)
                        SELECT c.code, c.datetime, c.open, c.high, c.low, c.close, c.volume, c.amount,
                               now(), c.id
                        FROM {CANDIDATE} c LEFT JOIN {FORMAL} f
                          ON f.code=c.code AND f.datetime=c.datetime
                        WHERE c.datetime >= toDateTime(%(start)s)
                          AND c.datetime < toDateTime(%(end)s)
                          AND f.code IS NULL
                        {SETTINGS}
                        """, parameters=params,
                    )
                after = int(client.query(
                    f"""
                    SELECT count()
                    FROM {CANDIDATE} c LEFT JOIN {FORMAL} f
                      ON f.code=c.code AND f.datetime=c.datetime
                    WHERE c.datetime >= toDateTime(%(start)s)
                      AND c.datetime < toDateTime(%(end)s)
                      AND f.code IS NULL
                    {SETTINGS}
                    """, parameters=params,
                ).result_rows[0][0])
                total_before += before
                total_after += after
                total_conflicts += overlap
                total_inserted += before - after
                print(json.dumps({"year": year, "month": month, "missing_before": before,
                                  "conflicts": overlap, "inserted": before - after,
                                  "missing_after": after}, ensure_ascii=False), flush=True)
                if after:
                    raise RuntimeError(f"formal_missing_after_insert={year}-{month:02d}:{after}")
        summary["formal_conflict_rows"] = total_conflicts
        summary["formal_missing_before"] = total_before
        summary["formal_missing_after"] = total_after
        summary["formal_inserted_rows"] = total_inserted
        summary["formal_mutation"] = bool(total_inserted)

    out = runtime_path("quality_plan", RUN_ID, "summary.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

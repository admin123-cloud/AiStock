from __future__ import annotations

"""Promote complete purchased 5m code-days before 2010.

Policy: before 2010, a purchased code-day with exactly 48 unique valid bars
may be promoted even when its 5m-vs-daily reconciliation has a minor price,
volume, or amount difference.  Incomplete source code-days remain in the
review queue.  Existing formal (code, datetime) keys are never overwritten.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

STAGE = "stock.purchased_history_minute_5_stage"
FORMAL = "stock.kline_minute_5"
CANDIDATE = "stock.purchased_history_minute_5_pre2010_candidate_20260918"


def ensure_candidate(client) -> None:
    client.command(f"""
    CREATE TABLE IF NOT EXISTS {CANDIDATE} (
        code String, datetime DateTime('Asia/Shanghai'), open Float64,
        high Float64, low Float64, close Float64, volume Int64, amount Float64,
        created_at DateTime('Asia/Shanghai'), id UInt64, source_year UInt16
    ) ENGINE = ReplacingMergeTree(created_at)
    PARTITION BY toYYYYMM(datetime) ORDER BY (code, datetime)
    """)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2009)
    parser.add_argument("--code-prefix", default="", help="Optional first-digit code shard, e.g. 0 or 6")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.start_year < 1990 or args.end_year > 2009 or args.start_year > args.end_year:
        raise SystemExit("range must be within 1990-2009")
    client = clickhouse_client()
    ensure_candidate(client)
    prefix = str(args.code_prefix).strip()
    if prefix and not prefix.isdigit():
        raise SystemExit("code-prefix must be numeric")
    stage_prefix = " AND code LIKE %(prefix)s" if prefix else ""
    stage_prefix_s = " AND s.code LIKE %(prefix)s" if prefix else ""
    formal_prefix = " AND code LIKE %(prefix)s" if prefix else ""
    total_candidate = 0
    total_missing_formal = 0
    total_inserted = 0
    for year in range(args.start_year, args.end_year + 1):
        params = {"year": year}
        if prefix:
            params["prefix"] = prefix + "%"
        complete = f"""
            SELECT code, toDate(datetime) AS trade_date
            FROM {STAGE}
            WHERE toYear(datetime)=%(year)s {stage_prefix}
            GROUP BY code, trade_date
            HAVING count()=48 AND uniqExact(datetime)=48
        """
        candidate_count = client.query(
            f"SELECT count() FROM {STAGE} s INNER JOIN ({complete}) ok ON ok.code=s.code AND ok.trade_date=toDate(s.datetime) WHERE toYear(s.datetime)=%(year)s {stage_prefix_s}",
            parameters=params,
        ).result_rows[0][0]
        missing_count = client.query(
            f"""
            SELECT count()
            FROM (
                SELECT s.code, s.datetime
                FROM {STAGE} s INNER JOIN ({complete}) ok
                  ON ok.code=s.code AND ok.trade_date=toDate(s.datetime)
                WHERE toYear(s.datetime)=%(year)s
            ) c
            LEFT ANTI JOIN (SELECT code,datetime FROM {FORMAL} WHERE toYear(datetime)=%(year)s {formal_prefix}) f
              ON f.code=c.code AND f.datetime=c.datetime
            """,
            parameters=params,
        ).result_rows[0][0]
        total_candidate += int(candidate_count)
        total_missing_formal += int(missing_count)
        print(f"year={year} complete_stage_rows={candidate_count} missing_formal_rows={missing_count}", flush=True)
        if not args.write or not missing_count:
            continue
        client.command(
            f"""
            INSERT INTO {FORMAL} (code,datetime,open,high,low,close,volume,amount,created_at,id)
            SELECT c.code,c.datetime,c.open,c.high,c.low,c.close,c.volume,c.amount,now(),c.id
            FROM (
                SELECT s.code,s.datetime,s.open,s.high,s.low,s.close,
                       toInt64(ifNull(s.source_volume_lots,0)) AS volume,
                       toFloat64(ifNull(s.source_amount_yuan,0)) AS amount,
                       s.id
                FROM {STAGE} s INNER JOIN ({complete}) ok
                  ON ok.code=s.code AND ok.trade_date=toDate(s.datetime)
                WHERE toYear(s.datetime)=%(year)s {stage_prefix_s}
            ) c
            LEFT ANTI JOIN (SELECT code,datetime FROM {FORMAL} WHERE toYear(datetime)=%(year)s) f
                  ON f.code=c.code AND f.datetime=c.datetime
            """,
            parameters=params,
        )
        total_inserted += int(missing_count)
        print(f"year={year} inserted={missing_count}", flush=True)
    print(f"candidate_rows={total_candidate}")
    print(f"missing_formal_rows={total_missing_formal}")
    print(f"inserted_rows={total_inserted}")
    print(f"write={args.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

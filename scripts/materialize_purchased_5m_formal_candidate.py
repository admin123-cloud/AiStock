from __future__ import annotations

"""Build an isolated formal 5m candidate from audited stock code-days."""

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client

CANDIDATE_TABLE = "stock.purchased_history_minute_5_formal_candidate_20260916"
STAGE_TABLE = "stock.purchased_history_minute_5_stage"
AUDIT_TABLE = "stock.purchased_history_minute_5_daily_snapshot_recon"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", action="append", required=True)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def create_table(client: Any) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {CANDIDATE_TABLE}
        (
            `batch_id` String, `code` String, `datetime` DateTime('Asia/Shanghai'),
            `open` Float64, `high` Float64, `low` Float64, `close` Float64,
            `volume` Int64, `amount` Float64, `created_at` DateTime('Asia/Shanghai'),
            `id` UInt64
        ) ENGINE = MergeTree
        PARTITION BY toYYYYMM(datetime)
        ORDER BY (batch_id, code, datetime)
        """
    )


def main() -> int:
    args = parse_args()
    client = clickhouse_client()
    if args.write:
        create_table(client)

    total = 0
    for batch_id in args.batch_id:
        expected = client.query(
            f"""
            SELECT sum(minute_rows)
            FROM (
                SELECT code, trade_date, any(minute_rows) AS minute_rows,
                       any(security_type) AS security_type,
                       argMax(overall_status, checked_at) AS overall_status
                FROM {AUDIT_TABLE}
                WHERE batch_id=%(batch)s
                GROUP BY code, trade_date
            )
            WHERE overall_status=%(status)s AND security_type='stock'
            """,
            parameters={"batch": batch_id, "status": "pass"},
        ).result_rows[0][0] or 0
        candidate_existing = client.query(
            f"SELECT count() FROM {CANDIDATE_TABLE} WHERE batch_id=%(batch)s",
            parameters={"batch": batch_id},
        ).result_rows[0][0] if args.write else 0
        if candidate_existing:
            print(f"existing_candidate_rows={candidate_existing}; refusing duplicate batch={batch_id}", file=sys.stderr)
            return 3

        sql = f"""
        INSERT INTO {CANDIDATE_TABLE}
            (batch_id, code, datetime, open, high, low, close, volume, amount, created_at, id)
        SELECT
            s.batch_id, s.code, s.datetime, s.open, s.high, s.low, s.close,
            toInt64(s.source_volume_lots), s.source_amount_yuan, s.ingested_at, s.id
        FROM {STAGE_TABLE} AS s
        INNER JOIN (
            SELECT code, trade_date, argMax(overall_status, checked_at) AS overall_status
            FROM {AUDIT_TABLE}
            WHERE batch_id=%(batch)s
            GROUP BY code, trade_date
        ) AS a
          ON a.code=s.code AND a.trade_date=toDate(s.datetime)
        WHERE s.batch_id=%(batch)s AND a.overall_status=%(status)s
          AND s.security_type='stock'
          AND s.open > 0 AND s.high > 0 AND s.low > 0 AND s.close > 0
          AND s.source_volume_lots IS NOT NULL
          AND s.source_volume_lots >= 0
          AND s.source_volume_lots = toInt64(s.source_volume_lots)
          AND s.source_amount_yuan IS NOT NULL
          AND s.source_amount_yuan >= 0
        """
        if args.write:
            client.command(sql, parameters={"batch": batch_id, "status": "pass"})
            actual = client.query(
                f"SELECT count(), uniqExact(tuple(code,datetime)) FROM {CANDIDATE_TABLE} WHERE batch_id=%(batch)s",
                parameters={"batch": batch_id},
            ).result_rows[0]
            if int(actual[0]) != int(expected) or int(actual[1]) != int(expected):
                print(f"candidate_count_mismatch batch={batch_id} expected={expected} actual={actual}", file=sys.stderr)
                return 4
            total += int(actual[0])
            print(f"batch={batch_id} expected={expected} actual={actual[0]} unique={actual[1]}")
        else:
            actual = client.query(
                f"""
                SELECT count(), uniqExact(tuple(s.code,s.datetime))
                FROM {STAGE_TABLE} AS s
                INNER JOIN (
                    SELECT code, trade_date, argMax(overall_status, checked_at) AS overall_status
                    FROM {AUDIT_TABLE}
                    WHERE batch_id=%(batch)s
                    GROUP BY code, trade_date
                ) AS a
                  ON a.code=s.code AND a.trade_date=toDate(s.datetime)
                WHERE s.batch_id=%(batch)s AND a.overall_status=%(status)s
                  AND s.security_type='stock'
                  AND s.open > 0 AND s.high > 0 AND s.low > 0 AND s.close > 0
                  AND s.source_volume_lots IS NOT NULL
                  AND s.source_volume_lots >= 0
                  AND s.source_volume_lots = toInt64(s.source_volume_lots)
                  AND s.source_amount_yuan IS NOT NULL
                  AND s.source_amount_yuan >= 0
                """,
                parameters={"batch": batch_id, "status": "pass"},
            ).result_rows[0]
            total += int(actual[0])
            print(f"batch={batch_id} expected={expected} actual={actual[0]} unique={actual[1]}")

    print(f"total_candidate_rows={total}")
    print(f"write={args.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

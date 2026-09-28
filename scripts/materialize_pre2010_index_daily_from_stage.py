"""Materialize pre-2010 index daily bars from complete purchased 5m stage data.

The ordinary-stock daily materializer intentionally uses the formal 5m table.
Indexes are kept in an isolated 5m stage, so this companion path derives only
the explicitly queued pre-2010 missing daily keys from that stage.  It requires
48 unique 5m timestamps and positive OHLC, and never overwrites an existing
formal daily key.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_client


DEFAULT_RUN_ID = "purchased_history_review_20260918_5m_source_closeout_v5"
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
COLUMNS = [
    "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
    "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id",
]


def _sql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _id(code: str, trade_date: object) -> int:
    payload = f"purchased-5m-index-derived-daily:{code}:{trade_date}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = clickhouse_client()
    review = client.query(
        """
        SELECT code, trade_date
        FROM stock.purchased_history_data_review_items
        WHERE run_id = %(run_id)s
          AND issue_type = 'project_daily_missing_for_5m'
          AND trade_date < toDate('2010-01-01')
        ORDER BY code, trade_date
        """,
        parameters={"run_id": args.review_run_id},
    ).result_rows
    review_keys = {(str(code), day) for code, day in review}
    if not review_keys:
        print("review_keys=0 complete_index_days=0 insert_candidates=0")
        return 0

    predicates = " OR ".join(
        f"(code = {_sql(code)} AND toDate(datetime) = toDate({_sql(str(day))}))"
        for code, day in sorted(review_keys)
    )
    source = client.query(
        f"""
        SELECT
            code, toDate(datetime) AS trade_date,
            argMin(open, datetime) AS open,
            max(high) AS high,
            min(low) AS low,
            argMax(close, datetime) AS close,
            sum(ifNull(source_volume_lots, 0)) AS volume,
            sum(ifNull(source_amount_yuan, 0)) AS amount,
            count() AS bars,
            uniqExact(datetime) AS unique_bars
        FROM
        (
            SELECT
                code, datetime,
                any(open) AS open, any(high) AS high, any(low) AS low, any(close) AS close,
                any(source_volume_lots) AS source_volume_lots,
                any(source_amount_yuan) AS source_amount_yuan
            FROM stock.purchased_history_minute_5_stage
            WHERE security_type = 'index'
              AND ({predicates})
            GROUP BY code, datetime
        )
        GROUP BY code, trade_date
        HAVING bars = 48
           AND unique_bars = 48
           AND open > 0 AND high > 0 AND low > 0 AND close > 0
        ORDER BY code, trade_date
        """
    ).result_rows
    source_keys = {(str(code), day) for code, day, *_ in source}
    existing_rows = client.query(
        f"""
        SELECT code, trade_date
        FROM stock.kline_daily
        WHERE {" OR ".join(f"(code = {_sql(code)} AND trade_date = toDate({_sql(str(day))}))" for code, day in sorted(review_keys))}
        """
    ).result_rows
    existing_keys = {(str(code), day) for code, day in existing_rows}
    candidates = [row for row in source if (str(row[0]), row[1]) not in existing_keys]
    print(
        f"review_keys={len(review_keys)} complete_index_days={len(source_keys)} "
        f"existing_daily={len(existing_keys)} insert_candidates={len(candidates)} write={args.write}"
    )
    if not args.write:
        return 0

    created_at = datetime.now(BUSINESS_TZ).replace(tzinfo=None)
    rows = []
    for code, trade_date, open_, high, low, close, volume, amount, _bars, _unique in candidates:
        rows.append([
            str(code), trade_date, float(open_), float(high), float(low), float(close),
            float(volume or 0), float(amount or 0), 0.0, 0.0, 0.0, 0.0, created_at,
            _id(str(code), trade_date),
        ])
    if rows:
        client.insert("stock.kline_daily", rows, column_names=COLUMNS)

    check = client.query(
        f"""
        SELECT count(), countIf(open > 0 AND high > 0 AND low > 0 AND close > 0)
        FROM stock.kline_daily
        WHERE {" OR ".join(f"(code = {_sql(code)} AND trade_date = toDate({_sql(str(day))}))" for code, day in sorted(source_keys))}
        """
    ).result_rows[0]
    if int(check[0]) != len(source_keys) or int(check[1]) != len(source_keys):
        raise RuntimeError(f"index daily validation failed: expected={len(source_keys)} actual={check}")
    print(f"inserted={len(rows)} validated_positive_daily={check[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

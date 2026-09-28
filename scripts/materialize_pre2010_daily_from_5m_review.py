"""Fill pre-2010 missing formal daily bars from complete formal 5-minute bars.

This is deliberately limited to review items whose only issue is a missing
daily row.  It never changes an existing daily key, and requires exactly 48
unique 5-minute bars with positive OHLC before a row can be written.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils.market_warehouse import clickhouse_client


DEFAULT_RUN_ID = "purchased_history_review_20260918_5m_source_closeout_v4"
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
COLUMNS = [
    "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
    "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id",
]


def _id(code: str, trade_date: object) -> int:
    payload = f"purchased-5m-derived-daily:{code}:{trade_date}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-run-id", default=DEFAULT_RUN_ID)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--batch-size", type=int, default=5000)
    return parser.parse_args()


def _chunks(values: list[str], size: int):
    iterator = iter(values)
    while batch := list(islice(iterator, size)):
        yield batch


def _sql_codes(codes: list[str]) -> str:
    return ", ".join("'" + code.replace("'", "''") + "'" for code in codes)


def main() -> int:
    args = parse_args()
    client = clickhouse_client()
    review_rows = client.query(
        """
        SELECT code, trade_date
        FROM stock.purchased_history_data_review_items
        WHERE run_id = %(run_id)s
          AND issue_type = 'project_daily_missing_for_5m'
          AND trade_date < toDate('2010-01-01')
        ORDER BY trade_date, code
        """,
        parameters={"run_id": args.review_run_id},
    ).result_rows
    review_keys = {(str(code), trade_date) for code, trade_date in review_rows}
    source = []
    # kline_minute_5 is ordered by code first.  Querying small code batches keeps
    # the audit bounded instead of scanning its whole multi-hundred-million-row history.
    for year in range(2000, 2010):
        year_keys = {(code, day) for code, day in review_keys if day.year == year}
        if not year_keys:
            continue
        for codes in _chunks(sorted({code for code, _ in year_keys}), 50):
            code_sql = _sql_codes(codes)
            rows = client.query(
                f"""
                SELECT
                    code, toDate(datetime) AS trade_date,
                    argMin(open, datetime) AS open, max(high) AS high, min(low) AS low,
                    argMax(close, datetime) AS close, sum(volume) AS volume, sum(amount) AS amount,
                    count() AS bars, uniqExact(datetime) AS unique_bars
                FROM stock.kline_minute_5
                WHERE code IN ({code_sql})
                  AND datetime >= toDateTime('{year}-01-01')
                  AND datetime < toDateTime('{year + 1}-01-01')
                GROUP BY code, trade_date
                HAVING bars = 48 AND unique_bars = 48
                   AND open > 0 AND high > 0 AND low > 0 AND close > 0
                """
            ).result_rows
            source.extend(row for row in rows if (str(row[0]), row[1]) in year_keys)
        print(f"year={year} review_keys={len(year_keys)} complete_5m={sum(1 for row in source if row[1].year == year)}", flush=True)
    source_keys = {(str(row[0]), row[1]) for row in source}
    existing_keys = set()
    for year in range(2000, 2010):
        year_keys = {(code, day) for code, day in review_keys if day.year == year}
        if not year_keys:
            continue
        for codes in _chunks(sorted({code for code, _ in year_keys}), 200):
            rows = client.query(
                f"""
                SELECT code, trade_date FROM stock.kline_daily
                WHERE code IN ({_sql_codes(codes)})
                  AND trade_date >= toDate('{year}-01-01')
                  AND trade_date < toDate('{year + 1}-01-01')
                """
            ).result_rows
            existing_keys.update((str(code), day) for code, day in rows if (str(code), day) in year_keys)
    candidates = [row for row in source if (str(row[0]), row[1]) not in existing_keys]
    print(
        f"review_keys={len(review_keys)} "
        f"complete_5m_candidates={len(source)} existing_daily={len(existing_keys)} "
        f"insert_candidates={len(candidates)} write={args.write}"
    )
    if not args.write:
        return 0

    created_at = datetime.now(BUSINESS_TZ).replace(tzinfo=None)
    rows = []
    inserted = 0
    for code, trade_date, open_, high, low, close, volume, amount, _bars, _unique in candidates:
        rows.append([
            str(code), trade_date, float(open_), float(high), float(low), float(close),
            float(volume or 0), float(amount or 0), 0.0, 0.0, 0.0, 0.0, created_at,
            _id(str(code), trade_date),
        ])
        if len(rows) >= args.batch_size:
            client.insert("stock.kline_daily", rows, column_names=COLUMNS)
            inserted += len(rows)
            rows = []
    if rows:
        client.insert("stock.kline_daily", rows, column_names=COLUMNS)
        inserted += len(rows)

    formal_keys = set()
    for year in range(2000, 2010):
        year_keys = {(code, day) for code, day in review_keys if day.year == year}
        for codes in _chunks(sorted({code for code, _ in year_keys}), 200):
            rows = client.query(
                f"""
                SELECT code, trade_date FROM stock.kline_daily
                WHERE code IN ({_sql_codes(codes)})
                  AND trade_date >= toDate('{year}-01-01')
                  AND trade_date < toDate('{year + 1}-01-01')
                """
            ).result_rows
            formal_keys.update((str(code), day) for code, day in rows if (str(code), day) in year_keys)
    remaining = len(source_keys - formal_keys)
    if remaining:
        raise RuntimeError(f"formal daily validation failed; remaining={remaining}")
    print(f"inserted={inserted} remaining_missing_daily={remaining}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

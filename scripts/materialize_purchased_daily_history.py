from __future__ import annotations

"""Materialize valid purchased daily rows missing from stock.kline_daily.

The source snapshot is read-only input.  The script is idempotent by the
canonical (code, trade_date) key and never overwrites an existing formal row.
Use without --write for a candidate report; --write inserts only missing,
valid ordinary-stock rows for the requested years.
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.dataset as ds

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DAILY_TABLE = "stock.kline_daily"
DEFAULT_DAILY_ROOT = Path(
    r"F:\AiStock\data\data\warehouse\purchased_history\daily_snapshot\purchased_daily_snapshot_20260916_r1"
)
DAILY_COLUMNS = [
    "code", "trade_date", "open", "high", "low", "close", "prev_close",
    "change_amount", "change_pct", "volume_lots", "amount_yuan", "turnover_rate",
    "ingested_at", "id",
]


def sql_codes(codes: list[str]) -> str:
    return ",".join("'" + code.replace("'", "''") + "'" for code in codes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", action="append", type=int, dest="years")
    parser.add_argument("--daily-root", type=Path, default=DEFAULT_DAILY_ROOT)
    parser.add_argument("--batch-size", type=int, default=10000)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def as_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        if value != value:
            return 0.0
    except Exception:
        return 0.0
    return float(value)


def main() -> int:
    args = parse_args()
    years = sorted(set(args.years or list(range(2000, 2006))))
    if any(year < 1990 or year > 2100 for year in years):
        print("invalid_year", file=sys.stderr)
        return 2
    if not args.daily_root.exists():
        print(f"daily_root_missing={args.daily_root}", file=sys.stderr)
        return 2

    client = clickhouse_client()
    dataset = ds.dataset(args.daily_root, format="parquet", partitioning="hive")
    inserted_total = 0
    candidate_total = 0
    for year in years:
        table = dataset.to_table(
            columns=DAILY_COLUMNS,
            filter=(
                (ds.field("trade_year") == year)
                & (ds.field("security_type") == "stock")
                & (ds.field("quality_status") == "valid")
            ),
        )
        frame = table.to_pandas()
        if frame.empty:
            print(f"year={year} source_valid=0 existing=0 candidate=0")
            continue
        # ``quality_status=valid`` only means the source fields were present.
        # Formal kline_daily must never receive zero/non-positive OHLC rows.
        positive_ohlc = (frame[["open", "high", "low", "close"]] > 0).all(axis=1)
        rejected_nonpositive = int((~positive_ohlc).sum())
        frame = frame[positive_ohlc].copy()
        frame["trade_date"] = [value if isinstance(value, date) else value.date() for value in frame.trade_date]
        codes = sorted({str(code) for code in frame.code})
        existing_rows = client.query(
            f"SELECT code, trade_date FROM {DAILY_TABLE} "
            f"WHERE code IN ({sql_codes(codes)}) "
            "AND trade_date BETWEEN %(min_date)s AND %(max_date)s",
            parameters={
                "min_date": min(frame.trade_date),
                "max_date": max(frame.trade_date),
            },
        ).result_rows
        existing_keys = {(str(row[0]), row[1]) for row in existing_rows}
        candidate = frame[
            [((str(code), trade_date) not in existing_keys) for code, trade_date in zip(frame.code, frame.trade_date)]
        ].copy()
        candidate_total += len(candidate)
        print(
            f"year={year} source_valid_positive={len(frame)} "
            f"rejected_nonpositive_ohlc={rejected_nonpositive} "
            f"existing={len(existing_rows)} candidate={len(candidate)}"
        )
        if not args.write or candidate.empty:
            continue

        candidate = candidate.sort_values(["code", "trade_date"])
        created_at = datetime.now(BUSINESS_TZ)
        rows: list[list[Any]] = []
        for row in candidate.itertuples(index=False):
            prev_close = as_float(row.prev_close)
            amplitude = ((as_float(row.high) - as_float(row.low)) / prev_close * 100.0) if prev_close else 0.0
            rows.append(
                [
                    str(row.code), row.trade_date, as_float(row.open), as_float(row.high), as_float(row.low),
                    as_float(row.close), as_float(row.volume_lots), as_float(row.amount_yuan), amplitude,
                    as_float(row.change_pct), as_float(row.change_amount), as_float(row.turnover_rate),
                    created_at, int(row.id),
                ]
            )
            if len(rows) >= args.batch_size:
                client.insert(DAILY_TABLE, rows, column_names=[
                    "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
                    "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id",
                ])
                inserted_total += len(rows)
                rows = []
        if rows:
            client.insert(DAILY_TABLE, rows, column_names=[
                "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
                "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id",
            ])
            inserted_total += len(rows)
    print(f"years={years}")
    print(f"candidate_total={candidate_total}")
    print(f"inserted_total={inserted_total}")
    print(f"write={args.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

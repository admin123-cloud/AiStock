from __future__ import annotations

"""Reconcile purchased 5m stage rows against the purchased daily snapshot.

The existing kline_daily table is useful as a runtime view but is not a
complete reference for the purchased historical source.  This audit keeps
the source snapshot as the reference and writes only a dedicated audit table;
it never writes kline_daily or kline_minute_5.
"""

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.dataset as ds

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client
from utils.purchased_5m_reconciliation import (
    PRICE_RELATIVE_TOLERANCE,
    RELATIVE_TOLERANCE,
    max_price_abs_diff,
    optional_float,
    price_relative_diff,
)

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_STAGE = "stock.purchased_history_minute_5_stage"
AUDIT_TABLE = "stock.purchased_history_minute_5_daily_snapshot_recon"
DEFAULT_DAILY_ROOT = Path(
    r"F:\AiStock\data\data\warehouse\purchased_history\daily_snapshot\purchased_daily_snapshot_20260916_r1"
)

AUDIT_COLUMNS = [
    "batch_id",
    "code",
    "trade_date",
    "security_type",
    "minute_rows",
    "unique_datetime_rows",
    "daily_snapshot_rows",
    "daily_quality_status",
    "minute_open",
    "minute_high",
    "minute_low",
    "minute_close",
    "daily_open",
    "daily_high",
    "daily_low",
    "daily_close",
    "minute_volume_lots",
    "daily_volume_lots",
    "minute_amount_yuan",
    "daily_amount_yuan",
    "max_price_abs_diff",
    "price_relative_diff",
    "volume_relative_diff",
    "amount_relative_diff",
    "price_status",
    "volume_status",
    "amount_status",
    "overall_status",
    "reason",
    "checked_at",
]


def create_table(client: Any) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}
        (
            `batch_id` String, `code` String, `trade_date` Date,
            `security_type` LowCardinality(String), `minute_rows` UInt32,
            `unique_datetime_rows` UInt32, `daily_snapshot_rows` UInt32,
            `daily_quality_status` LowCardinality(String),
            `minute_open` Float64, `minute_high` Float64, `minute_low` Float64,
            `minute_close` Float64, `daily_open` Nullable(Float64),
            `daily_high` Nullable(Float64), `daily_low` Nullable(Float64),
            `daily_close` Nullable(Float64), `minute_volume_lots` Nullable(Float64),
            `daily_volume_lots` Nullable(Float64), `minute_amount_yuan` Nullable(Float64),
            `daily_amount_yuan` Nullable(Float64), `max_price_abs_diff` Nullable(Float64),
            `price_relative_diff` Nullable(Float64),
            `volume_relative_diff` Nullable(Float64), `amount_relative_diff` Nullable(Float64),
            `price_status` LowCardinality(String), `volume_status` LowCardinality(String),
            `amount_status` LowCardinality(String), `overall_status` LowCardinality(String),
            `reason` String, `checked_at` DateTime('Asia/Shanghai')
        ) ENGINE = ReplacingMergeTree(checked_at)
        ORDER BY (batch_id, code, trade_date)
        """
    )
    client.command(
        f"ALTER TABLE {AUDIT_TABLE} ADD COLUMN IF NOT EXISTS price_relative_diff Nullable(Float64)"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", action="append", required=True)
    parser.add_argument("--daily-root", type=Path, default=DEFAULT_DAILY_ROOT)
    parser.add_argument("--price-relative-tolerance", type=float, default=PRICE_RELATIVE_TOLERANCE)
    parser.add_argument("--relative-tolerance", type=float, default=RELATIVE_TOLERANCE)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="append a newer version for an existing batch; consumers select the latest checked_at row",
    )
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
    except Exception:
        return None
    return float(value)


def main() -> int:
    args = parse_args()
    if args.price_relative_tolerance < 0 or args.relative_tolerance < 0:
        print("tolerances_must_be_non_negative", file=sys.stderr)
        return 2
    if not args.daily_root.exists():
        print(f"daily_root_missing={args.daily_root}", file=sys.stderr)
        return 2
    client = clickhouse_client()
    if args.write:
        create_table(client)

    dataset = ds.dataset(args.daily_root, format="parquet", partitioning="hive")
    for batch_id in args.batch_id:
        if args.write:
            existing = client.query(
                f"SELECT count() FROM {AUDIT_TABLE} WHERE batch_id=%(batch)s",
                parameters={"batch": batch_id},
            ).result_rows[0][0]
            if existing and not args.refresh:
                print(f"existing_audit_rows={existing}; refusing duplicate batch={batch_id}", file=sys.stderr)
                return 3

        bounds = client.query(
            f"""
            SELECT min(toDate(datetime)), max(toDate(datetime))
            FROM {MINUTE_STAGE}
            WHERE batch_id=%(batch)s
            """,
            parameters={"batch": batch_id},
        ).result_rows[0]
        if bounds[0] is None or bounds[1] is None:
            print(f"no_stage_rows={batch_id}", file=sys.stderr)
            return 4

        min_date, max_date = bounds
        total_rows = 0
        statuses: dict[str, int] = {}
        day = min_date
        while day <= max_date:
            next_day = day + timedelta(days=1)
            aggregates = client.query(
                f"""
                SELECT code, toDate(datetime) AS trade_date, any(security_type), count(),
                       argMin(open, datetime), max(high), min(low), argMax(close, datetime),
                       sum(source_volume_lots), sum(source_amount_yuan), uniqExact(datetime)
                FROM {MINUTE_STAGE}
                WHERE batch_id=%(batch)s
                  AND datetime >= toDateTime(%(day)s)
                  AND datetime < toDateTime(%(next_day)s)
                GROUP BY code, trade_date
                ORDER BY code, trade_date
                """,
                parameters={"batch": batch_id, "day": day, "next_day": next_day},
            ).result_rows
            if not aggregates:
                day = next_day
                continue
            table = dataset.to_table(
                columns=[
                    "code", "security_type", "quality_status", "trade_date",
                    "open", "high", "low", "close", "volume_lots", "amount_yuan",
                ],
                filter=(ds.field("trade_date") == day) & (ds.field("security_type") == "stock"),
            )
            daily_by_key = {
                (str(row.code), row.trade_date): row
                for row in table.to_pandas().itertuples(index=False)
            }
            audit_rows: list[list[Any]] = []
            checked_at = datetime.now(BUSINESS_TZ)
            for row in aggregates:
                (
                    code, trade_date, security_type, minute_rows, minute_open,
                    minute_high, minute_low, minute_close, minute_volume,
                    minute_amount, unique_datetime_rows,
                ) = row
                daily_row = daily_by_key.get((str(code), trade_date)) if security_type == "stock" else None
                daily_quality = str(daily_row.quality_status) if daily_row is not None else "missing"
                daily_rows = 1 if daily_row is not None else 0
                daily_open = optional_float(daily_row.open) if daily_row is not None else None
                daily_high = optional_float(daily_row.high) if daily_row is not None else None
                daily_low = optional_float(daily_row.low) if daily_row is not None else None
                daily_close = optional_float(daily_row.close) if daily_row is not None else None
                daily_volume = as_float(daily_row.volume_lots) if daily_row is not None else None
                daily_amount = as_float(daily_row.amount_yuan) if daily_row is not None else None
                price_diff = None
                if daily_row is not None and daily_quality == "valid":
                    daily_prices = (daily_open, daily_high, daily_low, daily_close)
                    price_diff = max_price_abs_diff(
                        (minute_open, minute_high, minute_low, minute_close),
                        daily_prices,
                    )
                    price_ratio = price_relative_diff(price_diff, daily_prices)
                else:
                    price_ratio = None
                volume_diff = None
                if security_type == "stock" and minute_volume is not None and daily_volume is not None:
                    volume_diff = abs(float(minute_volume) - daily_volume) / max(abs(daily_volume), 1.0)
                amount_diff = None
                if minute_amount is not None and daily_amount is not None:
                    amount_diff = abs(float(minute_amount) - daily_amount) / max(abs(daily_amount), 1.0)

                price_status = "not_applicable" if security_type != "stock" else (
                    "missing" if daily_rows == 0 or daily_quality != "valid" else (
                        "pass"
                        if price_ratio is not None and price_ratio <= args.price_relative_tolerance
                        else "review"
                    )
                )
                volume_status = "not_applicable" if security_type != "stock" else (
                    "missing" if volume_diff is None else ("pass" if volume_diff <= args.relative_tolerance else "review")
                )
                amount_status = "missing" if amount_diff is None else (
                    "pass" if amount_diff <= args.relative_tolerance else "review"
                )
                reasons: list[str] = []
                if int(minute_rows) != 48 or int(unique_datetime_rows) != 48:
                    reasons.append(
                        f"5m bars={int(minute_rows)}, unique_datetime={int(unique_datetime_rows)}, expected=48"
                    )
                if security_type == "stock" and (daily_rows != 1 or daily_quality != "valid"):
                    reasons.append(f"daily_snapshot={daily_quality}")
                if price_status not in ("pass", "not_applicable"):
                    reasons.append(
                        f"price_status={price_status}; price_relative_diff={price_ratio}; "
                        f"tolerance={args.price_relative_tolerance}"
                    )
                if volume_status not in ("pass", "not_applicable"):
                    reasons.append(f"volume_status={volume_status}")
                if amount_status != "pass":
                    reasons.append(f"amount_status={amount_status}")
                overall = "pass" if not reasons else "review"
                statuses[overall] = statuses.get(overall, 0) + 1
                audit_rows.append(
                    [
                        batch_id, str(code), trade_date, str(security_type), int(minute_rows), int(unique_datetime_rows),
                        daily_rows, daily_quality, float(minute_open), float(minute_high), float(minute_low),
                        float(minute_close), daily_open, daily_high, daily_low, daily_close,
                        as_float(minute_volume), daily_volume, as_float(minute_amount), daily_amount,
                        price_diff, price_ratio, volume_diff, amount_diff,
                        price_status, volume_status, amount_status,
                        overall, "; ".join(reasons), checked_at,
                    ]
                )
            if args.write and audit_rows:
                client.insert(AUDIT_TABLE, audit_rows, column_names=AUDIT_COLUMNS)
            total_rows += len(audit_rows)
            day = next_day
        print("--- purchased 5m purchased-daily reconciliation ---")
        print(f"batch_id={batch_id}")
        print(f"daily_keys={total_rows}")
        print(f"statuses={statuses}")
        print(f"write={args.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

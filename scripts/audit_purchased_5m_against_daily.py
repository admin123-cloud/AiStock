from __future__ import annotations

"""Audit purchased 5m daily aggregates against the existing kline_daily.

This is a hard gate for later formal writes.  It only writes the dedicated
reconciliation audit table; it never writes kline_daily or kline_minute_5.
"""

import argparse
import sys
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client
from utils.purchased_5m_reconciliation import (
    PRICE_RELATIVE_TOLERANCE,
    RELATIVE_TOLERANCE,
    max_price_abs_diff,
    price_relative_diff,
)

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_STAGE = "stock.purchased_history_minute_5_stage"
AUDIT_TABLE = "stock.purchased_history_minute_5_daily_recon_audit"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--price-relative-tolerance", type=float, default=PRICE_RELATIVE_TOLERANCE)
    parser.add_argument("--relative-tolerance", type=float, default=RELATIVE_TOLERANCE)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def sql_codes(codes: list[str]) -> str:
    return ",".join("'" + code.replace("'", "''") + "'" for code in codes)


def main() -> int:
    args = parse_args()
    if args.price_relative_tolerance < 0 or args.relative_tolerance < 0:
        print("tolerances_must_be_non_negative", file=sys.stderr)
        return 2
    client = clickhouse_client()
    if args.write:
        existing = client.query(
            f"SELECT count() FROM {AUDIT_TABLE} WHERE batch_id=%(batch)s", parameters={"batch": args.batch_id}
        ).result_rows[0][0]
        if existing:
            print(f"existing_audit_rows={existing}; refusing duplicate batch", file=sys.stderr)
            return 3

    aggregates = client.query(
        f"""
        SELECT code, toDate(datetime) AS trade_date, any(security_type), count(),
               argMin(open, datetime), max(high), min(low), argMax(close, datetime),
               sum(source_volume_lots), sum(source_amount_yuan), uniqExact(datetime)
        FROM {MINUTE_STAGE}
        WHERE batch_id=%(batch)s
        GROUP BY code, trade_date
        ORDER BY code, trade_date
        """,
        parameters={"batch": args.batch_id},
    ).result_rows
    if not aggregates:
        print("no_stage_rows", file=sys.stderr)
        return 4

    codes = sorted({str(row[0]) for row in aggregates})
    min_date = min(row[1] for row in aggregates)
    max_date = max(row[1] for row in aggregates)
    daily_rows = client.query(
        f"""
        SELECT code, trade_date, any(open), any(high), any(low), any(close), any(volume), any(amount), count()
        FROM stock.kline_daily
        WHERE code IN ({sql_codes(codes)})
          AND trade_date BETWEEN %(min_date)s AND %(max_date)s
        GROUP BY code, trade_date
        """,
        parameters={"min_date": min_date, "max_date": max_date},
    ).result_rows
    daily_by_key = {(str(row[0]), row[1]): row for row in daily_rows}

    checked_at = datetime.now(BUSINESS_TZ)
    audit_rows: list[list[Any]] = []
    statuses: dict[str, int] = {}
    for row in aggregates:
        code, trade_date, security_type, minute_count, minute_open, minute_high, minute_low, minute_close, minute_volume, minute_amount, unique_count = row
        daily = daily_by_key.get((str(code), trade_date))
        daily_count = int(daily[8]) if daily else 0
        daily_open = daily[2] if daily else None
        daily_high = daily[3] if daily else None
        daily_low = daily[4] if daily else None
        daily_close = daily[5] if daily else None
        daily_volume = daily[6] if daily else None
        daily_amount = daily[7] if daily else None
        price_diff = None
        if daily:
            daily_prices = (daily_open, daily_high, daily_low, daily_close)
            price_diff = max_price_abs_diff(
                (minute_open, minute_high, minute_low, minute_close),
                daily_prices,
            )
            price_ratio = price_relative_diff(price_diff, daily_prices)
        else:
            price_ratio = None
        volume_diff = None
        if security_type != "index" and minute_volume is not None and daily_volume is not None:
            volume_diff = abs(float(minute_volume) - float(daily_volume)) / max(abs(float(daily_volume)), 1.0)
        amount_diff = None
        if minute_amount is not None and daily_amount is not None:
            amount_diff = abs(float(minute_amount) - float(daily_amount)) / max(abs(float(daily_amount)), 1.0)
        price_status = "missing" if not daily else (
            "pass"
            if price_ratio is not None and price_ratio <= args.price_relative_tolerance
            else "review"
        )
        volume_status = "not_applicable" if security_type == "index" else (
            "missing" if volume_diff is None else ("pass" if volume_diff <= args.relative_tolerance else "review")
        )
        amount_status = "missing" if amount_diff is None else (
            "pass" if amount_diff <= args.relative_tolerance else "review"
        )
        reasons: list[str] = []
        if minute_count != 48 or unique_count != 48:
            reasons.append(f"5m bars={minute_count}, unique_datetime={unique_count}, expected=48")
        if daily_count != 1:
            reasons.append("daily row missing or non-unique")
        if price_status != "pass":
            reasons.append(
                f"price_status={price_status}; "
                f"price_relative_diff={price_ratio}; "
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
                args.batch_id, code, trade_date, security_type, minute_count, daily_count,
                minute_open, minute_high, minute_low, minute_close, daily_open, daily_high,
                daily_low, daily_close, minute_volume, daily_volume, minute_amount, daily_amount,
                price_diff, volume_diff, amount_diff,
                price_status, volume_status, amount_status,
                overall, "; ".join(reasons), checked_at,
            ]
        )

    if args.write:
        client.insert(AUDIT_TABLE, audit_rows)
    print("--- purchased 5m daily reconciliation ---")
    print(f"batch_id={args.batch_id}")
    print(f"daily_keys={len(audit_rows)}")
    print(f"codes={len(codes)}")
    print(f"statuses={statuses}")
    print(f"write={args.write}")
    if not args.write:
        for row in audit_rows:
            if row[24] != "pass":
                print(f"review={row[1]}@{row[2]} reason={row[25]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

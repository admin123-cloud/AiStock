"""Fill pre-2010 purchased 5m gaps from 1m or explicit carry-forward.

This is a user-authorized historical exception for all security types before
2010. Complete 1m buckets are aggregated into 5m bars. If 1m is also absent,
at most three missing 5m bars per day may carry the last available price and
have zero volume/amount; larger gaps remain pending. Every added row is
isolated by ``source_category`` so synthetic bars remain auditable.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import date, datetime
from pathlib import Path
from zipfile import ZipFile
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_purchased_review_against_1m import (
    DEFAULT_BJ_ROOT,
    DEFAULT_HS_ROOT,
    DEFAULT_INDEX_ROOT,
    EXPECTED_ENDS,
    _derive_5m,
    _read_source_days,
    _source_root,
    _source_name,
)
from utils.market_warehouse import clickhouse_client


RUN_ID = "purchased_history_review_20260918_5m_source_closeout_v10"
BATCH_ID = "purchased_1m_pre2010_gap_fill_20260918"
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
COLUMNS = [
    "source", "source_category", "source_archive", "source_entry", "batch_id", "source_code",
    "code", "security_type", "universe_decision", "period", "datetime", "open", "high", "low",
    "close", "source_volume_lots", "source_amount_yuan", "price_adjustment", "row_hash", "ingested_at", "id",
]


def _sql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _id(code: str, stamp: datetime, category: str) -> int:
    payload = f"{BATCH_ID}:{code}:{stamp.isoformat()}:{category}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=RUN_ID)
    parser.add_argument("--security-type", choices=("all", "stock", "index"), default="all")
    parser.add_argument("--code", default="", help="Optional canonical code for a targeted run.")
    parser.add_argument("--trade-date", default="", help="Optional YYYY-MM-DD for a targeted run.")
    parser.add_argument("--hs-root", type=Path, default=DEFAULT_HS_ROOT)
    parser.add_argument("--index-root", type=Path, default=DEFAULT_INDEX_ROOT)
    parser.add_argument("--bj-root", type=Path, default=DEFAULT_BJ_ROOT)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def _queue(client, run_id: str, security_type: str, code: str, trade_date: str) -> list[tuple[str, date, str]]:
    security_filter = "" if security_type == "all" else " AND security_type = %(security_type)s"
    code_filter = "" if not code else " AND code = %(code)s"
    date_filter = "" if not trade_date else " AND trade_date = toDate(%(trade_date)s)"
    rows = client.query(
        f"""
        SELECT code, trade_date, security_type
        FROM stock.purchased_history_data_review_items
        WHERE run_id = %(run_id)s
          AND issue_type IN ('source_5m_incomplete_bars', 'source_5m_file_invalid_no_datetime')
          AND trade_date < toDate('2010-01-01')
          {security_filter}
          {code_filter}
          {date_filter}
        ORDER BY code, trade_date
        """,
        parameters={"run_id": run_id, "security_type": security_type, "code": code, "trade_date": trade_date},
    ).result_rows
    return [(str(code), day, str(kind)) for code, day, kind in rows]


def _stage_rows(client, code: str, trade_date: date) -> dict[int, tuple]:
    rows = client.query(
        f"""
        SELECT
            datetime, any(open), any(high), any(low), any(close),
            any(source_volume_lots), any(source_amount_yuan)
        FROM stock.purchased_history_minute_5_stage
        WHERE code = {_sql(code)} AND toDate(datetime) = toDate({_sql(str(trade_date))})
        GROUP BY datetime
        ORDER BY datetime
        """
    ).result_rows
    return {int(row[0].strftime("%H%M")): row for row in rows}


def _last_close(client, code: str, trade_date: date, current: dict[int, tuple]) -> float | None:
    if current:
        return float(list(current.values())[-1][4])
    row = client.query(
        f"""
        SELECT close
        FROM stock.purchased_history_minute_5_stage
        WHERE code = {_sql(code)} AND datetime < toDateTime({_sql(str(trade_date) + ' 00:00:00')})
        ORDER BY datetime DESC
        LIMIT 1
        """
    ).result_rows
    return float(row[0][0]) if row and row[0][0] is not None else None


def _archive_rows(root: Path, code: str, trade_date: date, security_type: str) -> tuple[str, str, list[tuple]]:
    archive_path = root / f"{trade_date.year}_1min.zip"
    entry = _source_name(code, trade_date.year, security_type)
    if not archive_path.exists():
        return str(archive_path), entry, []
    with ZipFile(archive_path) as archive:
        if entry not in set(archive.namelist()):
            return str(archive_path), entry, []
        by_day, _ = _read_source_days(archive, entry, {trade_date})
        return str(archive_path), entry, by_day.get(trade_date, [])


def main() -> int:
    args = parse_args()
    client = clickhouse_client()
    keys = _queue(client, args.run_id, args.security_type, args.code, args.trade_date)
    additions: list[list[object]] = []
    summary = []
    for code, trade_date, security_type in keys:
        existing = _stage_rows(client, code, trade_date)
        missing_count = len(EXPECTED_ENDS) - len(existing)
        if missing_count > 3:
            summary.append({
                "code": code, "trade_date": str(trade_date), "security_type": security_type, "existing_5m": len(existing),
                "one_minute_rows": 0, "one_minute_unique": 0,
                "one_minute_duplicates": 0, "derived_5m": 0,
                "added": 0, "derived_added": 0, "synthetic_added": 0,
                "decision": "keep_pending_gap_gt_3",
            })
            continue
        source_root = _source_root(code, security_type, args.hs_root, args.index_root, args.bj_root)
        archive, entry, one_minute = _archive_rows(source_root, code, trade_date, security_type)
        derived, unique_1m, duplicate_1m = _derive_5m(one_minute)
        derived_by_time = {int(row["minute"]): row for row in derived}
        previous_close = _last_close(client, code, trade_date, existing)
        added = 0
        derived_added = 0
        synthetic_added = 0
        for end in EXPECTED_ENDS:
            if end in existing:
                previous_close = float(existing[end][4])
                continue
            row = derived_by_time.get(end)
            category = "purchased_1m_derived" if row else "purchased_1m_synthetic"
            if row:
                open_, high, low, close = (float(row[k]) for k in ("open", "high", "low", "close"))
                volume = float(row["volume_lots"])
                amount = None
                previous_close = close
                derived_added += 1
            elif previous_close is not None and previous_close > 0:
                open_ = high = low = close = previous_close
                volume = 0.0
                amount = 0.0
                synthetic_added += 1
            else:
                continue
            stamp = datetime.combine(trade_date, datetime.min.time()).replace(tzinfo=BUSINESS_TZ)
            hour, minute = divmod(end, 100)
            stamp = stamp.replace(hour=hour, minute=minute)
            raw = f"{code}|{stamp.isoformat()}|{open_}|{high}|{low}|{close}|{volume}|{amount}|{category}"
            additions.append([
                "purchased_history_1m", category, archive, entry, BATCH_ID, code.split(".")[0],
                code, security_type, "pass", "5m", stamp, open_, high, low, close, volume, amount,
                "none", hashlib.sha256(raw.encode()).hexdigest(),
                datetime.now(BUSINESS_TZ), _id(code, stamp, category),
            ])
            added += 1
        summary.append({
            "code": code, "trade_date": str(trade_date), "security_type": security_type, "existing_5m": len(existing),
            "one_minute_rows": len(one_minute), "one_minute_unique": unique_1m,
            "one_minute_duplicates": duplicate_1m, "derived_5m": len(derived),
            "added": added, "derived_added": derived_added, "synthetic_added": synthetic_added,
        })

    print(f"queue_keys={len(keys)} additions={len(additions)} write={args.write}")
    for row in summary:
        print(row)
    if args.write and additions:
        client.insert("stock.purchased_history_minute_5_stage", additions, column_names=COLUMNS)
        print(f"inserted={len(additions)} batch_id={BATCH_ID}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

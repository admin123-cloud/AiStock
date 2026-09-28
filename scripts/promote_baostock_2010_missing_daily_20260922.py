"""Validate and promote Baostock evidence for the remaining 2010 daily gaps.

This is intentionally scoped to the active review projection.  It writes a
versioned candidate first, then promotes only rows with complete positive OHLC,
volume/amount fields and a valid listing-date gate.  Beijing exchange codes
unsupported by Baostock and rows with blank volume/amount remain review-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import baostock as bs

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

RUN = "purchased_history_review_20260922_formal5m_reaggregate_a_auto_b_c_manual"
CANDIDATE = "stock.baostock_2010_missing_daily_candidate_20260922"
REPORT = Path(r"F:\AiStock\data\data\runtime\quality_plan\baostock_2010_missing_daily_20260922\report.json")


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def ddl() -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {CANDIDATE} (
      code String,
      trade_date Date,
      open Float64,
      high Float64,
      low Float64,
      close Float64,
      volume_lots Float64,
      amount_yuan Float64,
      prev_close Float64,
      change_pct Float64,
      source String,
      source_symbol String,
      fetched_at DateTime('Asia/Shanghai'),
      validation_status LowCardinality(String),
      validation_reason String
    ) ENGINE=MergeTree ORDER BY (code, trade_date)
    """


def valid_row(r: dict) -> tuple[bool, str, dict | None]:
    try:
        vals = {k: float(r[k]) for k in ("open", "high", "low", "close", "volume", "amount")}
    except (TypeError, ValueError, KeyError):
        return False, "blank_or_non_numeric_field", None
    o, h, lo, c = vals["open"], vals["high"], vals["low"], vals["close"]
    if min(o, h, lo, c) <= 0:
        return False, "nonpositive_ohlc", None
    if h < max(o, c) or lo > min(o, c):
        return False, "invalid_ohlc_bounds", None
    if vals["volume"] < 0 or vals["amount"] < 0:
        return False, "negative_volume_or_amount", None
    prev = float(r.get("preclose") or 0)
    pct = float(r.get("pctChg") or 0)
    return True, "validated_baostock", {
        "date": r["date"], "open": o, "high": h, "low": lo, "close": c,
        "volume_lots": vals["volume"] / 100.0, "amount_yuan": vals["amount"],
        "prev_close": prev, "change_pct": pct,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--promote", action="store_true")
    args = ap.parse_args()
    c = clickhouse_client()
    c.command(ddl())
    targets = c.query(
        f"SELECT code,trade_date FROM stock.purchased_history_data_review_items "
        f"WHERE run_id={q(RUN)} AND issue_type='purchased_daily_missing_ohlc_2010'"
    ).result_rows
    by_code: dict[str, list[str]] = defaultdict(list)
    for code, d in targets:
        by_code[str(code)].append(str(d))
    metadata = {
        str(r[0]): r[1]
        for r in c.query(
            "SELECT code,list_date FROM stocks WHERE code IN (" + ",".join(q(x) for x in by_code) + ")"
        ).result_rows
    }
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
    good: list[tuple] = []
    bad: list[dict] = []
    unsupported: dict[str, str] = {}
    fetched_at = datetime.now(ZoneInfo("Asia/Shanghai")).replace(microsecond=0)
    try:
        for code in sorted(by_code):
            dates = by_code[code]
            list_date = metadata.get(code)
            # No pre-listing candidate is allowed, even if the review queue is stale.
            dates = [d for d in dates if list_date is None or d >= str(list_date)]
            market, raw = code.split(".", 1)
            symbol = f"{market.lower()}.{raw}"
            start = (datetime.strptime(min(dates), "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
            end = (datetime.strptime(max(dates), "%Y-%m-%d") + timedelta(days=5)).strftime("%Y-%m-%d")
            result = bs.query_history_k_data_plus(
                symbol,
                "date,open,high,low,close,volume,amount,preclose,pctChg",
                start_date=start, end_date=end, frequency="d", adjustflag="3",
            )
            if result.error_code != "0":
                unsupported[code] = f"{result.error_code}: {result.error_msg}"
                continue
            frame = result.get_data()
            records = {} if frame is None else {str(r["date"]): r for r in frame.to_dict("records")}
            for d in dates:
                raw_row = records.get(d)
                if raw_row is None:
                    bad.append({"code": code, "date": d, "reason": "date_not_returned"})
                    continue
                ok, reason, row = valid_row(raw_row)
                if not ok:
                    bad.append({"code": code, "date": d, "reason": reason})
                    continue
                good.append((code, datetime.strptime(d, "%Y-%m-%d").date(), row["open"], row["high"], row["low"], row["close"], row["volume_lots"], row["amount_yuan"], row["prev_close"], row["change_pct"], "baostock", symbol, fetched_at, "accepted_A", reason))
    finally:
        bs.logout()
    # Refuse to append to a partially populated candidate from an earlier run.
    c.command(f"TRUNCATE TABLE {CANDIDATE}")
    if good:
        cols = "code,trade_date,open,high,low,close,volume_lots,amount_yuan,prev_close,change_pct,source,source_symbol,fetched_at,validation_status,validation_reason"
        for i in range(0, len(good), 2000):
            c.insert(CANDIDATE, good[i:i+2000], column_names=cols.split(","))
    promoted = 0
    if args.promote and good:
        # Keys were empty in formal after the lifecycle cleanup; keep a defensive anti-join.
        c.command(f"""
        INSERT INTO stock.kline_daily
          (code,trade_date,open,high,low,close,volume,amount,amplitude,change_pct,change_amount,turnover_rate,created_at,id)
        SELECT c.code,c.trade_date,c.open,c.high,c.low,c.close,c.volume_lots,c.amount_yuan,
               0.,c.change_pct,c.close-c.prev_close,0.,c.fetched_at,0
        FROM {CANDIDATE} c
        LEFT JOIN stock.kline_daily k ON k.code=c.code AND k.trade_date=c.trade_date
        WHERE k.code = ''
        """)
        promoted = len(good)
        # Replace the old missing-OHLC rows with only the unresolved Baostock failures.
        c.command(f"ALTER TABLE stock.purchased_history_data_review_items DELETE WHERE run_id={q(RUN)} AND issue_type='purchased_daily_missing_ohlc_2010'")
        bad_rows = []
        now = fetched_at
        for item in bad:
            code, d, reason = item["code"], item["date"], item["reason"]
            bad_rows.append((RUN, f"{code}|{d}|1d|baostock", "purchased_daily_baostock_unresolved", "baostock", code, datetime.strptime(d, "%Y-%m-%d").date(), "1d", "stock", 1, 0, 1, "baostock_2010_missing_daily_20260922", "", "", "", "0" * 64, now, reason))
        if bad_rows:
            cols = "run_id,review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason"
            c.insert("stock.purchased_history_data_review_items", bad_rows, column_names=cols.split(","))
    payload = {"run_id": RUN, "candidate_table": CANDIDATE, "target_rows": len(targets), "candidate_rows": len(good), "unresolved_rows": len(bad), "unsupported_codes": unsupported, "promoted_rows": promoted, "promote": args.promote, "completed_at": fetched_at.isoformat()}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


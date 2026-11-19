"""Repair missing stock daily bars from the online Baostock provider.

Only exact expected code/date gaps are considered.  Source rows are validated
against overlapping persisted bars before an optional formal-table write.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import qmtmini_daily_backfill_validate as daily
from scripts.daily_kline_coverage_maintenance import _daily_args
from scripts.sync_all_klines import KlineSyncer
from utils.kline_units import normalize_daily_units


def _symbol(code: str) -> str:
    raw, market = str(code).upper().split(".", 1)
    return f"{market.lower()}{raw}"


def _baostock_symbol(code: str) -> str:
    raw, market = str(code).upper().split(".", 1)
    return f"{market.lower()}.{raw}"


def _fetch(code: str, start_date: str, end_date: str) -> tuple[str, pd.DataFrame | None, str | None]:
    try:
        import baostock as bs

        result = bs.query_history_k_data_plus(
            _baostock_symbol(code),
            "date,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="3",
        )
        if result.error_code != "0":
            return code, None, f"baostock_{result.error_code}: {result.error_msg}"
        frame = result.get_data()
        if frame is None or frame.empty or "date" not in frame.columns:
            return code, None, None
        frame = frame.rename(columns={"vol": "volume"})
        required = ["date", "open", "high", "low", "close", "volume", "amount"]
        if any(col not in frame.columns for col in required):
            return code, None, "missing_source_columns"
        frame = frame[required].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
        for col in required[1:]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=required).drop_duplicates(subset=["date"], keep="last")
        # Sina exposes shares/yuan; the persisted contract is lots/yuan.
        frame = normalize_daily_units(frame, volume_unit="shares", amount_unit="yuan")
        return code, frame.sort_values("date").reset_index(drop=True), None
    except Exception as exc:  # keep one provider failure isolated to the code
        return code, None, f"{type(exc).__name__}: {exc}"


def _init_baostock_worker() -> None:
    import baostock as bs
    result = bs.login()
    if result.error_code != "0":
        raise RuntimeError(f"baostock worker login failed: {result.error_code} {result.error_msg}")


def _missing_keys(client, args: argparse.Namespace) -> set[tuple[str, Any]]:
    codes = daily.select_codes_for_args(client, args)
    if not codes:
        return set()
    code_sql = ",".join(daily.quote_sql(code) for code in codes)
    dates = [row[0] for row in client.query(
        f"SELECT trade_date FROM trade_calendar WHERE market='SH' AND is_trading=1 "
        f"AND trade_date >= toDate({daily.quote_sql(args.start_date)}) "
        f"AND trade_date <= toDate({daily.quote_sql(args.end_date)}) ORDER BY trade_date"
    ).result_rows]
    metadata = {
        str(row[0]).upper(): {"list_date": row[1], "delist_date": row[2]}
        for row in client.query(f"SELECT code,list_date,delist_date FROM stocks WHERE code IN ({code_sql})").result_rows
    }
    expected, _ = daily.expected_daily_coverage_keys(codes, dates, metadata)
    daily.ensure_market_status_table(client)
    status_rows = client.query(
        f"SELECT code,start_date,end_date,status,evidence_url,reason FROM {daily.MARKET_STATUS_TABLE} FINAL "
        f"WHERE code IN ({code_sql}) AND start_date <= toDate({daily.quote_sql(args.end_date)}) "
        f"AND end_date >= toDate({daily.quote_sql(args.start_date)}) "
        "AND status IN ('suspended','delisted','not_trading') AND review_state='verified'"
    ).result_rows
    effective_expected = expected - daily.known_market_status_keys(expected, status_rows)
    actual = {
        (str(row[0]).upper(), row[1]) for row in client.query(
            f"SELECT code,trade_date FROM kline_daily FINAL WHERE code IN ({code_sql}) "
            f"AND trade_date >= toDate({daily.quote_sql(args.start_date)}) "
            f"AND trade_date <= toDate({daily.quote_sql(args.end_date)})"
        ).result_rows
    }
    return effective_expected - actual


def _validate_overlap(client, code: str, frame: pd.DataFrame) -> tuple[bool, str]:
    if frame.empty:
        return False, "empty_source"
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        return False, "nonpositive_ohlc"
    if (frame["high"] < frame[["open", "close"]].max(axis=1)).any() or (frame["low"] > frame[["open", "close"]].min(axis=1)).any():
        return False, "invalid_ohlc_bounds"
    start, end = frame["date"].min(), frame["date"].max()
    rows = client.query(
        f"SELECT trade_date,open,high,low,close,volume,amount FROM kline_daily FINAL "
        f"WHERE code={daily.quote_sql(code)} AND trade_date BETWEEN toDate({daily.quote_sql(start)}) AND toDate({daily.quote_sql(end)})"
    ).result_rows
    if not rows:
        return True, "no_overlap"
    existing = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
    existing["date"] = existing["date"].astype(str)
    merged = frame.merge(existing, on="date", suffixes=("_src", "_db"))
    for field, tolerance in (("open", 0.01), ("high", 0.01), ("low", 0.01), ("close", 0.01), ("amount", 0.03), ("volume", 0.03)):
        if len(merged) and ((merged[f"{field}_src"] - merged[f"{field}_db"]).abs() / merged[f"{field}_db"].replace(0, pd.NA)).fillna(0).gt(tolerance).any():
            return False, f"overlap_mismatch_{field}"
    return True, "validated_overlap"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default="2026-09-18")
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=Path("F:/Stock/AiStockData/data/runtime/daily_kline_coverage/sina_repair_latest.json"))
    args = parser.parse_args()
    client = daily.ch_client()
    audit_args = _daily_args(args.start_date, args.end_date, [], 30, args.report)
    missing = _missing_keys(client, audit_args)
    by_code: dict[str, set[str]] = {}
    for code, trade_date in missing:
        by_code.setdefault(code, set()).add(str(trade_date))
    codes = sorted(by_code)
    if args.max_codes > 0:
        codes = codes[: args.max_codes]
    fetched: dict[str, pd.DataFrame] = {}
    failures: dict[str, str] = {}
    validated: dict[str, dict[str, Any]] = {}
    # Baostock uses one process-wide session; keep requests serial and stable.
    executor_type = ThreadPoolExecutor
    import baostock as bs
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
    try:
        if args.workers > 1:
            pool = ProcessPoolExecutor(max_workers=args.workers, initializer=_init_baostock_worker)
        else:
            pool = ThreadPoolExecutor(max_workers=1)
        with pool:
            futures = {
                pool.submit(
                    _fetch,
                    code,
                    (datetime.strptime(min(by_code[code]), "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d"),
                    (datetime.strptime(max(by_code[code]), "%Y-%m-%d") + timedelta(days=7)).strftime("%Y-%m-%d"),
                ): code
                for code in codes
            }
            for future in as_completed(futures):
                code, frame, error = future.result()
                if error:
                    failures[code] = error
                elif frame is not None:
                    fetched[code] = frame
    finally:
        bs.logout()
    syncer = KlineSyncer() if args.apply else None
    written = 0
    for code, frame in fetched.items():
        target_dates = by_code[code]
        ok, reason = _validate_overlap(client, code, frame)
        target_frame = frame[frame["date"].isin(target_dates)].copy()
        validated[code] = {"rows": len(target_frame), "reason": reason, "dates": sorted(target_frame["date"].tolist())}
        if not ok or target_frame.empty:
            continue
        if args.apply:
            count = int(syncer._save_kline_to_db(code, "1d", target_frame) or 0)
            written += count
    payload = {
        "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "start_date": args.start_date, "end_date": args.end_date, "apply": args.apply,
        "missing_code_dates_before": len(missing), "candidate_codes": len(codes),
        "fetched_codes": len(fetched), "failed_codes": len(failures),
        "validated_codes": len([x for x in validated.values() if x["reason"] in {"validated_overlap", "no_overlap"}]),
        "written_rows": written, "failures": failures, "validated": validated, "report": str(args.report),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("missing_code_dates_before", "candidate_codes", "fetched_codes", "failed_codes", "validated_codes", "written_rows", "report")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


"""Read-only external and source-shape audit for the 2000-2010 review queue.

The script consumes the already generated gate audit CSV, checks unresolved
daily keys against BaoStock, and emits evidence reports only. It never writes
to ClickHouse or changes the active review manifest.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import runtime_path

DEFAULT_AUDIT = runtime_path(
    "quality_plan", "pre2010_purchase_daily_envelope_20260923_v3",
    "code_day_period_audit.csv",
)
DEFAULT_OUT = runtime_path("quality_plan", "pre2010_gate_blockers_20260923_v1")
FIELDS = "date,open,high,low,close,volume,amount,tradestatus"
_BS: Any = None


def _init_worker() -> None:
    global _BS
    import baostock as bs

    result = bs.login()
    if result.error_code != "0":
        raise RuntimeError(f"baostock login failed: {result.error_code} {result.error_msg}")
    _BS = bs


def _fetch_code(task: tuple[str, str, str]) -> dict[str, Any]:
    code, start, end = task
    if not (code.endswith(".SH") or code.endswith(".SZ")):
        return {"code": code, "status": "unsupported_market", "error": "BaoStock audit limited to SH/SZ"}
    symbol = code[-2:].lower() + "." + code[:6]
    try:
        result = _BS.query_history_k_data_plus(
            symbol, FIELDS, start_date=start, end_date=end,
            frequency="d", adjustflag="3",
        )
        if result.error_code != "0":
            return {"code": code, "status": "query_error", "error": f"{result.error_code}: {result.error_msg}"}
        frame = result.get_data()
        records: list[dict[str, Any]] = []
        if frame is not None and not frame.empty:
            for row in frame.to_dict("records"):
                item: dict[str, Any] = {"code": code}
                for key in FIELDS.split(","):
                    value = row.get(key)
                    if key in {"date", "tradestatus"}:
                        item[key] = None if value in (None, "") else str(value)
                    else:
                        try:
                            item[key] = float(value) if value not in (None, "") else None
                        except (TypeError, ValueError):
                            item[key] = None
                records.append(item)
        time.sleep(0.08)
        return {"code": code, "status": "ok", "records": records}
    except Exception as exc:  # keep provider failures isolated by code
        return {"code": code, "status": "exception", "error": f"{type(exc).__name__}: {exc}"}


def _finite_positive(row: dict[str, Any]) -> bool:
    values = [row.get(k) for k in ("open", "high", "low", "close")]
    return all(v is not None and math.isfinite(v) and v > 0 for v in values)


def _classify(row: dict[str, Any] | None) -> tuple[str, str]:
    if row is None:
        return "no_exact_daily_row", "BaoStock returned no row for target date"
    status = row.get("tradestatus")
    vol, amount = row.get("volume"), row.get("amount")
    if status == "0" and vol == 0 and amount == 0:
        return "confirmed_suspended_no_trade", "tradestatus=0 and volume/amount are zero"
    if status == "1" and _finite_positive(row) and vol is not None and amount is not None and vol > 0 and amount > 0:
        high, low = row.get("high"), row.get("low")
        opn, close = row.get("open"), row.get("close")
        if high >= max(opn, close) and low <= min(opn, close):
            return "confirmed_traded_daily", "tradestatus=1 with valid OHLC and positive volume/amount"
        return "invalid_external_ohlc", "tradestatus=1 but OHLC bounds are inconsistent"
    if status == "0":
        return "suspended_row_fields_inconsistent", "tradestatus=0 but volume/amount not both zero"
    if status == "1":
        return "traded_row_fields_invalid", "tradestatus=1 but OHLC/volume/amount failed validation"
    return "external_row_unclassified", f"unexpected tradestatus={status!r}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-csv", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-codes", type=int, default=0, help="test cap; 0 audits all missing-daily codes")
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        raise ValueError("workers must be between 1 and 4")

    frame = pd.read_csv(args.audit_csv, dtype={"code": str, "trade_date": str}, low_memory=False)
    blockers = frame[frame["decision"].astype(str).str.upper().ne("A")].copy()
    category_counts = Counter(blockers["primary_category"].fillna("unknown").astype(str))
    missing = blockers[blockers["primary_category"].eq("purchased_daily_missing_or_ambiguous")].copy()
    target_dates: dict[str, set[str]] = defaultdict(set)
    for row in missing[["code", "trade_date"]].drop_duplicates().itertuples(index=False):
        target_dates[str(row.code)].add(str(row.trade_date))

    codes = sorted(target_dates)
    if args.max_codes:
        codes = codes[:args.max_codes]
    tasks = []
    for code in codes:
        dates = sorted(target_dates[code])
        tasks.append((code, dates[0], dates[-1]))

    fetched: dict[str, dict[str, dict[str, Any]]] = {}
    provider_status: Counter[str] = Counter()
    if tasks:
        with ProcessPoolExecutor(max_workers=min(args.workers, len(tasks)), initializer=_init_worker) as pool:
            futures = {pool.submit(_fetch_code, task): task[0] for task in tasks}
            for future in as_completed(futures):
                result = future.result()
                code = result["code"]
                provider_status[result["status"]] += 1
                fetched[code] = {str(r["date"]): r for r in result.get("records", []) if r.get("date")}
                if result.get("error"):
                    fetched.setdefault(code, {})["__error__"] = {"error": result["error"]}

    period_rows: list[dict[str, Any]] = []
    day_rows: list[dict[str, Any]] = []
    classified: Counter[str] = Counter()
    check_counts: Counter[str] = Counter()
    by_year_status: Counter[tuple[str, str]] = Counter()
    for code in codes:
        by_date = fetched.get(code, {})
        error = by_date.get("__error__", {}).get("error", "")
        for day in sorted(target_dates[code]):
            ext = by_date.get(day)
            cls, detail = _classify(ext)
            by_year_status[(day[:4], cls)] += 1
            if error and not ext:
                cls, detail = "provider_error", error
            classified[cls] += 1
            related = missing[(missing["code"].eq(code)) & (missing["trade_date"].eq(day))]
            period_checks = []
            for item in related.to_dict("records"):
                minute_high, minute_low = item.get("minute_high"), item.get("minute_low")
                minute_vol, minute_amt = item.get("minute_volume_lots"), item.get("minute_amount_yuan")
                price_ok = volume_ok = amount_ok = None
                if cls == "confirmed_suspended_no_trade":
                    if (minute_vol or 0) == 0 and (minute_amt or 0) == 0:
                        check = "zero_minute_aggregate_on_confirmed_suspension"
                    elif (minute_vol or 0) > 0 or (minute_amt or 0) > 0:
                        check = "minute_activity_conflicts_with_confirmed_suspension"
                    else:
                        check = "minute_quantity_missing_on_confirmed_suspension"
                elif cls == "confirmed_traded_daily" and ext:
                    price_ok = (
                        minute_high is not None and minute_low is not None
                        and minute_high <= ext["high"] and minute_low >= ext["low"]
                    )
                    # BaoStock reports shares; the gate's local unit is lots.
                    external_lots = ext["volume"] / 100.0
                    volume_ok = minute_vol is not None and minute_vol <= external_lots
                    amount_ok = minute_amt is not None and minute_amt <= ext["amount"]
                    check = "external_daily_envelope_pass" if price_ok and volume_ok and amount_ok else "external_daily_conflict"
                else:
                    check = "not_comparable"
                check_counts[check] += 1
                if cls == "confirmed_traded_daily" and ext:
                    check_counts["external_price_envelope_pass" if price_ok else "external_price_envelope_fail"] += 1
                    check_counts["external_volume_upper_bound_pass" if volume_ok else "external_volume_upper_bound_fail"] += 1
                    check_counts["external_amount_upper_bound_pass" if amount_ok else "external_amount_upper_bound_fail"] += 1
                period_checks.append(check)
                period_rows.append({
                    "code": code, "trade_date": day, "period": item.get("period"),
                    "gate_reason": item.get("reasons"), "external_status": cls,
                    "external_check": check, "external_open": ext.get("open") if ext else None,
                    "external_high": ext.get("high") if ext else None,
                    "external_low": ext.get("low") if ext else None,
                    "external_close": ext.get("close") if ext else None,
                    "external_volume_shares": ext.get("volume") if ext else None,
                    "external_volume_lots": ext.get("volume") / 100.0 if ext and ext.get("volume") is not None else None,
                    "external_amount_yuan": ext.get("amount") if ext else None,
                    "tradestatus": ext.get("tradestatus") if ext else None,
                    "price_envelope_pass": price_ok,
                    "volume_upper_bound_pass": volume_ok,
                    "amount_upper_bound_pass": amount_ok,
                    "minute_high": minute_high, "minute_low": minute_low,
                    "minute_volume_lots": minute_vol, "minute_amount_yuan": minute_amt,
                })
            day_rows.append({
                "code": code, "trade_date": day, "external_status": cls,
                "external_detail": detail, "tradestatus": ext.get("tradestatus") if ext else None,
                "open": ext.get("open") if ext else None, "high": ext.get("high") if ext else None,
                "low": ext.get("low") if ext else None, "close": ext.get("close") if ext else None,
                "volume_shares": ext.get("volume") if ext else None,
                "volume_lots": ext.get("volume") / 100.0 if ext and ext.get("volume") is not None else None,
                "amount_yuan": ext.get("amount") if ext else None,
                "period_count": len(related), "period_checks": ";".join(sorted(set(period_checks))),
            })

    args.output_dir.mkdir(parents=True, exist_ok=True)
    day_frame = pd.DataFrame(day_rows)
    period_frame = pd.DataFrame(period_rows)
    day_frame.to_csv(args.output_dir / "baostock_missing_daily_code_days.csv", index=False, encoding="utf-8-sig")
    period_frame.to_csv(args.output_dir / "baostock_missing_daily_period_checks.csv", index=False, encoding="utf-8-sig")
    if not period_frame.empty:
        period_frame["_minute_has_positive"] = (
            pd.to_numeric(period_frame["minute_volume_lots"], errors="coerce").fillna(0).gt(0)
            | pd.to_numeric(period_frame["minute_amount_yuan"], errors="coerce").fillna(0).gt(0)
        )
    period_summary = period_frame.groupby(["code", "trade_date"], as_index=False).agg(
        checked_periods=("period", "size"),
        all_periods_external_envelope_pass=("external_check", lambda values: bool((values == "external_daily_envelope_pass").all())),
        any_minute_positive=("_minute_has_positive", "any"),
    ) if not period_frame.empty else pd.DataFrame(columns=["code", "trade_date", "checked_periods", "all_periods_external_envelope_pass", "any_minute_positive"])
    merged_days = day_frame.merge(period_summary, on=["code", "trade_date"], how="left")
    b_candidates = merged_days[
        merged_days["external_status"].eq("confirmed_traded_daily")
        & merged_days["all_periods_external_envelope_pass"].fillna(False)
    ]
    no_trade_zero = merged_days[
        merged_days["external_status"].eq("confirmed_suspended_no_trade")
        & ~merged_days["any_minute_positive"].fillna(False)
    ]
    no_trade_conflict = merged_days[
        merged_days["external_status"].eq("confirmed_suspended_no_trade")
        & merged_days["any_minute_positive"].fillna(False)
    ]
    no_exact = merged_days[merged_days["external_status"].eq("no_exact_daily_row")]
    b_candidates.to_csv(args.output_dir / "baostock_B_candidate_code_days.csv", index=False, encoding="utf-8-sig")
    if not period_frame.empty and not b_candidates.empty:
        b_periods = period_frame.merge(b_candidates[["code", "trade_date"]], on=["code", "trade_date"], how="inner")
    else:
        b_periods = pd.DataFrame()
    b_periods.to_csv(args.output_dir / "baostock_B_candidate_period_rows.csv", index=False, encoding="utf-8-sig")
    no_trade_zero.to_csv(args.output_dir / "baostock_confirmed_suspension_zero_minute_candidates.csv", index=False, encoding="utf-8-sig")
    no_trade_conflict.to_csv(args.output_dir / "baostock_suspension_minute_conflicts.csv", index=False, encoding="utf-8-sig")
    no_exact.to_csv(args.output_dir / "baostock_no_exact_daily_row.csv", index=False, encoding="utf-8-sig")
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "audit_input": str(args.audit_csv), "output_dir": str(args.output_dir),
        "queue_scope": "read-only audit CSV rows where decision != A",
        "blocker_period_items": int(len(blockers)), "blocker_unique_code_days": int(blockers[["code", "trade_date"]].drop_duplicates().shape[0]),
        "blocker_category_period_items": dict(category_counts),
        "daily_missing_period_items": int(len(missing)),
        "daily_missing_unique_code_days": int(missing[["code", "trade_date"]].drop_duplicates().shape[0]),
        "daily_missing_codes": len(target_dates), "requested_codes": len(codes),
        "bao_stock_code_query_status": dict(provider_status),
        "bao_stock_code_day_status": dict(classified),
                "bao_stock_code_day_status_by_year": {
                    year: {status: count for (yr, status), count in sorted(by_year_status.items()) if yr == year}
                    for year in sorted({yr for yr, _ in by_year_status})
                },
                "bao_stock_period_check_counts": dict(check_counts),
        "baostock_B_candidate_code_days": len(b_candidates),
        "baostock_B_candidate_period_rows": int(period_summary.loc[period_summary["all_periods_external_envelope_pass"].fillna(False), "checked_periods"].sum()) if not period_summary.empty else 0,
        "confirmed_suspension_zero_minute_code_days": len(no_trade_zero),
        "confirmed_suspension_minute_conflict_code_days": len(no_trade_conflict),
        "no_exact_daily_row_code_days": len(no_exact),
        "formal_tables_mutated": False, "review_queue_mutated": False,
        "active_review_manifest_updated": False,
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

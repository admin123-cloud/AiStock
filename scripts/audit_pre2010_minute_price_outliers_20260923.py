"""Cross-check minute/daily price-envelope failures against external daily feeds.

This is a read-only audit of the 2026-09-23 gate CSV. It does not write to
ClickHouse or alter any review/formal data.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_purchased_review_against_external_daily import _fetch_provider
from utils.paths import runtime_path

DEFAULT_AUDIT = runtime_path(
    "quality_plan", "pre2010_purchase_daily_envelope_20260923_v3",
    "code_day_period_audit.csv",
)
DEFAULT_OUT = runtime_path("quality_plan", "pre2010_minute_price_outliers_20260923_v1")
WSTOCK_EXPORT = Path(r"E:\AStockHistory\wstock\输出\来源归档\out_历史混合")


def _bs_symbol(code: str) -> str:
    return f"{code[-2:].lower()}.{code[:6]}"


def _valid_provider_row(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    try:
        opn, high, low, close = (float(row[k]) for k in ("open", "high", "low", "close"))
        return min(opn, high, low, close) > 0 and high >= max(opn, close) and low <= min(opn, close)
    except (KeyError, TypeError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-csv", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    audit = pd.read_csv(args.audit_csv, dtype={"code": str, "trade_date": str}, low_memory=False)
    failed = audit[
        audit["decision"].astype(str).str.upper().eq("C")
        & audit["primary_category"].eq("minute_price_outside_daily_range")
    ].copy()
    targets = failed[["code", "trade_date"]].drop_duplicates().sort_values(["trade_date", "code"])
    wstock_files: dict[str, list[Path]] = {}
    if WSTOCK_EXPORT.exists():
        for path in WSTOCK_EXPORT.rglob("*.csv"):
            wstock_files.setdefault(path.name.upper(), []).append(path)

    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
    bao: dict[tuple[str, str], dict[str, Any] | None] = {}
    errors: dict[str, str] = {}
    try:
        for code in sorted(targets["code"].unique()):
            dates = sorted(targets.loc[targets["code"].eq(code), "trade_date"].tolist())
            if not (code.endswith(".SH") or code.endswith(".SZ")):
                errors[code] = "unsupported_market"
                continue
            result = bs.query_history_k_data_plus(
                _bs_symbol(code),
                "date,open,high,low,close,volume,amount,tradestatus",
                start_date=dates[0], end_date=dates[-1], frequency="d", adjustflag="3",
            )
            if result.error_code != "0":
                errors[code] = f"{result.error_code}: {result.error_msg}"
                continue
            frame = result.get_data()
            if frame is not None and not frame.empty:
                for row in frame.to_dict("records"):
                    bao[(code, str(row["date"]))] = row
    finally:
        bs.logout()

    sina_cache: dict[tuple[str, int], pd.DataFrame | None] = {}
    sina_errors: dict[str, str] = {}
    for code, day in targets.itertuples(index=False):
        key = (str(code), int(str(day)[:4]))
        if key in sina_cache:
            continue
        try:
            sina_cache[key] = _fetch_provider(key[0], key[1], "sina")
        except Exception as exc:
            sina_cache[key] = None
            sina_errors[f"{key[0]}:{key[1]}"] = f"{type(exc).__name__}: {exc}"

    period_rows: list[dict[str, Any]] = []
    for row in failed.to_dict("records"):
        code, day = str(row["code"]), str(row["trade_date"])
        b = bao.get((code, day))
        sframe = sina_cache.get((code, int(day[:4])))
        srow = None
        if sframe is not None and not sframe.empty:
            hit = sframe[sframe["date"].eq(day)]
            if not hit.empty:
                srow = hit.iloc[0].to_dict()
        minute_hi, minute_lo = row.get("minute_high"), row.get("minute_low")
        if b is not None and _valid_provider_row({k: b.get(k) for k in ("open", "high", "low", "close")}):
            b_high, b_low = float(b["high"]), float(b["low"])
            b_envelope = minute_hi is not None and minute_lo is not None and float(minute_hi) <= b_high and float(minute_lo) >= b_low
        else:
            b_high = b_low = None
            b_envelope = None
        s_envelope = None
        if _valid_provider_row(srow):
            s_high, s_low = float(srow["high"]), float(srow["low"])
            s_envelope = minute_hi is not None and minute_lo is not None and float(minute_hi) <= s_high and float(minute_lo) >= s_low
        else:
            s_high = s_low = None
        market = code[-2:]
        wstock_name = f"{market}{code[:6]}.csv".upper()
        wstock_paths = wstock_files.get(wstock_name, [])
        wstock_options: list[dict[str, Any]] = []
        wstock_error = ""
        for wstock_path in wstock_paths:
            try:
                wframe = pd.read_csv(wstock_path, usecols=["Date", "Open", "Low", "High", "Close", "Volume", "Amount"])
                wframe["_date"] = wframe["Date"].astype(str).str[:10]
                wday = wframe[wframe["_date"].eq(day)]
                if not wday.empty:
                    option = {
                        "path": str(wstock_path),
                        "rows": len(wday),
                        "open": float(wday["Open"].iloc[0]),
                        "high": float(wday["High"].max()),
                        "low": float(wday["Low"].min()),
                        "close": float(wday["Close"].iloc[-1]),
                        "volume": float(pd.to_numeric(wday["Volume"], errors="coerce").fillna(0).sum()),
                        "amount": float(pd.to_numeric(wday["Amount"], errors="coerce").fillna(0).sum()),
                        "slots": int(wday["Date"].astype(str).nunique()),
                    }
                    daily_high, daily_low = row.get("daily_high"), row.get("daily_low")
                    option["inside_purchase_envelope"] = (
                        daily_high is not None and daily_low is not None
                        and option["high"] <= float(daily_high) and option["low"] >= float(daily_low)
                    )
                    option["amount_diff_pct"] = (
                        abs(option["amount"] - float(row["daily_amount_yuan"])) / max(abs(float(row["daily_amount_yuan"])), 1.0)
                        if row.get("daily_amount_yuan") is not None else None
                    )
                    option["volume_not_over_daily"] = (
                        option["volume"] <= float(row["daily_volume_lots"])
                        if row.get("daily_volume_lots") is not None else False
                    )
                    wstock_options.append(option)
            except Exception as exc:
                wstock_error = f"{type(exc).__name__}: {exc}"
        wstock_options.sort(key=lambda x: (
            x["rows"] == 48 and x["slots"] == 48,
            x["inside_purchase_envelope"],
            x["volume_not_over_daily"],
            x["amount_diff_pct"] is not None and x["amount_diff_pct"] <= 0.003,
            x["amount_diff_pct"] is not None,
        ), reverse=True)
        wr = wstock_options[0] if wstock_options else {}
        wstock_envelope = None
        if wr and wr["high"] > 0 and wr["low"] > 0:
            wstock_envelope = bool(wr["inside_purchase_envelope"])
        period_rows.append({
            "code": code, "trade_date": day, "period": row.get("period"),
            "gate_reason": row.get("reasons"),
            "purchase_daily_open": row.get("daily_open"), "purchase_daily_high": row.get("daily_high"),
            "purchase_daily_low": row.get("daily_low"), "purchase_daily_close": row.get("daily_close"),
            "minute_open": row.get("minute_open"), "minute_high": minute_hi,
            "minute_low": minute_lo, "minute_close": row.get("minute_close"),
            "baostock_open": b.get("open") if b else None, "baostock_high": b_high,
            "baostock_low": b_low, "baostock_close": b.get("close") if b else None,
            "baostock_price_envelope_pass": b_envelope,
            "sina_open": srow.get("open") if srow else None, "sina_high": s_high,
            "sina_low": s_low, "sina_close": srow.get("close") if srow else None,
            "sina_price_envelope_pass": s_envelope,
            "sina_amount_yuan": srow.get("amount") if srow else None,
            "sina_volume_lots": srow.get("volume") if srow else None,
            "wstock_path": wr.get("path", ""), "wstock_files_found": len(wstock_paths),
            "wstock_rows": wr.get("rows"), "wstock_unique_slots": wr.get("slots"),
            "wstock_open": wr.get("open"), "wstock_high": wr.get("high"),
            "wstock_low": wr.get("low"), "wstock_close": wr.get("close"),
            "wstock_volume_lots": wr.get("volume"), "wstock_amount_yuan": wr.get("amount"),
            "wstock_amount_diff_pct": wr.get("amount_diff_pct"),
            "wstock_volume_not_over_daily": wr.get("volume_not_over_daily"),
            "wstock_price_envelope_pass": wstock_envelope,
            "wstock_error": wstock_error,
        })

    result_frame = pd.DataFrame(period_rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result_frame.to_csv(args.output_dir / "price_outlier_external_crosscheck.csv", index=False, encoding="utf-8-sig")
    day = result_frame.drop_duplicates(["code", "trade_date"])
    wstock_usable = result_frame.drop_duplicates(["code", "trade_date"])
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "audit_input": str(args.audit_csv), "output_dir": str(args.output_dir),
        "price_outlier_period_items": len(failed), "price_outlier_unique_code_days": len(targets),
        "bao_stock_price_envelope_pass_code_days": int(day["baostock_price_envelope_pass"].fillna(False).sum()),
        "bao_stock_price_envelope_fail_code_days": int(day["baostock_price_envelope_pass"].eq(False).sum()),
        "sina_price_envelope_pass_code_days": int(day["sina_price_envelope_pass"].fillna(False).sum()),
        "sina_price_envelope_fail_code_days": int(day["sina_price_envelope_pass"].eq(False).sum()),
        "bao_stock_missing_exact_days": int(day["baostock_price_envelope_pass"].isna().sum()),
        "sina_missing_exact_days": int(day["sina_price_envelope_pass"].isna().sum()),
        "wstock_files_present": int(day["wstock_files_found"].fillna(0).gt(0).sum()),
        "wstock_exact_days": int(day["wstock_rows"].notna().sum()),
        "wstock_full_48_slot_days": int((day["wstock_rows"].eq(48) & day["wstock_unique_slots"].eq(48)).fillna(False).sum()),
        "wstock_price_envelope_pass_code_days": int(day["wstock_price_envelope_pass"].fillna(False).sum()),
        "wstock_price_envelope_fail_code_days": int(day["wstock_price_envelope_pass"].eq(False).sum()),
        "sina_query_errors": sina_errors, "baostock_query_errors": errors,
        "formal_tables_mutated": False, "review_queue_mutated": False,
        "active_review_manifest_updated": False,
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build read-only 5m/15m/30m/60m candidates from existing WStock exports.

Only code-days with a gate-reported price, OHLC, or source-structure blocker
are considered. Output stays in the runtime quality-plan area; no ClickHouse
or review-queue mutation is performed.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import runtime_path

AUDIT = runtime_path(
    "quality_plan", "pre2010_purchase_daily_envelope_20260923_v3",
    "code_day_period_audit.csv",
)
WSTOCK_ROOT = Path(r"E:\AStockHistory\wstock\输出\来源归档\out_历史混合")
OUT = runtime_path("quality_plan", "pre2010_wstock_source_recovery_20260923_v1")
PERIODS = {"15m": 3, "30m": 6, "60m": 12}
TARGET_CATEGORIES = {
    "minute_price_outside_daily_range",
    "invalid_minute_bar_or_quantity",
    "source_structure_incomplete",
}


def main() -> int:
    audit = pd.read_csv(AUDIT, dtype={"code": str, "trade_date": str}, low_memory=False)
    targets = audit[
        audit["decision"].astype(str).str.upper().eq("C")
        & audit["primary_category"].isin(TARGET_CATEGORIES)
    ].copy()
    daily = targets.groupby(["code", "trade_date"], as_index=False).agg(
        daily_open=("daily_open", "first"), daily_high=("daily_high", "first"),
        daily_low=("daily_low", "first"), daily_close=("daily_close", "first"),
        daily_volume_lots=("daily_volume_lots", "first"), daily_amount_yuan=("daily_amount_yuan", "first"),
        blocked_periods=("period", lambda values: ",".join(sorted(set(values)))),
        trigger_categories=("primary_category", lambda values: ",".join(sorted(set(values)))),
    )
    file_index: dict[str, list[Path]] = defaultdict(list)
    if WSTOCK_ROOT.exists():
        for path in WSTOCK_ROOT.rglob("*.csv"):
            file_index[path.name.upper()].append(path)

    statuses: list[dict] = []
    candidates: list[dict] = []
    reason_counts: Counter[str] = Counter()
    for dayrow in daily.to_dict("records"):
        code, trade_date = dayrow["code"], dayrow["trade_date"]
        daily_complete = all(
            pd.notna(dayrow.get(field))
            for field in ("daily_high", "daily_low", "daily_volume_lots", "daily_amount_yuan")
        )
        market = code[-2:]
        name = f"{market}{code[:6]}.csv".upper()
        options = []
        errors = []
        for path in file_index.get(name, []):
            try:
                frame = pd.read_csv(path, usecols=["Date", "Open", "Low", "High", "Close", "Volume", "Amount"])
                frame["Date"] = frame["Date"].astype(str)
                exact = frame[frame["Date"].str[:10].eq(trade_date)].sort_values("Date").copy()
                if exact.empty:
                    continue
                for col in ("Open", "Low", "High", "Close", "Volume", "Amount"):
                    exact[col] = pd.to_numeric(exact[col], errors="coerce")
                slots = exact["Date"].nunique()
                actual_slots = set(exact["Date"].astype(str).str[11:16])
                expected_slots = {
                    value.strftime("%H:%M")
                    for start, end in (("09:35", "11:30"), ("13:05", "15:00"))
                    for value in pd.date_range(f"2000-01-01 {start}", f"2000-01-01 {end}", freq="5min")
                }
                schedule_valid = actual_slots == expected_slots
                volume_total = float(exact["Volume"].fillna(0).sum())
                amount_total = float(exact["Amount"].fillna(0).sum())
                quantity_legal = bool((exact[["Volume", "Amount"]].fillna(0) >= 0).all().all())
                price_legal = bool(
                    exact[["Open", "High", "Low", "Close"]].notna().all().all()
                    and (exact[["Open", "High", "Low", "Close"]] > 0).all().all()
                    and (exact["High"] >= exact[["Open", "Close"]].max(axis=1)).all()
                    and (exact["Low"] <= exact[["Open", "Close"]].min(axis=1)).all()
                )
                h = float(exact["High"].max())
                lo = float(exact["Low"].min())
                inside = h <= float(dayrow["daily_high"]) and lo >= float(dayrow["daily_low"]) if daily_complete else None
                vol_upper = volume_total <= float(dayrow["daily_volume_lots"]) if daily_complete else None
                amount_upper = amount_total <= float(dayrow["daily_amount_yuan"]) if daily_complete else None
                amount_diff = (
                    abs(amount_total - float(dayrow["daily_amount_yuan"])) / max(abs(float(dayrow["daily_amount_yuan"])), 1.0)
                    if daily_complete else None
                )
                # Prefer complete and structurally valid rows inside the independently
                # verified daily envelope; path ordering only breaks exact ties.
                score = (
                    len(exact) == 48 and slots == 48 and schedule_valid and quantity_legal,
                    price_legal, inside is True, vol_upper is True, amount_upper is True,
                    -(amount_diff if amount_diff is not None else 1.0),
                )
                options.append((score, path, exact, {
                    "rows": len(exact), "slots": slots, "schedule_valid": schedule_valid,
                    "quantity_legal": quantity_legal, "high": h, "low": lo,
                    "volume": volume_total, "amount": amount_total,
                    "price_legal": price_legal, "inside_daily_envelope": inside,
                    "volume_not_over_daily": vol_upper, "amount_not_over_daily": amount_upper,
                    "amount_diff_pct": amount_diff, "daily_complete": daily_complete,
                }))
            except Exception as exc:
                errors.append(f"{path}: {type(exc).__name__}: {exc}")
        if not options:
            reason = "no_existing_wstock_export" if not file_index.get(name) else "no_exact_day_in_existing_exports"
            reason_counts[reason] += 1
            statuses.append({"code": code, "trade_date": trade_date, "status": reason, "wstock_files_found": len(file_index.get(name, [])), "blocked_periods": dayrow["blocked_periods"], "trigger_categories": dayrow["trigger_categories"], "error": ";".join(errors)})
            continue
        options.sort(key=lambda item: item[0], reverse=True)
        _, source_path, bars, summary = options[0]
        source_valid = (
            summary["rows"] == 48 and summary["slots"] == 48
            and summary["schedule_valid"] and summary["quantity_legal"]
            and summary["price_legal"]
        )
        daily_valid = (
            summary["daily_complete"] and summary["inside_daily_envelope"]
            and summary["volume_not_over_daily"] and summary["amount_not_over_daily"]
        )
        day_valid = source_valid and bool(daily_valid)
        if day_valid:
            status = "B_candidate_ready"
        elif source_valid and not summary["daily_complete"]:
            status = "complete_wstock_needs_valid_daily"
        else:
            status = "wstock_present_but_gate_blocked"
        reason_counts[status] += 1
        statuses.append({
            "code": code, "trade_date": trade_date, "status": status,
            "wstock_files_found": len(file_index.get(name, [])), "source_path": str(source_path),
            "trigger_categories": dayrow["trigger_categories"],
            "rows": summary["rows"], "unique_slots": summary["slots"],
            "slot_schedule_valid": summary["schedule_valid"], "quantity_legal": summary["quantity_legal"],
            "high": summary["high"], "low": summary["low"],
            "volume_lots": summary["volume"], "amount_yuan": summary["amount"],
            "price_legal": summary["price_legal"], "inside_daily_envelope": summary["inside_daily_envelope"],
            "volume_not_over_daily": summary["volume_not_over_daily"],
            "amount_not_over_daily": summary["amount_not_over_daily"],
            "amount_diff_pct": summary["amount_diff_pct"],
            "blocked_periods": dayrow["blocked_periods"], "error": ";".join(errors),
        })
        if not day_valid:
            continue
        bars = bars.reset_index(drop=True)
        for ix, source_bar in bars.iterrows():
            candidates.append({
                "code": code, "trade_date": trade_date, "period": "5m",
                "bar_index": int(ix), "source_first_time": str(source_bar["Date"]),
                "source_last_time": str(source_bar["Date"]),
                "open": float(source_bar["Open"]), "high": float(source_bar["High"]),
                "low": float(source_bar["Low"]), "close": float(source_bar["Close"]),
                "volume_lots": float(source_bar["Volume"]), "amount_yuan": float(source_bar["Amount"]),
                "source": "wstock_5m_existing_export", "source_path": str(source_path),
                "source_5m_rows": 48, "source_5m_unique_slots": 48,
                "daily_open": dayrow["daily_open"], "daily_high": dayrow["daily_high"],
                "daily_low": dayrow["daily_low"], "daily_close": dayrow["daily_close"],
                "daily_volume_lots": dayrow["daily_volume_lots"], "daily_amount_yuan": dayrow["daily_amount_yuan"],
                "bar_gate_price_and_ohlc_pass": bool(
                    float(source_bar["High"]) >= max(float(source_bar["Open"]), float(source_bar["Close"]))
                    and float(source_bar["Low"]) <= min(float(source_bar["Open"]), float(source_bar["Close"]))
                    and float(source_bar["High"]) <= float(dayrow["daily_high"])
                    and float(source_bar["Low"]) >= float(dayrow["daily_low"])
                ),
            })
        for period, group_size in PERIODS.items():
            if len(bars) != 48:
                continue
            for ix in range(48 // group_size):
                group = bars.iloc[ix * group_size:(ix + 1) * group_size]
                open_, close = float(group["Open"].iloc[0]), float(group["Close"].iloc[-1])
                high, low = float(group["High"].max()), float(group["Low"].min())
                volume = float(group["Volume"].fillna(0).sum())
                amount = float(group["Amount"].fillna(0).sum())
                valid = (
                    min(open_, high, low, close) > 0
                    and high >= max(open_, close) and low <= min(open_, close)
                    and high <= float(dayrow["daily_high"]) and low >= float(dayrow["daily_low"])
                    and volume >= 0 and amount >= 0
                )
                candidates.append({
                    "code": code, "trade_date": trade_date, "period": period,
                    "trigger_categories": dayrow["trigger_categories"],
                    "bar_index": ix, "source_first_time": str(group["Date"].iloc[0]),
                    "source_last_time": str(group["Date"].iloc[-1]),
                    "open": open_, "high": high, "low": low, "close": close,
                    "volume_lots": volume, "amount_yuan": amount,
                    "source": "wstock_5m_reaggregated", "source_path": str(source_path),
                    "source_5m_rows": 48, "source_5m_unique_slots": 48,
                    "daily_open": dayrow["daily_open"], "daily_high": dayrow["daily_high"],
                    "daily_low": dayrow["daily_low"], "daily_close": dayrow["daily_close"],
                    "daily_volume_lots": dayrow["daily_volume_lots"], "daily_amount_yuan": dayrow["daily_amount_yuan"],
                    "bar_gate_price_and_ohlc_pass": valid,
                })

    OUT.mkdir(parents=True, exist_ok=True)
    status_frame = pd.DataFrame(statuses)
    candidate_frame = pd.DataFrame(candidates)
    status_frame.to_csv(OUT / "wstock_source_recovery_code_days.csv", index=False, encoding="utf-8-sig")
    candidate_frame.to_csv(OUT / "wstock_reaggregated_period_candidates.csv", index=False, encoding="utf-8-sig")
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "audit_input": str(AUDIT), "wstock_export_root": str(WSTOCK_ROOT), "output_dir": str(OUT),
        "target_code_days": len(daily), "target_categories": sorted(TARGET_CATEGORIES),
        "wstock_status_counts": dict(reason_counts),
        "candidate_period_rows": len(candidate_frame),
        "candidate_period_gate_failed_rows": int((~candidate_frame["bar_gate_price_and_ohlc_pass"].astype(bool)).sum()) if not candidate_frame.empty else 0,
        "candidate_rows_by_period": candidate_frame.groupby("period").size().to_dict() if not candidate_frame.empty else {},
        "formal_tables_mutated": False, "review_queue_mutated": False,
        "active_review_manifest_updated": False,
    }
    (OUT / "manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

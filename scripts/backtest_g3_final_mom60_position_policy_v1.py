from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import backtest_g2_g3_market_style_router_v1 as base  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_final_mom60_position_policy_v1")
MODEL = "g3_final_with_g2_gap_supplement"
CURRENT_FORMAL_POLICY = "mainwave_hard_le_5_50"
LEGACY_SENSITIVITY_POLICIES = {
    "mainwave_tier_5_10_50_25_0",
    "all_g3_tier_5_10_50_25_0",
}
POLICIES = [
    "base_50",
    "mainwave_tier_5_10_50_25_0",
    "all_g3_tier_5_10_50_25_0",
    "mainwave_hard_le_5_50",
    "mainwave_hard_le_10_50",
]
EXISTING_SELECTED = base.OUT_DIR / f"{MODEL}_selected_candidates.csv"
EXISTING_COMBINED_LOTS = base.OUT_DIR / "combined_trade_lots_with_prev_context.csv"


def _safe_float(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return out if math.isfinite(out) else default


def _pct(value: Any) -> str:
    out = _safe_float(value)
    return "" if not math.isfinite(out) else f"{out:.2%}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, max_rows: int = 120) -> str:
    if df.empty:
        return "_no data_"
    out = df.head(max_rows).copy()
    for col in pct_cols or set():
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out.to_markdown(index=False)


def _is_mainwave(row: dict[str, Any]) -> bool:
    return str(row.get("mode") or row.get("route") or "") in {"institutional_mainwave", "score120_core"}


def _is_g3(row: dict[str, Any]) -> bool:
    return str(row.get("engine") or "") == "g3_final"


def _policy_slot_pct(row: dict[str, Any], policy: str) -> tuple[float, str]:
    mom60 = _safe_float(row.get("mom60"))
    if not math.isfinite(mom60):
        mom60 = _safe_float(row.get("index_mom60"))
    if not math.isfinite(mom60):
        return base.SLOT_PCT, "missing_mom60_use_base"

    applies_mainwave = _is_g3(row) and _is_mainwave(row)
    applies_all_g3 = _is_g3(row)
    if policy == "base_50":
        return base.SLOT_PCT, "base_50"
    if policy == "mainwave_hard_le_5_50":
        if applies_mainwave and mom60 > 0.05:
            return 0.0, "mainwave_mom60_gt_5_block"
        return base.SLOT_PCT, "base_50"
    if policy == "mainwave_hard_le_10_50":
        if applies_mainwave and mom60 > 0.10:
            return 0.0, "mainwave_mom60_gt_10_block"
        return base.SLOT_PCT, "base_50"
    if policy == "mainwave_tier_5_10_50_25_0":
        if applies_mainwave and mom60 > 0.10:
            return 0.0, "mainwave_mom60_gt_10_block"
        if applies_mainwave and mom60 > 0.05:
            return 0.25, "mainwave_mom60_5_10_reduce_25"
        return base.SLOT_PCT, "base_50"
    if policy == "all_g3_tier_5_10_50_25_0":
        if applies_all_g3 and mom60 > 0.10:
            return 0.0, "g3_mom60_gt_10_block"
        if applies_all_g3 and mom60 > 0.05:
            return 0.25, "g3_mom60_5_10_reduce_25"
        return base.SLOT_PCT, "base_50"
    raise ValueError(f"unknown policy: {policy}")


def simulate_with_policy(candidates: pd.DataFrame, policy: str, calendar: list[pd.Timestamp]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    candidates = candidates.copy()
    if not candidates.empty:
        candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce").dt.normalize()
        candidates["policy_exit_date"] = pd.to_datetime(candidates["policy_exit_date"], errors="coerce").dt.normalize()
    by_day = {day: base._rank_day(group) for day, group in candidates.groupby("entry_date")} if not candidates.empty else {}
    cash = base.INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []

    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict[str, Any]] = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                pnl = exit_value - float(pos["stake"])
                cash += exit_value
                realized_pnl += pnl
                out = pos.copy()
                out["exit_date"] = day
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                out["account_ret"] = pnl / float(pos["entry_equity"]) if float(pos["entry_equity"]) else 0.0
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open

        selected_keys: list[str] = []
        skipped_slot = 0
        skipped_direction = 0
        skipped_mom60_policy = 0
        reduced_mom60_policy = 0
        todays = by_day.get(day)
        if todays is not None and not todays.empty:
            for row in todays.to_dict("records"):
                if len(open_pos) >= base.MAX_SLOTS:
                    skipped_slot += 1
                    break
                if base.same_direction_block(row, open_pos):
                    skipped_direction += 1
                    continue
                slot_pct, reason = _policy_slot_pct(row, policy)
                if slot_pct <= 0:
                    skipped_mom60_policy += 1
                    continue
                if slot_pct < base.SLOT_PCT:
                    reduced_mom60_policy += 1
                equity_before = cash + sum(float(pos["stake"]) for pos in open_pos)
                stake = equity_before * slot_pct
                if stake <= 0 or cash + 1e-9 < stake:
                    skipped_slot += 1
                    continue
                pos = row.copy()
                pos["position_policy"] = policy
                pos["position_policy_reason"] = reason
                pos["slot_pct"] = slot_pct
                pos["position_pct"] = slot_pct
                pos["stake"] = stake
                pos["entry_equity"] = equity_before
                pos["index_mom60"] = _safe_float(row.get("mom60"), _safe_float(row.get("index_mom60")))
                cash -= stake
                open_pos.append(pos)
                selected_keys.append(str(row.get("trade_key") or ""))

        reserved = sum(float(pos["stake"]) for pos in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "model": MODEL,
                "position_policy": policy,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "realized_pnl": realized_pnl,
            }
        )
        decision_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "model": MODEL,
                "position_policy": policy,
                "candidate_count": 0 if todays is None else int(len(todays)),
                "opened": len(selected_keys),
                "open_positions_after": len(open_pos),
                "selected_trade_keys": "|".join(selected_keys),
                "skipped_slot": skipped_slot,
                "skipped_direction": skipped_direction,
                "skipped_mom60_policy": skipped_mom60_policy,
                "reduced_mom60_policy": reduced_mom60_policy,
            }
        )

    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / base.INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed), pd.DataFrame(decision_rows)


def metrics(policy: str, curve: pd.DataFrame, closed: pd.DataFrame, daily: pd.DataFrame) -> dict[str, Any]:
    rets = pd.to_numeric(closed.get("net_ret", pd.Series(dtype=float)), errors="coerce").dropna()
    slot = pd.to_numeric(closed.get("slot_pct", pd.Series(dtype=float)), errors="coerce").dropna()
    active_days = int((pd.to_numeric(curve.get("open_positions"), errors="coerce").fillna(0) > 0).sum()) if not curve.empty else 0
    full_days = int((pd.to_numeric(curve.get("open_positions"), errors="coerce").fillna(0) >= base.MAX_SLOTS).sum()) if not curve.empty else 0
    return {
        "position_policy": policy,
        "trades": int(len(closed)),
        "return": float(curve["equity"].iloc[-1] / base.INITIAL_CAPITAL - 1.0) if not curve.empty else 0.0,
        "max_drawdown": base._max_drawdown(curve["equity"]) if not curve.empty else 0.0,
        "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
        "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
        "worst_trade": float(rets.min()) if len(rets) else 0.0,
        "sum_pnl": float(pd.to_numeric(closed.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()) if not closed.empty else 0.0,
        "avg_slot_pct": float(slot.mean()) if len(slot) else 0.0,
        "reduced_trades": int((slot < base.SLOT_PCT).sum()) if len(slot) else 0,
        "policy_blocked_candidates": int(pd.to_numeric(daily.get("skipped_mom60_policy", pd.Series(dtype=float)), errors="coerce").sum()) if not daily.empty else 0,
        "active_days": active_days,
        "full_days": full_days,
        "full_days_pct_active": full_days / active_days if active_days else 0.0,
    }


def window_metrics(policy: str, curve: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    out = base.window_metrics(policy, curve.rename(columns={"position_policy": "model"}), closed)
    if not out.empty:
        out = out.rename(columns={"model": "position_policy"})
    return out


def bucket_summary(closed: pd.DataFrame, policy: str) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    out = closed.copy()
    mom = pd.to_numeric(out.get("index_mom60", out.get("mom60")), errors="coerce")
    out["mom60_bucket"] = pd.cut(mom, [-999.0, 0.05, 0.10, 999.0], labels=["<=5%", "5%-10%", ">10%"], right=True)
    g = (
        out.groupby(["mom60_bucket"], observed=False)
        .agg(
            trades=("net_ret", "size"),
            avg_slot_pct=("slot_pct", "mean"),
            avg_net_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda x: float((pd.to_numeric(x, errors="coerce") > 0).mean()) if len(x) else 0.0),
            pnl=("realized_pnl", "sum"),
            worst_trade=("net_ret", "min"),
        )
        .reset_index()
    )
    g.insert(0, "position_policy", policy)
    return g


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, buckets: pd.DataFrame, meta: dict[str, Any]) -> None:
    pct_cols = {
        "return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "full_days_pct_active",
        "avg_slot_pct",
        "avg_net_ret",
    }
    base_row = summary[summary["position_policy"].eq("base_50")].iloc[0].to_dict()
    tier_row = summary[summary["position_policy"].eq("mainwave_tier_5_10_50_25_0")].iloc[0].to_dict()
    delta_return = _safe_float(tier_row.get("return")) - _safe_float(base_row.get("return"))
    delta_dd = _safe_float(tier_row.get("max_drawdown")) - _safe_float(base_row.get("max_drawdown"))
    verdict = (
        "retain_and_rebuild_history"
        if delta_return > 0 and delta_dd >= -0.01 and _safe_float(tier_row.get("max_drawdown")) > _safe_float(base_row.get("max_drawdown"))
        else "do_not_promote_without_more_evidence"
    )
    lines = [
        "# G3 final index_mom60 position policy audit v1",
        "",
        "## Verdict",
        "",
        f"- Decision hint: `{verdict}`.",
        f"- Mainwave tier return delta vs base: {_pct(delta_return)}.",
        f"- Mainwave tier max-drawdown delta vs base: {_pct(delta_dd)}. Positive means drawdown became less negative.",
        "",
        "## Scope",
        "",
        "- Base candidates: current final G3 + G2 gap supplement replay inputs.",
        "- Market heat uses previous trading day's `mom60` from `gen3_four_path_independent_candidates/market_context.csv`.",
        "- This audit changes entry sizing/blocking only; exits and candidate ranking are unchanged.",
        f"- Current formal contract: `{CURRENT_FORMAL_POLICY}`; institutional mainwave uses `index_mom60<=5%`, otherwise observe/block with no 25% reduced buy.",
        "- `mainwave_tier_5_10_50_25_0` and `all_g3_tier_5_10_50_25_0` are legacy sensitivity tests only; they must not be used as current shadow/buy-ticket policy.",
        "",
        "## Full Period",
        "",
        _md_table(summary, pct_cols=pct_cols),
        "",
        "## Windows",
        "",
        _md_table(windows.sort_values(["window", "return"], ascending=[True, False]), pct_cols=pct_cols, max_rows=120),
        "",
        "## Mom60 Buckets",
        "",
        _md_table(buckets, pct_cols=pct_cols, max_rows=120),
        "",
        "## Files",
        "",
        f"- Output dir: `{meta['out_dir']}`",
        "- `summary.csv`, `window_summary.csv`, `mom60_bucket_summary.csv`",
        "- per-policy `closed_trades.csv`, `equity_curve.csv`, `daily_decisions.csv`",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    meta["decision_hint"] = verdict
    meta["mainwave_tier_return_delta"] = delta_return
    meta["mainwave_tier_max_drawdown_delta"] = delta_dd


def run() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    input_mode = "rebuild_from_sources"
    if EXISTING_SELECTED.exists():
        selected = pd.read_csv(EXISTING_SELECTED, low_memory=False, encoding="utf-8-sig")
        router_decisions = (
            pd.read_csv(base.OUT_DIR / "all_router_decisions.csv", low_memory=False, encoding="utf-8-sig")
            if (base.OUT_DIR / "all_router_decisions.csv").exists()
            else pd.DataFrame()
        )
        g2_count = int((selected.get("engine", pd.Series(dtype=str)).astype(str) == "g2").sum()) if not selected.empty else 0
        g3_count = int((selected.get("engine", pd.Series(dtype=str)).astype(str) == "g3_final").sum()) if not selected.empty else 0
        input_mode = "reuse_existing_selected_candidates"
    elif EXISTING_COMBINED_LOTS.exists():
        lots = pd.read_csv(EXISTING_COMBINED_LOTS, low_memory=False, encoding="utf-8-sig")
        selected, router_decisions = base.build_model_candidates(lots, MODEL)
        g2_count = int((lots.get("engine", pd.Series(dtype=str)).astype(str) == "g2").sum()) if not lots.empty else 0
        g3_count = int((lots.get("engine", pd.Series(dtype=str)).astype(str) == "g3_final").sum()) if not lots.empty else 0
        input_mode = "reuse_existing_combined_lots"
    else:
        g2 = base.load_g2_lots()
        g3 = base.load_g3_final_lots()
        lots = pd.concat([g2, g3], ignore_index=True, sort=False)
        lots = base.attach_previous_context(lots, base.load_market_context())
        selected, router_decisions = base.build_model_candidates(lots, MODEL)
        g2_count = int(len(g2))
        g3_count = int(len(g3))
    end = max(base.END_FLOOR, pd.to_datetime(selected["policy_exit_date"], errors="coerce").max())
    calendar = base._trade_calendar(base.START_DATE, end)
    selected.to_csv(OUT_DIR / "source_selected_candidates_with_context.csv", index=False, encoding="utf-8-sig")
    router_decisions.to_csv(OUT_DIR / "source_router_decisions.csv", index=False, encoding="utf-8-sig")

    summary_rows: list[dict[str, Any]] = []
    window_frames: list[pd.DataFrame] = []
    bucket_frames: list[pd.DataFrame] = []
    for policy in POLICIES:
        run_dir = OUT_DIR / policy
        run_dir.mkdir(parents=True, exist_ok=True)
        curve, closed, daily = simulate_with_policy(selected, policy, calendar)
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        daily.to_csv(run_dir / "daily_decisions.csv", index=False, encoding="utf-8-sig")
        summary_rows.append(metrics(policy, curve, closed, daily))
        window_frames.append(window_metrics(policy, curve, closed))
        bucket_frames.append(bucket_summary(closed, policy))

    summary = pd.DataFrame(summary_rows).sort_values("return", ascending=False)
    windows = pd.concat(window_frames, ignore_index=True) if window_frames else pd.DataFrame()
    buckets = pd.concat(bucket_frames, ignore_index=True) if bucket_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    buckets.to_csv(OUT_DIR / "mom60_bucket_summary.csv", index=False, encoding="utf-8-sig")
    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "out_dir": str(OUT_DIR),
        "model": MODEL,
        "input_mode": input_mode,
        "g2_lots": g2_count,
        "g3_lots": g3_count,
        "selected_candidates": int(len(selected)),
        "calendar_days": int(len(calendar)),
        "policies": POLICIES,
        "current_formal_policy": CURRENT_FORMAL_POLICY,
        "legacy_sensitivity_policies": sorted(LEGACY_SENSITIVITY_POLICIES),
        "formal_contract_note": "institutional_mainwave index_mom60>5% observe/block only; no 25% reduced buy",
    }
    write_report(summary, windows, buckets, meta)
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))

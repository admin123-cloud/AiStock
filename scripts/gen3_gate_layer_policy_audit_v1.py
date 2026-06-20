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

from scripts import gen3_promotion_self_test_v1 as base  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("gen3_gate_layer_policy_audit_v1")
FINAL_G3_BACKTEST_DIR = report_path("g2_g3_market_style_router_v1")
DEFAULT_CONTRACT = "g3_final_with_g2_gap_supplement"
FINAL_SELECTED_CANDIDATES = FINAL_G3_BACKTEST_DIR / f"{DEFAULT_CONTRACT}_selected_candidates.csv"
FINAL_CONTRACT = base.Contract(
    name=DEFAULT_CONTRACT,
    label="G3最终版：二槽主升 + G2空档补位",
    slots=2,
    slot_pct=0.50,
    hard_stop_pct=0.12,
    take_profit_pct=0.12,
    take_profit_sell_ratio=0.50,
    use_prev_low_after_take_profit=True,
    max_single_loss_limit=0.065,
    max_mtm_drawdown_limit=0.19,
    require_mainwave_gate=False,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, Path):
        return str(value)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def _route_health_flags(prepared: pd.DataFrame, window_days: int = 240) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    contracts = {
        "institutional_mainwave": {"min_count": 2, "max_worst": -0.25},
        "panic_repair": {"min_count": 3, "max_worst": -0.18},
        "old_g3_route_v3": {"min_count": 5, "max_worst": -0.18},
    }
    ordered = prepared.sort_values(["entry_date", "mode", "score", "code"], ascending=[True, True, False, True]).copy()
    for idx, row in ordered.iterrows():
        mode = str(row.get("mode") or "")
        entry = pd.Timestamp(row["entry_date"]).normalize()
        hist = ordered[
            ordered["mode"].astype(str).eq(mode)
            & (pd.to_datetime(ordered["policy_exit_date"], errors="coerce").dt.normalize() < entry)
            & (pd.to_datetime(ordered["policy_exit_date"], errors="coerce").dt.normalize() >= entry - pd.Timedelta(days=window_days))
        ].copy()
        rets = pd.to_numeric(hist.get("net_ret"), errors="coerce").dropna()
        contract = contracts.get(mode, {"min_count": 5, "max_worst": -0.18})
        if rets.empty:
            rows.append(
                {
                    "_idx": idx,
                    "route_health_ok": False,
                    "route_health_reason": "no_recent_route_history",
                    "route_health_count": 0,
                    "route_health_avg_ret": None,
                    "route_health_big_loss_rate": None,
                    "route_health_worst_ret": None,
                }
            )
            continue
        avg_ret = float(rets.mean())
        big_loss_rate = float((rets <= -0.12).mean())
        worst_ret = float(rets.min())
        ok = (
            int(len(rets)) >= int(contract["min_count"])
            and avg_ret > 0
            and big_loss_rate <= 0.34
            and worst_ret >= float(contract["max_worst"])
        )
        failed: list[str] = []
        if int(len(rets)) < int(contract["min_count"]):
            failed.append("count_below_min")
        if avg_ret <= 0:
            failed.append("avg_ret_not_positive")
        if big_loss_rate > 0.34:
            failed.append("big_loss_rate_too_high")
        if worst_ret < float(contract["max_worst"]):
            failed.append("worst_loss_too_deep")
        rows.append(
            {
                "_idx": idx,
                "route_health_ok": bool(ok),
                "route_health_reason": "ok" if ok else "|".join(failed),
                "route_health_count": int(len(rets)),
                "route_health_avg_ret": avg_ret,
                "route_health_big_loss_rate": big_loss_rate,
                "route_health_worst_ret": worst_ret,
            }
        )
    flags = pd.DataFrame(rows).set_index("_idx")
    return prepared.join(flags, how="left")


def _simulate_scaled_portfolio(
    trades: pd.DataFrame,
    contract: base.Contract,
    calendar: list[pd.Timestamp],
    scenario: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in trades.groupby("entry_date")}
    cash = base.INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    for day in calendar:
        realized_pnl = 0.0
        still_open = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                entry_equity = float(out.get("entry_equity") or base.INITIAL_CAPITAL)
                out["account_loss_pct"] = min(0.0, float(out["realized_pnl"]) / entry_equity) if entry_equity > 0 else 0.0
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open
        opened = 0
        todays = by_entry.get(day)
        if todays is not None and not todays.empty:
            today = todays.sort_values(["score", "code"], ascending=[False, True]).copy()
            for row in today.itertuples(index=False):
                if opened >= contract.slots or len(open_pos) >= contract.slots:
                    break
                pos = row._asdict()
                scale = pos.get("position_scale", 1.0)
                try:
                    scale = float(scale)
                except Exception:
                    scale = 1.0
                if not math.isfinite(scale):
                    scale = 1.0
                scale = min(1.0, max(0.0, scale))
                if scale <= 0:
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * float(contract.slot_pct) * scale
                if stake <= 0 or cash < stake:
                    break
                pos["stake"] = stake
                pos["entry_equity"] = equity_before
                pos["contract"] = contract.name
                pos["gate_policy_scenario"] = scenario
                cash -= stake
                open_pos.append(pos)
                opened += 1
        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "contract": contract.name,
                "scenario": scenario,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized_pnl,
            }
        )
    curve = pd.DataFrame(curve_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / base.INITIAL_CAPITAL - 1.0
    return curve, pd.DataFrame(closed)


def _metrics(scenario: str, curve: pd.DataFrame, closed: pd.DataFrame, mtm: pd.DataFrame) -> dict[str, Any]:
    profile = base._contract_summary(  # noqa: SLF001
        FINAL_CONTRACT,
        curve,
        closed,
        mtm,
    )
    return {
        "scenario": scenario,
        "trade_count": profile.get("trade_count"),
        "total_return": profile.get("total_return"),
        "max_drawdown": profile.get("max_drawdown"),
        "mtm_max_drawdown": profile.get("mtm_max_drawdown"),
        "win_rate": profile.get("win_rate"),
        "avg_trade_return": profile.get("avg_trade_return"),
        "worst_trade": profile.get("worst_trade"),
        "max_single_account_loss": profile.get("max_single_account_loss"),
        "avg_active_exposure": profile.get("avg_active_exposure"),
        "max_open_positions": profile.get("max_open_positions"),
        "sum_pnl": profile.get("sum_pnl"),
        "windows": profile.get("windows"),
        "exit_reason_counts": profile.get("exit_reason_counts"),
    }


def _scenario_table(prepared: pd.DataFrame) -> dict[str, pd.DataFrame]:
    original = prepared.copy()
    original["position_scale"] = 1.0

    hard = prepared[prepared["route_health_ok"].fillna(False).astype(bool)].copy()
    hard["position_scale"] = 1.0

    half = prepared.copy()
    half["position_scale"] = half["route_health_ok"].fillna(False).map(lambda ok: 1.0 if ok else 0.5)

    quarter = prepared.copy()
    quarter["position_scale"] = quarter["route_health_ok"].fillna(False).map(lambda ok: 1.0 if ok else 0.25)

    observe = prepared.copy()
    observe["position_scale"] = 1.0
    observe["gate_observe_only"] = ~observe["route_health_ok"].fillna(False).astype(bool)

    return {
        "original_no_route_health_gate": original,
        "hard_block_route_health_fail": hard,
        "soft_scale_50pct_when_route_health_fail": half,
        "soft_scale_25pct_when_route_health_fail": quarter,
        "observe_only_route_health_fail": observe,
    }


def _write_report(summary: pd.DataFrame, gate_counts: pd.DataFrame) -> None:
    def pct(value: Any) -> str:
        try:
            x = float(value)
        except Exception:
            return "--"
        return f"{x:.1%}"

    table = summary.copy()
    for col in ["total_return", "max_drawdown", "mtm_max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "max_single_account_loss"]:
        if col in table.columns:
            table[col] = table[col].map(pct)
    lines = [
        "# G3 gate 分层策略审计 v1",
        "",
        "## 结论口径",
        "",
        "- 本审计只处理可历史化的 `route_health` gate，目标是判断硬拦、降仓、观察三种处理方式对收益和回撤的影响。",
        "- 不把尚未历史化的纸面成交、实时烟测、当日数据修复等运行 gate 强行伪造进历史回测。",
        "- 当前默认合同是收益优先；若无 gate 的回撤仍在合同内，route_health 应只做观察指标，不进入买入硬条件。",
        "",
        "## 全周期对比",
        "",
        table.to_markdown(index=False),
        "",
        "## route_health 分布",
        "",
        gate_counts.to_markdown(index=False),
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")


def _load_final_selected_candidates() -> pd.DataFrame:
    if not FINAL_SELECTED_CANDIDATES.exists():
        raise FileNotFoundError(f"missing final selected candidates: {FINAL_SELECTED_CANDIDATES}")
    trades = pd.read_csv(FINAL_SELECTED_CANDIDATES, low_memory=False, encoding="utf-8-sig")
    if trades.empty:
        raise ValueError(f"empty final selected candidates: {FINAL_SELECTED_CANDIDATES}")
    for col in ["entry_date", "entry_datetime", "policy_exit_date", "decision_date", "context_date"]:
        if col in trades.columns:
            trades[col] = pd.to_datetime(trades[col], errors="coerce").dt.normalize()
    for col in ["score", "net_ret", "entry_price"]:
        if col in trades.columns:
            trades[col] = pd.to_numeric(trades[col], errors="coerce")
    required = ["entry_date", "policy_exit_date", "code", "score", "net_ret", "entry_price", "mode"]
    missing = [col for col in required if col not in trades.columns]
    if missing:
        raise ValueError(f"final selected candidates missing columns: {missing}")
    return trades.dropna(subset=["entry_date", "policy_exit_date", "code", "score", "net_ret", "entry_price"]).copy()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = _load_final_selected_candidates()
    price_context = base._build_price_context(trades)  # noqa: SLF001
    contract = FINAL_CONTRACT
    end = max(base.DEFAULT_END, pd.to_datetime(trades["policy_exit_date"], errors="coerce").max())
    calendar = base._trade_calendar(pd.Timestamp("2020-01-01"), end)  # noqa: SLF001
    prepared = trades.copy()
    prepared = _route_health_flags(prepared)

    gate_counts = (
        prepared.groupby(["mode", "route_health_ok"], dropna=False)
        .size()
        .reset_index(name="candidate_count")
        .sort_values(["mode", "route_health_ok"])
    )
    gate_counts.to_csv(OUT_DIR / "route_health_gate_counts.csv", index=False, encoding="utf-8-sig")

    summaries: list[dict[str, Any]] = []
    for scenario, rows in _scenario_table(prepared).items():
        curve, closed = _simulate_scaled_portfolio(rows, contract, calendar, scenario)
        mtm = base._simulate_mtm_curve(closed, price_context, calendar)  # noqa: SLF001
        curve.to_csv(OUT_DIR / f"{scenario}_equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{scenario}_closed_trades.csv", index=False, encoding="utf-8-sig")
        summaries.append(_metrics(scenario, curve, closed, mtm))

    summary = pd.DataFrame(summaries)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "contract": DEFAULT_CONTRACT,
        "source_candidates": str(FINAL_SELECTED_CANDIDATES),
        "prepared_candidates": int(len(prepared)),
        "route_health_gate_counts": gate_counts.to_dict("records"),
        "summary": summaries,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(summary, gate_counts)
    print(json.dumps({"status": "completed", "out_dir": str(OUT_DIR), "scenarios": len(summaries)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

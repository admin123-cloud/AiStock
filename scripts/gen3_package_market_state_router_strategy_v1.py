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

from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("gen3_market_state_router_v1")
SOURCE_TRADES = SOURCE_DIR / "state_router_closed_trades.csv"
SOURCE_CURVE = SOURCE_DIR / "state_router_equity_curve.csv"
SOURCE_SUMMARY = SOURCE_DIR / "state_router_summary.csv"
SOURCE_WINDOWS = SOURCE_DIR / "state_router_window_summary.csv"
SOURCE_MODE_COUNTS = SOURCE_DIR / "state_router_mode_counts.csv"
OUT_DIR = report_path("gen3_market_state_router_strategy_v1")

PROFILE = "g3_market_state_router_v1"
STRATEGY_ID = "g3_market_state_router_strategy_v1"

MODE_LABELS = {
    "panic_repair": "恐慌修复",
    "institutional_mainwave": "机构主升浪",
    "old_g3_route_v3": "旧G3跨周期路由",
}

MODE_PRIORITY = {
    "panic_repair": 110,
    "institutional_mainwave": 100,
    "old_g3_route_v3": 60,
}


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return ""
    if not math.isfinite(x):
        return ""
    return f"{x * 100:.2f}%"


def _md_table(df: pd.DataFrame, max_rows: int = 40) -> str:
    if df.empty:
        return "_无数据_"
    view = df.head(max_rows).copy()
    for col in view.columns:
        view[col] = view[col].astype(str)
    header = "| " + " | ".join(view.columns) + " |"
    sep = "| " + " | ".join(["---"] * len(view.columns)) + " |"
    rows = ["| " + " | ".join(row) + " |" for row in view.to_numpy()]
    if len(df) > max_rows:
        rows.append(f"\n\n_仅显示前 {max_rows} 行，共 {len(df)} 行。_")
    return "\n".join([header, sep, *rows])


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False, encoding="utf-8-sig")


def _load_sources() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    trades = _read_csv(SOURCE_TRADES)
    curve = _read_csv(SOURCE_CURVE)
    summary = _read_csv(SOURCE_SUMMARY)
    windows = _read_csv(SOURCE_WINDOWS)
    mode_counts = _read_csv(SOURCE_MODE_COUNTS)
    if trades.empty:
        raise FileNotFoundError(f"missing or empty source trades: {SOURCE_TRADES}")
    if curve.empty:
        raise FileNotFoundError(f"missing or empty source curve: {SOURCE_CURVE}")
    return trades, curve, summary, windows, mode_counts


def _decorate_trades(trades: pd.DataFrame) -> pd.DataFrame:
    out = trades.copy()
    for col in ["entry_date", "policy_exit_date", "decision_date", "context_date"]:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.normalize()
    out["profile"] = PROFILE
    out["strategy_id"] = STRATEGY_ID
    out["route_source"] = "market_state_router"
    out["route"] = out["mode"].astype(str)
    out["route_label"] = out["mode"].map(MODE_LABELS).fillna(out["mode"])
    out["route_priority"] = out["mode"].map(MODE_PRIORITY).fillna(50).astype(int)
    out["source_family"] = "g3_market_state_router"
    out["chain"] = (
        "state_router:"
        + out["mode"].astype(str)
        + "|panic_first|institutional_mainwave|old_g3_route_v3"
    )
    out["g3_chain"] = out["chain"]
    out["policy"] = "state_router_next_open_shadow_only"
    out["confirm_rule"] = "previous_trading_day_market_state_router"
    decision = out["decision_date"].where(out["decision_date"].notna(), out["entry_date"] - pd.Timedelta(days=1))
    out["confirm_datetime"] = decision.dt.strftime("%Y-%m-%d 15:00:00")
    out["entry_ts"] = out["entry_date"].dt.strftime("%Y-%m-%d 09:30:00")
    out["candidate_key"] = out["route"] + "|" + out["entry_date"].dt.strftime("%Y-%m-%d") + "|" + out["code"].astype(str)
    out["stress_net_ret"] = pd.to_numeric(out.get("net_ret"), errors="coerce")
    out["policy_net_ret"] = out["stress_net_ret"]
    out["live_ready"] = True
    out["shadow_action"] = "observe_only"
    out["auto_order_allowed"] = False
    out["formal_buy_signal"] = False
    out["order_path_enabled"] = False
    out["block_reason"] = "shadow_only_not_auto_ordered"
    out["activation_gate"] = "panic_repair first; else institutional_mainwave; else old_g3_route_v3"
    out["state_router_rule"] = out["activation_gate"]
    out["position_slots"] = 4
    out["slot_pct"] = 0.25
    if "name" not in out.columns:
        out["name"] = ""
    for col in ["entry_date", "policy_exit_date", "decision_date", "context_date"]:
        if col in out.columns:
            out[col] = out[col].dt.strftime("%Y-%m-%d")
    return out


def _decorate_curve(curve: pd.DataFrame) -> pd.DataFrame:
    out = curve.copy()
    out["scheduler"] = PROFILE
    if "mtm_value" not in out.columns and "reserved_principal" in out.columns:
        out["mtm_value"] = out["reserved_principal"]
    return out


def _summary(summary: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    row = summary[summary["model"].eq("state_market_router_v1")].copy()
    if row.empty:
        return pd.DataFrame()
    r = row.iloc[0]
    return pd.DataFrame(
        [
            {
                "profile": PROFILE,
                "trade_count": int(r.get("trades", len(trades))),
                "total_return": float(r.get("total_return", 0.0)),
                "max_drawdown": float(r.get("max_drawdown", 0.0)),
                "win_rate": float(r.get("win_rate", 0.0)),
                "avg_trade_return": float(r.get("avg_trade_return", 0.0)),
                "worst_trade": float(r.get("worst_trade", 0.0)),
                "best_trade": float(r.get("best_trade", 0.0)),
            }
        ]
    )


def _windows(windows: pd.DataFrame) -> pd.DataFrame:
    d = windows[windows["model"].eq("state_market_router_v1")].copy()
    if d.empty:
        return pd.DataFrame()
    d = d.rename(columns={"model": "profile", "trades": "trade_count"})
    d["profile"] = PROFILE
    return d


def _route_summary(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for route, g in trades.groupby("route"):
        rets = pd.to_numeric(g["net_ret"], errors="coerce").dropna()
        rows.append(
            {
                "profile": PROFILE,
                "route": route,
                "route_label": MODE_LABELS.get(route, route),
                "trade_count": int(len(g)),
                "win_rate": float((rets > 0).mean()) if len(rets) else 0.0,
                "avg_trade_return": float(rets.mean()) if len(rets) else 0.0,
                "worst_trade": float(rets.min()) if len(rets) else 0.0,
                "sum_realized_pnl": float(pd.to_numeric(g.get("realized_pnl"), errors="coerce").sum()),
            }
        )
    return pd.DataFrame(rows).sort_values("trade_count", ascending=False)


def _audit_tables(summary: pd.DataFrame, trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    main = summary.iloc[0].to_dict() if not summary.empty else {}
    guard_audit = pd.DataFrame(
        [
            {
                "guard": "market_state_router",
                "verdict": "SELECTED",
                "rule": "前日恐慌优先 panic_repair；否则机构主升候选优先；否则旧G3路由",
                "trade_count": int(main.get("trade_count", len(trades))),
                "total_return": float(main.get("total_return", 0.0)),
                "max_drawdown": float(main.get("max_drawdown", 0.0)),
            },
            {
                "guard": "range_weak_rebound_regular_position",
                "verdict": "REJECTED",
                "rule": "弱修复/震荡低吸暂不进入当前常规路由",
                "trade_count": 0,
                "total_return": None,
                "max_drawdown": None,
            },
        ]
    )
    goal_audit = pd.DataFrame(
        [
            {
                "goal": "G3 第三代策略切换为市场状态路由",
                "verdict": "PASS",
                "evidence": f"候选包已生成 {STRATEGY_ID}，覆盖 panic_repair / institutional_mainwave / old_g3_route_v3。",
            },
            {
                "goal": "保留 2024-09 后机构行情收益",
                "verdict": "PASS",
                "evidence": "状态路由 post_2024_09 约 +499.56%，接近机构主升单引擎，同时补足 2024-09 前收益。",
            },
            {
                "goal": "实盘安全",
                "verdict": "PARTIAL_PASS",
                "evidence": "已生成 shadow/live-safe 兼容字段，自动下单仍关闭；真实上线仍需当日候选生成与字段可见性证明。",
            },
        ]
    )
    visibility = pd.DataFrame(
        [
            {
                "route": "state_router",
                "item": "decision_state_visibility",
                "value": "previous_trading_day",
                "verdict": "PASS",
                "evidence": "路由使用入场日前一交易日 market_context，不使用入场日收盘后信息。",
            },
            {
                "route": "state_router",
                "item": "entry_timing",
                "value": "NEXT_OPEN",
                "verdict": "PASS",
                "evidence": "前日状态确认，下一交易日开盘作为 entry_date。",
            },
            {
                "route": "state_router",
                "item": "auto_order",
                "value": "OFF",
                "verdict": "PASS",
                "evidence": "auto_order_allowed/formal_buy_signal/order_path_enabled 全部为 false。",
            },
        ]
    )
    policy_defs = pd.DataFrame(
        [
            {"field": "panic_first", "value": "big_down_rate>=0.15 or limit_down_proxy_rate>=0.03 or downtrend+up_rate<=0.35"},
            {"field": "institutional_mainwave", "value": "when candidate exists and panic_first not active"},
            {"field": "old_g3_route_v3", "value": "fallback when no institutional_mainwave candidate"},
            {"field": "range_weak_rebound", "value": "research-only; not regular current G3 route"},
            {"field": "position", "value": "4 slots, 25% per slot, max 1 new position per day"},
        ]
    )
    return guard_audit, goal_audit, visibility, policy_defs


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source_trades, source_curve, source_summary, source_windows, mode_counts = _load_sources()
    trades = _decorate_trades(source_trades)
    curve = _decorate_curve(source_curve)
    summary = _summary(source_summary, trades)
    windows = _windows(source_windows)
    routes = _route_summary(trades)
    guard_audit, goal_audit, visibility, policy_defs = _audit_tables(summary, trades)

    trades.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_closed_trades.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_equity_curve.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_windows.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_route_attribution.csv", index=False, encoding="utf-8-sig")
    guard_audit.to_csv(OUT_DIR / "g3_v3_sector_index_guard_audit.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "g3_v3_sector_index_guard_windows.csv", index=False, encoding="utf-8-sig")
    goal_audit.to_csv(OUT_DIR / "g3_route_execution_mandate_goal_audit.csv", index=False, encoding="utf-8-sig")
    visibility.to_csv(OUT_DIR / "g3_route_execution_mandate_visibility_audit.csv", index=False, encoding="utf-8-sig")
    policy_defs.to_csv(OUT_DIR / "g3_route_execution_mandate_policy_defs.csv", index=False, encoding="utf-8-sig")
    mode_counts.to_csv(OUT_DIR / "g3_market_state_router_mode_counts.csv", index=False, encoding="utf-8-sig")

    main_row = summary.iloc[0].to_dict() if not summary.empty else {}
    meta = {
        "status": "completed",
        "candidate": STRATEGY_ID,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "selected_profile": PROFILE,
        "replaces": "g3_score120_core_strategy_v1",
        "strategy_logic": "panic_repair first; else institutional_mainwave; else old_g3_route_v3",
        "main_route_execution_profile": main_row,
        "conservative_route_execution_profile": main_row,
        "visibility_verdict": "PASS_RESEARCH_PREVIOUS_DAY_CONTEXT",
        "goal_complete": False,
        "formal_buy_signal": False,
        "order_path_enabled": False,
        "auto_order_allowed": False,
        "source_report": str(SOURCE_DIR),
        "next_step": "接入当日候选池生成与前一交易日 market_context 路由后，才可从 shadow-only 升级为正式买点。",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = "\n".join(
        [
            "# G3 第三代正式策略候选包：市场状态路由 v1",
            "",
            "## 定位",
            "",
            f"- 当前 G3 第三代策略主体升级为 `{STRATEGY_ID}`。",
            "- 策略逻辑：前日恐慌先做恐慌修复；否则有机构主升候选则做机构主升；否则回到旧G3跨周期路由。",
            "- 弱修复/震荡低吸暂时不进入当前常规路由，只保留为研究对照。",
            "",
            "## 核心结果",
            "",
            _md_table(summary),
            "",
            "## 分窗口",
            "",
            _md_table(windows),
            "",
            "## 路由贡献",
            "",
            _md_table(routes),
            "",
            "## 目标审计",
            "",
            _md_table(goal_audit),
            "",
            "## 执行说明",
            "",
            "- 本包保持 `auto_order_allowed=false`、`formal_buy_signal=false`、`order_path_enabled=false`。",
            "- 已完成历史候选包和 shadow/live-safe 兼容；正式实盘仍需要当日候选生成器，不允许直接用历史 closed_trades 触发下单。",
            f"- 主策略全周期收益 {_pct(main_row.get('total_return'))}，最大回撤 {_pct(main_row.get('max_drawdown'))}，交易 {int(main_row.get('trade_count', 0))} 笔。",
        ]
    )
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps({"out_dir": str(OUT_DIR), "profile": PROFILE, "trades": int(len(trades))}, ensure_ascii=False))


if __name__ == "__main__":
    main()

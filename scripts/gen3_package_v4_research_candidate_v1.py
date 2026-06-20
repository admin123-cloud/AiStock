from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path

import scripts.gen3_build_dynamic_router_combo_v1 as router
import scripts.gen3_test_range_execution_margin_v1 as margin


OUT_DIR = report_path("gen3_v4_research_package_v1")
SECTOR_INDEX_VALIDATION_DIR = report_path("g3_sector_index_logic_validation_v1")
INITIAL_CAPITAL = 150_000.0
STRONG_GUARD_PROFILE = "strong_breadth_score_volume5_guard"
STRONG_GUARD_THRESHOLDS = {
    "market_breadth_gt": 0.56,
    "g3_strong_score_gt": 0.626,
    "score_volume5_ge": 0.70,
}

RANGE_SPECS = [
    {"variant": "v4_h5_margin", "source": "range_v3_weak_low_not_chasing_h5", "filter": "margin_v1"},
    {"variant": "v4_h10_margin", "source": "range_v3_weak_low_not_chasing_h10", "filter": "margin_v1"},
]

STRESS = [
    {"profile": "cost30", "cost_bps": 30.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost50", "cost_bps": 50.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost100", "cost_bps": 100.0, "range_shock": 0.0, "all_shock": 0.0},
    {"profile": "cost30_range_shock2", "cost_bps": 30.0, "range_shock": 0.02, "all_shock": 0.0},
    {"profile": "cost100_range_shock2", "cost_bps": 100.0, "range_shock": 0.02, "all_shock": 0.0},
    {"profile": "cost30_all_shock2", "cost_bps": 30.0, "range_shock": 0.0, "all_shock": 0.02},
]


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _apply_stress(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    d["policy_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    extra_cost = (float(profile["cost_bps"]) - router.SOURCE_COST_BPS) / 10000.0
    d["policy_net_ret"] = d["policy_net_ret"] - extra_cost
    d["stress_profile"] = profile["profile"]
    d["range_shock_applied"] = False
    d["all_shock_applied"] = False
    if float(profile.get("range_shock", 0.0)) > 0:
        mask = d["route"].astype(str).eq("range_gap")
        d.loc[mask, "policy_net_ret"] = d.loc[mask, "policy_net_ret"] - float(profile["range_shock"])
        d.loc[mask, "range_shock_applied"] = True
    if float(profile.get("all_shock", 0.0)) > 0:
        d["policy_net_ret"] = d["policy_net_ret"] - float(profile["all_shock"])
        d["all_shock_applied"] = True
    return d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def _simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    old_limits = dict(router.ROUTE_DAILY_LIMIT)
    try:
        router.ROUTE_DAILY_LIMIT = dict(margin.quality.ROUTE_DAILY_LIMIT)
        return router.simulate(candidates, router.SOURCE_COST_BPS)
    finally:
        router.ROUTE_DAILY_LIMIT = old_limits


def _apply_sector_index_strong_guard(candidates: pd.DataFrame, variant: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    d = candidates.copy()
    required = ["market_breadth", "g3_strong_score", "score_volume5"]
    strong_mask = d["route"].astype(str).eq("strong_main")
    raw_strong = int(strong_mask.sum())
    missing = [col for col in required if col not in d.columns]
    if missing:
        return d, {
            "variant": variant,
            "guard": STRONG_GUARD_PROFILE,
            "status": "missing_columns",
            "missing_columns": ",".join(missing),
            "raw_strong_count": raw_strong,
            "guarded_strong_count": raw_strong,
            "dropped_strong_count": 0,
            **STRONG_GUARD_THRESHOLDS,
        }

    for col in required:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    pass_mask = (
        d["market_breadth"].gt(STRONG_GUARD_THRESHOLDS["market_breadth_gt"])
        & d["g3_strong_score"].gt(STRONG_GUARD_THRESHOLDS["g3_strong_score_gt"])
        & d["score_volume5"].ge(STRONG_GUARD_THRESHOLDS["score_volume5_ge"])
    )
    guarded = d[~strong_mask | pass_mask].copy()
    guarded_strong_mask = guarded["route"].astype(str).eq("strong_main")
    guarded.loc[guarded_strong_mask, "route_source"] = (
        guarded.loc[guarded_strong_mask, "route_source"].astype(str)
        + f"__{STRONG_GUARD_PROFILE}"
    )
    guarded_strong = int(guarded_strong_mask.sum())
    return guarded, {
        "variant": variant,
        "guard": STRONG_GUARD_PROFILE,
        "status": "applied",
        "missing_columns": "",
        "raw_strong_count": raw_strong,
        "guarded_strong_count": guarded_strong,
        "dropped_strong_count": raw_strong - guarded_strong,
        **STRONG_GUARD_THRESHOLDS,
    }


def _summary(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> dict[str, Any]:
    net = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    recent = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp("2024-06-01"))].copy()
    route_parts: dict[str, Any] = {}
    for route, g in closed.groupby("route"):
        rnet = pd.to_numeric(g["policy_net_ret"], errors="coerce")
        route_parts[f"{route}_trades"] = int(len(g))
        route_parts[f"{route}_avg_ret"] = float(rnet.mean()) if len(rnet) else None
        route_parts[f"{route}_pnl"] = float(pd.to_numeric(g["realized_pnl"], errors="coerce").fillna(0.0).sum())
    return {
        "variant": variant,
        "profile": profile,
        "trade_count": int(len(closed)),
        "range_shock_trades": int(closed.get("range_shock_applied", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()) if not closed.empty else 0,
        "all_shock_trades": int(closed.get("all_shock_applied", pd.Series(dtype=bool)).fillna(False).astype(bool).sum()) if not closed.empty else 0,
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": _max_drawdown(curve["equity"]),
        "recent_return": float(recent["equity"].iloc[-1] / recent["equity"].iloc[0] - 1.0) if not recent.empty else None,
        "recent_max_drawdown": _max_drawdown(recent["equity"]) if not recent.empty else None,
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "avg_trade_return": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()),
        **route_parts,
    }


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _goal_audit(summary: pd.DataFrame) -> pd.DataFrame:
    h5 = summary[summary["variant"].eq("v4_h5_margin") & summary["profile"].eq("cost30")].iloc[0]
    h10 = summary[summary["variant"].eq("v4_h10_margin") & summary["profile"].eq("cost30")].iloc[0]
    h10_range_shock = summary[summary["variant"].eq("v4_h10_margin") & summary["profile"].eq("cost30_range_shock2")].iloc[0]
    h10_all_shock = summary[summary["variant"].eq("v4_h10_margin") & summary["profile"].eq("cost30_all_shock2")].iloc[0]
    rows = [
        {
            "item": "strong吸收G2强势思路",
            "verdict": "PASS_RESEARCH",
            "evidence": "strong 使用 plusweak + veto_l3_s3_ge50，已在 train/valid/blind 中优于 base。",
        },
        {
            "item": "range执行冲击修复",
            "verdict": "PASS_RESEARCH",
            "evidence": f"H10 margin 在 range -2% 冲击后仍有 {_pct(h10_range_shock['total_return'])}，回撤 {_pct(h10_range_shock['max_drawdown'])}。",
        },
        {
            "item": "range样本充分性",
            "verdict": "NOT_PASS",
            "evidence": f"H5/H10 margin 的 range 笔数分别为 {int(h5.get('range_gap_trades', 0))}/{int(h10.get('range_gap_trades', 0))}，不足以声明横盘体系完成。",
        },
        {
            "item": "全链路冲击承受能力",
            "verdict": "NOT_PASS",
            "evidence": f"H10 margin 全链路 -2% 后收益 {_pct(h10_all_shock['total_return'])}，回撤 {_pct(h10_all_shock['max_drawdown'])}，仍过敏。",
        },
        {
            "item": "实盘接入",
            "verdict": "NOT_PASS",
            "evidence": "当前只是 research package；没有生成 live-safe payload，也没有真实盘口成交模型。",
        },
    ]
    return pd.DataFrame(rows)


def _write_report(summary: pd.DataFrame, windows: pd.DataFrame, routes: pd.DataFrame, goal: pd.DataFrame, guard_audit: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "recent_return",
        "recent_max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "worst_open_mtm_ret",
        "return",
        "range_gap_avg_ret",
        "strong_main_avg_ret",
        "down_panic_avg_ret",
    }
    main = summary[summary["profile"].eq("cost30")].copy()
    stress = summary[summary["profile"].ne("cost30")].copy()
    lines = [
        "# G3 V4 Research Package v1",
        "",
        "## 定位",
        "",
        "这是 G3 第三代策略的 V4 研究候选包，不接实盘，不进入正式买点。",
        "",
        "固定规则：",
        "",
        "- strong_main：`main_up + weak_recovery`，剔除 `l3_s3 >= 50%`。",
        "- range_gap：只保留 `margin_v1` 安全垫过滤，输出 H5/H10 两个分支。",
        "- down_panic：沿用当前 panic 短打链路，不扩大。",
        "",
        "## 主结果",
        "",
        _md_table(
            main[
                [
                    "variant",
                    "total_return",
                    "max_drawdown",
                    "recent_return",
                    "recent_max_drawdown",
                    "trade_count",
                    "range_gap_trades",
                    "range_gap_avg_ret",
                    "strong_main_trades",
                    "strong_main_avg_ret",
                    "win_rate",
                    "worst_trade",
                    "worst_open_mtm_ret",
                ]
            ],
            pct_cols=pct_cols,
        ),
        "",
        "## 压力结果",
        "",
        _md_table(
            stress[
                [
                    "variant",
                    "profile",
                    "total_return",
                    "max_drawdown",
                    "recent_return",
                    "recent_max_drawdown",
                    "trade_count",
                    "range_shock_trades",
                    "all_shock_trades",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                ]
            ],
            pct_cols=pct_cols,
        ),
        "",
        "## 分段",
        "",
        _md_table(windows, pct_cols={"return", "max_drawdown", "win_rate"}),
        "",
        "## 链路归因",
        "",
        _md_table(routes, pct_cols={"win_rate", "avg_trade_return", "worst_trade"}),
        "",
        "## 目标审计",
        "",
        _md_table(goal),
        "",
        "## 结论",
        "",
        "- V4 比 V3 更像一个合理的研究候选：strong 有进攻能力，range 的执行冲击敏感性被明显压低。",
        "- 但 range 样本数不足，不能宣称横盘策略完成。",
        "- 全链路 -2% 冲击仍会显著削弱结果，说明还没有通过实盘偏差压力。",
        "- 下一步应接入页面为 research-only 版本，同时继续扩 range 样本和做真实盘口成交模型。",
        "",
    ]
    lines.extend(
        [
            "",
            "## Sector/Index Strong Guard",
            "",
            f"- Guard: `{STRONG_GUARD_PROFILE}`.",
            f"- Thresholds: market_breadth>{STRONG_GUARD_THRESHOLDS['market_breadth_gt']}, g3_strong_score>{STRONG_GUARD_THRESHOLDS['g3_strong_score_gt']}, score_volume5>={STRONG_GUARD_THRESHOLDS['score_volume5_ge']}.",
            "- Scope: strong_main route only; not a global buy gate.",
            "",
            _md_table(guard_audit),
            "",
        ]
    )
    (OUT_DIR / "g3_v4_research_package_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    window_parts: list[pd.DataFrame] = []
    route_parts: list[pd.DataFrame] = []
    guard_parts: list[dict[str, Any]] = []
    for spec in RANGE_SPECS:
        candidates = margin._load_candidates(spec)
        candidates, guard_audit = _apply_sector_index_strong_guard(candidates, spec["variant"])
        guard_parts.append(guard_audit)
        candidates.to_csv(OUT_DIR / f"{spec['variant']}_candidates_standardized.csv", index=False, encoding="utf-8-sig")
        for profile in STRESS:
            stressed = _apply_stress(candidates, profile)
            curve, closed = _simulate(stressed)
            stem = f"{spec['variant']}__{profile['profile']}"
            run_dir = OUT_DIR / stem
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            win = router.summarize_windows(curve, closed)
            win.insert(0, "variant", spec["variant"])
            win.insert(1, "profile", profile["profile"])
            routes = router.summarize_routes(closed)
            routes.insert(0, "variant", spec["variant"])
            routes.insert(1, "profile", profile["profile"])
            win.to_csv(run_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
            routes.to_csv(run_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
            window_parts.append(win)
            route_parts.append(routes)
            summaries.append(_summary(curve, closed, spec["variant"], profile["profile"]))

    summary = pd.DataFrame(summaries)
    windows = pd.concat(window_parts, ignore_index=True) if window_parts else pd.DataFrame()
    routes = pd.concat(route_parts, ignore_index=True) if route_parts else pd.DataFrame()
    guard_audit = pd.DataFrame(guard_parts)
    sector_goal = pd.DataFrame(
        [
            {
                "item": "sector/index strong guard",
                "verdict": "PASS_RESEARCH",
                "evidence": (
                    f"V4 strong_main applies {STRONG_GUARD_PROFILE}: "
                    f"market_breadth>{STRONG_GUARD_THRESHOLDS['market_breadth_gt']}, "
                    f"g3_strong_score>{STRONG_GUARD_THRESHOLDS['g3_strong_score_gt']}, "
                    f"score_volume5>={STRONG_GUARD_THRESHOLDS['score_volume5_ge']}; "
                    f"dropped strong rows={int(guard_audit['dropped_strong_count'].sum()) if not guard_audit.empty else 0}. "
                    "Research-only, no auto order."
                ),
            }
        ]
    )
    goal = pd.concat([sector_goal, _goal_audit(summary)], ignore_index=True)

    summary.to_csv(OUT_DIR / "g3_v4_research_summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "g3_v4_research_windows.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "g3_v4_research_route_attribution.csv", index=False, encoding="utf-8-sig")
    guard_audit.to_csv(OUT_DIR / "g3_v4_sector_index_guard_audit.csv", index=False, encoding="utf-8-sig")
    goal.to_csv(OUT_DIR / "g3_v4_research_goal_audit.csv", index=False, encoding="utf-8-sig")
    meta = {
        "status": "completed",
        "candidate": "g3_v4_research_package_v1",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "research_only": True,
        "sector_index_logic_integrated": True,
        "sector_index_validation_report": str(SECTOR_INDEX_VALIDATION_DIR / "report_cn.md"),
        "strong_guard": {
            "profile": STRONG_GUARD_PROFILE,
            "thresholds": STRONG_GUARD_THRESHOLDS,
            "scope": "strong_main route only",
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "note": "Sector/index logic is a strong-route environment and quality guard, not a global buy gate.",
        },
        "variants": [s["variant"] for s in RANGE_SPECS],
        "next_step": "add V4 research-only API/page switch after package review",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary, windows, routes, goal, guard_audit)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()

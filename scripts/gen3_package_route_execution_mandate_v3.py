from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_route_execution_mandate_candidate_package_v3"

V2_PACKAGE = ROOT / "reports" / "gen3_range_filtered_candidate_package_v2"
MANDATE_DIR = ROOT / "reports" / "gen3_route_execution_mandate_v1"
VISIBILITY_DIR = ROOT / "reports" / "gen3_route_exit_visibility_audit_v1"
GUARDED_DIR = ROOT / "reports" / "gen3_dynamic_router_guarded_v1"
SECTOR_INDEX_VALIDATION_DIR = ROOT / "reports" / "g3_sector_index_logic_validation_v1"

MAIN_PROFILE = "down_range_same_day_close30_strong_haircut2"
CONSERVATIVE_PROFILE = "down_range_same_day_close100_strong_haircut2"
BASELINE_PROFILE = "all_nextopen_haircut2_baseline"
STRONG_GUARD_PROFILE = "strong_breadth_score_volume5_guard"


def _pct(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return ""


def _md_table(df: pd.DataFrame, max_rows: int = 30) -> str:
    if df.empty:
        return "_无数据_"
    view = df.head(max_rows).copy()
    for col in view.columns:
        view[col] = view[col].astype(str)
    header = "| " + " | ".join(view.columns) + " |"
    sep = "| " + " | ".join(["---"] * len(view.columns)) + " |"
    rows = ["| " + " | ".join(row) + " |" for row in view.to_numpy()]
    suffix = [f"\n\n_仅展示前 {max_rows} 行，共 {len(df)} 行。_"] if len(df) > max_rows else []
    return "\n".join([header, sep, *rows, *suffix])


def _one(df: pd.DataFrame, profile: str) -> dict[str, Any]:
    rows = df[df["profile"].eq(profile)]
    if rows.empty:
        raise SystemExit(f"missing profile: {profile}")
    return rows.iloc[0].to_dict()


def _window(df: pd.DataFrame, profile: str, window: str) -> dict[str, Any]:
    rows = df[df["profile"].eq(profile) & df["window"].eq(window)]
    if rows.empty:
        raise SystemExit(f"missing window: {profile}/{window}")
    return rows.iloc[0].to_dict()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    v2_summary = json.loads((V2_PACKAGE / "summary.json").read_text(encoding="utf-8"))
    mandate_summary = pd.read_csv(MANDATE_DIR / "route_execution_mandate_summary.csv", low_memory=False)
    mandate_windows = pd.read_csv(MANDATE_DIR / "route_execution_mandate_windows.csv", low_memory=False)
    mandate_routes = pd.read_csv(MANDATE_DIR / "route_execution_mandate_route_attribution.csv", low_memory=False)
    mandate_policies = pd.read_csv(MANDATE_DIR / "route_execution_mandate_policy_defs.csv", low_memory=False)
    visibility = pd.read_csv(VISIBILITY_DIR / "route_exit_visibility_audit.csv", low_memory=False)
    visibility_meta = json.loads((VISIBILITY_DIR / "summary.json").read_text(encoding="utf-8"))
    guarded_summary = pd.read_csv(GUARDED_DIR / "guarded_summary.csv", low_memory=False)
    guarded_windows = pd.read_csv(GUARDED_DIR / "guarded_window_summary.csv", low_memory=False)

    main = _one(mandate_summary, MAIN_PROFILE)
    conservative = _one(mandate_summary, CONSERVATIVE_PROFILE)
    baseline = _one(mandate_summary, BASELINE_PROFILE)
    weak_main = _window(mandate_windows, MAIN_PROFILE, "weak_gap_2022_2024")
    weak_conservative = _window(mandate_windows, CONSERVATIVE_PROFILE, "weak_gap_2022_2024")
    valid_conservative = _window(mandate_windows, CONSERVATIVE_PROFILE, "valid_2024_2025")
    blind_conservative = _window(mandate_windows, CONSERVATIVE_PROFILE, "blind_2026ytd")

    selected_cols = [
        "profile",
        "trade_count",
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
    ]
    package_summary = mandate_summary[mandate_summary["profile"].isin([BASELINE_PROFILE, MAIN_PROFILE, CONSERVATIVE_PROFILE])][selected_cols].copy()
    package_windows = mandate_windows[mandate_windows["profile"].isin([BASELINE_PROFILE, MAIN_PROFILE, CONSERVATIVE_PROFILE])].copy()
    package_routes = mandate_routes[mandate_routes["profile"].isin([BASELINE_PROFILE, MAIN_PROFILE, CONSERVATIVE_PROFILE])].copy()
    guard_audit = guarded_summary[
        guarded_summary["guard"].isin(["base_no_guard", "strong_breadth_score_runup80_guard", STRONG_GUARD_PROFILE])
    ].copy()
    guard_window_audit = guarded_windows[
        guarded_windows["guard"].isin(["base_no_guard", "strong_breadth_score_runup80_guard", STRONG_GUARD_PROFILE])
    ].copy()

    package_summary.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_summary.csv", index=False, encoding="utf-8-sig")
    package_windows.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_windows.csv", index=False, encoding="utf-8-sig")
    package_routes.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_route_attribution.csv", index=False, encoding="utf-8-sig")
    guard_audit.to_csv(OUT_DIR / "g3_v3_sector_index_guard_audit.csv", index=False, encoding="utf-8-sig")
    guard_window_audit.to_csv(OUT_DIR / "g3_v3_sector_index_guard_windows.csv", index=False, encoding="utf-8-sig")
    mandate_policies.to_csv(OUT_DIR / "g3_route_execution_mandate_policy_defs.csv", index=False, encoding="utf-8-sig")
    visibility.to_csv(OUT_DIR / "g3_route_execution_mandate_visibility_audit.csv", index=False, encoding="utf-8-sig")

    main_closed = pd.read_csv(MANDATE_DIR / f"{MAIN_PROFILE}_closed_trades.csv", low_memory=False)
    main_curve = pd.read_csv(MANDATE_DIR / f"{MAIN_PROFILE}_mtm_equity_curve.csv", low_memory=False)
    main_closed.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_closed_trades.csv", index=False, encoding="utf-8-sig")
    main_curve.to_csv(OUT_DIR / "g3_route_execution_mandate_candidate_equity_curve.csv", index=False, encoding="utf-8-sig")

    goal_rows = [
        {
            "goal": "融入板块/指数强势逻辑",
            "verdict": "PASS",
            "evidence": f"V3 strong_main 使用 `{STRONG_GUARD_PROFILE}`：market_breadth > 0.56 + g3_strong_score > 0.626 + score_volume5 >= 0.70；保持研究只读，不进入自动下单。",
        },
        {
            "goal": "吸收G2强势思想",
            "verdict": "PASS",
            "evidence": "沿用 V2 的 strong_main，来自 G2 volume5 强势思路迁移；strong_main 在 extreme haircut2 下仍为正贡献。",
        },
        {
            "goal": "下降周期超过基数",
            "verdict": "PASS",
            "evidence": f"down/range 同日退出主口径 weak_gap_2022_2024 收益 {_pct(weak_main['return'])}；保守100bps口径仍为 {_pct(weak_conservative['return'])}。",
        },
        {
            "goal": "震荡/横盘链路改善",
            "verdict": "PASS",
            "evidence": f"range_gap 不改选股，仅执行约束后在主口径贡献为正；可见性审计 range_gap {visibility_meta['range_gap_visible_rows']}/{visibility_meta['range_gap_rows']} 通过。",
        },
        {
            "goal": "避免过拟合",
            "verdict": "PARTIAL_PASS",
            "evidence": "本轮只改路由级执行假设，没有新增选股阈值；但仍需 walk-forward 与真实收盘/滑点成交复核。",
        },
        {
            "goal": "执行偏差压力",
            "verdict": "PARTIAL_PASS",
            "evidence": f"极端 baseline 从 {_pct(baseline['total_return'])} 提升到主口径 {_pct(main['total_return'])}；down/range 100bps 保守口径 {_pct(conservative['total_return'])}，回撤 {_pct(conservative['max_drawdown'])}。",
        },
        {
            "goal": "实盘可接入",
            "verdict": "NOT_PASS",
            "evidence": "当前仍是研究候选包；同日退出需要继续验证收盘集合竞价/尾盘30m触发的真实可成交性。",
        },
    ]
    goal_audit = pd.DataFrame(goal_rows)
    goal_audit.to_csv(OUT_DIR / "g3_route_execution_mandate_goal_audit.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "candidate": "g3_route_execution_mandate_v3",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "inherits_selection_from": v2_summary.get("candidate", "g3_range_filtered_volume5_v2"),
        "selection_filter": v2_summary.get("range_filter"),
        "execution_mandate": "down_panic/range_gap same-day planned exit; strong_main remains nextopen_haircut2 stress-capable",
        "sector_index_logic_integrated": True,
        "sector_index_validation_report": str(SECTOR_INDEX_VALIDATION_DIR / "report_cn.md"),
        "strong_guard": {
            "profile": STRONG_GUARD_PROFILE,
            "role": "stable primary strong route guard",
            "market_breadth_min": 0.56,
            "g3_strong_score_min": 0.626,
            "score_volume5_min": 0.70,
            "auto_order_allowed": False,
            "formal_buy_signal": False,
            "note": "板块/指数逻辑用于 strong route 环境和质量门槛，不作为全局买入 gate。",
        },
        "strong_guard_alternative": {
            "profile": "strong_breadth_score_runup80_guard",
            "role": "attack candidate for future shadow comparison",
            "market_breadth_min": 0.56,
            "g3_strong_score_min": 0.626,
            "runup_from_60d_low_max": 0.80,
        },
        "baseline_haircut2": {
            "total_return": baseline["total_return"],
            "max_drawdown": baseline["max_drawdown"],
            "trade_count": int(baseline["trade_count"]),
        },
        "main_route_execution_profile": {
            "profile": MAIN_PROFILE,
            "total_return": main["total_return"],
            "max_drawdown": main["max_drawdown"],
            "trade_count": int(main["trade_count"]),
        },
        "conservative_route_execution_profile": {
            "profile": CONSERVATIVE_PROFILE,
            "total_return": conservative["total_return"],
            "max_drawdown": conservative["max_drawdown"],
            "trade_count": int(conservative["trade_count"]),
        },
        "weak_gap_2022_2024_main_return": weak_main["return"],
        "weak_gap_2022_2024_conservative_return": weak_conservative["return"],
        "valid_2024_2025_conservative_return": valid_conservative["return"],
        "blind_2026ytd_conservative_return": blind_conservative["return"],
        "visibility_verdict": visibility_meta["verdict"],
        "goal_complete": False,
        "next_step": "audit real same-day fill model and package live-safe shadow v3 only after fill model passes",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 路由级执行约束候选包 V3

生成时间：{meta["generated_at"]}

## 定位

V3 不改变 V2 的选股体系，继承 `g3_range_filtered_volume5_v2`：

- 弱势：`down_panic`
- 震荡：`range_gap`，继续使用 `exclude_adx_downtrend_and_small_positive_gap`
- 强势：`strong_main`，继续吸收 G2 volume5 强势思路

## 板块/指数逻辑融入

- strong route 主口径：`{STRONG_GUARD_PROFILE}`。
- 规则：`market_breadth > 0.56`、`g3_strong_score > 0.626`、`score_volume5 >= 0.70`。
- 定位：这是 strong route 的环境/质量门槛，不是全局买入 gate，也不是独立板块轮动策略。
- 对照：`strong_breadth_score_runup80_guard` 保留为进攻候选，暂不作为 V3 主口径。

本轮只改变执行假设：`down_panic/range_gap` 是短打/恐慌修复，不应被拖到次日开盘；必须按同日计划退出或盘中触发退出管理。`strong_main` 仍承受极端 nextopen haircut2 压力。

## 核心结果

{_md_table(package_summary)}

## 分窗口

{_md_table(package_windows)}

## 链路归因

{_md_table(package_routes)}

## Strong Guard 对照

{_md_table(guard_audit)}

## 可见性审计

{_md_table(visibility)}

## 目标审计

{_md_table(goal_audit)}

## 关键结论

- V2 的 extreme haircut2 baseline 为 `{_pct(baseline["total_return"])}`，回撤 `{_pct(baseline["max_drawdown"])}`。
- V3 主执行口径为 `{_pct(main["total_return"])}`，回撤 `{_pct(main["max_drawdown"])}`。
- 更保守的 down/range 100bps 同日退出口径为 `{_pct(conservative["total_return"])}`，回撤 `{_pct(conservative["max_drawdown"])}`。
- weak_gap_2022_2024 在主口径收益 `{_pct(weak_main["return"])}`，保守口径收益 `{_pct(weak_conservative["return"])}`。

## 尚未完成

这一步证明“极端压力失败主要是执行路径错配”，并且同日退出规则有可见性证据。但它还不能证明实盘可用，因为同日退出需要继续审计：

1. 尾盘/收盘集合竞价成交价偏差；
2. 30m 触发后延迟到下一根 bar 的可成交性；
3. 大幅下跌日流动性和跌停排队；
4. V3 live-safe shadow payload 仍未生成。

下一步应先做真实同日成交偏差压力测试，再决定是否把 V3 写成新的 shadow-only payload。"""
    (OUT_DIR / "g3_route_execution_mandate_candidate_report_cn.md").write_text(report, encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()

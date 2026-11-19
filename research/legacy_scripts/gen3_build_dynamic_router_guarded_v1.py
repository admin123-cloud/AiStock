from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_build_dynamic_router_combo_v1 import (
    OUT_DIR as BASE_OUT_DIR,
    SOURCE_COST_BPS,
    STRONG_PATH,
    md_table,
    pct,
    simulate,
    standardize_panic,
    standardize_range,
    summarize,
    summarize_routes,
    summarize_windows,
)


OUT_DIR = ROOT / "reports" / "gen3_dynamic_router_guarded_v1"

GUARDS = [
    {
        "name": "base_no_guard",
        "desc": "原始三链组合，不过滤 strong_main。",
    },
    {
        "name": "strong_breadth_gt56",
        "desc": "strong_main 仅在 market_breadth > 0.56 时开仓。",
        "market_breadth_min": 0.56,
    },
    {
        "name": "strong_score_gt626",
        "desc": "strong_main 仅保留 g3_strong_score > 0.626。",
        "g3_strong_score_min": 0.626,
    },
    {
        "name": "strong_cap_pressure_le30",
        "desc": "strong_main 过滤 cap_pressure_amount_share > 0.30 的筹码压力。",
        "cap_pressure_amount_share_max": 0.30,
    },
    {
        "name": "strong_breadth_score_guard",
        "desc": "strong_main 同时要求 market_breadth > 0.56 且 g3_strong_score > 0.626。",
        "market_breadth_min": 0.56,
        "g3_strong_score_min": 0.626,
    },
    {
        "name": "strong_breadth_score_volume5_guard",
        "desc": "strong_main 要求广度、强势分数，并保留 G2 volume5 高分确认。",
        "market_breadth_min": 0.56,
        "g3_strong_score_min": 0.626,
        "score_volume5_min": 0.70,
    },
    {
        "name": "strong_breadth_score_l3_guard",
        "desc": "strong_main 要求广度、强势分数，并要求三级板块即时强度不为零。",
        "market_breadth_min": 0.56,
        "g3_strong_score_min": 0.626,
        "l3_rt_strong3_ratio_min": 0.05,
    },
    {
        "name": "strong_breadth_score_runup80_guard",
        "desc": "strong_main 要求广度、强势分数，并把 60 日低点涨幅压到 80% 以内。",
        "market_breadth_min": 0.56,
        "g3_strong_score_min": 0.626,
        "runup_from_60d_low_max": 0.80,
    },
]


def standardize_strong_guarded(guard: dict) -> pd.DataFrame:
    d = pd.read_csv(STRONG_PATH)
    numeric_cols = [
        "market_breadth",
        "g3_strong_score",
        "cap_pressure_amount_share",
        "entry_price",
        "net_ret",
        "score_volume5",
        "v4_score",
        "l3_rt_strong3_ratio",
        "runup_from_60d_low",
    ]
    for col in numeric_cols:
        if col in d.columns:
            d[col] = pd.to_numeric(d[col], errors="coerce")
    mask = pd.Series(True, index=d.index)
    if "market_breadth_min" in guard:
        mask &= d["market_breadth"].gt(float(guard["market_breadth_min"]))
    if "g3_strong_score_min" in guard:
        mask &= d["g3_strong_score"].gt(float(guard["g3_strong_score_min"]))
    if "cap_pressure_amount_share_max" in guard:
        mask &= d["cap_pressure_amount_share"].le(float(guard["cap_pressure_amount_share_max"]))
    if "score_volume5_min" in guard:
        mask &= d["score_volume5"].ge(float(guard["score_volume5_min"]))
    if "l3_rt_strong3_ratio_min" in guard:
        mask &= d["l3_rt_strong3_ratio"].ge(float(guard["l3_rt_strong3_ratio_min"]))
    if "runup_from_60d_low_max" in guard:
        mask &= d["runup_from_60d_low"].le(float(guard["runup_from_60d_low_max"]))
    d = d[mask].copy()

    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "strong_main"
    out["route_source"] = guard["name"]
    out["route_priority"] = 2
    out["score"] = pd.to_numeric(d.get("g3_strong_score", d.get("score_volume5", d.get("v4_score", 0.0))), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_price"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("net_ret"), errors="coerce")
    return out.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def candidate_set(guard: dict) -> pd.DataFrame:
    d = pd.concat([standardize_panic(), standardize_range(), standardize_strong_guarded(guard)], ignore_index=True)
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"]).copy()
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


def run_guard(guard: dict) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    candidates = candidate_set(guard)
    curve, closed = simulate(candidates, SOURCE_COST_BPS)
    summary = summarize(curve, closed, SOURCE_COST_BPS)
    summary["guard"] = guard["name"]
    summary["desc"] = guard["desc"]
    summary["candidate_count"] = int(len(candidates))
    summary["strong_candidate_count"] = int((candidates["route"] == "strong_main").sum())
    windows = summarize_windows(curve, closed)
    windows.insert(0, "guard", guard["name"])
    routes = summarize_routes(closed)
    routes.insert(0, "guard", guard["name"])
    tag = guard["name"]
    curve.to_csv(OUT_DIR / f"{tag}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(OUT_DIR / f"{tag}_closed_trades.csv", index=False, encoding="utf-8-sig")
    return summary, windows, routes


def run_guard_cost(guard: dict, cost_bps: float) -> tuple[dict, pd.DataFrame]:
    candidates = candidate_set(guard)
    curve, closed = simulate(candidates, cost_bps)
    summary = summarize(curve, closed, cost_bps)
    summary["guard"] = guard["name"]
    summary["desc"] = guard["desc"]
    summary["candidate_count"] = int(len(candidates))
    summary["strong_candidate_count"] = int((candidates["route"] == "strong_main").sum())
    windows = summarize_windows(curve, closed)
    windows.insert(0, "cost_bps", cost_bps)
    windows.insert(0, "guard", guard["name"])
    tag = f"{guard['name']}_cost{int(cost_bps)}"
    curve.to_csv(OUT_DIR / f"{tag}_mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(OUT_DIR / f"{tag}_closed_trades.csv", index=False, encoding="utf-8-sig")
    return summary, windows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    window_parts = []
    route_parts = []
    stress_rows = []
    stress_window_parts = []
    for guard in GUARDS:
        summary, windows, routes = run_guard(guard)
        summary_rows.append(summary)
        window_parts.append(windows)
        route_parts.append(routes)
        if guard["name"] in {
            "strong_breadth_gt56",
            "strong_breadth_score_guard",
            "strong_breadth_score_volume5_guard",
            "strong_breadth_score_l3_guard",
            "strong_breadth_score_runup80_guard",
        }:
            for cost_bps in [30.0, 50.0, 100.0]:
                stress_summary, stress_windows = run_guard_cost(guard, cost_bps)
                stress_rows.append(stress_summary)
                stress_window_parts.append(stress_windows)
    summary_df = pd.DataFrame(summary_rows).sort_values(["max_drawdown", "total_return"], ascending=[False, False])
    window_df = pd.concat(window_parts, ignore_index=True)
    route_df = pd.concat(route_parts, ignore_index=True)
    stress_df = pd.DataFrame(stress_rows)
    stress_window_df = pd.concat(stress_window_parts, ignore_index=True) if stress_window_parts else pd.DataFrame()
    summary_df.to_csv(OUT_DIR / "guarded_summary.csv", index=False, encoding="utf-8-sig")
    window_df.to_csv(OUT_DIR / "guarded_window_summary.csv", index=False, encoding="utf-8-sig")
    route_df.to_csv(OUT_DIR / "guarded_route_attribution.csv", index=False, encoding="utf-8-sig")
    stress_df.to_csv(OUT_DIR / "guarded_stress_summary.csv", index=False, encoding="utf-8-sig")
    stress_window_df.to_csv(OUT_DIR / "guarded_stress_windows.csv", index=False, encoding="utf-8-sig")

    best_dd = summary_df.iloc[0].to_dict()
    viable = summary_df[(summary_df["total_return"].ge(1.0)) & (summary_df["max_drawdown"].ge(-0.20))]
    best_viable = viable.sort_values("total_return", ascending=False).iloc[0].to_dict() if not viable.empty else {}
    lines = [
        "# G3 动态路由强势链降回撤规则审计 V1",
        "",
        "## 目的",
        "",
        "- 只测试少数固定、可解释的 strong_main 风控门槛，不做参数网格搜索。",
        "- down_panic 与 range_gap 保持不变，专门验证最大回撤是否来自 strong_main 假强势。",
        f"- 基准组合产物目录：`{BASE_OUT_DIR.relative_to(ROOT)}`。",
        "",
        "## 总表",
        "",
        md_table(
            summary_df[
                [
                    "guard",
                    "total_return",
                    "max_drawdown",
                    "trade_count",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "worst_open_mtm_ret",
                    "strong_candidate_count",
                ]
            ],
            {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"},
        ),
        "",
        "## 最低回撤版本",
        "",
        f"- `{best_dd.get('guard')}`：收益 {pct(best_dd.get('total_return'))}，回撤 {pct(best_dd.get('max_drawdown'))}，交易 {best_dd.get('trade_count')} 笔。",
    ]
    if best_viable:
        lines.extend(
            [
                "",
                "## 暂定可继续版本",
                "",
                f"- `{best_viable.get('guard')}`：收益 {pct(best_viable.get('total_return'))}，回撤 {pct(best_viable.get('max_drawdown'))}，交易 {best_viable.get('trade_count')} 笔。",
            ]
        )
    lines.extend(
        [
            "",
            "## 分段窗口",
            "",
            md_table(window_df, {"return", "max_drawdown", "win_rate"}),
            "",
            "## 候选版本成本压力",
            "",
            md_table(
                stress_df[
                    [
                        "guard",
                        "cost_bps",
                        "total_return",
                        "max_drawdown",
                        "trade_count",
                        "win_rate",
                        "avg_trade_return",
                        "worst_trade",
                    ]
                ],
                {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade"},
            ),
            "",
            "## 下一步",
            "",
            "1. 对候选 guard 版本补 50/100bps 压力测试。",
            "2. 如果 2022-2024、2024-2025、2026 仍稳定，再做跌停不可卖和尾盘低开冲击。",
        ]
    )
    (OUT_DIR / "dynamic_router_guarded_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "best_drawdown": best_dd,
                "best_viable": best_viable,
                "summary": str(OUT_DIR / "guarded_summary.csv"),
                "windows": str(OUT_DIR / "guarded_window_summary.csv"),
                "stress_summary": str(OUT_DIR / "guarded_stress_summary.csv"),
                "stress_windows": str(OUT_DIR / "guarded_stress_windows.csv"),
                "next_step": "guarded_combo_pressure_test",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()

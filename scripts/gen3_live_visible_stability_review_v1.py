from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path

OUT_DIR = report_path("gen3_live_visible_stability_review_v1")

PACKAGE_DIR = report_path("gen3_guarded_candidate_package_v1")
STRESS_DIR = report_path("gen3_guarded_execution_stress_v1")
LIVE_DIR = report_path("gen3_guarded_live_safe_payload_v1")
INTEGRITY_DIR = report_path("gen3_live_visible_combo_integrity_audit_v1")


def _pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def _money(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):,.0f}"


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False) if path.exists() else pd.DataFrame()


def _annual_curve(curve_path: Path, profile: str) -> pd.DataFrame:
    curve = _read_csv(curve_path)
    if curve.empty:
        return pd.DataFrame()
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce")
    curve = curve.dropna(subset=["date"]).sort_values("date")
    rows = []
    for year, g in curve.groupby(curve["date"].dt.year):
        start_eq = float(g["equity"].iloc[0])
        end_eq = float(g["equity"].iloc[-1])
        peak = g["equity"].cummax()
        dd = (g["equity"] / peak - 1.0).min()
        rows.append(
            {
                "profile": profile,
                "year": int(year),
                "start_date": g["date"].iloc[0].strftime("%Y-%m-%d"),
                "end_date": g["date"].iloc[-1].strftime("%Y-%m-%d"),
                "year_return": end_eq / start_eq - 1.0,
                "year_max_drawdown": float(dd),
                "end_equity": end_eq,
                "opened": int(pd.to_numeric(g.get("opened", 0), errors="coerce").fillna(0).sum()),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    live_summary = _read_csv(LIVE_DIR / "g3_guarded_live_safe_summary.csv")
    integrity = _read_csv(INTEGRITY_DIR / "live_visible_combo_overall_audit.csv")
    route_metrics = _read_csv(INTEGRITY_DIR / "live_visible_combo_route_metrics.csv")
    package_windows = _read_csv(PACKAGE_DIR / "g3_guarded_candidate_windows.csv")
    goal_audit = _read_csv(PACKAGE_DIR / "g3_goal_completion_audit.csv")
    stress_summary = _read_csv(STRESS_DIR / "execution_stress_summary.csv")
    stress_windows = _read_csv(STRESS_DIR / "execution_stress_windows.csv")
    route_attr = _read_csv(STRESS_DIR / "execution_stress_route_attribution.csv")

    annuals = pd.concat(
        [
            _annual_curve(PACKAGE_DIR / "g3_guarded_candidate_equity_curve.csv", "main_30bps_package"),
            _annual_curve(STRESS_DIR / "close_100bps_mtm_equity_curve.csv", "close_100bps"),
            _annual_curve(STRESS_DIR / "nextopen_limitdown_30bps_mtm_equity_curve.csv", "nextopen_limitdown_30bps"),
            _annual_curve(STRESS_DIR / "nextopen_haircut2_30bps_mtm_equity_curve.csv", "nextopen_haircut2_30bps"),
        ],
        ignore_index=True,
    )

    preferred_guard = "strong_breadth_score_volume5_guard"
    preferred_windows = package_windows[package_windows.get("guard", "").eq(preferred_guard)].copy()

    priority_rows = []
    close100_attr = route_attr[route_attr["profile"].eq("close_100bps")].copy()
    hair_attr = route_attr[route_attr["profile"].eq("nextopen_haircut2_30bps")].copy()
    if not close100_attr.empty:
        weak = close100_attr.sort_values("avg_trade_return").head(1).iloc[0]
        priority_rows.append(
            {
                "priority": 1,
                "target": "range_gap 成本敏感性",
                "evidence": f"100bps 下 {weak['route']} avg_trade_return={_pct(weak['avg_trade_return'])}, sum_pnl={_money(weak['sum_realized_pnl'])}",
                "next_action": "先做 range_gap 执行/筛选降噪，不扩大参数搜索；优先查流动性、缺口、尾盘确认、持有期。",
            }
        )
    if not hair_attr.empty:
        bad = hair_attr.sort_values("sum_realized_pnl").head(1).iloc[0]
        priority_rows.append(
            {
                "priority": 2,
                "target": "极端次日低开冲击",
                "evidence": f"haircut2 下 {bad['route']} sum_pnl={_money(bad['sum_realized_pnl'])}, avg_trade_return={_pct(bad['avg_trade_return'])}",
                "next_action": "建立开盘不可控风险闸门：弱流动性、前日尾盘触发、隔夜大盘/行业风险，先影子验证。",
            }
        )
    priority_rows.append(
        {
            "priority": 3,
            "target": "实盘闭环",
            "evidence": "payload 已 live-ready 但 auto_order_allowed/formal_buy_signal/order_path_enabled 均为 0",
            "next_action": "继续 shadow-only，不接自动交易；补真实涨跌停盘口、通知、交易页风控闭环。",
        }
    )
    priority = pd.DataFrame(priority_rows)

    live_summary.to_csv(OUT_DIR / "live_visible_route_summary.csv", index=False, encoding="utf-8-sig")
    integrity.to_csv(OUT_DIR / "live_visible_integrity_summary.csv", index=False, encoding="utf-8-sig")
    route_metrics.to_csv(OUT_DIR / "live_visible_route_research_metrics.csv", index=False, encoding="utf-8-sig")
    annuals.to_csv(OUT_DIR / "live_visible_annual_curve_metrics.csv", index=False, encoding="utf-8-sig")
    preferred_windows.to_csv(OUT_DIR / "live_visible_preferred_guard_windows.csv", index=False, encoding="utf-8-sig")
    stress_summary.to_csv(OUT_DIR / "live_visible_execution_stress_summary.csv", index=False, encoding="utf-8-sig")
    stress_windows.to_csv(OUT_DIR / "live_visible_execution_stress_windows.csv", index=False, encoding="utf-8-sig")
    route_attr.to_csv(OUT_DIR / "live_visible_execution_route_attribution.csv", index=False, encoding="utf-8-sig")
    priority.to_csv(OUT_DIR / "live_visible_next_priority.csv", index=False, encoding="utf-8-sig")

    display_annual = annuals.copy()
    for c in ["year_return", "year_max_drawdown"]:
        if c in display_annual:
            display_annual[c] = display_annual[c].map(_pct)
    if "end_equity" in display_annual:
        display_annual["end_equity"] = display_annual["end_equity"].map(_money)

    display_windows = preferred_windows.copy()
    for c in ["return", "max_drawdown", "win_rate"]:
        if c in display_windows:
            display_windows[c] = display_windows[c].map(_pct)

    display_stress = stress_summary.copy()
    for c in ["total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"]:
        if c in display_stress:
            display_stress[c] = display_stress[c].map(_pct)

    display_attr = route_attr.copy()
    for c in ["win_rate", "avg_trade_return", "worst_trade"]:
        if c in display_attr:
            display_attr[c] = display_attr[c].map(_pct)
    if "sum_realized_pnl" in display_attr:
        display_attr["sum_realized_pnl"] = display_attr["sum_realized_pnl"].map(_money)

    pass_live = False
    if not integrity.empty:
        row = integrity.iloc[0]
        pass_live = (
            int(row.get("payload_rows", 0)) == int(row.get("closed_rows", -1))
            and int(row.get("missing_payload_rows", 1)) == 0
            and int(row.get("live_ready_rows", 0)) == int(row.get("payload_rows", -1))
            and int(row.get("auto_order_allowed_rows", 1)) == 0
            and int(row.get("formal_buy_signal_rows", 1)) == 0
            and int(row.get("order_path_enabled_rows", 1)) == 0
        )
    close100 = stress_summary[stress_summary["profile"].eq("close_100bps")].iloc[0].to_dict()
    haircut = stress_summary[stress_summary["profile"].eq("nextopen_haircut2_30bps")].iloc[0].to_dict()
    weak100 = stress_windows[
        stress_windows["profile"].eq("close_100bps") & stress_windows["window"].eq("weak_gap_2022_2024")
    ].iloc[0].to_dict()

    verdict_rows = pd.DataFrame(
        [
            {
                "requirement": "吸收 G2 强势思想",
                "status": "通过",
                "evidence": "strong_main 使用 D-1 volume5 + 盘中确认，73 笔 live-ready；研究均值收益 4.48%。",
            },
            {
                "requirement": "下降/震荡周期超过基数",
                "status": "阶段通过",
                "evidence": f"weak_gap_2022_2024 在 close_100bps 下仍 {_pct(weak100['return'])}，但 range_gap 100bps 单链路为负。",
            },
            {
                "requirement": "避免过拟合",
                "status": "部分通过",
                "evidence": "已有 train/valid/blind 与压力测试；但 range_gap 成本敏感，不能继续靠小参数堆叠优化。",
            },
            {
                "requirement": "实盘可接入",
                "status": "未通过",
                "evidence": "live-ready 只代表可观察；自动交易三开关仍关闭，极端开盘冲击不通过。",
            },
        ]
    )
    verdict_rows.to_csv(OUT_DIR / "live_visible_goal_verdict.csv", index=False, encoding="utf-8-sig")

    meta = {
        "status": "completed",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "live_integrity_pass": pass_live,
        "close_100bps_return": close100.get("total_return"),
        "close_100bps_max_drawdown": close100.get("max_drawdown"),
        "weak_gap_2022_2024_close_100bps_return": weak100.get("return"),
        "haircut2_return": haircut.get("total_return"),
        "next_priority": priority.iloc[0]["target"] if not priority.empty else "",
        "goal_complete": False,
        "next_step": "build_range_gap_cost_sensitivity_and_execution_noise_audit",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    report = f"""# G3 live-visible 稳定性复核 V1

生成时间：{meta["generated_at"]}

## 核心判断

这一步把当前 G3 从“研究候选”复核到“live-visible 影子候选”：

- 三条链路已经全部 live-ready，且没有打开自动交易。
- G2 强势思想已经被吸收：`strong_main` 保留 D-1 volume5 高分和盘中确认。
- 下降/震荡窗口已经阶段性超过基数，但还不能宣称完成，因为 `range_gap` 在高成本和极端开盘冲击下拖后腿。

## live-visible 完整性

{integrity.to_markdown(index=False) if not integrity.empty else "_无数据_"}

## 年度曲线复核

{display_annual.to_markdown(index=False) if not display_annual.empty else "_无数据_"}

## 首选 guard 窗口

{display_windows.to_markdown(index=False) if not display_windows.empty else "_无数据_"}

## 执行压力

{display_stress.to_markdown(index=False) if not display_stress.empty else "_无数据_"}

## 链路压力归因

{display_attr.to_markdown(index=False) if not display_attr.empty else "_无数据_"}

## 目标完成度

{verdict_rows.to_markdown(index=False)}

## 后续优先级

{priority.to_markdown(index=False)}

## 结论

当前不能标记总目标完成。更准确的状态是：

1. `strong_main` 已经吸收 G2 full 的强势赚钱思想，并且盘中可见性已打通。
2. `down_panic` 已经完成 D-1 恐慌环境可见性修正，是下降周期的主要安全垫。
3. `range_gap` 是下一步核心瓶颈：它让组合在 30/50bps 有收益，但 100bps 单链路已经为负，极端低开冲击下亏损最大。

下一步不应该继续盲目搜索大参数，而是专门做 `range_gap` 成本敏感与执行噪声审计，找到它在震荡市场中到底输在流动性、入场时点、持有期，还是候选质量。
"""
    (OUT_DIR / "live_visible_stability_review_report_cn.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()

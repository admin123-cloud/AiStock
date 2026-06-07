from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_guarded_candidate_package_v1"
GUARDED_DIR = ROOT / "reports" / "gen3_dynamic_router_guarded_v1"
EXEC_DIR = ROOT / "reports" / "gen3_guarded_execution_stress_v1"
DOWN_DIR = ROOT / "reports" / "gen3_down_panic_v3_regime_audit_v1"
RANGE_DIR = ROOT / "reports" / "gen3_range_v3_mtm_pressure_v1"
STRONG_DIR = ROOT / "reports" / "gen3_strong_v2_independent_source_v1"
CURRENT_G3_DIR = ROOT / "reports" / "gen3_final_candidate_package_v1"


def pct(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def plot_curve(curve: pd.DataFrame, out: Path) -> None:
    d = curve.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    d["equity_norm"] = pd.to_numeric(d["equity"], errors="coerce") / float(pd.to_numeric(d["equity"], errors="coerce").iloc[0])
    plt.figure(figsize=(12, 6))
    plt.plot(d["date"], d["equity_norm"], label="G3 guarded volume5")
    plt.title("G3 guarded candidate equity curve")
    plt.xlabel("date")
    plt.ylabel("equity / start")
    plt.grid(True, alpha=0.25)
    plt.legend()
    plt.tight_layout()
    plt.savefig(out, dpi=160)
    plt.close()


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    curve = load_csv(GUARDED_DIR / "strong_breadth_score_volume5_guard_cost30_mtm_equity_curve.csv")
    trades = load_csv(GUARDED_DIR / "strong_breadth_score_volume5_guard_cost30_closed_trades.csv")
    stress = load_csv(EXEC_DIR / "execution_stress_summary.csv")
    stress_windows = load_csv(EXEC_DIR / "execution_stress_windows.csv")
    guarded_summary = load_csv(GUARDED_DIR / "guarded_summary.csv")
    guarded_windows = load_csv(GUARDED_DIR / "guarded_window_summary.csv")
    route_attr = load_csv(EXEC_DIR / "execution_stress_route_attribution.csv")
    down_windows = load_csv(DOWN_DIR / "down_panic_v3_window_metrics.csv")
    range_summary = load_csv(RANGE_DIR / "range_v3_mtm_pressure_summary.csv")
    strong_summary = load_csv(STRONG_DIR / "strong_v2_eval_summary.csv")
    current_annual = load_csv(CURRENT_G3_DIR / "final_candidate_annual.csv")

    curve.to_csv(OUT_DIR / "g3_guarded_candidate_equity_curve.csv", index=False, encoding="utf-8-sig")
    trades.to_csv(OUT_DIR / "g3_guarded_candidate_closed_trades.csv", index=False, encoding="utf-8-sig")
    guarded_summary.to_csv(OUT_DIR / "g3_guarded_candidate_summary.csv", index=False, encoding="utf-8-sig")
    guarded_windows.to_csv(OUT_DIR / "g3_guarded_candidate_windows.csv", index=False, encoding="utf-8-sig")
    stress.to_csv(OUT_DIR / "g3_guarded_execution_stress_summary.csv", index=False, encoding="utf-8-sig")
    stress_windows.to_csv(OUT_DIR / "g3_guarded_execution_stress_windows.csv", index=False, encoding="utf-8-sig")
    if not curve.empty:
        plot_curve(curve, OUT_DIR / "g3_guarded_candidate_equity_curve.png")

    main_row = guarded_summary[guarded_summary["guard"].eq("strong_breadth_score_volume5_guard")].iloc[0]
    close100 = stress[stress["profile"].eq("close_100bps")].iloc[0]
    nextopen = stress[stress["profile"].eq("nextopen_limitdown_30bps")].iloc[0]
    severe = stress[stress["profile"].eq("nextopen_haircut2_30bps")].iloc[0]
    weak100 = stress_windows[
        stress_windows["profile"].eq("close_100bps") & stress_windows["window"].eq("weak_gap_2022_2024")
    ].iloc[0]
    nextopen_weak = stress_windows[
        stress_windows["profile"].eq("nextopen_limitdown_30bps") & stress_windows["window"].eq("weak_gap_2022_2024")
    ].iloc[0]
    completion = pd.DataFrame(
        [
            {
                "requirement": "吸收 G2 强势思想",
                "status": "基本满足",
                "evidence": "strong_main 使用广度 + g3_strong_score + score_volume5>=0.70，保留 G2 volume5 高分确认思想。",
            },
            {
                "requirement": "下降周期超过基数",
                "status": "满足",
                "evidence": f"down_panic_v3 2022-2024 为 {pct(down_windows[down_windows['window'].eq('weak_gap_2022_2024')]['return'].iloc[0])}，组合 100bps 弱势窗口仍为 {pct(weak100['return'])}。",
            },
            {
                "requirement": "震荡/弱反弹周期超过基数",
                "status": "阶段满足",
                "evidence": "true_range 仍失败，但 range_v3 weak_low_not_chasing_h5 在 30/50bps 有效；组合窗口为正。",
            },
            {
                "requirement": "不要过拟合",
                "status": "部分满足",
                "evidence": "采用少数固定可解释 guard，分 train/valid/blind/2022-2024 校验；但仍需未来样本继续观察。",
            },
            {
                "requirement": "执行偏差压力",
                "status": "部分满足",
                "evidence": f"100bps 与 next-open/跌停延迟仍为正；极端每笔 next-open 额外 2% 冲击为 {pct(severe['total_return'])}，不通过。",
            },
            {
                "requirement": "可接实盘",
                "status": "未满足",
                "evidence": "尚未接入 shadow live、涨跌停真实盘口/排队、交易页风控和通知闭环。",
            },
        ]
    )
    completion.to_csv(OUT_DIR / "g3_goal_completion_audit.csv", index=False, encoding="utf-8-sig")

    lines = [
        "# G3 guarded 候选策略包 V1",
        "",
        "## 候选定义",
        "",
        "- `down_panic`：弱势恐慌出清链，沿用 down_panic_v3。",
        "- `range_gap`：震荡/弱反弹缺口链，使用 `range_v3_weak_low_not_chasing_h5`。",
        "- `strong_main`：吸收 G2 full 的强势思想，使用 strong_v2 main_up，并增加 `market_breadth > 0.56`、`g3_strong_score > 0.626`、`score_volume5 >= 0.70`。",
        "- 统一资金：slot5、每笔 20%、每日最多开 2 笔；同日优先级 `down_panic > strong_main > range_gap`。",
        "",
        "## 主结果",
        "",
        f"- 30bps：收益 {pct(main_row['total_return'])}，最大回撤 {pct(main_row['max_drawdown'])}，交易 {int(main_row['trade_count'])} 笔。",
        f"- 100bps 重新定价：收益 {pct(close100['total_return'])}，最大回撤 {pct(close100['max_drawdown'])}。",
        f"- 次日开盘 + 跌停延迟：收益 {pct(nextopen['total_return'])}，最大回撤 {pct(nextopen['max_drawdown'])}。",
        f"- 极端 next-open 再额外 2% 冲击：收益 {pct(severe['total_return'])}，最大回撤 {pct(severe['max_drawdown'])}，不通过。",
        "",
        "## 分段稳定性",
        "",
        md_table(
            guarded_windows[guarded_windows["guard"].eq("strong_breadth_score_volume5_guard")],
            {"return", "max_drawdown", "win_rate"},
        ),
        "",
        "## 执行压力",
        "",
        md_table(stress, {"total_return", "max_drawdown", "win_rate", "avg_trade_return", "worst_trade", "worst_open_mtm_ret"}),
        "",
        "## 链路贡献",
        "",
        md_table(route_attr, {"win_rate", "avg_trade_return", "worst_trade"}),
        "",
        "## 目标完成度审计",
        "",
        md_table(completion),
        "",
        "## 结论",
        "",
        "- 当前 G3 已经形成一个比原 G3 formal 更强的候选：收益恢复、2022-2024 与 2024-2025 窗口为正，且强势链明确吸收 G2 volume5 思想。",
        "- 但目标尚未完全完成：极端开盘冲击不通过，且尚未完成 shadow live/真实盘口可成交/通知闭环，所以不能接实盘。",
        "- 下一步应做 shadow-only 输出与真实交易页隔离验证，继续收集样本，而不是再做离线参数优化。",
    ]
    (OUT_DIR / "g3_guarded_candidate_package_report_cn.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "candidate": "g3_guarded_volume5_v1",
                "main_30bps": main_row.to_dict(),
                "close_100bps": close100.to_dict(),
                "nextopen_limitdown_30bps": nextopen.to_dict(),
                "severe_nextopen_haircut2": severe.to_dict(),
                "weak_gap_100bps": weak100.to_dict(),
                "weak_gap_nextopen_limitdown": nextopen_weak.to_dict(),
                "completion_audit": str(OUT_DIR / "g3_goal_completion_audit.csv"),
                "next_step": "shadow_only_integration_and_live_visibility_audit",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()

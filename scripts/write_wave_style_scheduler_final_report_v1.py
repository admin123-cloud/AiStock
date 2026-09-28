from __future__ import annotations

import sys
import math
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path  # noqa: E402


SCHED_DIR = report_path("wave_style_model_scheduler_v1")
TEMPLATE_DIR = report_path("wave_style_template_strategy_backtest_v1")
WINNER = "scheduler_focus_240d_score120_aggr25"


def _pct(value: object) -> str:
    try:
        x = float(value)
        if not math.isfinite(x):
            return ""
        return f"{x:.2%}"
    except Exception:
        return ""


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, limit: int | None = None) -> str:
    out = df.copy()
    if limit is not None:
        out = out.head(limit)
    for col in pct_cols or set():
        if col in out.columns:
            out[col] = out[col].map(_pct)
    return out.to_markdown(index=False)


def _winner_label_table() -> pd.DataFrame:
    trades = pd.read_csv(SCHED_DIR / WINNER / "closed_trades.csv", encoding="utf-8-sig")
    if trades.empty:
        return pd.DataFrame()
    for col in ["net_ret", "realized_pnl", "selected_score", "rank_key"]:
        trades[col] = pd.to_numeric(trades.get(col), errors="coerce")
    grouped = (
        trades.groupby(["selected_variant", "template_label"], dropna=False)
        .agg(
            trades=("net_ret", "size"),
            pnl=("realized_pnl", "sum"),
            avg_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda x: float((x > 0).mean())),
            worst_ret=("net_ret", "min"),
            avg_score=("selected_score", "mean"),
        )
        .reset_index()
        .sort_values("pnl", ascending=False)
    )
    grouped["pnl"] = grouped["pnl"].round(0)
    grouped["avg_score"] = grouped["avg_score"].round(2)
    return grouped


def _drawdown_window_table() -> pd.DataFrame:
    curve = pd.read_csv(SCHED_DIR / WINNER / "equity_curve.csv", encoding="utf-8-sig")
    trades = pd.read_csv(SCHED_DIR / WINNER / "closed_trades.csv", encoding="utf-8-sig")
    if curve.empty or trades.empty:
        return pd.DataFrame()
    curve["date"] = pd.to_datetime(curve["date"]).dt.normalize()
    curve["peak2"] = pd.to_numeric(curve["equity"], errors="coerce").cummax()
    curve["dd2"] = pd.to_numeric(curve["equity"], errors="coerce") / curve["peak2"] - 1.0
    valley_idx = curve["dd2"].idxmin()
    peak_idx = curve.loc[:valley_idx, "equity"].idxmax()
    peak_date = curve.loc[peak_idx, "date"]
    valley_date = curve.loc[valley_idx, "date"]

    trades["entry_date"] = pd.to_datetime(trades["entry_date"]).dt.normalize()
    for col in ["net_ret", "realized_pnl", "selected_score"]:
        trades[col] = pd.to_numeric(trades.get(col), errors="coerce")
    window = trades[(trades["entry_date"] >= peak_date) & (trades["entry_date"] <= valley_date)].copy()
    grouped = (
        window.groupby(["selected_variant", "template_label"], dropna=False)
        .agg(
            trades=("net_ret", "size"),
            pnl=("realized_pnl", "sum"),
            avg_ret=("net_ret", "mean"),
            win_rate=("net_ret", lambda x: float((x > 0).mean())),
            worst_ret=("net_ret", "min"),
        )
        .reset_index()
        .sort_values("pnl")
    )
    grouped["pnl"] = grouped["pnl"].round(0)
    grouped.insert(0, "window", f"{peak_date.date()} 至 {valley_date.date()}")
    return grouped


def main() -> int:
    sched_summary = pd.read_csv(SCHED_DIR / "summary.csv", encoding="utf-8-sig")
    sched_annual = pd.read_csv(SCHED_DIR / "annual_summary.csv", encoding="utf-8-sig")
    selection = pd.read_csv(SCHED_DIR / "selection_summary.csv", encoding="utf-8-sig")
    template_summary = pd.read_csv(TEMPLATE_DIR / "summary.csv", encoding="utf-8-sig")

    full_sched = sched_summary[sched_summary["window"].eq("full")].sort_values("excess_ret", ascending=False)
    post_sched = sched_summary[sched_summary["window"].eq("post_2024_09")].sort_values("excess_ret", ascending=False)
    blind_2026 = sched_summary[sched_summary["window"].eq("blind_2026ytd")].sort_values("excess_ret", ascending=False)
    train = sched_summary[sched_summary["window"].eq("train_2020_2023")].copy()
    full_template = template_summary[template_summary["window"].eq("full")].sort_values("excess_ret", ascending=False)

    winner_full = full_sched[full_sched["model"].eq(WINNER)].iloc[0]
    winner_post = post_sched[post_sched["model"].eq(WINNER)].iloc[0]
    winner_blind = blind_2026[blind_2026["model"].eq(WINNER)].iloc[0]
    winner_train = train[train["model"].eq(WINNER)].iloc[0]

    baseline_rows: list[dict[str, object]] = [
        {
            "口径": "指数基线：上证 999999.SH",
            "全周期收益": winner_full["index_ret"],
            "全周期超额": 0.0,
            "最大回撤": None,
            "交易数": None,
        }
    ]
    for _, row in full_template.head(3).iterrows():
        baseline_rows.append(
            {
                "口径": f"固定模板：{row['variant']}",
                "全周期收益": row["strategy_ret"],
                "全周期超额": row["excess_ret"],
                "最大回撤": row["max_drawdown"],
                "交易数": int(row["closed"]),
            }
        )
    for _, row in full_sched.head(8).iterrows():
        baseline_rows.append(
            {
                "口径": f"调度器：{row['model']}",
                "全周期收益": row["strategy_ret"],
                "全周期超额": row["excess_ret"],
                "最大回撤": row["max_drawdown"],
                "交易数": int(row["closed"]),
            }
        )
    baseline = pd.DataFrame(baseline_rows)
    baseline.to_csv(SCHED_DIR / "final_baseline_comparison.csv", index=False, encoding="utf-8-sig")

    annual = sched_annual[sched_annual["model"].eq(WINNER)].copy()
    annual = annual[["window", "closed", "strategy_ret", "index_ret", "excess_ret", "max_drawdown", "win_rate", "mean_trade_ret"]]
    winner_selection = selection[selection["scheduler"].eq(WINNER)].copy()
    winner_selection = winner_selection[["selected_variant", "selected_days", "closed", "avg_ret", "win_rate", "pnl"]].sort_values("pnl", ascending=False)
    winner_selection["pnl"] = winner_selection["pnl"].round(0)

    sched_cols = [
        "model",
        "closed",
        "strategy_ret",
        "index_ret",
        "excess_ret",
        "max_drawdown",
        "win_rate",
        "mean_trade_ret",
        "avg_open_positions",
    ]
    pct_cols = {"strategy_ret", "index_ret", "excess_ret", "max_drawdown", "win_rate", "mean_trade_ret"}
    score120 = full_sched[full_sched["model"].astype(str).str.contains("score120_aggr25", regex=False)][sched_cols]

    label_table = _winner_label_table()
    dd_table = _drawdown_window_table()

    lines = [
        "# 波段赢家高收益模型调度研究结论 v4",
        "",
        "## 核心结论",
        "",
        f"- 当前收益最强版本仍是 `{WINNER}`：全周期收益 {_pct(winner_full['strategy_ret'])}，同期指数 {_pct(winner_full['index_ret'])}，超额 {_pct(winner_full['excess_ret'])}，最大回撤 {_pct(winner_full['max_drawdown'])}。",
        f"- 2024-09 后窗口收益 {_pct(winner_post['strategy_ret'])}，2026 样本外收益 {_pct(winner_blind['strategy_ret'])}；真正的收益来源是 2024-09 后的大波段主升，而不是震荡市低吸。",
        f"- 2020-2023 窗口仍为 {_pct(winner_train['strategy_ret'])}，说明纯弱势/无主线阶段仍应尽量少交易。",
        "- 继续加胜率、均值、坏亏损过滤没有改善收益，反而把大肉交易一起过滤掉；继续加仓到 30%-40% 也会因为现金占用降低成交数。",
        "- 当前框架的主要短板不是风险过滤，而是候选池仍太窄：它会错过一些新一轮机构主升风格，需要升级为“前一波赚钱股票风格相似度”模型。",
        "",
        "## 最终基线对比",
        "",
        _md_table(baseline, {"全周期收益", "全周期超额", "最大回撤"}),
        "",
        "## 调度器全周期排序",
        "",
        _md_table(full_sched[sched_cols], pct_cols, limit=12),
        "",
        "## score120 系列压力测试",
        "",
        _md_table(score120, pct_cols),
        "",
        "## 2024-09 后窗口",
        "",
        _md_table(post_sched[sched_cols], pct_cols, limit=10),
        "",
        "## 2026 样本外窗口",
        "",
        _md_table(blind_2026[sched_cols], pct_cols, limit=10),
        "",
        "## 最佳调度器分年",
        "",
        _md_table(annual, pct_cols),
        "",
        "## 最佳调度器模板贡献",
        "",
        _md_table(winner_selection, {"avg_ret", "win_rate"}),
        "",
        "## 成交标签贡献",
        "",
        _md_table(label_table, {"avg_ret", "win_rate", "worst_ret"}),
        "",
        "## 最大回撤来源",
        "",
        _md_table(dd_table, {"avg_ret", "win_rate", "worst_ret"}),
        "",
        "## 策略定义",
        "",
        f"`{WINNER}` 每天只使用当日之前已经退出的交易，滚动观察最近 240 天哪一类波段赢家模板最赚钱。候选模板限制在 `learned_sector_h20`、`learned_sector_strict_h20`、`learned_sector_trend_h10`、`learned_sector_trend_h20` 四类主升/容量风格内；滚动评分达到 1.20 才允许开仓，单槽仓位 25%，最多 5 槽，每日最多开 1 笔。",
        "",
        "## 新增审计结论",
        "",
        "- 粗暴删除 `trend_h10` 不可取：它总体贡献差，但在长盈精密、深信服等修复主升阶段贡献过关键收益。",
        "- 加入 `bigwave` 模板没有抬高收益：`base_plus_big20` 约 +275%，低于当前 +289%，说明当前赢家模板已部分捕捉大波段，额外大盘门槛偏滞后。",
        "- 只做 `breakout_acceleration` 或只做 `capacity_theme_mainwave` 都低于完整调度器，说明收益来自两种模式的阶段切换，而不是固定标签。",
        "- 最大回撤集中在 2024-11-13 至 2025-06-06，主要是消费电子、软件服务、半导体在高位震荡中的连续回撤；这类亏损不能用简单胜率阈值消掉。",
        "",
        "## 下一步",
        "",
        "- 从历史每个上涨波段里提取真实赚钱股票，学习它们的风格向量：行业/主题、成交额分位、趋势位置、20/60 日动量、离高点距离、涨停/大阳频率、机构容量、回撤形态。",
        "- 每一轮新行情只参考上一轮或最近几轮真正赚钱的股票，生成“市场正在奖励什么”的相似度分数。",
        "- 用相似度分数替代静态模板分数，和当前 `score120_aggr25` 调度器合并回测，目标是保持 2024-09 后进攻性，同时减少 2024-11 至 2025-06 这种高位跟风亏损。",
    ]

    out = SCHED_DIR / "FINAL_RESEARCH_CN.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

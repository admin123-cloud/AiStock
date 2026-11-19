from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, reports_root  # noqa: E402


START = pd.Timestamp("2020-01-01")
PRE_END = pd.Timestamp("2024-09-30")
OUT_DIR = report_path("gen3_pre_2024_10_deeper_mode_exploration_v2")


@dataclass(frozen=True)
class CandidateSource:
    key: str
    label: str
    path: Path
    family: str


def rp(*parts: str) -> Path:
    return reports_root().joinpath(*parts)


WAVE_CANDIDATES = [
    CandidateSource(
        "wave_learned_sector_h5",
        "波段模板 learned_sector h5",
        rp("wave_style_template_strategy_backtest_v1", "learned_sector_h5", "candidates.csv"),
        "wave_template",
    ),
    CandidateSource(
        "wave_learned_sector_h10",
        "波段模板 learned_sector h10",
        rp("wave_style_template_strategy_backtest_v1", "learned_sector_h10", "candidates.csv"),
        "wave_template",
    ),
    CandidateSource(
        "wave_learned_sector_h20",
        "波段模板 learned_sector h20",
        rp("wave_style_template_strategy_backtest_v1", "learned_sector_h20", "candidates.csv"),
        "wave_template",
    ),
    CandidateSource(
        "wave_trend_h10",
        "趋势模板 learned_sector_trend h10",
        rp("wave_style_template_strategy_backtest_v1", "learned_sector_trend_h10", "candidates.csv"),
        "trend_template",
    ),
    CandidateSource(
        "wave_trend_h20",
        "趋势模板 learned_sector_trend h20",
        rp("wave_style_template_strategy_backtest_v1", "learned_sector_trend_h20", "candidates.csv"),
        "trend_template",
    ),
    CandidateSource(
        "wave_bigwave_h10",
        "大波段模板 learned_sector_bigwave h10",
        rp("wave_style_template_strategy_backtest_v1", "learned_sector_bigwave_h10", "candidates.csv"),
        "bigwave_template",
    ),
    CandidateSource(
        "wave_bigwave_h20",
        "大波段模板 learned_sector_bigwave h20",
        rp("wave_style_template_strategy_backtest_v1", "learned_sector_bigwave_h20", "candidates.csv"),
        "bigwave_template",
    ),
    CandidateSource(
        "wave_strict_h20",
        "严格主线模板 learned_sector_strict h20",
        rp("wave_style_template_strategy_backtest_v1", "learned_sector_strict_h20", "candidates.csv"),
        "strict_template",
    ),
]


def read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False, **kwargs)


def qbucket(s: pd.Series, bins: Iterable[float], labels: list[str]) -> pd.Series:
    return pd.cut(pd.to_numeric(s, errors="coerce"), bins=list(bins), labels=labels, include_lowest=True)


def load_wave_candidates(source: CandidateSource) -> pd.DataFrame:
    usecols = [
        "code",
        "stock_name",
        "industry",
        "l2_sector_name",
        "template_label",
        "trade_date",
        "entry_date",
        "policy_exit_date",
        "net_ret",
        "gross_ret",
        "hold_days",
        "rank_key",
        "wave_style_score",
        "learned_count",
        "learned_avg_return",
        "index_mom20",
        "index_mom60",
        "amount_rank",
        "capacity_score",
        "trend_score",
        "position_score",
        "volume_score",
        "acceleration_score",
        "learned_sector_score",
        "climax_penalty",
        "range_pos120",
        "close_to_high60",
        "amount_ratio20_60",
        "amount5_20",
        "ret5",
        "ret20",
        "limit_up_days20",
        "big_up_days20",
        "max_dd20",
        "above_ma20",
        "above_ma60",
    ]
    df = read_csv(source.path, usecols=lambda c: c in usecols)
    if df.empty:
        return df
    df["source_key"] = source.key
    df["source_label"] = source.label
    df["family"] = source.family
    df["entry_date_norm"] = pd.to_datetime(df["entry_date"], errors="coerce")
    df["ret_norm"] = pd.to_numeric(df["net_ret"], errors="coerce")
    df = df[(df["entry_date_norm"] >= START) & (df["entry_date_norm"] <= PRE_END) & df["ret_norm"].notna()].copy()
    if df.empty:
        return df
    df["year"] = df["entry_date_norm"].dt.year
    df["rank_pct_in_day"] = (
        df.groupby(["source_key", "entry_date_norm"])["rank_key"]
        .rank(ascending=False, pct=True, method="first")
        .astype(float)
    )
    df["rank_bucket"] = qbucket(
        df["rank_pct_in_day"],
        [0, 0.01, 0.03, 0.05, 0.1, 0.25, 1.0],
        ["top1%", "top3%", "top5%", "top10%", "top25%", "tail"],
    )
    df["index_mom20_bucket"] = qbucket(
        df["index_mom20"],
        [-2, -0.08, -0.03, 0.0, 0.03, 0.08, 2],
        ["mom20_crash", "mom20_down", "mom20_flat_neg", "mom20_flat_pos", "mom20_up", "mom20_hot"],
    )
    df["index_mom60_bucket"] = qbucket(
        df["index_mom60"],
        [-2, -0.08, -0.03, 0.0, 0.03, 0.08, 2],
        ["mom60_crash", "mom60_down", "mom60_flat_neg", "mom60_flat_pos", "mom60_up", "mom60_hot"],
    )
    df["range_pos120_bucket"] = qbucket(
        df["range_pos120"],
        [-1, 0.25, 0.5, 0.75, 0.9, 1.01, 3],
        ["low", "mid_low", "mid_high", "near_high", "new_high", "extreme"],
    )
    df["ret20_bucket"] = qbucket(
        df["ret20"],
        [-3, -0.15, -0.05, 0.0, 0.08, 0.2, 3],
        ["ret20_deep_down", "ret20_down", "ret20_flat_neg", "ret20_flat_pos", "ret20_up", "ret20_hot"],
    )
    df["max_dd20_bucket"] = qbucket(
        df["max_dd20"],
        [-3, -0.30, -0.18, -0.10, -0.05, 0.01],
        ["dd20_deep", "dd20_large", "dd20_mid", "dd20_small", "dd20_tight"],
    )
    df["amount_ratio_bucket"] = qbucket(
        df["amount_ratio20_60"],
        [-1, 0.8, 1.0, 1.3, 1.8, 100],
        ["amount_shrink", "amount_neutral", "amount_warm", "amount_hot", "amount_extreme"],
    )
    df["amount5_20_bucket"] = qbucket(
        df["amount5_20"],
        [-1, 0.8, 1.0, 1.3, 1.8, 100],
        ["recent_shrink", "recent_neutral", "recent_warm", "recent_hot", "recent_extreme"],
    )
    df["learned_avg_bucket"] = qbucket(
        df["learned_avg_return"],
        [-3, -0.05, 0.0, 0.05, 0.12, 0.25, 3],
        ["learned_bad", "learned_flat_neg", "learned_flat_pos", "learned_good", "learned_strong", "learned_extreme"],
    )
    df["climax_bucket"] = qbucket(
        df["climax_penalty"],
        [-1, 0.01, 3, 6, 10, 100],
        ["no_climax", "low_climax", "mid_climax", "high_climax", "extreme_climax"],
    )
    return df


def metric_row(df: pd.DataFrame, label: str, key: str, group_col: str = "", group_value: str = "") -> dict:
    ret = pd.to_numeric(df["ret_norm"], errors="coerce").dropna()
    years = df.loc[ret.index, "year"] if "year" in df.columns else pd.Series(dtype=int)
    by_year = df.groupby("year")["ret_norm"].agg(["count", "mean"]) if "year" in df.columns else pd.DataFrame()
    positive_years = int((by_year["mean"] > 0).sum()) if not by_year.empty else 0
    min_year_count = int(by_year["count"].min()) if not by_year.empty else 0
    total_pnl_proxy = float(ret.sum()) if len(ret) else np.nan
    pos = ret[ret > 0].sort_values(ascending=False)
    top5_share = float(pos.head(5).sum() / pos.sum()) if pos.sum() > 0 else np.nan
    diagnosis = "交易数太少" if len(ret) < 20 else (
        "单笔收益太低" if ret.mean() < 0.005 or (ret > 0).mean() < 0.5 else (
            "年度稳定性不足" if positive_years < 3 else (
                "收益集中度偏高" if pd.notna(top5_share) and top5_share > 0.5 else "可继续建模"
            )
        )
    )
    return {
        "key": key,
        "label": label,
        "group_col": group_col,
        "group_value": group_value,
        "trade_count": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else np.nan,
        "avg_ret": float(ret.mean()) if len(ret) else np.nan,
        "median_ret": float(ret.median()) if len(ret) else np.nan,
        "sum_ret_proxy": total_pnl_proxy,
        "worst_ret": float(ret.min()) if len(ret) else np.nan,
        "best_ret": float(ret.max()) if len(ret) else np.nan,
        "positive_years": positive_years,
        "min_year_count": min_year_count,
        "top5_positive_share": top5_share,
        "diagnosis": diagnosis,
        "years": ",".join(str(int(y)) for y in sorted(years.dropna().unique())) if len(years) else "",
    }


def group_scan(df: pd.DataFrame, source: CandidateSource) -> pd.DataFrame:
    group_cols = [
        "template_label",
        "l2_sector_name",
        "rank_bucket",
        "index_mom20_bucket",
        "index_mom60_bucket",
        "range_pos120_bucket",
        "ret20_bucket",
        "max_dd20_bucket",
        "amount_ratio_bucket",
        "amount5_20_bucket",
        "learned_avg_bucket",
        "climax_bucket",
    ]
    rows = [metric_row(df, source.label, source.key, "ALL", "ALL")]
    for col in group_cols:
        if col not in df.columns:
            continue
        for value, g in df.groupby(col, dropna=False, observed=False):
            if len(g) < 20:
                continue
            rows.append(metric_row(g, source.label, source.key, col, str(value)))
    return pd.DataFrame(rows)


def combo_scan(df: pd.DataFrame, source: CandidateSource) -> pd.DataFrame:
    combos = [
        ("template_label", "index_mom60_bucket"),
        ("template_label", "rank_bucket"),
        ("template_label", "range_pos120_bucket"),
        ("template_label", "ret20_bucket"),
        ("template_label", "amount5_20_bucket"),
        ("index_mom60_bucket", "ret20_bucket"),
        ("index_mom60_bucket", "range_pos120_bucket"),
        ("rank_bucket", "ret20_bucket"),
        ("rank_bucket", "climax_bucket"),
        ("l2_sector_name", "template_label"),
    ]
    rows: list[dict] = []
    for a, b in combos:
        if a not in df.columns or b not in df.columns:
            continue
        for (va, vb), g in df.groupby([a, b], dropna=False, observed=False):
            if len(g) < 30:
                continue
            rows.append(metric_row(g, source.label, source.key, f"{a}+{b}", f"{va}|{vb}"))
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out = out.sort_values(["diagnosis", "avg_ret", "trade_count"], ascending=[True, False, False])
    return out


def simulate_daily_top(df: pd.DataFrame, source: CandidateSource, variant: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    d = df.copy()
    if variant == "top1":
        d = d[d["rank_pct_in_day"] <= 0.01]
    elif variant == "top3":
        d = d[d["rank_pct_in_day"] <= 0.03]
    elif variant == "top1_no_climax":
        d = d[(d["rank_pct_in_day"] <= 0.01) & (pd.to_numeric(d["climax_penalty"], errors="coerce").fillna(0) <= 3)]
    elif variant == "top1_mom60_not_hot":
        d = d[(d["rank_pct_in_day"] <= 0.01) & (pd.to_numeric(d["index_mom60"], errors="coerce").fillna(0) <= 0.05)]
    elif variant == "top1_pullback_not_chase":
        d = d[
            (d["rank_pct_in_day"] <= 0.01)
            & (pd.to_numeric(d["ret20"], errors="coerce").between(-0.05, 0.20))
            & (pd.to_numeric(d["range_pos120"], errors="coerce") <= 0.95)
        ]
    else:
        pass
    if d.empty:
        return pd.DataFrame(), pd.DataFrame(), metric_row(d, f"{source.label} {variant}", f"{source.key}_{variant}")
    d = d.sort_values(["entry_date_norm", "rank_key"], ascending=[True, False])
    picks = d.groupby("entry_date_norm", as_index=False).head(1).copy()
    # Closed-trade proxy: one slot, no overlapping positions. This intentionally
    # tests whether the pattern itself has enough edge before a full engine.
    closed: list[dict] = []
    equity_rows: list[dict] = []
    equity = 1.0
    next_free = pd.Timestamp.min
    for _, row in picks.iterrows():
        entry = row["entry_date_norm"]
        if entry < next_free:
            continue
        ret = float(row["ret_norm"])
        equity *= max(0.01, 1.0 + ret)
        rec = row.to_dict()
        rec["variant"] = variant
        rec["proxy_equity"] = equity
        closed.append(rec)
        exit_date = pd.to_datetime(row.get("policy_exit_date", pd.NaT), errors="coerce")
        if pd.isna(exit_date):
            exit_date = entry + pd.Timedelta(days=int(row.get("hold_days", 10)) + 3)
        next_free = exit_date
        equity_rows.append({"date": entry.date().isoformat(), "equity": equity, "ret_from_start": equity - 1.0})
    closed_df = pd.DataFrame(closed)
    curve_df = pd.DataFrame(equity_rows)
    if not curve_df.empty:
        curve_df["peak"] = curve_df["equity"].cummax()
        curve_df["drawdown"] = curve_df["equity"] / curve_df["peak"] - 1.0
    metrics = metric_row(closed_df, f"{source.label} {variant}", f"{source.key}_{variant}")
    metrics["proxy_total_return"] = float(equity - 1.0)
    metrics["proxy_max_drawdown"] = float(curve_df["drawdown"].min()) if not curve_df.empty else np.nan
    return closed_df, curve_df, metrics


def load_router_evidence() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base = rp("gen3_state_router_optimization_audit_v1")
    bear = read_csv(base / "bear_mode_library_audit.csv")
    old = read_csv(base / "old_g3_route_efficiency.csv")
    panic = read_csv(base / "panic_repair_context_buckets.csv")
    return bear, old, panic


def load_previous_v1() -> pd.DataFrame:
    return read_csv(rp("gen3_pre_2024_10_strategy_research_v1", "source_summary.csv"))


def pct(x: float | int | None) -> str:
    if x is None or pd.isna(x):
        return ""
    return f"{float(x):.2%}"


def markdown_table(df: pd.DataFrame, cols: list[str], n: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    d = df[cols].copy()
    if n:
        d = d.head(n)
    for col in d.columns:
        if pd.api.types.is_float_dtype(d[col]):
            if any(k in col for k in ["ret", "rate", "drawdown", "share", "worst", "best"]):
                d[col] = d[col].map(pct)
            else:
                d[col] = d[col].map(lambda x: "" if pd.isna(x) else f"{x:.4f}")
        else:
            d[col] = d[col].map(lambda x: "" if pd.isna(x) else str(x).replace("|", "\\|"))
    return d.to_markdown(index=False)


def render_report(
    source_summary: pd.DataFrame,
    segment_summary: pd.DataFrame,
    combo_summary: pd.DataFrame,
    proxy_summary: pd.DataFrame,
    bear: pd.DataFrame,
    old: pd.DataFrame,
    panic: pd.DataFrame,
    v1: pd.DataFrame,
) -> str:
    stable_segments = segment_summary[
        (segment_summary["diagnosis"].eq("可继续建模"))
        & (segment_summary["positive_years"] >= 3)
        & (segment_summary["trade_count"] >= 50)
    ].sort_values(["avg_ret", "trade_count"], ascending=[False, False])
    stable_combos = combo_summary[
        (combo_summary["diagnosis"].eq("可继续建模"))
        & (combo_summary["positive_years"] >= 3)
        & (combo_summary["trade_count"] >= 50)
    ].sort_values(["avg_ret", "trade_count"], ascending=[False, False])
    proxy_rank = proxy_summary.sort_values("proxy_total_return", ascending=False)
    lines: list[str] = []
    lines.append("# G3 2024-10 前挣钱模式第二阶段探索 v2")
    lines.append("")
    lines.append("## 研究目标")
    lines.append("")
    lines.append("本轮继续探索 2024-10 前旧市场的赚钱结构，不改当前正式/影子交易链路。重点不再是拼已有 G3 路由，而是从大样本候选池和已有状态审计中寻找可迁移子模式。")
    lines.append("")
    lines.append("## 新结论")
    lines.append("")
    lines.append("1. 旧市场不是没有进攻模式，但进攻模式不是“机构主升常开”，也不是直接买模板分数第一名。更接近的是“行业/板块 × 模板标签 × 市场状态”的短中周期题材波段口袋。")
    lines.append("2. 2020-2024/09 真正可继续建模的旧市场核心仍是两条：`panic_repair` 与旧 `old_g3_route_v3` 的窄子路由；泛 `range_weak_rebound` 和单独 `ice/true_floor reclaim` 继续被反证。")
    lines.append("3. 波段/题材模板的大样本候选池里存在正期望分层，但日度 top 代理回放多数失效，说明旧市场需要重新排序候选，而不是沿用当前 `rank_key`。")
    lines.append("4. 对 2024-10 前的 G3，下一步不应继续增加熊市补位交易数，而应把旧 `old_g3_route_v3`、panic 场景、行业/模板口袋重写成统一的状态 alpha 候选源。")
    lines.append("")
    lines.append("## 与第一阶段结论的关系")
    lines.append("")
    if not v1.empty:
        pre = v1[v1["window"].eq("pre_2024_10")].copy()
        pre = pre[pre["source_key"].isin(["panic_repair_m30_close5_cost30", "market_state_router_existing", "range_filtered_candidate_v2", "range_v3_weak_low_not_chasing_h5", "range_v3_true_floor_reclaim_h3", "range_v2_ice_reclaim_top1"])]
        lines.append(markdown_table(pre, ["source_label", "trade_count", "win_rate", "avg_trade_return", "total_return", "max_drawdown", "diagnosis"]))
    lines.append("")
    lines.append("第一阶段说明“哪些大路线不能粗暴打开”；第二阶段进一步说明“旧市场有效 alpha 应从更窄的子模式重建”。")
    lines.append("")
    lines.append("## 已有 G3 状态证据再确认")
    lines.append("")
    lines.append("### 熊市/弱市模式库")
    if not bear.empty:
        lines.append(markdown_table(bear, ["mode", "bear_trades", "bear_avg_ret", "bear_win_rate", "bear_big_loss_rate", "bear_worst_trade", "years"]))
    lines.append("")
    lines.append("这里最重要的是：`panic_repair` 与 `old_g3_route_v3` 在弱市/熊市仍有正均值；`range_weak_rebound` 是负均值。也就是说旧市场不是缺低吸，而是缺“更窄的低吸定义”。")
    lines.append("")
    lines.append("### 旧 G3 子路由效率")
    if not old.empty:
        lines.append(markdown_table(old, ["route", "trades", "avg_ret", "win_rate", "worst_trade", "best_trade"]))
    lines.append("")
    lines.append("旧 G3 里 `down_panic` 和 `range_gap` 都比泛 range 可靠，`strong_main` 单笔弱，说明旧市场强势路线要换成题材/板块模板，而不是沿用机构主升。")
    lines.append("")
    lines.append("### panic 场景")
    if not panic.empty:
        lines.append(markdown_table(panic, ["market_style_prev", "ma_skeleton", "adx_layer", "trades", "avg_ret", "win_rate", "worst_trade"], 12))
    lines.append("")
    lines.append("panic 最好的子场景是标准震荡或标准下跌中的混合/熊排列修复；`weak_repair + downtrend/uptrend` 子场景反而容易失败。")
    lines.append("")
    lines.append("## 波段/题材候选池大样本分层")
    lines.append("")
    lines.append("### 单字段稳定子模式")
    lines.append(markdown_table(stable_segments, ["label", "group_col", "group_value", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_ret", "positive_years", "diagnosis"], 40))
    lines.append("")
    lines.append("### 双字段稳定子模式")
    lines.append(markdown_table(stable_combos, ["label", "group_col", "group_value", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_ret", "positive_years", "diagnosis"], 50))
    lines.append("")
    lines.append("解释：这些表不是最终策略，只说明候选池中哪些条件有旧市场 alpha。高质量条件更偏行业/模板口袋，例如电池、光伏、乘用车、酿酒等在特定模板标签下的阶段性波段；这些口袋必须再做无未来函数滚动学习，不能把全样本行业名单直接写死。")
    lines.append("")
    lines.append("## 日度 top 候选代理回放")
    lines.append("")
    lines.append(markdown_table(proxy_rank, ["label", "trade_count", "win_rate", "avg_ret", "median_ret", "proxy_total_return", "proxy_max_drawdown", "worst_ret", "positive_years", "diagnosis"], 40))
    lines.append("")
    lines.append("代理回放是单槽、不重叠、每天最多取 1 笔的粗验证；结果反而是重要反证：大多数 top1/top3 版本回撤很深或单笔收益不足，当前模板 `rank_key` 不能直接作为旧市场买入排序。")
    lines.append("")
    lines.append("## 当前更充分的模式图谱")
    lines.append("")
    lines.append("- 模式 A：情绪恐慌修复。核心是 panic_repair，适合标准下跌/震荡后的极端释放，持仓短，尾部相对可控。")
    lines.append("- 模式 B：旧 G3 down_panic/range_gap。不是泛低吸，而是恐慌或跳空/箱体修复后的有限子路由。")
    lines.append("- 模式 C：行业/板块短中波段口袋。旧市场候选池有 alpha，但需要按行业、模板标签、市场状态重新学习排序，不能等同 2024-10 后机构主升，也不能直接买全局 top1。")
    lines.append("- 模式 D：箱体/主线小冰点。当前证据仍不足，单独大样本均值弱，只能作为确认因子，不适合作为主信号。")
    lines.append("- 模式 E：Alpha191/通用因子层。本轮未找到足够直接覆盖 G3 的稳定闭合产物，暂不作为主路径。")
    lines.append("")
    lines.append("## 可迁移路线")
    lines.append("")
    lines.append("1. 重建一个 `pre_2024_state_alpha` 研究源，不碰 shadow：输入由 panic_repair、old_g3_route_v3 的 down_panic/range_gap、行业/模板口袋三类组成。")
    lines.append("2. 每一类单独做无未来函数日度候选，再用滚动 240 日健康度打开/关闭，而不是按硬日期切换。")
    lines.append("3. 对题材模板模式先做滚动行业/模板学习，再叠加旧市场保护：指数 60 日不过热、个股 20 日不过度高潮；这些保护不能单独替代重排模型。")
    lines.append("4. 对 range 类保留“不追高/弱修复低吸”原过滤，禁止直接打开 true_floor/ice_reclaim 全量。")
    lines.append("5. 下一步必须做真正组合级回放：单日限 1、最多 3-4 槽、30m/日线退出、按年度独立压力测试。")
    lines.append("")
    lines.append("## 产物")
    lines.append("")
    lines.append("- `source_summary.csv`：各候选池整体统计。")
    lines.append("- `segment_summary.csv`：单字段分层。")
    lines.append("- `combo_segment_summary.csv`：双字段分层。")
    lines.append("- `daily_top_proxy_summary.csv`：日度 top 候选代理回放。")
    lines.append("- `daily_top_proxy_*`：对应闭合样本和代理曲线。")
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_sources: list[pd.DataFrame] = []
    source_rows: list[dict] = []
    segment_rows: list[pd.DataFrame] = []
    combo_rows: list[pd.DataFrame] = []
    proxy_rows: list[dict] = []
    for source in WAVE_CANDIDATES:
        df = load_wave_candidates(source)
        if df.empty:
            continue
        all_sources.append(df)
        source_rows.append(metric_row(df, source.label, source.key, "ALL", "ALL"))
        seg = group_scan(df, source)
        if not seg.empty:
            segment_rows.append(seg)
        combo = combo_scan(df, source)
        if not combo.empty:
            combo_rows.append(combo)
        for variant in ["top1", "top3", "top1_no_climax", "top1_mom60_not_hot", "top1_pullback_not_chase"]:
            closed, curve, metrics = simulate_daily_top(df, source, variant)
            proxy_rows.append(metrics)
            if not closed.empty:
                safe = f"{source.key}_{variant}"
                closed.to_csv(OUT_DIR / f"daily_top_proxy_{safe}_closed.csv", index=False, encoding="utf-8-sig")
                curve.to_csv(OUT_DIR / f"daily_top_proxy_{safe}_curve.csv", index=False, encoding="utf-8-sig")
    source_summary = pd.DataFrame(source_rows).sort_values(["avg_ret", "trade_count"], ascending=[False, False])
    segment_summary = pd.concat(segment_rows, ignore_index=True) if segment_rows else pd.DataFrame()
    combo_summary = pd.concat(combo_rows, ignore_index=True) if combo_rows else pd.DataFrame()
    proxy_summary = pd.DataFrame(proxy_rows)
    bear, old, panic = load_router_evidence()
    v1 = load_previous_v1()
    source_summary.to_csv(OUT_DIR / "source_summary.csv", index=False, encoding="utf-8-sig")
    segment_summary.to_csv(OUT_DIR / "segment_summary.csv", index=False, encoding="utf-8-sig")
    combo_summary.to_csv(OUT_DIR / "combo_segment_summary.csv", index=False, encoding="utf-8-sig")
    proxy_summary.to_csv(OUT_DIR / "daily_top_proxy_summary.csv", index=False, encoding="utf-8-sig")
    manifest = {
        "start": str(START.date()),
        "pre_end": str(PRE_END.date()),
        "sources": [{"key": s.key, "path": str(s.path), "label": s.label} for s in WAVE_CANDIDATES],
        "out_dir": str(OUT_DIR),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report = render_report(source_summary, segment_summary, combo_summary, proxy_summary, bear, old, panic, v1)
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()

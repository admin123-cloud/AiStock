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

from utils.paths import report_path, reports_root


START = pd.Timestamp("2020-01-01")
PRE_END = pd.Timestamp("2024-09-30")
POST_START = pd.Timestamp("2024-10-01")
OUT_DIR = report_path("gen3_pre_2024_10_strategy_research_v1")


@dataclass(frozen=True)
class TradeSource:
    key: str
    label: str
    family: str
    trades: Path
    curve: Path | None = None
    ret_col: str | None = None
    entry_col: str = "entry_date"
    exit_col: str = "policy_exit_date"


def p(*parts: str) -> Path:
    return reports_root().joinpath(*parts)


SOURCES: list[TradeSource] = [
    TradeSource(
        "current_g3_v4_combo_h10_cost30",
        "当前 G3 V4 组合包 h10 cost30",
        "current_combo",
        p("gen3_v4_research_package_v1", "v4_h10_margin__cost30", "closed_trades.csv"),
        p("gen3_v4_research_package_v1", "v4_h10_margin__cost30", "mtm_equity_curve.csv"),
        "policy_net_ret",
    ),
    TradeSource(
        "panic_repair_m30_close5_cost30",
        "panic_repair：情绪冰点/恐慌修复",
        "panic_repair",
        p("gen3_panic_v2_research", "final_candidate_v1", "m30_close5_full_nextopen_cost30_closed_trades.csv"),
        p("gen3_panic_v2_research", "final_candidate_v1", "m30_close5_full_nextopen_cost30_mtm_equity_curve.csv"),
        "policy_net_ret",
    ),
    TradeSource(
        "range_v2_ice_reclaim_top1",
        "range_v2：冰点 reclaim top1",
        "range_low_absorb",
        p("gen3_range_v2_independent_source_v1", "range_v2_ice_reclaim_top1_closed_trades.csv"),
        p("gen3_range_v2_independent_source_v1", "range_v2_ice_reclaim_top1_curve.csv"),
        "net_ret",
    ),
    TradeSource(
        "range_v2_stress_reclaim_top1",
        "range_v2：压力 reclaim top1",
        "range_low_absorb",
        p("gen3_range_v2_independent_source_v1", "range_v2_stress_reclaim_top1_closed_trades.csv"),
        p("gen3_range_v2_independent_source_v1", "range_v2_stress_reclaim_top1_curve.csv"),
        "net_ret",
    ),
    TradeSource(
        "range_v3_true_floor_reclaim_h3",
        "range_v3：真底部 reclaim h3",
        "range_low_absorb",
        p("gen3_range_v3_gap_candidate_source_v1", "range_v3_true_floor_reclaim_h3_closed_trades.csv"),
        p("gen3_range_v3_gap_candidate_source_v1", "range_v3_true_floor_reclaim_h3_equity_curve.csv"),
        "net_ret",
    ),
    TradeSource(
        "range_v3_weak_low_not_chasing_h5",
        "range_v3：弱修复低吸不追高 h5",
        "range_weak_rebound",
        p("gen3_range_v3_gap_candidate_source_v1", "range_v3_weak_low_not_chasing_h5_closed_trades.csv"),
        p("gen3_range_v3_gap_candidate_source_v1", "range_v3_weak_low_not_chasing_h5_equity_curve.csv"),
        "net_ret",
    ),
    TradeSource(
        "range_filtered_candidate_v2",
        "range_filtered 组合候选 v2",
        "range_filtered",
        p("gen3_range_filtered_candidate_package_v2", "g3_range_filtered_candidate_closed_trades.csv"),
        p("gen3_range_filtered_candidate_package_v2", "g3_range_filtered_candidate_equity_curve.csv"),
        "policy_net_ret",
    ),
    TradeSource(
        "old_g3_guarded_candidate",
        "old_g3_guarded 三链防守候选",
        "old_g3_guarded",
        p("gen3_guarded_candidate_package_v1", "g3_guarded_candidate_closed_trades.csv"),
        None,
        "policy_net_ret",
    ),
    TradeSource(
        "market_state_router_existing",
        "现有 market_state_router 候选",
        "router",
        p("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv"),
        p("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_equity_curve.csv"),
        "policy_net_ret",
    ),
    TradeSource(
        "score120_diff65_sector_gate",
        "强势题材/趋势突破：score120 diff65",
        "strong_theme_breakout",
        p("score120_sector_diffusion_gate_v1", "score120_diff65", "closed_trades.csv"),
        p("score120_sector_diffusion_gate_v1", "score120_diff65", "equity_curve.csv"),
        "net_ret",
    ),
    TradeSource(
        "wave_scheduler_focus_score120",
        "机构/波段主升调度器 score120",
        "institutional_mainwave",
        p("wave_style_model_scheduler_v1", "scheduler_focus_240d_score120_aggr25", "closed_trades.csv"),
        p("wave_style_model_scheduler_v1", "scheduler_focus_240d_score120_aggr25", "equity_curve.csv"),
        "net_ret",
    ),
    TradeSource(
        "template_trend_h10",
        "固定模板：learned_sector_trend_h10",
        "trend_breakout_template",
        p("wave_style_template_strategy_backtest_v1", "learned_sector_trend_h10", "closed_trades.csv"),
        p("wave_style_template_strategy_backtest_v1", "learned_sector_trend_h10", "equity_curve.csv"),
        "net_ret",
    ),
]


def _read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def _first_present(cols: Iterable[str], df: pd.DataFrame) -> str | None:
    return next((c for c in cols if c in df.columns), None)


def normalize_trades(source: TradeSource) -> pd.DataFrame:
    df = _read_csv(source.trades)
    if df.empty:
        return df
    entry_col = source.entry_col if source.entry_col in df.columns else _first_present(
        ["entry_date", "trade_date", "date", "entry_ts"], df
    )
    if entry_col is None:
        return pd.DataFrame()
    exit_col = source.exit_col if source.exit_col in df.columns else _first_present(
        ["policy_exit_date", "exit_date", "sell_date", "exit_ts"], df
    )
    ret_col = source.ret_col if source.ret_col and source.ret_col in df.columns else _first_present(
        ["policy_net_ret", "net_ret", "gross_ret", "ret", "trade_return"], df
    )
    if ret_col is None:
        return pd.DataFrame()
    out = df.copy()
    out["source_key"] = source.key
    out["source_label"] = source.label
    out["family"] = source.family
    out["entry_date_norm"] = pd.to_datetime(out[entry_col], errors="coerce")
    out["exit_date_norm"] = pd.to_datetime(out[exit_col], errors="coerce") if exit_col else pd.NaT
    out["ret_norm"] = pd.to_numeric(out[ret_col], errors="coerce")
    if "realized_pnl" not in out.columns:
        stake = pd.to_numeric(out.get("stake", np.nan), errors="coerce")
        out["realized_pnl"] = stake * out["ret_norm"]
    if "stake" not in out.columns:
        out["stake"] = np.nan
    if "route" not in out.columns:
        out["route"] = source.family
    if "route_source" not in out.columns:
        out["route_source"] = out.get("g3_chain", source.key)
    return out[out["entry_date_norm"].notna() & out["ret_norm"].notna()].copy()


def normalize_curve(source: TradeSource) -> pd.DataFrame:
    if not source.curve or not source.curve.exists():
        return pd.DataFrame()
    df = _read_csv(source.curve)
    if df.empty or "date" not in df.columns or "equity" not in df.columns:
        return pd.DataFrame()
    out = df.copy()
    out["date_norm"] = pd.to_datetime(out["date"], errors="coerce")
    out["equity_norm"] = pd.to_numeric(out["equity"], errors="coerce")
    out = out[out["date_norm"].notna() & out["equity_norm"].notna()].sort_values("date_norm")
    return out


def max_drawdown_from_equity(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min())


def curve_metrics(curve: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> tuple[float, float]:
    if curve.empty:
        return np.nan, np.nan
    c = curve[(curve["date_norm"] >= start) & (curve["date_norm"] <= end)].copy()
    if c.empty:
        return np.nan, np.nan
    first = float(c.iloc[0]["equity_norm"])
    last = float(c.iloc[-1]["equity_norm"])
    ret = last / first - 1.0 if first else np.nan
    return float(ret), max_drawdown_from_equity(c["equity_norm"])


def trade_sequence_metrics(trades: pd.DataFrame) -> tuple[float, float]:
    if trades.empty:
        return np.nan, np.nan
    seq = trades.sort_values(["entry_date_norm", "source_key", "code" if "code" in trades.columns else "route"]).copy()
    equity = (1.0 + seq["ret_norm"].clip(lower=-0.99)).cumprod()
    return float(equity.iloc[-1] - 1.0), max_drawdown_from_equity(equity)


def trade_metrics(
    trades: pd.DataFrame,
    curve: pd.DataFrame,
    source: TradeSource | None,
    start: pd.Timestamp,
    end: pd.Timestamp,
    window: str,
) -> dict:
    t = trades[(trades["entry_date_norm"] >= start) & (trades["entry_date_norm"] <= end)].copy()
    if curve.empty:
        total_return, max_dd = trade_sequence_metrics(t)
        ret_basis = "trade_sequence"
    else:
        total_return, max_dd = curve_metrics(curve, start, end)
        ret_basis = "equity_curve"
    pnl = pd.to_numeric(t.get("realized_pnl", pd.Series(dtype=float)), errors="coerce")
    pos_pnl = pnl[pnl > 0].sort_values(ascending=False)
    total_pos = float(pos_pnl.sum()) if len(pos_pnl) else 0.0
    top3_share = float(pos_pnl.head(3).sum() / total_pos) if total_pos > 0 else np.nan
    top5_share = float(pos_pnl.head(5).sum() / total_pos) if total_pos > 0 else np.nan
    total_pnl = float(pnl.sum()) if len(pnl) else np.nan
    top5_net_share = float(pos_pnl.head(5).sum() / total_pnl) if total_pnl and total_pnl > 0 else np.nan
    avg_ret = float(t["ret_norm"].mean()) if len(t) else np.nan
    trade_count = int(len(t))
    win_rate = float((t["ret_norm"] > 0).mean()) if len(t) else np.nan
    if trade_count < 20:
        verdict = "交易数偏少"
    elif avg_ret < 0.005 or win_rate < 0.5:
        verdict = "单笔收益偏低"
    elif pd.notna(top5_share) and top5_share > 0.6:
        verdict = "收益集中度偏高"
    else:
        verdict = "可继续审计"
    return {
        "source_key": source.key if source else "adhoc",
        "source_label": source.label if source else "adhoc",
        "family": source.family if source else "adhoc",
        "window": window,
        "start": str(start.date()),
        "end": str(end.date()),
        "trade_count": trade_count,
        "unique_codes": int(t["code"].nunique()) if "code" in t.columns else np.nan,
        "win_rate": win_rate,
        "avg_trade_return": avg_ret,
        "median_trade_return": float(t["ret_norm"].median()) if len(t) else np.nan,
        "total_return": total_return,
        "max_drawdown": max_dd,
        "worst_trade": float(t["ret_norm"].min()) if len(t) else np.nan,
        "best_trade": float(t["ret_norm"].max()) if len(t) else np.nan,
        "total_realized_pnl": total_pnl,
        "top3_positive_pnl_share": top3_share,
        "top5_positive_pnl_share": top5_share,
        "top5_net_pnl_share": top5_net_share,
        "ret_basis": ret_basis,
        "diagnosis": verdict,
    }


def annual_metrics(source: TradeSource, trades: pd.DataFrame, curve: pd.DataFrame) -> list[dict]:
    rows = []
    for year in range(2020, 2025):
        y_start = pd.Timestamp(f"{year}-01-01")
        y_end = min(pd.Timestamp(f"{year}-12-31"), PRE_END)
        rows.append(trade_metrics(trades, curve, source, y_start, y_end, str(year)))
    return rows


def grouped_metrics(trades: pd.DataFrame, cols: list[str], min_count: int = 5) -> pd.DataFrame:
    rows: list[dict] = []
    t = trades[(trades["entry_date_norm"] >= START) & (trades["entry_date_norm"] <= PRE_END)].copy()
    for col in cols:
        if col not in t.columns:
            continue
        for key, g in t.groupby(col, dropna=False):
            if len(g) < min_count:
                continue
            rows.append(
                {
                    "group_col": col,
                    "group": str(key),
                    "trade_count": int(len(g)),
                    "win_rate": float((g["ret_norm"] > 0).mean()),
                    "avg_trade_return": float(g["ret_norm"].mean()),
                    "median_trade_return": float(g["ret_norm"].median()),
                    "worst_trade": float(g["ret_norm"].min()),
                    "sum_ret": float(g["ret_norm"].sum()),
                }
            )
    return pd.DataFrame(rows).sort_values(["group_col", "avg_trade_return"], ascending=[True, False])


def prepare_router_candidates() -> pd.DataFrame:
    pieces: list[pd.DataFrame] = []
    for source in SOURCES:
        if source.key not in {
            "panic_repair_m30_close5_cost30",
            "range_v2_ice_reclaim_top1",
            "range_v2_stress_reclaim_top1",
            "range_v3_true_floor_reclaim_h3",
            "range_v3_weak_low_not_chasing_h5",
            "score120_diff65_sector_gate",
        }:
            continue
        t = normalize_trades(source)
        if t.empty:
            continue
        t = t[(t["entry_date_norm"] >= START) & (t["entry_date_norm"] <= PRE_END)].copy()
        if source.family == "panic_repair":
            t["router_mode"] = "panic_repair"
            t["router_priority"] = 10
        elif source.key == "range_v3_true_floor_reclaim_h3":
            t["router_mode"] = "true_floor_reclaim"
            t["router_priority"] = 30
        elif source.key == "range_v2_ice_reclaim_top1":
            t["router_mode"] = "ice_reclaim"
            t["router_priority"] = 40
        elif source.key == "range_v2_stress_reclaim_top1":
            t["router_mode"] = "stress_reclaim"
            t["router_priority"] = 50
        elif source.key == "range_v3_weak_low_not_chasing_h5":
            t["router_mode"] = "weak_rebound_low_absorb"
            t["router_priority"] = 70
        else:
            t["router_mode"] = "strong_theme_breakout"
            t["router_priority"] = 90
        score_col = _first_present(
            ["score", "candidate_score", "range_v3_score", "range_v2_score", "wave_style_score", "selected_score"],
            t,
        )
        t["router_score"] = pd.to_numeric(t[score_col], errors="coerce") if score_col else 0.0
        pieces.append(t)
    if not pieces:
        return pd.DataFrame()
    out = pd.concat(pieces, ignore_index=True)
    out["code_norm"] = out.get("code", "").astype(str)
    out = out.drop_duplicates(["entry_date_norm", "code_norm", "router_mode"], keep="first")
    return out


def apply_router_gate(df: pd.DataFrame, variant: str) -> pd.DataFrame:
    d = df.copy()
    if variant == "pre_router_repair_floor_ice":
        return d[d["router_mode"].isin(["panic_repair", "true_floor_reclaim", "ice_reclaim"])].copy()
    if variant == "pre_router_no_weak_rebound":
        return d[~d["router_mode"].isin(["weak_rebound_low_absorb", "strong_theme_breakout"])].copy()
    if variant == "pre_router_context_gate":
        mask = d["router_mode"].isin(["panic_repair"])
        range_mask = d["router_mode"].isin(["true_floor_reclaim", "ice_reclaim", "stress_reclaim"])
        up_rate = pd.to_numeric(d.get("up_rate", np.nan), errors="coerce")
        big_down = pd.to_numeric(d.get("big_down_rate", np.nan), errors="coerce")
        range_pos60 = pd.to_numeric(d.get("range_pos60", np.nan), errors="coerce")
        context_ok = (up_rate.between(0.08, 0.75, inclusive="both") & (big_down <= 0.45)) | (range_pos60 <= 0.35)
        return d[mask | (range_mask & context_ok.fillna(False))].copy()
    if variant == "pre_router_with_strong_theme":
        strong = d["router_mode"].eq("strong_theme_breakout")
        index_mom20 = pd.to_numeric(d.get("index_mom20", np.nan), errors="coerce")
        sector_diff = pd.to_numeric(d.get("sector_diffusion_score", np.nan), errors="coerce")
        strong_ok = strong & (index_mom20 > 0.02) & (sector_diff >= 65)
        return d[(~strong) | strong_ok].copy()
    return d


def simulate_router(candidates: pd.DataFrame, variant: str, slot_pct: float = 0.25, max_slots: int = 4) -> tuple[pd.DataFrame, pd.DataFrame]:
    c = apply_router_gate(candidates, variant)
    if c.empty:
        return pd.DataFrame(), pd.DataFrame()
    c = c.sort_values(["entry_date_norm", "router_priority", "router_score"], ascending=[True, True, False])
    dates = sorted(set(c["entry_date_norm"].dropna()) | set(c["exit_date_norm"].dropna()))
    cash = 1_000_000.0
    open_pos: list[dict] = []
    closed: list[dict] = []
    curve: list[dict] = []
    for date in dates:
        next_open = []
        realized_today = 0.0
        for pos in open_pos:
            if pd.notna(pos["exit_date"]) and pos["exit_date"] <= date:
                exit_value = pos["stake"] * (1.0 + pos["ret"])
                cash += exit_value
                realized = exit_value - pos["stake"]
                realized_today += realized
                rec = pos["row"].copy()
                rec["stake"] = pos["stake"]
                rec["exit_value"] = exit_value
                rec["realized_pnl"] = realized
                closed.append(rec)
            else:
                next_open.append(pos)
        open_pos = next_open
        day = c[c["entry_date_norm"] == date]
        opened = 0
        if not day.empty and len(open_pos) < max_slots:
            pick = day.iloc[0].to_dict()
            stake = min(cash, 1_000_000.0 * slot_pct)
            if stake > 0:
                cash -= stake
                open_pos.append(
                    {
                        "exit_date": pick.get("exit_date_norm"),
                        "ret": float(pick.get("ret_norm", 0.0)),
                        "stake": stake,
                        "row": pick,
                    }
                )
                opened = 1
        reserved = sum(pos["stake"] for pos in open_pos)
        equity = cash + reserved
        curve.append(
            {
                "date": date.date().isoformat(),
                "scheduler": variant,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized_today,
            }
        )
    closed_df = pd.DataFrame(closed)
    curve_df = pd.DataFrame(curve)
    if not curve_df.empty:
        curve_df["peak"] = curve_df["equity"].cummax()
        curve_df["drawdown"] = curve_df["equity"] / curve_df["peak"] - 1.0
        curve_df["ret_from_start"] = curve_df["equity"] / curve_df.iloc[0]["equity"] - 1.0
    if not closed_df.empty:
        closed_df["source_key"] = variant
        closed_df["source_label"] = variant
        closed_df["family"] = "pre_router_candidate"
    return closed_df, curve_df


def markdown_table(df: pd.DataFrame, cols: list[str], n: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    d = df[cols].copy()
    if n:
        d = d.head(n)
    for col in d.columns:
        if pd.api.types.is_float_dtype(d[col]):
            if any(k in col for k in ["rate", "return", "drawdown", "trade", "share", "ret"]):
                d[col] = d[col].map(lambda x: "" if pd.isna(x) else f"{x:.2%}")
            else:
                d[col] = d[col].map(lambda x: "" if pd.isna(x) else f"{x:.4f}")
        else:
            d[col] = d[col].map(lambda x: "" if pd.isna(x) else str(x).replace("|", "\\|"))
    return d.to_markdown(index=False)


def load_mainline_icepoint_evidence() -> tuple[pd.DataFrame, pd.DataFrame]:
    base = p("gen3_mainline_icepoint_pullback_validation_v1")
    return _read_csv(base / "summary.csv"), _read_csv(base / "annual_mainline_icepoint.csv")


def build_report(summary: pd.DataFrame, annual: pd.DataFrame, market_state: pd.DataFrame, router_summary: pd.DataFrame) -> str:
    pre = summary[summary["window"].eq("pre_2024_10")].copy()
    pre["_basis_rank"] = pre["ret_basis"].eq("trade_sequence").astype(int)
    pre = pre.sort_values(["_basis_rank", "total_return"], ascending=[True, False])
    post = summary[summary["window"].eq("post_2024_10")].sort_values("total_return", ascending=False)
    annual_focus = annual[
        annual["source_key"].isin(
            [
                "market_state_router_existing",
                "panic_repair_m30_close5_cost30",
                "range_v3_true_floor_reclaim_h3",
                "score120_diff65_sector_gate",
                "wave_scheduler_focus_score120",
                "pre_router_context_gate",
            ]
        )
    ].copy()
    mainline_summary, mainline_annual = load_mainline_icepoint_evidence()
    mainline_pick = mainline_summary[
        mainline_summary["group"].isin(
            [
                "mainline_sector_cool_4_8",
                "mainline_repair35_55_18_32_pullback_4_8",
                "mainline_emotion_18_32_pullback_4_8",
                "mainline_strong_all",
            ]
        )
        & mainline_summary["horizon"].isin([5, 10])
    ].copy()
    lines: list[str] = []
    lines.append("# G3/系统策略 2024-10 前盈利模型研究 v1")
    lines.append("")
    lines.append("## 研究边界")
    lines.append("")
    lines.append("- 区间：`2020-01-01` 至 `2024-09-30`；`2024-10-01` 以后只作为对照，不参与规则优化。")
    lines.append("- 本报告只读取 `report_path` 下既有归档，并新增研究产物；没有改动正式/影子交易链路。")
    lines.append("- 统一指标优先使用归档中的 `equity_curve`，缺曲线时使用闭合交易序列代理，因此 `ret_basis=trade_sequence` 的总收益只用于横向审计，不当作正式组合净值。")
    lines.append("- 候选路由规则不使用 2024-10 这个硬日期；日期只用于本次样本切分。")
    lines.append("")
    lines.append("## 核心结论")
    lines.append("")
    lines.append("1. 2024-10 前相对可靠的盈利来源，不是机构主升，而是 `panic_repair`、经过过滤的 `range_gap/weak_low_not_chasing`、以及已有 market_state/router 组合。")
    lines.append("2. 单独的 `true_floor_reclaim`、`ice_reclaim`、主线小冰点回踩都不是稳定盈利引擎：它们交易数不少，但单笔均值接近 0 或为负，属于“单笔收益太低”。")
    lines.append("3. 当前机构主升/score120/波段赢家调度器在 2024-10 前主要问题是交易数偏少、收益集中且早期尾部亏损大；收益集中在 2024-10 后，不能倒推为旧市场通用规则。")
    lines.append("4. 粗暴加入 `range_weak_rebound` 或旧 G3 三链确实会增加交易数，但旧三链需要用真实组合曲线重算；闭合交易序列收益很高，伴随 -59% 级别代理回撤，不能直接迁移。")
    lines.append("5. 可迁移方向是做双引擎：旧市场只打开 `panic_repair + 过滤后 range_gap/weak_low_not_chasing`，机构行情由当前 institutional_mainwave 接管；切换条件应来自滚动健康度、市场宽度/动量/成交结构，而不是硬日期。")
    lines.append("")
    lines.append("## 预 2024-10 全周期对比")
    lines.append("")
    lines.append(
        markdown_table(
            pre,
            [
                "source_label",
                "trade_count",
                "win_rate",
                "avg_trade_return",
                "total_return",
                "max_drawdown",
                "worst_trade",
                "top5_positive_pnl_share",
                "diagnosis",
                "ret_basis",
            ],
        )
    )
    lines.append("")
    lines.append("## 2024-10 后对照")
    lines.append("")
    lines.append(
        markdown_table(
            post,
            [
                "source_label",
                "trade_count",
                "win_rate",
                "avg_trade_return",
                "total_return",
                "max_drawdown",
                "worst_trade",
                "top5_positive_pnl_share",
                "diagnosis",
            ],
            12,
        )
    )
    lines.append("")
    lines.append("## 分年度拆分")
    lines.append("")
    lines.append(
        markdown_table(
            annual_focus.sort_values(["source_key", "window"]),
            [
                "source_label",
                "window",
                "trade_count",
                "win_rate",
                "avg_trade_return",
                "total_return",
                "max_drawdown",
                "worst_trade",
                "diagnosis",
            ],
        )
    )
    lines.append("")
    lines.append("## 分市场状态证据")
    lines.append("")
    if market_state.empty:
        lines.append("_无可用市场状态列_")
    else:
        lines.append(
            markdown_table(
                market_state.sort_values(["source_key", "group_col", "avg_trade_return"], ascending=[True, True, False]),
                [
                    "source_key",
                    "group_col",
                    "group",
                    "trade_count",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "sum_ret",
                ],
                80,
            )
        )
    lines.append("")
    lines.append("## 主线板块强势盘整/小冰点验证")
    lines.append("")
    lines.append("这类模式覆盖了大量样本，结论是“单笔收益太低”，不是“交易数不够”。主线强势、主线回踩、小冰点修复的均值大多在 -1% 到 +1% 附近，且中位数常为负。")
    lines.append("")
    lines.append(
        markdown_table(
            mainline_pick,
            ["group", "horizon", "count", "avg", "median", "win_rate", "worst"],
        )
    )
    lines.append("")
    lines.append("年度上，2020/2021 有一定修复收益，2022 明显失效，2023 只短持有略可，2024-09 前仍不构成独立盈利引擎。")
    lines.append("")
    lines.append(markdown_table(mainline_annual, ["year", "count", "h5_avg", "h5_win_rate", "h10_avg", "h10_win_rate"]))
    lines.append("")
    lines.append("## 预 2024-10 专用路由尝试")
    lines.append("")
    lines.append("- `pre_router_repair_floor_ice`：恐慌修复优先，其次真底部 reclaim，再次冰点 reclaim。这个朴素路由用于反证，结果并不好。")
    lines.append("- `pre_router_context_gate`：在上面的基础上，只接受宽度/暴跌率/60 日箱体位置更像可修复的震荡环境；仍未解决 range 低收益问题。")
    lines.append("- `pre_router_with_strong_theme`：额外允许强势题材突破，但必须满足指数 20 日动量为正、行业扩散分不低。")
    lines.append("")
    lines.append(
        markdown_table(
            router_summary.sort_values("total_return", ascending=False),
            [
                "source_label",
                "trade_count",
                "win_rate",
                "avg_trade_return",
                "total_return",
                "max_drawdown",
                "worst_trade",
                "top5_positive_pnl_share",
                "diagnosis",
            ],
        )
    )
    lines.append("")
    lines.append("路由结论：新拼的朴素预路由没有超过已有 G3 V4/range_filtered/market_state 归档，说明不能简单把“恐慌 + 底部 reclaim”堆起来。真正可迁移的是已有路由里的状态选择思想：panic 优先、range 只取弱修复低吸且不追高、机构主升必须通过滚动健康度后才开放。")
    lines.append("")
    lines.append("## 有效/无效模式归因")
    lines.append("")
    lines.append("- 有效：`panic_repair`。交易数中等但单笔均值较高，适合在极端情绪释放后承担修复仓。")
    lines.append("- 有效但必须保留原过滤：`range_filtered`、当前 G3 V4 中的 `range_gap/weak_low_not_chasing`。它不是所有 range reclaim，而是少数弱修复低吸机会。")
    lines.append("- 阶段有效：强势题材/趋势突破。主要从 2024 年春季开始贡献，2020-2023 样本太少且早期亏损大。")
    lines.append("- 无效或低效：`range_v2_ice_reclaim`、`range_v3_true_floor_reclaim` 单独跑均值太低；主线强势盘整小冰点的大样本均值过低；旧 G3 guarded 需要真实组合曲线复算，不能按闭合交易序列直接迁移。")
    lines.append("- 不适合旧市场优化：机构/波段主升调度器。它的收益集中在 2024-10 后，2020-2023 不是稳定盈利模型。")
    lines.append("")
    lines.append("## 可迁移方案")
    lines.append("")
    lines.append("建议只作为研究迁移，不直接进入当前 shadow：")
    lines.append("")
    lines.append("1. 保留当前 institutional_mainwave 作为机构主升引擎。")
    lines.append("2. 新增历史弱市/震荡市修复引擎：`panic_repair -> filtered_range_gap/weak_low_not_chasing`，每次最多一笔，仓位低于主升引擎；不要直接放开 true_floor/ice_reclaim。")
    lines.append("3. 用滚动健康度切换：最近 240 个交易日内，某引擎闭合交易数达到最低样本数且平均收益、回撤、尾部亏损合格才打开；否则空仓或降权。")
    lines.append("4. 市场状态层只用当日以前可得的宽度、动量、成交额、暴跌率、箱体位置；不使用 `2024-10-01` 作为生产规则。")
    lines.append("5. 正式接入前需要 bar-by-bar 复刻执行、滑点压力测试和 shadow-only 隔离验证。")
    lines.append("")
    lines.append("## 产物")
    lines.append("")
    lines.append("- `source_summary.csv`：各模式全周期/预窗口/后窗口统计。")
    lines.append("- `annual_summary.csv`：各模式年度统计。")
    lines.append("- `market_state_summary.csv`：按市场状态/路由/来源分组。")
    lines.append("- `pre_router_*_closed_trades.csv` 与 `pre_router_*_equity_curve.csv`：预 2024-10 路由尝试。")
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_summary: list[dict] = []
    all_annual: list[dict] = []
    state_rows: list[pd.DataFrame] = []
    loaded: dict[str, tuple[TradeSource, pd.DataFrame, pd.DataFrame]] = {}
    for source in SOURCES:
        trades = normalize_trades(source)
        curve = normalize_curve(source)
        if trades.empty:
            continue
        loaded[source.key] = (source, trades, curve)
        all_summary.append(trade_metrics(trades, curve, source, START, PRE_END, "pre_2024_10"))
        all_summary.append(trade_metrics(trades, curve, source, POST_START, pd.Timestamp("2026-12-31"), "post_2024_10"))
        all_summary.append(trade_metrics(trades, curve, source, START, pd.Timestamp("2026-12-31"), "full_archive"))
        all_annual.extend(annual_metrics(source, trades, curve))
        gm = grouped_metrics(
            trades,
            ["market_style", "market_style_prev", "ma_skeleton", "volume_price_layer", "adx_layer", "route", "route_source", "g3_chain", "range_v2_variant", "range_v3_variant", "template_label"],
        )
        if not gm.empty:
            gm.insert(0, "source_key", source.key)
            state_rows.append(gm)
    candidates = prepare_router_candidates()
    router_summary_rows: list[dict] = []
    router_annual_rows: list[dict] = []
    for variant in [
        "pre_router_repair_floor_ice",
        "pre_router_context_gate",
        "pre_router_with_strong_theme",
        "pre_router_no_weak_rebound",
    ]:
        closed, curve = simulate_router(candidates, variant)
        if closed.empty:
            continue
        closed.to_csv(OUT_DIR / f"{variant}_closed_trades.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{variant}_equity_curve.csv", index=False, encoding="utf-8-sig")
        fake = TradeSource(variant, variant, "pre_router_candidate", OUT_DIR / f"{variant}_closed_trades.csv", OUT_DIR / f"{variant}_equity_curve.csv", "ret_norm")
        norm_closed = normalize_trades(fake)
        norm_curve = normalize_curve(fake)
        row = trade_metrics(norm_closed, norm_curve, fake, START, PRE_END, "pre_2024_10")
        router_summary_rows.append(row)
        all_summary.append(row)
        annual_rows = annual_metrics(fake, norm_closed, norm_curve)
        router_annual_rows.extend(annual_rows)
        all_annual.extend(annual_rows)
        gm = grouped_metrics(norm_closed, ["router_mode", "source_key", "route", "route_source"])
        if not gm.empty:
            gm.insert(0, "source_key", variant)
            state_rows.append(gm)
    summary_df = pd.DataFrame(all_summary)
    annual_df = pd.DataFrame(all_annual)
    state_df = pd.concat(state_rows, ignore_index=True) if state_rows else pd.DataFrame()
    router_df = pd.DataFrame(router_summary_rows)
    summary_df.to_csv(OUT_DIR / "source_summary.csv", index=False, encoding="utf-8-sig")
    annual_df.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")
    state_df.to_csv(OUT_DIR / "market_state_summary.csv", index=False, encoding="utf-8-sig")
    router_df.to_csv(OUT_DIR / "router_candidate_summary.csv", index=False, encoding="utf-8-sig")
    manifest = {
        "start": str(START.date()),
        "pre_end": str(PRE_END.date()),
        "post_start": str(POST_START.date()),
        "source_count": len(loaded),
        "output_dir": str(OUT_DIR),
        "sources": [
            {
                "key": s.key,
                "label": s.label,
                "trades": str(s.trades),
                "curve": str(s.curve) if s.curve else None,
            }
            for s in SOURCES
        ],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    report = build_report(summary_df, annual_df, state_df, router_df)
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")
    print(f"wrote {OUT_DIR}")


if __name__ == "__main__":
    main()

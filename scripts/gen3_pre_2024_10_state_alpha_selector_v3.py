from __future__ import annotations

import math
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_pre_2024_10_deeper_mode_exploration_v2 import (  # noqa: E402
    PRE_END,
    START,
    WAVE_CANDIDATES,
    load_wave_candidates,
)
from utils.paths import report_path, reports_root  # noqa: E402


OUT_DIR = report_path("gen3_pre_2024_10_state_alpha_selector_v3")


@dataclass(frozen=True)
class RollingVariant:
    name: str
    label: str
    lookback_days: int
    min_count: int
    min_avg_ret: float
    min_win_rate: float
    require_median_proxy: bool
    allow_hot: bool
    max_index_mom60: float = 0.08
    max_ret20: float = 0.22
    min_ret20: float = -9.0
    max_range_pos120: float = 0.98
    max_climax_penalty: float = 10.0
    min_max_dd20: float = -9.0
    max_amount_ratio20_60: float = 99.0


ROLLING_VARIANTS = [
    RollingVariant(
        "rolling_wave_broad_720",
        "滚动题材口袋 broad 720D",
        lookback_days=720,
        min_count=20,
        min_avg_ret=0.008,
        min_win_rate=0.51,
        require_median_proxy=False,
        allow_hot=False,
    ),
    RollingVariant(
        "rolling_wave_balanced_720",
        "滚动题材口袋 balanced 720D",
        lookback_days=720,
        min_count=30,
        min_avg_ret=0.012,
        min_win_rate=0.52,
        require_median_proxy=True,
        allow_hot=False,
    ),
    RollingVariant(
        "rolling_wave_strict_720",
        "滚动题材口袋 strict 720D",
        lookback_days=720,
        min_count=40,
        min_avg_ret=0.015,
        min_win_rate=0.53,
        require_median_proxy=True,
        allow_hot=False,
    ),
    RollingVariant(
        "rolling_wave_balanced_480",
        "滚动题材口袋 balanced 480D",
        lookback_days=480,
        min_count=20,
        min_avg_ret=0.010,
        min_win_rate=0.52,
        require_median_proxy=True,
        allow_hot=False,
    ),
    RollingVariant(
        "rolling_wave_pullback_conservative_720",
        "滚动题材口袋 pullback conservative 720D",
        lookback_days=720,
        min_count=35,
        min_avg_ret=0.018,
        min_win_rate=0.54,
        require_median_proxy=True,
        allow_hot=False,
        max_index_mom60=0.05,
        max_ret20=0.12,
        min_ret20=-0.08,
        max_range_pos120=0.90,
        max_climax_penalty=6.0,
        min_max_dd20=-0.22,
        max_amount_ratio20_60=1.8,
    ),
]


def rp(*parts: str) -> Path:
    return reports_root().joinpath(*parts)


def read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False, **kwargs)


def pct(v: float | int | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def num(v: float | int | None, digits: int = 2) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.{digits}f}"


def normalize_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def curve_from_trades(trades: pd.DataFrame, ret_col: str = "ret_norm") -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["date", "equity", "ret"])
    out = trades.sort_values(["entry_date_norm", "code"]).copy()
    ret = pd.to_numeric(out[ret_col], errors="coerce").fillna(0.0)
    out["equity"] = (1.0 + ret).cumprod()
    out["date"] = out["policy_exit_date_norm"].where(out["policy_exit_date_norm"].notna(), out["entry_date_norm"])
    return out[["date", "equity", ret_col]].rename(columns={ret_col: "ret"})


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def actual_curve_metrics(path: Path) -> tuple[float, float]:
    curve = read_csv(path)
    if curve.empty or "date" not in curve.columns or "equity" not in curve.columns:
        return np.nan, np.nan
    curve["date_norm"] = normalize_date(curve["date"])
    curve["equity_norm"] = pd.to_numeric(curve["equity"], errors="coerce")
    curve = curve[
        (curve["date_norm"] >= START)
        & (curve["date_norm"] <= PRE_END)
        & curve["equity_norm"].notna()
    ].sort_values("date_norm")
    if curve.empty:
        return np.nan, np.nan
    first = float(curve["equity_norm"].iloc[0])
    last = float(curve["equity_norm"].iloc[-1])
    total = last / first - 1.0 if first else np.nan
    return total, max_drawdown(curve["equity_norm"])


def summary_metrics(trades: pd.DataFrame, label: str, key: str) -> dict:
    ret = pd.to_numeric(trades.get("ret_norm", pd.Series(dtype=float)), errors="coerce").dropna()
    curve = curve_from_trades(trades.loc[ret.index] if len(ret) else trades)
    pos = ret[ret > 0].sort_values(ascending=False)
    pos_sum = float(pos.sum()) if len(pos) else 0.0
    by_year = (
        trades.loc[ret.index]
        .assign(year=lambda x: x["entry_date_norm"].dt.year)
        .groupby("year")["ret_norm"]
        .agg(["count", "mean"])
        if len(ret)
        else pd.DataFrame()
    )
    return {
        "key": key,
        "label": label,
        "trade_count": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else np.nan,
        "avg_ret": float(ret.mean()) if len(ret) else np.nan,
        "median_ret": float(ret.median()) if len(ret) else np.nan,
        "total_return_compound": float(curve["equity"].iloc[-1] - 1.0) if not curve.empty else np.nan,
        "capital_return_or_proxy": float(curve["equity"].iloc[-1] - 1.0) if not curve.empty else np.nan,
        "sum_ret_proxy": float(ret.sum()) if len(ret) else np.nan,
        "max_drawdown_trade_curve": max_drawdown(curve["equity"]) if not curve.empty else np.nan,
        "capital_max_drawdown_or_proxy": max_drawdown(curve["equity"]) if not curve.empty else np.nan,
        "worst_trade": float(ret.min()) if len(ret) else np.nan,
        "best_trade": float(ret.max()) if len(ret) else np.nan,
        "top5_positive_share": float(pos.head(5).sum() / pos_sum) if pos_sum > 0 else np.nan,
        "positive_years": int((by_year["mean"] > 0).sum()) if not by_year.empty else 0,
        "min_year_count": int(by_year["count"].min()) if not by_year.empty else 0,
        "diagnosis": diagnose(ret, by_year, pos_sum, pos),
    }


def diagnose(ret: pd.Series, by_year: pd.DataFrame, pos_sum: float, pos: pd.Series) -> str:
    if len(ret) < 20:
        return "交易数太少"
    if ret.mean() < 0.005 or (ret > 0).mean() < 0.50:
        return "策略单笔收益太低"
    if not by_year.empty and (by_year["mean"] > 0).sum() < 3:
        return "年度稳定性不足"
    if pos_sum > 0 and float(pos.head(5).sum() / pos_sum) > 0.50:
        return "收益集中度偏高"
    return "可继续建模"


def annual_metrics(trades: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    rows = []
    for year, g in trades.assign(year=lambda x: x["entry_date_norm"].dt.year).groupby("year"):
        row = summary_metrics(g, label, key)
        row["year"] = int(year)
        rows.append(row)
    return pd.DataFrame(rows)


def group_metrics(trades: pd.DataFrame, key: str, label: str, col: str) -> pd.DataFrame:
    if trades.empty or col not in trades.columns:
        return pd.DataFrame()
    rows = []
    for value, g in trades.groupby(col, dropna=False):
        row = summary_metrics(g, label, key)
        row["group_col"] = col
        row["group_value"] = "" if pd.isna(value) else str(value)
        rows.append(row)
    return pd.DataFrame(rows)


def load_wave_pool() -> pd.DataFrame:
    frames = []
    for source in WAVE_CANDIDATES:
        df = load_wave_candidates(source)
        if df.empty:
            continue
        keep_cols = [
            "source_key",
            "source_label",
            "family",
            "code",
            "stock_name",
            "l2_sector_name",
            "template_label",
            "entry_date_norm",
            "policy_exit_date",
            "ret_norm",
            "hold_days",
            "rank_key",
            "rank_pct_in_day",
            "index_mom20_bucket",
            "index_mom60_bucket",
            "range_pos120_bucket",
            "ret20_bucket",
            "max_dd20_bucket",
            "amount_ratio_bucket",
            "amount5_20_bucket",
            "learned_avg_bucket",
            "climax_bucket",
            "index_mom60",
            "ret20",
            "range_pos120",
            "max_dd20",
            "climax_penalty",
            "amount_ratio20_60",
            "learned_avg_return",
        ]
        frames.append(df[[c for c in keep_cols if c in df.columns]].copy())
    if not frames:
        return pd.DataFrame()
    pool = pd.concat(frames, ignore_index=True)
    pool["policy_exit_date_norm"] = normalize_date(pool["policy_exit_date"])
    pool = pool[
        (pool["entry_date_norm"] >= START)
        & (pool["entry_date_norm"] <= PRE_END)
        & pool["policy_exit_date_norm"].notna()
        & pool["ret_norm"].notna()
    ].copy()
    pool["candidate_id"] = (
        pool["source_key"].astype(str)
        + "|"
        + pool["code"].astype(str)
        + "|"
        + pool["entry_date_norm"].dt.strftime("%Y-%m-%d")
        + "|"
        + pool["hold_days"].astype(str)
        + "|"
        + pool.index.astype(str)
    )
    return pool


def pocket_ids(row) -> list[tuple[str, str]]:
    source = str(row.source_key)
    sector = str(row.l2_sector_name)
    template = str(row.template_label)
    mom60 = str(row.index_mom60_bucket)
    ret20 = str(row.ret20_bucket)
    amount = str(row.amount_ratio_bucket)
    climax = str(row.climax_bucket)
    return [
        ("sector_template", f"{source}|{sector}|{template}"),
        ("sector_template_mom60", f"{source}|{sector}|{template}|{mom60}"),
        ("template_mom60_ret20", f"{source}|{template}|{mom60}|{ret20}"),
        ("sector_template_amount", f"{source}|{sector}|{template}|{amount}"),
        ("sector_template_climax", f"{source}|{sector}|{template}|{climax}"),
    ]


class RollingPocketBook:
    def __init__(self, lookback_days: int):
        self.lookback_days = lookback_days
        self.samples: dict[str, deque[tuple[pd.Timestamp, float]]] = defaultdict(deque)
        self.sum_ret: dict[str, float] = defaultdict(float)
        self.win_count: dict[str, int] = defaultdict(int)

    def add(self, pocket_id: str, known_date: pd.Timestamp, ret: float) -> None:
        q = self.samples[pocket_id]
        q.append((known_date, ret))
        self.sum_ret[pocket_id] += ret
        if ret > 0:
            self.win_count[pocket_id] += 1

    def expire(self, current_date: pd.Timestamp) -> None:
        cutoff = current_date - pd.Timedelta(days=self.lookback_days)
        for pocket_id in list(self.samples.keys()):
            q = self.samples[pocket_id]
            while q and q[0][0] < cutoff:
                _, old_ret = q.popleft()
                self.sum_ret[pocket_id] -= old_ret
                if old_ret > 0:
                    self.win_count[pocket_id] -= 1
            if not q:
                self.samples.pop(pocket_id, None)
                self.sum_ret.pop(pocket_id, None)
                self.win_count.pop(pocket_id, None)

    def stats(self, pocket_id: str) -> tuple[int, float, float, float]:
        q = self.samples.get(pocket_id)
        if not q:
            return 0, np.nan, np.nan, np.nan
        n = len(q)
        avg = self.sum_ret[pocket_id] / n
        win = self.win_count[pocket_id] / n
        # This is a conservative cheap proxy: use the lower half share instead of a full rolling median.
        vals = [x[1] for x in q]
        med = float(np.median(vals)) if vals else np.nan
        return n, avg, win, med


def passes_current_guard(row, variant: RollingVariant) -> bool:
    if variant.allow_hot:
        return True
    index_mom60 = float(row.index_mom60) if pd.notna(row.index_mom60) else 0.0
    ret20 = float(row.ret20) if pd.notna(row.ret20) else 0.0
    range_pos = float(row.range_pos120) if pd.notna(row.range_pos120) else 0.5
    climax = float(row.climax_penalty) if pd.notna(row.climax_penalty) else 0.0
    max_dd20 = float(row.max_dd20) if pd.notna(row.max_dd20) else 0.0
    amount_ratio = float(row.amount_ratio20_60) if pd.notna(row.amount_ratio20_60) else 1.0
    return (
        index_mom60 <= variant.max_index_mom60
        and variant.min_ret20 <= ret20 <= variant.max_ret20
        and range_pos <= variant.max_range_pos120
        and climax <= variant.max_climax_penalty
        and max_dd20 >= variant.min_max_dd20
        and amount_ratio <= variant.max_amount_ratio20_60
    )


def rolling_wave_select(pool: pd.DataFrame, variant: RollingVariant) -> pd.DataFrame:
    if pool.empty:
        return pd.DataFrame()

    events = []
    query_by_date: dict[pd.Timestamp, list] = defaultdict(list)
    for row in pool.itertuples(index=False):
        pockets = pocket_ids(row)
        events.append((row.policy_exit_date_norm, float(row.ret_norm), pockets))
        query_by_date[row.entry_date_norm].append((row, pockets))
    events.sort(key=lambda x: x[0])

    book = RollingPocketBook(variant.lookback_days)
    selected = []
    event_i = 0
    active_until = pd.Timestamp.min

    for current_date in sorted(query_by_date.keys()):
        while event_i < len(events) and events[event_i][0] < current_date:
            known_date, ret, pockets = events[event_i]
            for _, pocket_id in pockets:
                book.add(pocket_id, known_date, ret)
            event_i += 1
        book.expire(current_date)

        if current_date <= active_until:
            continue

        best = None
        for row, pockets in query_by_date[current_date]:
            if not passes_current_guard(row, variant):
                continue
            best_pocket = None
            for pocket_type, pocket_id in pockets:
                n, avg, win, med = book.stats(pocket_id)
                if n < variant.min_count:
                    continue
                if avg < variant.min_avg_ret or win < variant.min_win_rate:
                    continue
                if variant.require_median_proxy and (pd.isna(med) or med < -0.002):
                    continue
                rank_pct = float(row.rank_pct_in_day) if pd.notna(row.rank_pct_in_day) else 1.0
                score = avg * 100.0 + (win - 0.5) * 6.0 + math.log1p(n) * 0.08 - rank_pct * 0.12
                candidate = {
                    "selector_score": score,
                    "hist_count": n,
                    "hist_avg_ret": avg,
                    "hist_win_rate": win,
                    "hist_median_ret": med,
                    "pocket_type": pocket_type,
                    "pocket_id": pocket_id,
                }
                if best_pocket is None or score > best_pocket["selector_score"]:
                    best_pocket = candidate
            if best_pocket is None:
                continue
            row_dict = row._asdict()
            row_dict.update(best_pocket)
            row_dict["selector_key"] = variant.name
            row_dict["selector_label"] = variant.label
            row_dict["alpha_family"] = "rolling_wave_pocket"
            if best is None or row_dict["selector_score"] > best["selector_score"]:
                best = row_dict

        if best is not None:
            selected.append(best)
            active_until = best["policy_exit_date_norm"]

    return pd.DataFrame(selected)


def load_state_core() -> pd.DataFrame:
    path = rp("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv")
    df = read_csv(path)
    if df.empty:
        return df
    df["entry_date_norm"] = normalize_date(df["entry_date"])
    df["policy_exit_date_norm"] = normalize_date(df["policy_exit_date"])
    df["ret_norm"] = pd.to_numeric(df.get("policy_net_ret", df.get("net_ret")), errors="coerce")
    df = df[
        (df["entry_date_norm"] >= START)
        & (df["entry_date_norm"] <= PRE_END)
        & df["entry_date_norm"].notna()
        & df["policy_exit_date_norm"].notna()
        & df["ret_norm"].notna()
        & df["mode"].isin(["panic_repair", "old_g3_route_v3"])
    ].copy()
    df["source_key"] = "state_router_core"
    df["source_label"] = "现有状态路由核心：panic + old_g3"
    df["family"] = "state_core"
    df["alpha_family"] = df["mode"].map({"panic_repair": "panic_repair", "old_g3_route_v3": "old_g3_route_v3"})
    df["selector_key"] = "state_router_core_pre"
    df["selector_label"] = "现有状态路由核心 pre"
    df["selector_score"] = np.where(df["mode"].eq("panic_repair"), 300.0, 200.0)
    df["pocket_type"] = df["mode"]
    df["pocket_id"] = df["mode"].astype(str) + "|" + df.get("market_style_prev", "").astype(str) + "|" + df.get("adx_layer", "").astype(str)
    if "stock_name" not in df.columns and "name" in df.columns:
        df["stock_name"] = df["name"]
    return df


def single_slot(candidates: pd.DataFrame, key: str, label: str, priority_col: str = "selector_score") -> pd.DataFrame:
    if candidates.empty:
        return candidates
    selected = []
    active_until = pd.Timestamp.min
    for current_date, day in candidates.sort_values(["entry_date_norm", priority_col], ascending=[True, False]).groupby(
        "entry_date_norm", sort=True
    ):
        if current_date <= active_until:
            continue
        best = day.sort_values(priority_col, ascending=False).iloc[0].to_dict()
        best["selector_key"] = key
        best["selector_label"] = label
        selected.append(best)
        active_until = best["policy_exit_date_norm"]
    return pd.DataFrame(selected)


def build_hybrids(state_core: pd.DataFrame, wave_results: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    hybrids = {}
    if not state_core.empty:
        hybrids["state_core_single_slot"] = single_slot(
            state_core,
            "state_core_single_slot",
            "panic + old_g3 单槽基线",
            "selector_score",
        )
    for name, wave in wave_results.items():
        if wave.empty:
            continue
        wave2 = wave.copy()
        wave2["selector_score"] = 240.0 + pd.to_numeric(wave2["selector_score"], errors="coerce").fillna(0.0)
        combo = pd.concat([state_core, wave2], ignore_index=True, sort=False)
        hybrids[f"hybrid_panic_wave_old_{name}"] = single_slot(
            combo,
            f"hybrid_panic_wave_old_{name}",
            f"混合选择器：panic 优先 + {name} + old_g3",
            "selector_score",
        )
        wave_fill = wave.copy()
        wave_fill["selector_score"] = 150.0 + pd.to_numeric(wave_fill["selector_score"], errors="coerce").fillna(0.0)
        combo_fill = pd.concat([state_core, wave_fill], ignore_index=True, sort=False)
        hybrids[f"hybrid_state_first_fill_{name}"] = single_slot(
            combo_fill,
            f"hybrid_state_first_fill_{name}",
            f"混合选择器：panic/old_g3 优先 + {name} 补空档",
            "selector_score",
        )
    return hybrids


def comparison_sources() -> dict[str, tuple[str, pd.DataFrame]]:
    sources = {}
    paths = {
        "current_g3_v4_combo_h10_cost30": (
            "当前 G3 V4 h10 cost30",
            rp("gen3_v4_research_package_v1", "v4_h10_margin__cost30", "closed_trades.csv"),
            rp("gen3_v4_research_package_v1", "v4_h10_margin__cost30", "mtm_equity_curve.csv"),
            "policy_net_ret",
        ),
        "market_state_router_existing": (
            "现有 market_state_router 候选",
            rp("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv"),
            rp("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_equity_curve.csv"),
            "policy_net_ret",
        ),
        "range_filtered_candidate_v2": (
            "range_filtered 组合候选 v2",
            rp("gen3_range_filtered_candidate_package_v2", "g3_range_filtered_candidate_closed_trades.csv"),
            rp("gen3_range_filtered_candidate_package_v2", "g3_range_filtered_candidate_equity_curve.csv"),
            "policy_net_ret",
        ),
    }
    for key, (label, path, curve_path, ret_col) in paths.items():
        df = read_csv(path)
        if df.empty:
            continue
        entry_col = "entry_date" if "entry_date" in df.columns else "trade_date"
        exit_col = "policy_exit_date" if "policy_exit_date" in df.columns else "exit_date"
        df["entry_date_norm"] = normalize_date(df[entry_col])
        df["policy_exit_date_norm"] = normalize_date(df[exit_col]) if exit_col in df.columns else df["entry_date_norm"]
        df["ret_norm"] = pd.to_numeric(df[ret_col] if ret_col in df.columns else df.get("net_ret"), errors="coerce")
        df = df[
            (df["entry_date_norm"] >= START)
            & (df["entry_date_norm"] <= PRE_END)
            & df["ret_norm"].notna()
        ].copy()
        actual_total, actual_dd = actual_curve_metrics(curve_path)
        sources[key] = (label, df, actual_total, actual_dd)
    return sources


def format_table(df: pd.DataFrame, cols: list[str], limit: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    out = df.copy()
    if limit:
        out = out.head(limit)
    for col in out.columns:
        if col.endswith("rate") or col in {
            "avg_ret",
            "median_ret",
            "total_return_compound",
            "capital_return_or_proxy",
            "sum_ret_proxy",
            "max_drawdown_trade_curve",
            "capital_max_drawdown_or_proxy",
            "worst_trade",
            "best_trade",
            "top5_positive_share",
            "hist_avg_ret",
            "hist_win_rate",
            "hist_median_ret",
        }:
            out[col] = out[col].map(pct)
    return out[cols].to_markdown(index=False)


def write_report(
    summary: pd.DataFrame,
    annual: pd.DataFrame,
    groups: pd.DataFrame,
    wave_results: dict[str, pd.DataFrame],
    hybrids: dict[str, pd.DataFrame],
) -> None:
    summary_sorted = summary.sort_values(
        ["diagnosis", "capital_return_or_proxy", "avg_ret"],
        ascending=[True, False, False],
    )
    best_rows = summary.sort_values("capital_return_or_proxy", ascending=False).head(12)
    hybrid_rows = summary[summary["key"].str.startswith("hybrid_", na=False)].sort_values(
        "capital_return_or_proxy", ascending=False
    )
    wave_rows = summary[summary["key"].str.startswith("rolling_wave_", na=False)].sort_values(
        "capital_return_or_proxy", ascending=False
    )

    selected_key = hybrid_rows.iloc[0]["key"] if not hybrid_rows.empty else ""
    selected = hybrids.get(selected_key, pd.DataFrame()) if selected_key else pd.DataFrame()
    selected_pockets = (
        selected.groupby(["alpha_family", "pocket_type", "pocket_id"], dropna=False)["ret_norm"]
        .agg(trade_count="count", avg_ret="mean", win_rate=lambda s: (s > 0).mean(), worst_trade="min")
        .reset_index()
        .sort_values(["trade_count", "avg_ret"], ascending=[False, False])
        if not selected.empty and "pocket_id" in selected.columns
        else pd.DataFrame()
    )

    report = f"""# G3 2024-10 前状态 Alpha 选择器第三阶段 v3

## 研究目标

本阶段继续探索 2024-10-01 以前的 G3/system 赚钱模式，但从“静态发现”推进到“无未来函数选择器雏形”。核心约束：

- 不使用 2024-10 以后样本训练或优化。
- 不用硬日期作为最终切换规则。
- 题材/行业口袋的历史收益，只有在样本 `policy_exit_date` 早于当前入场日时才进入滚动统计。
- 不改当前正式/影子交易链路，只新增研究脚本和 report_path 归档。

## 结论更新

1. 第三阶段没有推翻前两阶段结论：2024-10 前最可靠的底座仍是 `panic_repair` 与旧 G3 `old_g3_route_v3` 窄路由；粗暴增加 `range_weak_rebound` 或全量 `ice/true_floor reclaim` 仍不成立。
2. 题材/行业口袋可以迁移成“滚动学习候选源”，但必须用退出后可见样本滚动学习。直接 top1/top3 失败，不代表题材 alpha 不存在，而是说明旧市场需要“先识别口袋，再排序个股”。
3. 当前测试里，滚动题材口袋单独运行仍容易受交易数、回撤和年度稳定性约束；更合理的形态是作为 `panic_repair` 与旧 G3 之间的进攻补充，而不是替代当前机构主升。
4. 可迁移方案应叫 `pre_2024_state_alpha`，不是把当前 institutional_mainwave 改参数。它应该由三块组成：panic 修复、旧 G3 down_panic/range_gap、滚动题材口袋重排。

## 全周期对比

{format_table(best_rows, ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_or_proxy", "capital_max_drawdown_or_proxy", "worst_trade", "top5_positive_share", "positive_years", "diagnosis"])}

注：已有归档基线优先采用历史 equity curve 的实际收益/回撤；本阶段新构造的滚动选择器使用单槽交易序列代理收益/回撤，主要用于研究排序，不等同于正式资金曲线。

## 滚动题材口袋单独回放

{format_table(wave_rows, ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_or_proxy", "capital_max_drawdown_or_proxy", "worst_trade", "top5_positive_share", "positive_years", "diagnosis"])}

解读：如果交易数太少，说明条件太窄，不能作为主策略；如果交易数够但均值/中位数弱，说明不是缺信号，而是单笔收益质量不够。第三阶段继续把这两类问题拆开。

## 混合选择器回放

{format_table(hybrid_rows, ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return_or_proxy", "capital_max_drawdown_or_proxy", "worst_trade", "top5_positive_share", "positive_years", "diagnosis"])}

混合选择器的意义不是替代正式 G3，而是验证 2024-10 前是否存在可自动切换的老市场状态 alpha。若混合版本只提高收益但显著增加回撤或集中度，则只能作为研究候选，不能迁移到 shadow。

## 最佳混合版本的口袋来源

{format_table(selected_pockets, ["alpha_family", "pocket_type", "pocket_id", "trade_count", "avg_ret", "win_rate", "worst_trade"], limit=30)}

## 年度拆分

{format_table(annual.sort_values(["key", "year"]), ["key", "year", "trade_count", "win_rate", "avg_ret", "median_ret", "total_return_compound", "max_drawdown_trade_curve", "worst_trade", "diagnosis"], limit=80)}

## 状态/模式拆分

{format_table(groups.sort_values(["key", "group_col", "trade_count"], ascending=[True, True, False]), ["key", "group_col", "group_value", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_trade", "diagnosis"], limit=100)}

## 对挣钱模式的更充分认识

- `panic_repair`：旧市场中最像“可解释、可复用”的收益来源。它赚的是恐慌释放后的修复，而不是趋势延续。
- `old_g3_route_v3`：有效部分集中在 `down_panic/range_gap`，本质是窄条件下的跳空/恐慌/箱体修复，不是泛低吸。
- 滚动题材口袋：旧市场确实存在行业与模板共振的波段机会，但它的可交易化难点在个股重排和状态保护；不能照搬 2024-10 后机构主升排序。
- 无效模式：单独 `range_weak_rebound`、全量 `ice_reclaim`、全量 `true_floor_reclaim`、直接 top1/top3 模板买入，主要问题是单笔收益太低，而不是单纯交易数不足。

## 可迁移方案

建议下一步只做研究链路，不动 shadow：

1. 新建 `pre_2024_state_alpha` 研究源：输入为 panic、旧 G3 窄路由、滚动题材口袋。
2. 对每类 alpha 单独维护 240/480/720 日滚动健康度：`count >= N`、`avg_ret > 0`、`win_rate > 50%`、最近回撤不过阈值。
3. 当前 G3 institutional_mainwave 继续服务 2024-10 后机构主升；pre-state-alpha 只在机构主升健康度不足、且自身滚动健康度达标时接管。
4. 最终自动切换规则只能依赖滚动健康度和市场状态，不使用 2024-10-01 这样的硬日期。

## 产物

- `selector_summary.csv`：选择器与基线全周期指标。
- `selector_annual_summary.csv`：年度拆分。
- `selector_group_summary.csv`：状态/模式拆分。
- `rolling_wave_*_selected.csv`：滚动题材口袋入选样本。
- `hybrid_*_selected.csv`：混合选择器入选样本。
"""
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    pool = load_wave_pool()
    pd.DataFrame(
        [
            {
                "pool_rows": int(len(pool)),
                "source_count": int(pool["source_key"].nunique()) if not pool.empty else 0,
                "start": str(pool["entry_date_norm"].min().date()) if not pool.empty else "",
                "end": str(pool["entry_date_norm"].max().date()) if not pool.empty else "",
            }
        ]
    ).to_csv(OUT_DIR / "wave_pool_summary.csv", index=False, encoding="utf-8-sig")

    wave_results: dict[str, pd.DataFrame] = {}
    for variant in ROLLING_VARIANTS:
        selected = rolling_wave_select(pool, variant)
        wave_results[variant.name] = selected
        selected.to_csv(OUT_DIR / f"{variant.name}_selected.csv", index=False, encoding="utf-8-sig")

    state_core = load_state_core()
    state_core.to_csv(OUT_DIR / "state_core_pre_selected_source.csv", index=False, encoding="utf-8-sig")
    hybrids = build_hybrids(state_core, wave_results)
    for key, df in hybrids.items():
        df.to_csv(OUT_DIR / f"{key}_selected.csv", index=False, encoding="utf-8-sig")

    rows = []
    annual_frames = []
    group_frames = []

    for key, (label, df, actual_total, actual_dd) in comparison_sources().items():
        row = summary_metrics(df, label, key)
        if pd.notna(actual_total):
            row["capital_return_or_proxy"] = actual_total
        if pd.notna(actual_dd):
            row["capital_max_drawdown_or_proxy"] = actual_dd
        rows.append(row)
        annual_frames.append(annual_metrics(df, key, label))
        for col in ["mode", "route", "market_style_prev", "adx_layer"]:
            g = group_metrics(df, key, label, col)
            if not g.empty:
                group_frames.append(g)

    for variant in ROLLING_VARIANTS:
        df = wave_results.get(variant.name, pd.DataFrame())
        rows.append(summary_metrics(df, variant.label, variant.name))
        annual_frames.append(annual_metrics(df, variant.name, variant.label))
        for col in ["source_key", "family", "template_label", "l2_sector_name", "index_mom60_bucket", "ret20_bucket", "pocket_type"]:
            g = group_metrics(df, variant.name, variant.label, col)
            if not g.empty:
                group_frames.append(g)

    for key, df in hybrids.items():
        label = df["selector_label"].iloc[0] if not df.empty and "selector_label" in df.columns else key
        rows.append(summary_metrics(df, label, key))
        annual_frames.append(annual_metrics(df, key, label))
        for col in ["alpha_family", "mode", "source_key", "template_label", "l2_sector_name", "index_mom60_bucket", "pocket_type"]:
            g = group_metrics(df, key, label, col)
            if not g.empty:
                group_frames.append(g)

    summary = pd.DataFrame(rows)
    annual = pd.concat([x for x in annual_frames if not x.empty], ignore_index=True) if annual_frames else pd.DataFrame()
    groups = pd.concat([x for x in group_frames if not x.empty], ignore_index=True) if group_frames else pd.DataFrame()

    summary.to_csv(OUT_DIR / "selector_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "selector_annual_summary.csv", index=False, encoding="utf-8-sig")
    groups.to_csv(OUT_DIR / "selector_group_summary.csv", index=False, encoding="utf-8-sig")
    write_report(summary, annual, groups, wave_results, hybrids)

    print(f"wrote {OUT_DIR}")
    print(summary.sort_values("total_return_compound", ascending=False).head(20).to_string(index=False))


if __name__ == "__main__":
    main()

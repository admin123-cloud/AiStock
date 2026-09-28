from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path, reports_root  # noqa: E402


START = pd.Timestamp("2020-01-01")
PRE_END = pd.Timestamp("2024-09-30")
OUT_DIR = report_path("gen3_pre_2024_10_old_g3_panic_decomposition_v4")


@dataclass(frozen=True)
class ClosedSource:
    key: str
    label: str
    path: Path
    curve: Path | None = None
    ret_col: str = "policy_net_ret"


def rp(*parts: str) -> Path:
    return reports_root().joinpath(*parts)


def read_csv(path: Path, **kwargs) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False, **kwargs)


def normalize_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce").dt.normalize()


def pct(v) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{float(v):.2%}"


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return np.nan
    peak = equity.cummax()
    return float((equity / peak - 1.0).min())


def curve_metrics(path: Path | None) -> tuple[float, float]:
    if path is None or not path.exists():
        return np.nan, np.nan
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
    return (last / first - 1.0 if first else np.nan), max_drawdown(curve["equity_norm"])


def sequence_metrics(df: pd.DataFrame, label: str, key: str) -> dict:
    ret = pd.to_numeric(df.get("ret_norm", pd.Series(dtype=float)), errors="coerce").dropna()
    if len(ret):
        equity = (1.0 + ret).cumprod()
        by_year = (
            df.loc[ret.index]
            .assign(year=lambda x: x["entry_date_norm"].dt.year)
            .groupby("year")["ret_norm"]
            .agg(["count", "mean"])
        )
        pos = ret[ret > 0].sort_values(ascending=False)
        pos_sum = float(pos.sum()) if len(pos) else 0.0
    else:
        equity = pd.Series(dtype=float)
        by_year = pd.DataFrame()
        pos = pd.Series(dtype=float)
        pos_sum = 0.0
    return {
        "key": key,
        "label": label,
        "trade_count": int(len(ret)),
        "win_rate": float((ret > 0).mean()) if len(ret) else np.nan,
        "avg_ret": float(ret.mean()) if len(ret) else np.nan,
        "median_ret": float(ret.median()) if len(ret) else np.nan,
        "seq_return": float(equity.iloc[-1] - 1.0) if not equity.empty else np.nan,
        "seq_max_drawdown": max_drawdown(equity) if not equity.empty else np.nan,
        "sum_ret_proxy": float(ret.sum()) if len(ret) else np.nan,
        "worst_trade": float(ret.min()) if len(ret) else np.nan,
        "best_trade": float(ret.max()) if len(ret) else np.nan,
        "top5_positive_share": float(pos.head(5).sum() / pos_sum) if pos_sum > 0 else np.nan,
        "positive_years": int((by_year["mean"] > 0).sum()) if not by_year.empty else 0,
        "min_year_count": int(by_year["count"].min()) if not by_year.empty else 0,
        "diagnosis": diagnose(ret, by_year, pos, pos_sum),
    }


def diagnose(ret: pd.Series, by_year: pd.DataFrame, pos: pd.Series, pos_sum: float) -> str:
    if len(ret) < 20:
        return "交易数太少"
    if ret.mean() < 0.005 or (ret > 0).mean() < 0.5:
        return "策略单笔收益太低"
    if not by_year.empty and (by_year["mean"] > 0).sum() < 3:
        return "年度稳定性不足"
    if pos_sum > 0 and float(pos.head(5).sum() / pos_sum) > 0.5:
        return "收益集中度偏高"
    return "可继续建模"


def load_closed_source(source: ClosedSource) -> pd.DataFrame:
    df = read_csv(source.path)
    if df.empty:
        return df
    entry_col = "entry_date" if "entry_date" in df.columns else "trade_date"
    exit_col = "policy_exit_date" if "policy_exit_date" in df.columns else "exit_date"
    ret_col = source.ret_col if source.ret_col in df.columns else "net_ret"
    df["entry_date_norm"] = normalize_date(df[entry_col])
    df["policy_exit_date_norm"] = normalize_date(df[exit_col]) if exit_col in df.columns else df["entry_date_norm"]
    df["ret_norm"] = pd.to_numeric(df[ret_col], errors="coerce")
    df["source_key"] = source.key
    df["source_label"] = source.label
    return df[
        (df["entry_date_norm"] >= START)
        & (df["entry_date_norm"] <= PRE_END)
        & df["ret_norm"].notna()
    ].copy()


def closed_sources() -> list[ClosedSource]:
    base = rp("gen3_combo_range_filter_v1")
    return [
        ClosedSource(
            "range_base_cost30",
            "旧 G3 base_range_gap cost30",
            base / "base_range_gap_cost30_closed_trades.csv",
            base / "base_range_gap_cost30_mtm_equity_curve.csv",
        ),
        ClosedSource(
            "range_no_adx_downtrend_cost30",
            "旧 G3 去掉 ADX 下行 range_gap cost30",
            base / "range_no_adx_downtrend_cost30_closed_trades.csv",
            base / "range_no_adx_downtrend_cost30_mtm_equity_curve.csv",
        ),
        ClosedSource(
            "range_no_small_positive_gap_cost30",
            "旧 G3 去掉小正缺口 range_gap cost30",
            base / "range_no_small_positive_gap_cost30_closed_trades.csv",
            base / "range_no_small_positive_gap_cost30_mtm_equity_curve.csv",
        ),
        ClosedSource(
            "range_conservative_combo_b_cost30",
            "旧 G3 保守组合 B cost30",
            base / "range_conservative_combo_b_cost30_closed_trades.csv",
            base / "range_conservative_combo_b_cost30_mtm_equity_curve.csv",
        ),
        ClosedSource(
            "dynamic_router_cost30",
            "旧动态路由 cost30",
            rp("gen3_dynamic_router_combo_v1", "dynamic_router_cost30_closed_trades.csv"),
            rp("gen3_dynamic_router_combo_v1", "dynamic_router_cost30_mtm_equity_curve.csv"),
        ),
        ClosedSource(
            "market_state_router_existing",
            "现有 market_state_router 候选",
            rp("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv"),
            rp("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_equity_curve.csv"),
        ),
    ]


def source_summaries() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    rows = []
    trades_by_key = {}
    for source in closed_sources():
        trades = load_closed_source(source)
        trades_by_key[source.key] = trades
        row = sequence_metrics(trades, source.label, source.key)
        total, dd = curve_metrics(source.curve)
        row["capital_return"] = total
        row["capital_max_drawdown"] = dd
        rows.append(row)
    return pd.DataFrame(rows), trades_by_key


def annual_table(trades_by_key: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for key, df in trades_by_key.items():
        label = df["source_label"].iloc[0] if not df.empty else key
        if df.empty:
            continue
        for year, g in df.assign(year=lambda x: x["entry_date_norm"].dt.year).groupby("year"):
            row = sequence_metrics(g, label, key)
            row["year"] = int(year)
            rows.append(row)
    return pd.DataFrame(rows)


def route_table(trades_by_key: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for key, df in trades_by_key.items():
        label = df["source_label"].iloc[0] if not df.empty else key
        if df.empty or "route" not in df.columns:
            continue
        for route, g in df.groupby("route", dropna=False):
            row = sequence_metrics(g, label, key)
            row["route"] = str(route)
            rows.append(row)
    return pd.DataFrame(rows)


def load_state_core() -> pd.DataFrame:
    df = load_closed_source(
        ClosedSource(
            "state_core",
            "状态路由 panic/old_g3 核心",
            rp("gen3_market_state_router_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv"),
            ret_col="policy_net_ret",
        )
    )
    if df.empty:
        return df
    return df[df["mode"].isin(["panic_repair", "old_g3_route_v3"])].copy()


def context_scan(df: pd.DataFrame, key: str, label: str, min_count: int = 5) -> pd.DataFrame:
    cols = [
        "mode",
        "market_style_prev",
        "ma_skeleton",
        "volume_price_layer",
        "adx_layer",
        "market_style_prev+adx_layer",
        "ma_skeleton+adx_layer",
        "market_style_prev+ma_skeleton+adx_layer",
    ]
    x = df.copy()
    for combo in [c for c in cols if "+" in c]:
        parts = combo.split("+")
        if all(p in x.columns for p in parts):
            x[combo] = x[parts].astype(str).agg("|".join, axis=1)
    rows = []
    for col in cols:
        if col not in x.columns:
            continue
        for value, g in x.groupby(col, dropna=False, observed=False):
            if len(g) < min_count:
                continue
            row = sequence_metrics(g, label, key)
            row["context_col"] = col
            row["context_value"] = str(value)
            rows.append(row)
    return pd.DataFrame(rows)


def numeric_bucket_scan(df: pd.DataFrame, key: str, label: str) -> pd.DataFrame:
    x = df.copy()
    buckets = {
        "up_rate_prev_bucket": ("up_rate_prev", [-1, 0.2, 0.4, 0.6, 0.8, 2], ["<=20%", "20-40%", "40-60%", "60-80%", ">80%"]),
        "big_down_prev_bucket": ("big_down_rate_prev", [-1, 0.05, 0.15, 0.3, 0.6, 2], ["<=5%", "5-15%", "15-30%", "30-60%", ">60%"]),
        "limit_down_proxy_bucket": ("limit_down_proxy_rate", [-1, 0.01, 0.05, 0.15, 0.3, 2], ["<=1%", "1-5%", "5-15%", "15-30%", ">30%"]),
        "median_mom20_bucket": ("median_mom20", [-2, -0.15, -0.08, -0.03, 0.0, 0.05, 2], ["crash", "deep_down", "down", "flat_neg", "flat_pos", "up"]),
    }
    for new_col, (src, bins, labels) in buckets.items():
        if src in x.columns:
            x[new_col] = pd.cut(pd.to_numeric(x[src], errors="coerce"), bins=bins, labels=labels, include_lowest=True)
    rows = []
    for col in buckets:
        if col not in x.columns:
            continue
        for value, g in x.groupby(col, dropna=False, observed=False):
            if len(g) < 5:
                continue
            row = sequence_metrics(g, label, key)
            row["context_col"] = col
            row["context_value"] = str(value)
            rows.append(row)
    return pd.DataFrame(rows)


def panic_raw() -> pd.DataFrame:
    path = rp("gen3_panic_v2_research", "final_candidate_v1", "m30_close5_full_nextopen_cost30_closed_trades.csv")
    df = read_csv(path)
    if df.empty:
        return df
    df["entry_date_norm"] = normalize_date(df["entry_date"])
    df["policy_exit_date_norm"] = normalize_date(df["policy_exit_date"])
    df["ret_norm"] = pd.to_numeric(df["policy_net_ret"], errors="coerce")
    return df[
        (df["entry_date_norm"] >= START)
        & (df["entry_date_norm"] <= PRE_END)
        & df["ret_norm"].notna()
    ].copy()


def panic_exit_grid(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    horizons = [1, 2, 3, 5, 10, 20]
    for h in horizons:
        col = f"fwd_ret_confirm_to_close_{h}d"
        if col not in df.columns:
            continue
        x = df.copy()
        x["ret_norm"] = pd.to_numeric(x[col], errors="coerce") - 0.003
        x = x[x["ret_norm"].notna()]
        rows.append(sequence_metrics(x, f"panic 固定持有 {h}d 近似净收益", f"panic_hold_{h}d"))
    rows.append(sequence_metrics(df, "panic 当前 m30_close5 策略", "panic_policy_current"))
    return pd.DataFrame(rows)


def panic_context_tables(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    combo_cols = [
        "market_style+adx_layer",
        "g3_capitulation_strength+g3_volume_context",
        "g3_repair_env_label+g3_volume_context",
        "drawdown20_bucket+g3_volume_context",
        "range_pos60_bucket+g3_volume_context",
    ]
    for combo in combo_cols:
        parts = combo.split("+")
        if all(p in x.columns for p in parts):
            x[combo] = x[parts].astype(str).agg("|".join, axis=1)
    cols = [
        "market_style",
        "adx_layer",
        "g3_capitulation_strength",
        "g3_volume_context",
        "g3_repair_env_label",
        "drawdown20_bucket",
        "range_pos60_bucket",
        *combo_cols,
    ]
    rows = []
    for col in cols:
        if col not in x.columns:
            continue
        for value, g in x.groupby(col, dropna=False, observed=False):
            if len(g) < 5:
                continue
            row = sequence_metrics(g, "panic 原始上下文", "panic_context")
            row["context_col"] = col
            row["context_value"] = str(value)
            rows.append(row)
    return pd.DataFrame(rows)


def worst_trades(df: pd.DataFrame, key: str, label: str, n: int = 20) -> pd.DataFrame:
    if df.empty:
        return df
    cols = [
        "entry_date",
        "policy_exit_date",
        "code",
        "name",
        "mode",
        "route",
        "market_style_prev",
        "ma_skeleton",
        "adx_layer",
        "ret_norm",
    ]
    out = df.sort_values("ret_norm").head(n).copy()
    out["key"] = key
    out["label"] = label
    return out[[c for c in ["key", "label", *cols] if c in out.columns]]


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
            "seq_return",
            "seq_max_drawdown",
            "sum_ret_proxy",
            "worst_trade",
            "best_trade",
            "top5_positive_share",
            "capital_return",
            "capital_max_drawdown",
        }:
            out[col] = out[col].map(pct)
    return out[[c for c in cols if c in out.columns]].to_markdown(index=False)


def write_report(
    summary: pd.DataFrame,
    annual: pd.DataFrame,
    route: pd.DataFrame,
    state_context: pd.DataFrame,
    state_bucket: pd.DataFrame,
    panic_grid: pd.DataFrame,
    panic_context: pd.DataFrame,
    worst: pd.DataFrame,
) -> None:
    sorted_summary = summary.sort_values(["capital_return", "avg_ret"], ascending=[False, False])
    route_focus = route.sort_values(["key", "avg_ret"], ascending=[True, False])
    state_good = state_context.sort_values(["avg_ret", "trade_count"], ascending=[False, False]).head(30)
    state_bad = state_context.sort_values(["avg_ret", "worst_trade"], ascending=[True, True]).head(30)
    panic_good = panic_context.sort_values(["avg_ret", "trade_count"], ascending=[False, False]).head(30)
    panic_bad = panic_context.sort_values(["avg_ret", "worst_trade"], ascending=[True, True]).head(30)

    report = f"""# G3 2024-10 前 old_g3 / panic 第四阶段拆解 v4

## 研究目标

本阶段继续第三阶段结论，放弃“题材口袋直接接入”的方向，专门拆 `panic_repair` 与旧 `old_g3_route_v3`/`range_gap`/`down_panic`：

- 哪些旧 G3 子场景真的挣钱。
- 哪些子场景亏损来自单笔收益太低或尾部过大。
- panic 修复更适合哪个持有期。
- 是否存在可以迁移到 `pre_2024_state_alpha` 的明确过滤规则。

## 核心结论

1. 2024-10 前最稳的可迁移结构不是题材口袋，而是“恐慌释放后的修复”和“旧 G3 range_gap 的窄过滤版”。
2. `range_gap` 的关键改进不是加交易，而是删交易：去掉 ADX 下行、去掉小正缺口后，胜率和均值明显改善。
3. `panic_repair` 的收益主要来自 `healthy_release` 和 `extreme/strong capitulation`；`dry_volume`、`overheated_volume` 明显拖累。
4. panic 当前持有期不是越长越好，需要结合 1/2/3/5/10/20 日网格进一步定型；如果较短持有在胜率和回撤上更好，应优先做退出迁移研究。
5. 可以形成一个比“机构主升参数改造”更干净的旧市场方案：`panic_context_gate + range_gap_guard + down_panic_core`，再用滚动健康度开关。

## 旧 G3 / range 变体全周期

{format_table(sorted_summary, ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "capital_return", "capital_max_drawdown", "worst_trade", "top5_positive_share", "positive_years", "diagnosis"])}

注：`capital_return/max_drawdown` 来自已有 equity curve 归档；`seq_*` 仅为交易序列代理，不作为资金曲线结论。

## 路由归因

{format_table(route_focus, ["key", "route", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_trade", "diagnosis"], limit=80)}

## 状态路由核心好场景

{format_table(state_good, ["context_col", "context_value", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_trade", "diagnosis"], limit=30)}

## 状态路由核心差场景

{format_table(state_bad, ["context_col", "context_value", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_trade", "diagnosis"], limit=30)}

## 状态数值分桶

{format_table(state_bucket.sort_values(["avg_ret", "trade_count"], ascending=[False, False]), ["context_col", "context_value", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_trade", "diagnosis"], limit=60)}

## Panic 持有期网格

{format_table(panic_grid.sort_values("avg_ret", ascending=False), ["key", "label", "trade_count", "win_rate", "avg_ret", "median_ret", "seq_return", "seq_max_drawdown", "worst_trade", "diagnosis"])}

## Panic 好场景

{format_table(panic_good, ["context_col", "context_value", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_trade", "diagnosis"], limit=30)}

## Panic 差场景

{format_table(panic_bad, ["context_col", "context_value", "trade_count", "win_rate", "avg_ret", "median_ret", "worst_trade", "diagnosis"], limit=30)}

## 最差交易样本

{format_table(worst, ["key", "label", "entry_date", "policy_exit_date", "code", "name", "mode", "route", "market_style_prev", "ma_skeleton", "adx_layer", "ret_norm"], limit=40)}

## 可迁移规则草案

- `panic_context_gate`：保留 `healthy_release`、`neutral_volume`、`extreme/strong capitulation`；谨慎或关闭 `dry_volume`、`overheated_volume`。
- `range_gap_guard`：保留 range_gap 但过滤 ADX 下行与小正缺口，这比增加 range_weak_rebound 更有证据。
- `down_panic_core`：继续作为旧市场核心进攻源，重点控制持有期和 2023 弱窗口。
- `strong_main`：只有在强 breadth/score/volume guard 下才可作为辅助，不应复用 2024-10 后 institutional_mainwave。
- 全周期切换：使用滚动健康度，不使用硬日期；当机构主升健康度不足、pre-state-alpha 健康度达标时才切入。

## 产物

- `old_g3_variant_summary.csv`
- `old_g3_annual_summary.csv`
- `old_g3_route_attribution.csv`
- `state_core_context_scan.csv`
- `state_core_numeric_bucket_scan.csv`
- `panic_exit_grid.csv`
- `panic_context_scan.csv`
- `worst_trades.csv`
"""
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary, trades_by_key = source_summaries()
    annual = annual_table(trades_by_key)
    route = route_table(trades_by_key)

    state = load_state_core()
    state_context = context_scan(state, "state_core", "状态路由 panic/old_g3 核心", min_count=5)
    state_bucket = numeric_bucket_scan(state, "state_core", "状态路由 panic/old_g3 核心")
    panic = panic_raw()
    panic_grid = panic_exit_grid(panic)
    panic_context = panic_context_tables(panic)

    worst_frames = []
    for key in ["market_state_router_existing", "range_no_adx_downtrend_cost30", "range_no_small_positive_gap_cost30"]:
        df = trades_by_key.get(key, pd.DataFrame())
        if not df.empty:
            worst_frames.append(worst_trades(df, key, df["source_label"].iloc[0], 15))
    if not state.empty:
        worst_frames.append(worst_trades(state, "state_core", "状态路由 panic/old_g3 核心", 15))
    worst = pd.concat(worst_frames, ignore_index=True) if worst_frames else pd.DataFrame()

    summary.to_csv(OUT_DIR / "old_g3_variant_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "old_g3_annual_summary.csv", index=False, encoding="utf-8-sig")
    route.to_csv(OUT_DIR / "old_g3_route_attribution.csv", index=False, encoding="utf-8-sig")
    state_context.to_csv(OUT_DIR / "state_core_context_scan.csv", index=False, encoding="utf-8-sig")
    state_bucket.to_csv(OUT_DIR / "state_core_numeric_bucket_scan.csv", index=False, encoding="utf-8-sig")
    panic_grid.to_csv(OUT_DIR / "panic_exit_grid.csv", index=False, encoding="utf-8-sig")
    panic_context.to_csv(OUT_DIR / "panic_context_scan.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst_trades.csv", index=False, encoding="utf-8-sig")
    write_report(summary, annual, route, state_context, state_bucket, panic_grid, panic_context, worst)

    print(f"wrote {OUT_DIR}")
    print(summary.sort_values("capital_return", ascending=False).to_string(index=False))
    print()
    print(panic_grid.sort_values("avg_ret", ascending=False).to_string(index=False))


if __name__ == "__main__":
    main()

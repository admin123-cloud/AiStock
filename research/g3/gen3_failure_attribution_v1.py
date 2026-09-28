from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_failure_attribution_v1"


MAIN_DIR = ROOT / "reports" / "gen3_combo_panic_strong_execution_stress_v1"
RANGE_WEAK_DIR = ROOT / "reports" / "gen3_range_weak_shadow_mtm_v1"
WEAK_QUALITY_DIR = ROOT / "reports" / "gen3_combo_with_weak_quality_v2_shadow_v1"
RANGE_WEAK_COMBO_DIR = ROOT / "reports" / "gen3_combo_with_range_weak_shadow_v1"
PACKAGE_DIR = ROOT / "reports" / "gen3_final_candidate_package_v1"


def read_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path, low_memory=False)


def pct(x: Any) -> str:
    try:
        if pd.isna(x):
            return "--"
        return f"{float(x) * 100:.2f}%"
    except Exception:
        return "--"


def num(x: Any, digits: int = 2) -> str:
    try:
        if pd.isna(x):
            return "--"
        return f"{float(x):.{digits}f}"
    except Exception:
        return "--"


def normalize_book_curve(curve: pd.DataFrame) -> pd.DataFrame:
    if curve.empty or "date" not in curve.columns or "equity" not in curve.columns:
        return pd.DataFrame()
    out = curve.copy()
    out["date"] = pd.to_datetime(out["date"])
    first_equity = float(out["equity"].dropna().iloc[0])
    out["norm_equity"] = out["equity"] / first_equity if first_equity else np.nan
    out["year"] = out["date"].dt.year
    return out


def curve_summary(book: str, curve: pd.DataFrame) -> dict[str, Any]:
    curve = normalize_book_curve(curve)
    if curve.empty:
        return {"book": book, "available": False}
    start = curve.iloc[0]
    end = curve.iloc[-1]
    pre_2025 = curve[curve["date"] < pd.Timestamp("2025-01-01")]
    post_2025 = curve[curve["date"] >= pd.Timestamp("2025-01-01")]
    total_return = float(end["norm_equity"] - 1)
    pre_return = float(pre_2025.iloc[-1]["norm_equity"] - 1) if len(pre_2025) else np.nan
    post_increment = float(end["norm_equity"] - post_2025.iloc[0]["norm_equity"]) if len(post_2025) else np.nan
    post_share = post_increment / total_return if total_return else np.nan
    return {
        "book": book,
        "available": True,
        "start_date": start["date"].date().isoformat(),
        "end_date": end["date"].date().isoformat(),
        "total_return": total_return,
        "max_drawdown": float(curve["drawdown"].min()) if "drawdown" in curve.columns else np.nan,
        "worst_open_mtm_ret": float(curve["worst_open_mtm_ret"].min()) if "worst_open_mtm_ret" in curve.columns else np.nan,
        "pre_2025_return": pre_return,
        "post_2025_increment": post_increment,
        "post_2025_profit_share": post_share,
        "exposure_days": int((curve.get("open_positions", pd.Series(dtype=float)).fillna(0) > 0).sum()),
        "calendar_days": int(len(curve)),
        "exposure_day_ratio": float((curve.get("open_positions", pd.Series(dtype=float)).fillna(0) > 0).mean()),
    }


def trade_summary(book: str, trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {"book": book, "available": False}
    df = trades.copy()
    ret_col = "net_ret" if "net_ret" in df.columns else "policy_net_ret"
    if ret_col not in df.columns:
        return {"book": book, "available": False}
    rets = pd.to_numeric(df[ret_col], errors="coerce")
    return {
        "book": book,
        "available": True,
        "closed": int(len(df)),
        "win_rate": float((rets > 0).mean()),
        "mean_trade_ret": float(rets.mean()),
        "median_trade_ret": float(rets.median()),
        "worst_trade": float(rets.min()),
        "bad5_count": int((rets <= -0.05).sum()),
        "bad10_count": int((rets <= -0.10).sum()),
        "bad10_rate": float((rets <= -0.10).mean()),
        "total_realized_pnl": float(pd.to_numeric(df.get("realized_pnl", 0), errors="coerce").fillna(0).sum()),
    }


def group_trades(trades: pd.DataFrame, keys: list[str], book: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    ret_col = "net_ret" if "net_ret" in trades.columns else "policy_net_ret"
    df = trades.copy()
    df[ret_col] = pd.to_numeric(df[ret_col], errors="coerce")
    df["realized_pnl"] = pd.to_numeric(df.get("realized_pnl", 0), errors="coerce").fillna(0)
    for key in keys:
        if key not in df.columns:
            df[key] = "missing"
        df[key] = df[key].fillna("missing").astype(str)
    grouped = df.groupby(keys, dropna=False).agg(
        closed=(ret_col, "size"),
        win_rate=(ret_col, lambda x: float((x > 0).mean())),
        mean_trade_ret=(ret_col, "mean"),
        median_trade_ret=(ret_col, "median"),
        worst_trade=(ret_col, "min"),
        bad10_count=(ret_col, lambda x: int((x <= -0.10).sum())),
        realized_pnl=("realized_pnl", "sum"),
    ).reset_index()
    grouped.insert(0, "book", book)
    return grouped.sort_values(["realized_pnl", "worst_trade"], ascending=[True, True])


def top_worst(trades: pd.DataFrame, book: str, n: int = 30) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    ret_col = "net_ret" if "net_ret" in trades.columns else "policy_net_ret"
    df = trades.copy()
    df[ret_col] = pd.to_numeric(df[ret_col], errors="coerce")
    cols = [
        "entry_date", "trade_date", "exit_date", "policy_exit_date", "code", "name", "chain",
        "g3_chain", "market_style", "g3_market_style", ret_col, "realized_pnl",
        "entry_price", "exit_price", "exit_reason_proxy", "execution_variant",
    ]
    available = [c for c in cols if c in df.columns]
    out = df.sort_values(ret_col).head(n)[available].copy()
    out.insert(0, "book", book)
    if ret_col != "net_ret":
        out = out.rename(columns={ret_col: "net_ret"})
    return out


def annual_gap_table() -> pd.DataFrame:
    tables = []
    main = read_csv(MAIN_DIR / "combo_execution_stress_annual_raw.csv")
    if not main.empty:
        tables.append(main[main["profile"].eq("base_30bps")].assign(book="main_panic_strong_30bps"))
    rw = read_csv(RANGE_WEAK_DIR / "annual.csv")
    if not rw.empty:
        tables.append(rw[rw["cost_bps"].eq(30)].assign(profile=lambda x: x["book"]))
    weak = read_csv(WEAK_QUALITY_DIR / "annual.csv")
    if not weak.empty:
        tables.append(weak.assign(profile=lambda x: x["book"], book=lambda x: "weak_quality_combo_" + x["book"].astype(str)))
    range_combo = read_csv(RANGE_WEAK_COMBO_DIR / "annual.csv")
    if not range_combo.empty:
        tables.append(range_combo.assign(profile=lambda x: x["book"], book=lambda x: "range_weak_combo_" + x["book"].astype(str)))
    if not tables:
        return pd.DataFrame()
    cols = ["book", "profile", "year", "return", "max_drawdown", "worst_open_mtm_ret", "closed", "win_rate", "mean_trade_ret", "worst_trade"]
    return pd.concat(tables, ignore_index=True)[[c for c in cols if c in pd.concat(tables, ignore_index=True).columns]]


def benchmark_pivot() -> pd.DataFrame:
    bench = read_csv(PACKAGE_DIR / "benchmark_window_excess.csv")
    if bench.empty:
        return pd.DataFrame()
    keep = bench[["window", "benchmark_name", "strategy_return", "benchmark_return", "strategy_excess", "strategy_drawdown", "benchmark_drawdown"]].copy()
    return keep.sort_values(["window", "strategy_excess"])


def write_report(summary: pd.DataFrame, annual: pd.DataFrame, chain: pd.DataFrame, style: pd.DataFrame, worst: pd.DataFrame, bench: pd.DataFrame) -> None:
    main_row = summary[summary["book"].eq("main_panic_strong_30bps")].iloc[0].to_dict()
    range_row = summary[summary["book"].eq("range_weak_50_50_cost30")].iloc[0].to_dict()

    bad_years = annual.copy()
    if not bad_years.empty and "return" in bad_years.columns:
        bad_years = bad_years.sort_values("return").head(12)

    chain_focus = chain.copy()
    if not chain_focus.empty:
        chain_focus = chain_focus.sort_values("realized_pnl", ascending=True).head(12)

    style_focus = style.copy()
    if not style_focus.empty:
        style_focus = style_focus.sort_values("realized_pnl", ascending=True).head(12)

    lines = [
        "# G3 失败归因审计 v1",
        "",
        "## 结论先行",
        "",
        f"- 正式候选 `panic + strong` 全周期收益 {pct(main_row.get('total_return'))}，最大回撤 {pct(main_row.get('max_drawdown'))}，但 2025 年以后贡献占比 {pct(main_row.get('post_2025_profit_share'))}，收益集中问题仍然存在。",
        f"- `range_weak_50_50` 影子组合全周期收益 {pct(range_row.get('total_return'))}，最大回撤 {pct(range_row.get('max_drawdown'))}，交易次数不低但收益质量不足，暂不能正式化。",
        "- G3 当前不是实盘策略，只是研究候选。下一步必须优先修复市场环境识别和链路独立触发源，而不是继续把低质量链路塞进组合。",
        "",
        "## 全局曲线摘要",
        "",
        summary.to_markdown(index=False),
        "",
        "## 年度最弱切片",
        "",
        bad_years.to_markdown(index=False) if not bad_years.empty else "无年度表。",
        "",
        "## 链路贡献较弱项",
        "",
        chain_focus.to_markdown(index=False) if not chain_focus.empty else "无链路分组表。",
        "",
        "## 市场风格较弱项",
        "",
        style_focus.to_markdown(index=False) if not style_focus.empty else "无市场风格分组表。",
        "",
        "## 最差单票样本",
        "",
        worst.head(20).to_markdown(index=False) if not worst.empty else "无最差交易表。",
        "",
        "## 基准相对表现",
        "",
        bench.to_markdown(index=False) if not bench.empty else "无基准表。",
        "",
        "## 下一步目标",
        "",
        "1. 把市场环境分类从单一指数强弱改为三层四态：均线骨架、量价结构、ADX 趋势强度，再映射到 panic/range/weak/strong 四种打法。",
        "2. 对 strong 链路单独做尾部风险画像，先解释最差 20 笔为什么亏，再决定是否能保留。",
        "3. range/weak 暂停正式组合，只保留研究标签；下一步寻找新的箱体底部/弱反弹独立触发源。",
    ]
    (OUT_DIR / "g3_failure_attribution_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    main_trades = read_csv(MAIN_DIR / "base_30bps_closed_trades.csv")
    main_curve = read_csv(MAIN_DIR / "base_30bps_curve.csv")
    range_trades = read_csv(RANGE_WEAK_DIR / "range_weak_50_50_cost30_closed_trades.csv")
    range_curve = read_csv(RANGE_WEAK_DIR / "range_weak_50_50_cost30_mtm_curve.csv")

    summaries = [
        {**curve_summary("main_panic_strong_30bps", main_curve), **trade_summary("main_panic_strong_30bps", main_trades)},
        {**curve_summary("range_weak_50_50_cost30", range_curve), **trade_summary("range_weak_50_50_cost30", range_trades)},
    ]
    summary_df = pd.DataFrame(summaries)

    main_by_chain = group_trades(main_trades, ["chain"], "main_panic_strong_30bps")
    range_by_chain = group_trades(range_trades, ["g3_chain"], "range_weak_50_50_cost30")
    chain_df = pd.concat([main_by_chain, range_by_chain], ignore_index=True)

    main_style_col = "g3_market_style" if "g3_market_style" in main_trades.columns else "market_style"
    main_by_style = group_trades(main_trades, [main_style_col, "chain"], "main_panic_strong_30bps")
    range_by_style = group_trades(range_trades, ["market_style", "g3_chain"], "range_weak_50_50_cost30")
    style_df = pd.concat([main_by_style, range_by_style], ignore_index=True)

    worst_df = pd.concat([
        top_worst(main_trades, "main_panic_strong_30bps"),
        top_worst(range_trades, "range_weak_50_50_cost30"),
    ], ignore_index=True)

    annual_df = annual_gap_table()
    bench_df = benchmark_pivot()

    summary_df.to_csv(OUT_DIR / "curve_trade_summary.csv", index=False, encoding="utf-8-sig")
    annual_df.to_csv(OUT_DIR / "annual_weak_slices.csv", index=False, encoding="utf-8-sig")
    chain_df.to_csv(OUT_DIR / "chain_contribution.csv", index=False, encoding="utf-8-sig")
    style_df.to_csv(OUT_DIR / "market_style_contribution.csv", index=False, encoding="utf-8-sig")
    worst_df.to_csv(OUT_DIR / "worst_trades_top.csv", index=False, encoding="utf-8-sig")
    bench_df.to_csv(OUT_DIR / "benchmark_excess_audit.csv", index=False, encoding="utf-8-sig")

    meta = {
        "inputs": {
            "main_trades": str(MAIN_DIR / "base_30bps_closed_trades.csv"),
            "main_curve": str(MAIN_DIR / "base_30bps_curve.csv"),
            "range_weak_trades": str(RANGE_WEAK_DIR / "range_weak_50_50_cost30_closed_trades.csv"),
            "range_weak_curve": str(RANGE_WEAK_DIR / "range_weak_50_50_cost30_mtm_curve.csv"),
        },
        "outputs": [p.name for p in OUT_DIR.glob("*")],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(summary_df, annual_df, chain_df, style_df, worst_df, bench_df)


if __name__ == "__main__":
    main()

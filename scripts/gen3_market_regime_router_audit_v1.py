from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_market_regime_router_audit_v1"

MAIN_TRADES = ROOT / "reports" / "gen3_combo_panic_strong_execution_stress_v1" / "base_30bps_closed_trades.csv"
RANGE_WEAK_TRADES = ROOT / "reports" / "gen3_range_weak_shadow_mtm_v1" / "range_weak_50_50_cost30_closed_trades.csv"


ROUTE_EXPECTATION = {
    "standard_downtrend": {"panic"},
    "standard_range": {"panic", "range"},
    "weak_rebound": {"weak", "strong_probe"},
    "standard_uptrend": {"strong"},
    "unknown": set(),
}


def read_trades(path: Path, book: str) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    df["book"] = book
    if "chain" not in df.columns:
        df["chain"] = np.nan
    if "g3_chain" not in df.columns:
        df["g3_chain"] = np.nan
    df["route_chain"] = df["chain"].fillna("")
    range_mask = df["g3_chain"].astype(str).str.contains("range", na=False)
    weak_mask = df["g3_chain"].astype(str).str.contains("weak", na=False)
    df.loc[range_mask, "route_chain"] = "range"
    df.loc[weak_mask, "route_chain"] = "weak"
    df["route_chain"] = df["route_chain"].replace("", np.nan).fillna("unknown")
    return df


def fixed_four_state(row: pd.Series) -> str:
    existing = row.get("market_style")
    if isinstance(existing, str) and existing in {"standard_uptrend", "weak_rebound", "standard_range", "standard_downtrend"}:
        return existing

    g3_style = row.get("g3_market_style")
    if g3_style == "main_up":
        return "standard_uptrend"
    if g3_style == "weak_recovery":
        return "weak_rebound"
    if g3_style == "defense_or_failed":
        return "standard_downtrend"

    ma = row.get("ma_skeleton")
    adx = row.get("adx_layer")
    up_rate = pd.to_numeric(row.get("up_rate"), errors="coerce")
    breadth = pd.to_numeric(row.get("breadth_ma20"), errors="coerce")
    index_mom20 = pd.to_numeric(row.get("index_mom20"), errors="coerce")
    amount_ratio = pd.to_numeric(row.get("market_amount_ratio20"), errors="coerce")

    if ma == "bull_stack" and adx == "uptrend_strength" and pd.notna(breadth) and breadth >= 0.55 and pd.notna(up_rate) and up_rate >= 0.50 and pd.notna(index_mom20) and index_mom20 > 0:
        return "standard_uptrend"
    if ma == "bear_stack" and (adx == "downtrend_strength" or (pd.notna(up_rate) and up_rate <= 0.25) or (pd.notna(breadth) and breadth <= 0.35)):
        return "standard_downtrend"
    if ma in {"weak_repair", "mixed"} and pd.notna(index_mom20) and index_mom20 > -0.03 and pd.notna(amount_ratio) and amount_ratio >= 0.90 and pd.notna(up_rate) and up_rate >= 0.40:
        return "weak_rebound"
    if ma in {"mixed", "bull_stack"}:
        return "standard_range"
    return "unknown"


def expected_route(state: str, route_chain: str) -> str:
    expected = ROUTE_EXPECTATION.get(state, set())
    if not expected:
        return "unknown_state"
    if route_chain in expected:
        return "aligned"
    if state == "weak_rebound" and route_chain == "strong":
        return "strong_used_as_probe_in_weak_rebound"
    return "mismatch"


def summarize(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["net_ret"] = pd.to_numeric(d["net_ret"], errors="coerce")
    d["realized_pnl"] = pd.to_numeric(d.get("realized_pnl", 0), errors="coerce").fillna(0)
    return (
        d.groupby(keys, dropna=False)
        .agg(
            closed=("net_ret", "size"),
            win_rate=("net_ret", lambda x: float((x > 0).mean())),
            mean_net_ret=("net_ret", "mean"),
            median_net_ret=("net_ret", "median"),
            worst_net_ret=("net_ret", "min"),
            bad10_count=("net_ret", lambda x: int((x <= -0.10).sum())),
            realized_pnl=("realized_pnl", "sum"),
        )
        .reset_index()
        .sort_values(["realized_pnl", "worst_net_ret"], ascending=[True, True])
    )


def coverage(df: pd.DataFrame) -> pd.DataFrame:
    fields = ["ma_skeleton", "volume_price_layer", "adx_layer", "breadth_ma20", "up_rate", "market_amount_ratio20", "index_mom20", "market_style", "g3_market_style"]
    rows: list[dict[str, Any]] = []
    for (book, chain), g in df.groupby(["book", "route_chain"], dropna=False):
        row = {"book": book, "route_chain": chain, "rows": len(g)}
        for f in fields:
            row[f"{f}_coverage"] = float(g[f].notna().mean()) if f in g.columns else 0.0
        rows.append(row)
    return pd.DataFrame(rows)


def write_report(regime: pd.DataFrame, align: pd.DataFrame, cov: pd.DataFrame, worst_mismatch: pd.DataFrame) -> None:
    strong_cov = cov[cov["route_chain"].eq("strong")]
    strong_layer_ok = False
    if not strong_cov.empty:
        strong_layer_ok = all(float(strong_cov.iloc[0].get(f"{c}_coverage", 0)) > 0.95 for c in ["ma_skeleton", "volume_price_layer", "adx_layer"])

    mismatch = align[align["route_alignment"].eq("mismatch")]
    weak_probe = align[align["route_alignment"].eq("strong_used_as_probe_in_weak_rebound")]

    lines = [
        "# G3 市场环境路由审计 v1",
        "",
        "## 结论先行",
        "",
        f"- strong 链路三层字段覆盖是否完整：`{strong_layer_ok}`。当前 strong 主要依赖 `g3_market_style=main_up/weak_recovery`，不是完整三层四态路由。",
        f"- 明确错配交易数：{int(mismatch['closed'].sum()) if not mismatch.empty else 0}；弱反弹中把 strong 当观察打法的交易数：{int(weak_probe['closed'].sum()) if not weak_probe.empty else 0}。",
        "- 这说明 G3 还没有真正实现“不同市场不同买法”，只是部分链路贴了市场标签；下一步应先把路由器独立出来，再让四条链路分别服从路由。",
        "",
        "## 四态 x 链路表现",
        "",
        regime.to_markdown(index=False) if not regime.empty else "无数据。",
        "",
        "## 路由对齐情况",
        "",
        align.to_markdown(index=False) if not align.empty else "无数据。",
        "",
        "## 三层字段覆盖率",
        "",
        cov.to_markdown(index=False) if not cov.empty else "无数据。",
        "",
        "## 错配/探针中的最差样本",
        "",
        worst_mismatch.to_markdown(index=False) if not worst_mismatch.empty else "无错配样本。",
        "",
        "## 下一步目标",
        "",
        "1. 新建独立 `gen3_market_router_v2`：输入只用 D-1 或可证明盘中可见的三层字段，输出固定四态。",
        "2. 四条链路只读取路由结果：downtrend -> panic，range -> range，weak_rebound -> weak_rebound/小仓 strong_probe，uptrend -> strong。",
        "3. 对每个状态分别做候选源重建，禁止用同一套选股体系横跨四种市场。",
    ]
    (OUT_DIR / "g3_market_regime_router_audit_report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    main = read_trades(MAIN_TRADES, "main_panic_strong_30bps")
    range_weak = read_trades(RANGE_WEAK_TRADES, "range_weak_50_50_cost30")
    df = pd.concat([main, range_weak], ignore_index=True)

    df["fixed_four_state"] = df.apply(fixed_four_state, axis=1)
    df["route_alignment"] = df.apply(lambda r: expected_route(str(r["fixed_four_state"]), str(r["route_chain"])), axis=1)

    regime = summarize(df, ["book", "fixed_four_state", "route_chain"])
    align = summarize(df, ["book", "fixed_four_state", "route_chain", "route_alignment"])
    cov = coverage(df)

    mismatch_mask = df["route_alignment"].isin(["mismatch", "strong_used_as_probe_in_weak_rebound"])
    worst_cols = [c for c in ["book", "entry_date", "trade_date", "code", "name", "fixed_four_state", "route_chain", "route_alignment", "net_ret", "realized_pnl", "ma_skeleton", "volume_price_layer", "adx_layer", "market_style", "g3_market_style"] if c in df.columns]
    worst = df[mismatch_mask].copy()
    worst["net_ret"] = pd.to_numeric(worst["net_ret"], errors="coerce")
    worst = worst.sort_values("net_ret").head(30)[worst_cols]

    regime.to_csv(OUT_DIR / "four_state_chain_performance.csv", index=False, encoding="utf-8-sig")
    align.to_csv(OUT_DIR / "route_alignment_summary.csv", index=False, encoding="utf-8-sig")
    cov.to_csv(OUT_DIR / "three_layer_field_coverage.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "misrouted_or_probe_worst_trades.csv", index=False, encoding="utf-8-sig")
    write_report(regime, align, cov, worst)


if __name__ == "__main__":
    main()

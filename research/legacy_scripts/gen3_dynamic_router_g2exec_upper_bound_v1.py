from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.gen3_build_dynamic_router_combo_v1 as router


OUT_DIR = ROOT / "reports" / "gen3_dynamic_router_g2exec_upper_bound_v1"
G2_FULL_TRADES = ROOT / "reports" / "gen2_v2_complete_strategy_2020" / "runs" / "official" / "full" / "trades.csv"
G2_ONLY = ROOT / "reports" / "gen3_g2_full_gap_attribution_v1" / "g2_only_trades.csv"
STRONG_PLUSWEAK = (
    ROOT / "reports" / "gen3_strong_v2_independent_source_v1" / "strong_v2_main_up_plus_weak04_hold5_closed_trades.csv"
)


def _key(df: pd.DataFrame, date_col: str = "entry_date") -> pd.Series:
    date = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d")
    return df["code"].astype(str) + "|" + date.astype(str)


def _max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def _pct(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"{float(value) * 100:.2f}%"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = _pct(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def _load_g2_exec() -> pd.DataFrame:
    t = pd.read_csv(G2_FULL_TRADES, low_memory=False)
    t["entry_date"] = pd.to_datetime(t["buy_date"], errors="coerce").dt.normalize()
    t["exit_date"] = pd.to_datetime(t["sell_date"], errors="coerce").dt.normalize()
    for col in ["capital", "pnl", "return"]:
        t[col] = pd.to_numeric(t[col], errors="coerce")
    g = (
        t.dropna(subset=["entry_date", "exit_date", "code"])
        .groupby(["code", "entry_date"], dropna=False)
        .agg(
            g2_exit_date=("exit_date", "max"),
            g2_rows=("code", "size"),
            g2_capital=("capital", "sum"),
            g2_pnl=("pnl", "sum"),
            g2_avg_row_return=("return", "mean"),
            g2_exit_reason=("exit_reason", lambda x: "|".join(sorted(set(str(v) for v in x if pd.notna(v))))),
        )
        .reset_index()
    )
    g["key"] = _key(g)
    g["g2_net_ret"] = g["g2_pnl"] / g["g2_capital"].replace(0, pd.NA)
    return g[["key", "g2_exit_date", "g2_net_ret", "g2_rows", "g2_exit_reason", "g2_pnl"]]


def _standardize_strong_plusweak(g2_exec: pd.DataFrame, use_g2_exec: bool) -> pd.DataFrame:
    d = pd.read_csv(STRONG_PLUSWEAK, low_memory=False)
    out = pd.DataFrame()
    out["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["code"] = d["code"].astype(str)
    out["name"] = d.get("name", "")
    out["route"] = "strong_main"
    out["route_source"] = "strong_plusweak_hold5"
    out["route_priority"] = router.ROUTE_PRIORITY["strong_main"]
    out["score"] = pd.to_numeric(d.get("g3_strong_score", d.get("score_volume5", d.get("v4_score", 0.0))), errors="coerce").fillna(0.0)
    out["entry_price"] = pd.to_numeric(d.get("entry_price"), errors="coerce")
    out["policy_net_ret"] = pd.to_numeric(d.get("net_ret"), errors="coerce")
    out["key"] = _key(out)
    out = out.merge(g2_exec, on="key", how="left")
    out["g2_exec_matched"] = out["g2_net_ret"].notna() & out["g2_exit_date"].notna()
    if use_g2_exec:
        mask = out["g2_exec_matched"]
        out.loc[mask, "policy_exit_date"] = pd.to_datetime(out.loc[mask, "g2_exit_date"]).dt.normalize()
        out.loc[mask, "policy_net_ret"] = pd.to_numeric(out.loc[mask, "g2_net_ret"], errors="coerce")
        out.loc[mask, "route_source"] = "strong_plusweak_g2_exec_upper_bound"
    return out.drop(columns=["key"]).dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"])


def _load_candidates(g2_exec: pd.DataFrame, use_g2_exec: bool) -> pd.DataFrame:
    d = pd.concat(
        [router.standardize_panic(), router.standardize_range(), _standardize_strong_plusweak(g2_exec, use_g2_exec)],
        ignore_index=True,
    )
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "policy_net_ret", "code"]).copy()
    d["route_priority"] = pd.to_numeric(d["route_priority"], errors="coerce").fillna(0)
    d["score"] = pd.to_numeric(d["score"], errors="coerce").fillna(0)
    return d.sort_values(["entry_date", "route_priority", "score"], ascending=[True, False, False])


def _recent_window(curve: pd.DataFrame, start: str = "2024-06-01") -> dict[str, Any]:
    part = curve[pd.to_datetime(curve["date"]).ge(pd.Timestamp(start))].copy()
    if part.empty:
        return {"recent_return": None, "recent_max_drawdown": None}
    start_eq = float(part["equity"].iloc[0])
    end_eq = float(part["equity"].iloc[-1])
    return {
        "recent_return": end_eq / start_eq - 1.0 if start_eq > 0 else None,
        "recent_max_drawdown": _max_drawdown(part["equity"]),
    }


def _load_g2_only() -> pd.DataFrame:
    d = pd.read_csv(G2_ONLY, low_memory=False)
    d["entry_date"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    d = d[d["entry_date"].ge(pd.Timestamp("2024-06-01"))].copy()
    d["key"] = _key(d)
    d["pnl"] = pd.to_numeric(d["pnl"], errors="coerce").fillna(0.0)
    return d


def _coverage(closed: pd.DataFrame, g2_only: pd.DataFrame) -> dict[str, Any]:
    c = closed.copy()
    c["entry_date"] = pd.to_datetime(c["entry_date"], errors="coerce").dt.normalize()
    c["key"] = _key(c)
    covered = g2_only[g2_only["key"].isin(set(c["key"]))].copy()
    total = float(g2_only["pnl"].sum())
    pnl = float(covered["pnl"].sum())
    return {
        "g2_only_captured_count": int(len(covered)),
        "g2_only_captured_pnl_share": pnl / total if total else 0.0,
    }


def _run(label: str, use_g2_exec: bool, g2_exec: pd.DataFrame, g2_only: pd.DataFrame) -> dict[str, Any]:
    router.ROUTE_DAILY_LIMIT = {"down_panic": 2, "range_gap": 1, "strong_main": 2}
    candidates = _load_candidates(g2_exec, use_g2_exec)
    curve, closed = router.simulate(candidates, router.SOURCE_COST_BPS)
    out_dir = OUT_DIR / label
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(out_dir / "candidates_standardized.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(out_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(out_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
    router.summarize_windows(curve, closed).to_csv(out_dir / "window_summary.csv", index=False, encoding="utf-8-sig")
    router.summarize_routes(closed).to_csv(out_dir / "route_attribution.csv", index=False, encoding="utf-8-sig")
    summary = router.summarize(curve, closed, router.SOURCE_COST_BPS)
    summary.update(_recent_window(curve))
    summary.update(_coverage(closed, g2_only))
    strong = candidates[candidates["route"].eq("strong_main")]
    summary["strong_g2_exec_match_count"] = int(strong.get("g2_exec_matched", pd.Series(dtype=bool)).fillna(False).sum())
    summary["strong_candidate_count"] = int(len(strong))
    summary["variant"] = label
    summary["diagnostic_only"] = bool(use_g2_exec)
    return summary


def _write_report(summary_df: pd.DataFrame) -> None:
    lines = [
        "# G3 strong route 套用 G2 执行结果上界诊断 v1",
        "",
        "## 重要边界",
        "",
        "- `plusweak_hold5_limit2` 是正常候选对照。",
        "- `plusweak_g2exec_upper_bound_limit2` 对命中 G2 full 同代码同入场日的 strong 候选，临时套用 G2 full 的实际分批止盈/前低退出结果。",
        "- 第二组是诊断上界，不是可实盘规则，因为它引用了既有 G2 回测成交结果；它只用于判断差距是否主要来自执行器。",
        "",
        "## 结果",
        "",
        _md_table(
            summary_df[
                [
                    "variant",
                    "total_return",
                    "max_drawdown",
                    "recent_return",
                    "recent_max_drawdown",
                    "trade_count",
                    "win_rate",
                    "avg_trade_return",
                    "worst_trade",
                    "g2_only_captured_count",
                    "g2_only_captured_pnl_share",
                    "strong_g2_exec_match_count",
                    "strong_candidate_count",
                ]
            ],
            pct_cols={
                "total_return",
                "max_drawdown",
                "recent_return",
                "recent_max_drawdown",
                "win_rate",
                "avg_trade_return",
                "worst_trade",
                "g2_only_captured_pnl_share",
            },
        ),
        "",
        "## 解释",
        "",
        "- 如果上界明显接近 G2 full，说明 G3 候选源已经能看到很多强势机会，关键缺口在卖法与路由执行。",
        "- 如果上界仍远低于 G2 full，说明还缺少 G2 full 的另一类候选或仓位复利路径。",
        "- 下一步只能迁移 G2 执行思想，不能直接引用 G2 成交结果。",
        "",
    ]
    (OUT_DIR / "report_cn.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g2_exec = _load_g2_exec()
    g2_only = _load_g2_only()
    rows = [
        _run("plusweak_hold5_limit2", False, g2_exec, g2_only),
        _run("plusweak_g2exec_upper_bound_limit2", True, g2_exec, g2_only),
    ]
    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(OUT_DIR / "variant_summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(json.dumps({"status": "completed", "variants": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(summary_df)
    print(summary_df.to_string(index=False))


if __name__ == "__main__":
    main()

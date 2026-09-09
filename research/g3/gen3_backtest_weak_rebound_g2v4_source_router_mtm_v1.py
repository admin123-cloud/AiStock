from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
from typing import Any

import pandas as pd

import gen3_backtest_weak_rebound_g2v4_d1d2_legcash_v1 as legcash


ROOT = _PROJECT_ROOT
OUT_DIR = _report_path() / "gen3_weak_rebound_g2v4_source_router_mtm_v1"

BOOKS = {
    "balanced_router": legcash.ROUTER_BALANCED,
    "range_unknown_router": legcash.ROUTER_UNKNOWN,
}

SOURCE_POLICIES = {
    "wr_all_g2_original": None,
    "wr_volume5_only": {"volume5"},
    "wr_big_bull_only": {"big_bull"},
    "wr_no_g2": set(),
}

EXIT_POLICIES = [
    "base_original",
    "weak_d1d2_half_keep_rest",
]

PROFILES = [
    {"profile": "base_close30", "extra_cost": 0.0, "shock": 0.0},
    {"profile": "shock2", "extra_cost": 0.0, "shock": 0.02},
]


def pct(x: Any) -> str:
    if x is None or pd.isna(x):
        return "--"
    return f"{float(x) * 100:+.2f}%"


def money(x: Any) -> str:
    if x is None or pd.isna(x):
        return "--"
    return f"{float(x):,.0f}"


def md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    money_cols = money_cols or set()
    rows = []
    for _, row in df.iterrows():
        item = {}
        for col in df.columns:
            value = row[col]
            if col in pct_cols:
                item[col] = pct(value)
            elif col in money_cols:
                item[col] = money(value)
            elif isinstance(value, float):
                item[col] = f"{value:.4f}"
            else:
                item[col] = "" if pd.isna(value) else str(value)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def apply_source_policy(candidates: pd.DataFrame, source_policy: str) -> pd.DataFrame:
    allowed = SOURCE_POLICIES[source_policy]
    d = candidates.copy()
    if allowed is None:
        d["source_policy"] = source_policy
        return d
    is_wr_g2 = d["route"].eq("g2_v4_strong") & d["market_style"].eq("weak_rebound")
    keep = ~is_wr_g2 | d["source_family"].isin(allowed)
    out = d[keep].copy()
    out["source_policy"] = source_policy
    return out


def source_policy_delta(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    keys = ["book", "exit_policy", "profile"]
    base = summary[summary["source_policy"].eq("wr_all_g2_original")].copy()
    for _, b in base.iterrows():
        mask = (summary["book"].eq(b["book"])) & (summary["exit_policy"].eq(b["exit_policy"])) & (summary["profile"].eq(b["profile"]))
        for _, row in summary[mask & ~summary["source_policy"].eq("wr_all_g2_original")].iterrows():
            rows.append(
                {
                    "book": row["book"],
                    "exit_policy": row["exit_policy"],
                    "profile": row["profile"],
                    "source_policy": row["source_policy"],
                    "positions": int(row["positions"]),
                    "legs": int(row["legs"]),
                    "total_return": float(row["total_return"]),
                    "max_drawdown": float(row["max_drawdown"]),
                    "return_delta_vs_all": float(row["total_return"]) - float(b["total_return"]),
                    "dd_delta_vs_all": float(row["max_drawdown"]) - float(b["max_drawdown"]),
                }
            )
    return pd.DataFrame(rows).sort_values(keys + ["source_policy"]) if rows else pd.DataFrame()


def route_source_breakdown(closed: pd.DataFrame, labels: dict[str, str]) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    out = (
        closed.groupby(["route", "market_style", "source_family"], dropna=False)
        .agg(
            positions=("candidate_key", "nunique"),
            legs=("candidate_key", "size"),
            pnl=("realized_pnl", "sum"),
            avg_leg_return=("policy_net_ret", "mean"),
            bad5_rate=("policy_net_ret", lambda s: float((pd.to_numeric(s, errors="coerce") <= -0.05).mean())),
        )
        .reset_index()
    )
    for k, v in reversed(labels.items()):
        out.insert(0, k, v)
    return out


def weak_rebound_trade_counts(candidates: pd.DataFrame, book: str, source_policy: str) -> dict[str, Any]:
    wr = candidates[candidates["route"].eq("g2_v4_strong") & candidates["market_style"].eq("weak_rebound")]
    return {
        "book": book,
        "source_policy": source_policy,
        "weak_rebound_g2_positions": int(wr["candidate_key"].nunique()),
        "weak_rebound_g2_legs_source": int(pd.to_numeric(wr.get("source_leg_count", 0), errors="coerce").fillna(0).sum()),
        "weak_rebound_sources": ",".join(sorted(wr["source_family"].dropna().astype(str).unique().tolist())),
        "first_wr_entry": wr["entry_date_ts"].min().strftime("%Y-%m-%d") if not wr.empty else "",
        "last_wr_entry": wr["entry_date_ts"].max().strftime("%Y-%m-%d") if not wr.empty else "",
    }


def write_report(summary: pd.DataFrame, delta: pd.DataFrame, windows: pd.DataFrame, route_breakdown: pd.DataFrame, counts: pd.DataFrame, worst: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_leg_return",
        "bad5_rate",
        "return",
        "return_delta_vs_all",
        "dd_delta_vs_all",
        "avg_return",
        "policy_net_ret",
    }
    money_cols = {"pnl", "realized_pnl"}
    lines = [
        "# G3 weak_rebound G2 v4 来源路由 MTM 复算 v1",
        "",
        "## 回测时间与口径",
        "- 总曲线窗口：2020-01-01 至 2026-06-04。",
        "- G2 v4 实际成交来源窗口：2024-09-26 至 2026-05-20。",
        "- 本轮只改变弱反弹市场 `weak_rebound` 下的 G2 来源允许规则；标准主升 `standard_uptrend` 的 G2 保持原样。",
        "- 现金流规则沿用上一轮 leg-cash：初始资金 150000，G2 单笔 50% 权重、最多同时 2 笔；G3 单笔 20% 权重、最多同时 5 笔；每日最多新开 1 笔。",
        "- 压力口径：`base_close30` 为原始 G2/G3 退出并扣早退 30bps；`shock2` 为所有腿额外 -2% 冲击。",
        "",
        "## 英文名解释",
        "- `wr_all_g2_original`：弱反弹里保留全部 G2 v4 来源，也就是当前基准。",
        "- `wr_volume5_only`：弱反弹里只保留 G2 的 `volume5` 放量反包/修复主线。",
        "- `wr_big_bull_only`：弱反弹里只保留 G2 的 `big_bull` 大阳线后二次突破买点。",
        "- `wr_no_g2`：弱反弹里禁用 G2，作为损失机会的对照组。",
        "- `base_original`：保留原始 G2 分批退出。",
        "- `weak_d1d2_half_keep_rest`：弱反弹 G2 若 D1/D2 走弱，先退 50%，剩余按原 G2 退出。",
        "- `balanced_router`：四状态覆盖内，主升/弱反弹用 G2，标准震荡用 G3，未知上下文不交易。",
        "- `range_unknown_router`：在 balanced 基础上，2020-2023 未知上下文允许 G3 横盘候选补位。",
        "",
        "## 弱反弹 G2 样本数",
        md_table(counts),
        "",
        "## 总体 MTM 结果",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 相对保留全部 G2 的差异",
        md_table(delta, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 路由贡献拆分",
        md_table(route_breakdown, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最差交易腿",
        md_table(worst, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 阶段判断",
        "- 如果 `wr_volume5_only` 接近或优于 `wr_all_g2_original`，说明弱反弹主要应吸收 G2 的放量反包/修复逻辑。",
        "- 如果 `wr_big_bull_only` 明显变差，说明弱反弹里的二次突破更像少数机会，不能单独承载路由。",
        "- 如果 `wr_no_g2` 明显变差，说明弱反弹不能彻底禁用 G2；G3 需要吸收 G2，而不是只做 panic/range。",
        "- 若 D1/D2 半退出降低收益且没有同步降回撤，后续不再围绕退出比例微调，转向入场后 30m 真实承接或来源质量确认。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    window_rows = []
    route_frames = []
    count_rows = []
    worst_frames = []

    for book, path in BOOKS.items():
        base_candidates, legs, audit = legcash.load_inputs(path)
        for source_policy in SOURCE_POLICIES:
            candidates = apply_source_policy(base_candidates, source_policy)
            candidates.to_csv(OUT_DIR / f"{book}__{source_policy}_candidates.csv", index=False, encoding="utf-8-sig")
            count_rows.append(weak_rebound_trade_counts(candidates, book, source_policy))
            for exit_policy in EXIT_POLICIES:
                for profile in PROFILES:
                    curve, closed = legcash.simulate(candidates, legs, audit, exit_policy, profile)
                    run_dir = OUT_DIR / f"{book}__{source_policy}__{exit_policy}__{profile['profile']}"
                    run_dir.mkdir(parents=True, exist_ok=True)
                    curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                    closed.to_csv(run_dir / "closed_legs.csv", index=False, encoding="utf-8-sig")
                    row = legcash.summarize(curve, closed, book, exit_policy, profile["profile"])
                    row["source_policy"] = source_policy
                    row = {"book": row.pop("book"), "source_policy": row.pop("source_policy"), "exit_policy": row.pop("policy"), **row}
                    summary_rows.append(row)
                    for wr in legcash.window_metrics(curve, closed, book, exit_policy, profile["profile"]):
                        wr["source_policy"] = source_policy
                        wr = {"book": wr.pop("book"), "source_policy": wr.pop("source_policy"), "exit_policy": wr.pop("policy"), **wr}
                        window_rows.append(wr)
                    route_frames.append(route_source_breakdown(closed, {"book": book, "source_policy": source_policy, "exit_policy": exit_policy, "profile": profile["profile"]}))
                    if profile["profile"] == "base_close30":
                        keep = [
                            "book",
                            "source_policy",
                            "exit_policy",
                            "profile",
                            "entry_date_ts",
                            "policy_exit_date",
                            "code",
                            "name",
                            "route",
                            "source_family",
                            "market_style",
                            "leg_capital_ratio",
                            "policy_net_ret",
                            "realized_pnl",
                            "exit_reason",
                        ]
                        w = closed.sort_values("policy_net_ret").head(10).copy()
                        w["book"] = book
                        w["source_policy"] = source_policy
                        w["exit_policy"] = exit_policy
                        w["profile"] = profile["profile"]
                        worst_frames.append(w[[c for c in keep if c in w.columns]])

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    route_breakdown = pd.concat(route_frames, ignore_index=True) if route_frames else pd.DataFrame()
    counts = pd.DataFrame(count_rows)
    worst = pd.concat(worst_frames, ignore_index=True) if worst_frames else pd.DataFrame()
    delta = source_policy_delta(summary)

    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    delta.to_csv(OUT_DIR / "delta_vs_all_g2.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    route_breakdown.to_csv(OUT_DIR / "route_breakdown.csv", index=False, encoding="utf-8-sig")
    counts.to_csv(OUT_DIR / "weak_rebound_counts.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst_legs.csv", index=False, encoding="utf-8-sig")
    write_report(summary, delta, windows, route_breakdown, counts, worst)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

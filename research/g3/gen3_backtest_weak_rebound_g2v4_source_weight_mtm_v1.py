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
OUT_DIR = _report_path() / "gen3_weak_rebound_g2v4_source_weight_mtm_v1"

BOOKS = {
    "balanced_router": legcash.ROUTER_BALANCED,
    "range_unknown_router": legcash.ROUTER_UNKNOWN,
}

WEIGHT_POLICIES = {
    "base_g2_50": {"default_g2": 0.50, "g3": 0.20, "wr_volume5": 0.50, "wr_big_bull": 0.50},
    "wr_all_35": {"default_g2": 0.50, "g3": 0.20, "wr_volume5": 0.35, "wr_big_bull": 0.35},
    "wr_v5_50_bb_25": {"default_g2": 0.50, "g3": 0.20, "wr_volume5": 0.50, "wr_big_bull": 0.25},
    "wr_v5_35_bb_20": {"default_g2": 0.50, "g3": 0.20, "wr_volume5": 0.35, "wr_big_bull": 0.20},
}

PROFILES = [
    {"profile": "base_close30", "extra_cost": 0.0, "shock": 0.0},
    {"profile": "shock2", "extra_cost": 0.0, "shock": 0.02},
]

WINDOWS = legcash.WINDOWS
INITIAL_CAPITAL = legcash.INITIAL_CAPITAL
DAILY_OPEN_LIMIT = legcash.DAILY_OPEN_LIMIT


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


def weight_for_row(row: pd.Series, policy: dict[str, float]) -> float:
    route = str(row.get("route", ""))
    if route != "g2_v4_strong":
        return float(policy["g3"])
    if str(row.get("market_style", "")) == "weak_rebound":
        source = str(row.get("source_family", ""))
        if source == "volume5":
            return float(policy["wr_volume5"])
        if source == "big_bull":
            return float(policy["wr_big_bull"])
    return float(policy["default_g2"])


def simulate_weighted(candidates: pd.DataFrame, legs: pd.DataFrame, audit: pd.DataFrame, profile: dict[str, Any], weight_policy: dict[str, float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = candidates.sort_values(["entry_date_ts", "route_priority", "rank_key"], ascending=[True, True, False]).copy()
    legs_map = {str(k): g.copy() for k, g in legs.groupby("candidate_key", sort=False)}
    event_map = {
        str(row["candidate_key"]): legcash.policy_events(row, legs_map, {}, "base_original", profile)
        for _, row in d.iterrows()
    }
    cal = legcash.calendar(d["entry_date_ts"].min(), d["policy_exit_date"].max())
    by_day = {pd.Timestamp(k).normalize(): g.copy() for k, g in d.groupby("entry_date_ts")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve = []

    for day in cal:
        new_open = []
        for pos in open_pos:
            remaining_events = []
            for ev in pos["events"]:
                if pd.Timestamp(ev["date"]).normalize() <= day:
                    principal = pos["stake"] * float(ev["capital_ratio"])
                    proceeds = principal * (1.0 + float(ev["net_return"]))
                    cash += proceeds
                    out = {k: v for k, v in pos.items() if k != "events"}
                    out.update(
                        {
                            "policy_exit_date": day,
                            "leg_capital_ratio": float(ev["capital_ratio"]),
                            "policy_net_ret": float(ev["net_return"]),
                            "exit_reason": ev["exit_reason"],
                            "realized_pnl": proceeds - principal,
                            "exit_value": proceeds,
                        }
                    )
                    closed.append(out)
                    pos["remaining_principal"] -= principal
                else:
                    remaining_events.append(ev)
            pos["events"] = remaining_events
            if pos["remaining_principal"] > 1e-6 and remaining_events:
                new_open.append(pos)
        open_pos = new_open

        todays = by_day.get(day)
        opened = 0
        if todays is not None:
            for _, row in todays.iterrows():
                if opened >= DAILY_OPEN_LIMIT:
                    continue
                route = str(row["route"])
                if route == "g2_v4_strong" and sum(1 for p in open_pos if p["route"] == "g2_v4_strong") >= 2:
                    continue
                if route != "g2_v4_strong" and sum(1 for p in open_pos if p["route"] != "g2_v4_strong") >= 5:
                    continue
                equity_before = cash + sum(float(p["remaining_principal"]) for p in open_pos)
                stake = equity_before * weight_for_row(row, weight_policy)
                if stake <= 0 or cash < stake:
                    continue
                key = str(row["candidate_key"])
                events = event_map.get(key, [])
                if not events:
                    continue
                cash -= stake
                pos = row.to_dict()
                pos.update({"stake": stake, "remaining_principal": stake, "events": events, "profile": profile["profile"]})
                open_pos.append(pos)
                opened += 1
        equity = cash + sum(float(p["remaining_principal"]) for p in open_pos)
        curve.append({"date": day, "cash": cash, "open_principal": equity - cash, "equity": equity, "open_positions": len(open_pos)})

    return pd.DataFrame(curve), pd.DataFrame(closed)


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, book: str, weight_policy: str, profile: str) -> dict[str, Any]:
    ret = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "book": book,
        "weight_policy": weight_policy,
        "profile": profile,
        "positions": int(closed["candidate_key"].nunique()) if not closed.empty else 0,
        "legs": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / curve["equity"].iloc[0] - 1.0) if len(curve) else 0.0,
        "max_drawdown": legcash.max_drawdown(curve["equity"]) if len(curve) else 0.0,
        "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
        "bad5_rate": float((ret <= -0.05).mean()) if len(ret) else 0.0,
        "avg_leg_return": float(ret.mean()) if len(ret) else 0.0,
    }


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, book: str, weight_policy: str, profile: str) -> list[dict[str, Any]]:
    rows = []
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"], errors="coerce").dt.normalize()
    t = closed.copy()
    if not t.empty:
        t["entry_date_ts"] = pd.to_datetime(t["entry_date_ts"], errors="coerce").dt.normalize()
    for name, (start, end) in WINDOWS.items():
        s = pd.Timestamp(start)
        e = pd.Timestamp(end)
        cc = c[c["date"].between(s, e)]
        tt = t[t["entry_date_ts"].between(s, e)] if not t.empty else t
        ret = pd.to_numeric(tt.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "book": book,
                "weight_policy": weight_policy,
                "profile": profile,
                "window": name,
                "positions": int(tt["candidate_key"].nunique()) if not tt.empty else 0,
                "legs": int(len(tt)),
                "return": float(cc["equity"].iloc[-1] / cc["equity"].iloc[0] - 1.0) if len(cc) else 0.0,
                "max_drawdown": legcash.max_drawdown(cc["equity"]) if len(cc) else 0.0,
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return rows


def contribution(closed: pd.DataFrame, book: str, weight_policy: str, profile: str) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    out = (
        closed.groupby(["route", "market_style", "source_family"], dropna=False)
        .agg(
            positions=("candidate_key", "nunique"),
            legs=("candidate_key", "size"),
            stake_sum=("stake", "sum"),
            pnl=("realized_pnl", "sum"),
            avg_leg_return=("policy_net_ret", "mean"),
        )
        .reset_index()
    )
    out.insert(0, "profile", profile)
    out.insert(0, "weight_policy", weight_policy)
    out.insert(0, "book", book)
    return out


def delta_vs_base(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    base = summary[summary["weight_policy"].eq("base_g2_50")]
    for _, b in base.iterrows():
        mask = summary["book"].eq(b["book"]) & summary["profile"].eq(b["profile"]) & ~summary["weight_policy"].eq("base_g2_50")
        for _, row in summary[mask].iterrows():
            rows.append(
                {
                    "book": row["book"],
                    "profile": row["profile"],
                    "weight_policy": row["weight_policy"],
                    "positions": int(row["positions"]),
                    "legs": int(row["legs"]),
                    "total_return": float(row["total_return"]),
                    "max_drawdown": float(row["max_drawdown"]),
                    "return_delta_vs_base": float(row["total_return"]) - float(b["total_return"]),
                    "dd_delta_vs_base": float(row["max_drawdown"]) - float(b["max_drawdown"]),
                }
            )
    return pd.DataFrame(rows)


def write_report(summary: pd.DataFrame, delta: pd.DataFrame, windows: pd.DataFrame, contrib: pd.DataFrame, worst: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "bad5_rate",
        "avg_leg_return",
        "return",
        "return_delta_vs_base",
        "dd_delta_vs_base",
        "avg_leg_return",
        "policy_net_ret",
    }
    money_cols = {"pnl", "realized_pnl", "stake_sum"}
    lines = [
        "# G3 weak_rebound G2 v4 来源权重 MTM 复算 v1",
        "",
        "## 回测时间与口径",
        "- 总曲线窗口：2020-01-01 至 2026-06-04。",
        "- G2 v4 实际成交来源窗口：2024-09-26 至 2026-05-20。",
        "- 本轮保留 weak_rebound 里的 `volume5` 和 `big_bull` 两个来源，只改变仓位权重，不删除来源。",
        "- 现金流规则：初始资金 150000；G3 单笔 20%；标准主升 G2 仍为 50%；每日最多新开 1 笔。",
        "- 压力口径：`base_close30` 为原始退出；`shock2` 为每条交易腿额外 -2% 冲击。",
        "",
        "## 英文名解释",
        "- `base_g2_50`：基准，所有 G2 包括 weak_rebound 都按 50% 仓位。",
        "- `wr_all_35`：weak_rebound 里的 `volume5` 和 `big_bull` 都降到 35% 仓位。",
        "- `wr_v5_50_bb_25`：weak_rebound 里 `volume5` 保持 50%，`big_bull` 降到 25%。",
        "- `wr_v5_35_bb_20`：weak_rebound 里 `volume5` 降到 35%，`big_bull` 降到 20%。",
        "- `volume5`：G2 v4 放量反包/放量修复主线。",
        "- `big_bull`：G2 v4 大阳线后二次突破买点。",
        "- `balanced_router`：四状态覆盖内，主升/弱反弹用 G2，标准震荡用 G3，未知上下文不交易。",
        "- `range_unknown_router`：在 balanced 基础上，2020-2023 未知上下文允许 G3 横盘候选补位。",
        "",
        "## 总体 MTM 结果",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 相对基准差异",
        md_table(delta, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 路由贡献拆分",
        md_table(contrib, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最差交易腿",
        md_table(worst, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 阶段判断",
        "- 如果降权没有明显降低回撤，却显著降低收益，说明 weak_rebound G2 的问题不在仓位，而在入场质量确认。",
        "- 如果 `wr_v5_50_bb_25` 明显优于基准或改善 shock2，说明 big_bull 在弱反弹里应被降权。",
        "- 如果所有降权版本都输给基准，下一步应保留 50% 基准，转向 `30m 真实放量承接` 或 `入场日突破失败警戒`，而不是继续调仓位。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary_rows = []
    window_rows = []
    contrib_frames = []
    worst_frames = []

    for book, path in BOOKS.items():
        candidates, legs, audit = legcash.load_inputs(path)
        for weight_name, weight_policy in WEIGHT_POLICIES.items():
            for profile in PROFILES:
                curve, closed = simulate_weighted(candidates, legs, audit, profile, weight_policy)
                run_dir = OUT_DIR / f"{book}__{weight_name}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_legs.csv", index=False, encoding="utf-8-sig")
                summary_rows.append(summarize(curve, closed, book, weight_name, profile["profile"]))
                window_rows.extend(window_metrics(curve, closed, book, weight_name, profile["profile"]))
                contrib_frames.append(contribution(closed, book, weight_name, profile["profile"]))
                if profile["profile"] == "base_close30":
                    w = closed.sort_values("policy_net_ret").head(10).copy()
                    w["book"] = book
                    w["weight_policy"] = weight_name
                    keep = [
                        "book",
                        "weight_policy",
                        "entry_date_ts",
                        "policy_exit_date",
                        "code",
                        "name",
                        "route",
                        "market_style",
                        "source_family",
                        "stake",
                        "leg_capital_ratio",
                        "policy_net_ret",
                        "realized_pnl",
                        "exit_reason",
                    ]
                    worst_frames.append(w[[c for c in keep if c in w.columns]])

    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    contrib = pd.concat(contrib_frames, ignore_index=True) if contrib_frames else pd.DataFrame()
    worst = pd.concat(worst_frames, ignore_index=True) if worst_frames else pd.DataFrame()
    delta = delta_vs_base(summary)

    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    delta.to_csv(OUT_DIR / "delta_vs_base.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    contrib.to_csv(OUT_DIR / "route_contribution.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst_legs.csv", index=False, encoding="utf-8-sig")
    write_report(summary, delta, windows, contrib, worst)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_market_router_legcash_g2v4_g3range_v1"

G2_RUN = ROOT / "reports" / "gen2_v2_complete_strategy" / "runs" / "probe_expand_2020_2026"
G2_SIGNALS = G2_RUN / "signals.csv"
G2_TRADES = G2_RUN / "trades.csv"
G2_SUMMARY = G2_RUN / "summary.json"
G3_RANGE = (
    ROOT
    / "reports"
    / "gen3_range_first_panic_d1_accept_stability_v1"
    / "d1_reclaim60_amt12__d3neg__cost30"
    / "closed_trades.csv"
)

INITIAL_CAPITAL = 150_000.0
DAILY_OPEN_LIMIT = 1

PROFILES = [
    {"profile": "base_net", "extra_cost": 0.0, "shock": 0.0},
    {"profile": "cost100_approx", "extra_cost": 0.007, "shock": 0.0},
    {"profile": "shock2_approx", "extra_cost": 0.0, "shock": 0.02},
]

WINDOWS = {
    "full_2020_2026": ("2020-01-01", "2026-06-04"),
    "pre_g2_2020_2024_09": ("2020-01-01", "2024-09-25"),
    "g2_overlap_2024_09_2026": ("2024-09-26", "2026-06-04"),
    "valid_2025": ("2025-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-06-04"),
}


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


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def calendar(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    return [pd.Timestamp(d).normalize() for d in pd.bdate_range(start, end)]


def load_g2_candidates() -> pd.DataFrame:
    trades = pd.read_csv(G2_TRADES, low_memory=False)
    signals = pd.read_csv(G2_SIGNALS, low_memory=False)
    for col in ["buy_date", "sell_date"]:
        trades[col] = pd.to_datetime(trades[col], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    sig_cols = ["entry_date", "code", "source_family", "signal_family", "g2_v2_buy_logic", "g2_open_state"]
    sig = signals[[c for c in sig_cols if c in signals.columns]].drop_duplicates(["entry_date", "code"])

    group_cols = ["code", "name", "buy_date", "buy_datetime", "buy_price", "v4_rank", "v4_score"]
    candidates = []
    leg_rows = []
    for key, g in trades.groupby(group_cols, dropna=False, sort=False):
        row = dict(zip(group_cols, key))
        g = g.sort_values(["sell_date", "sell_datetime"]).copy()
        first_cap = float(pd.to_numeric(g["capital"], errors="coerce").iloc[0])
        # In G2 output, capital is sold-leg capital. A partial take-profit followed by a final sell
        # means the original lot was roughly sum of leg capitals.
        total_leg_cap = float(pd.to_numeric(g["capital"], errors="coerce").sum())
        open_cap_ref = max(first_cap, total_leg_cap)
        row.update(
            {
                "entry_date_ts": pd.to_datetime(row["buy_date"], errors="coerce").normalize(),
                "policy_exit_date": pd.to_datetime(g["sell_date"].max(), errors="coerce").normalize(),
                "route": "g2_v4_strong",
                "route_cn": "强势环境：G2 v4 分批交易腿",
                "rank_key": 10000.0 - float(row["v4_rank"] or 9999),
                "source_open_capital": open_cap_ref,
                "source_leg_count": int(len(g)),
            }
        )
        candidates.append(row)
        for i, (_, leg) in enumerate(g.iterrows()):
            cap = float(leg["capital"])
            leg_rows.append(
                {
                    "candidate_key": f"{row['code']}|{row['buy_date']}|{row['buy_datetime']}|{row['buy_price']}",
                    "leg_no": i + 1,
                    "sell_date": pd.to_datetime(leg["sell_date"], errors="coerce").normalize(),
                    "sell_datetime": leg["sell_datetime"],
                    "sell_ratio_source": float(leg["sell_ratio"]),
                    "capital_ratio": cap / open_cap_ref if open_cap_ref else 1.0,
                    "leg_return": float(leg["return"]),
                    "exit_reason": str(leg["exit_reason"]),
                }
            )
    cand = pd.DataFrame(candidates)
    cand["candidate_key"] = cand.apply(lambda r: f"{r['code']}|{r['buy_date']}|{r['buy_datetime']}|{r['buy_price']}", axis=1)
    cand = cand.merge(sig, left_on=["buy_date", "code"], right_on=["entry_date", "code"], how="left")
    cand["source_family"] = cand["source_family"].fillna("unknown")
    cand["signal_family"] = cand["signal_family"].fillna("")
    cand["g2_open_state"] = cand["g2_open_state"].fillna("UNKNOWN")
    legs = pd.DataFrame(leg_rows)
    legs.to_csv(OUT_DIR / "g2_source_trade_legs_normalized.csv", index=False, encoding="utf-8-sig")
    return cand.dropna(subset=["entry_date_ts", "policy_exit_date"]).copy()


def load_g3_candidates() -> pd.DataFrame:
    d = pd.read_csv(G3_RANGE, low_memory=False)
    out = pd.DataFrame()
    out["candidate_key"] = d["code"].astype(str) + "|" + d["entry_date"].astype(str)
    out["code"] = d["code"].astype(str)
    out["name"] = d["name"].astype(str)
    out["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["raw_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    out["route"] = "g3_range_balanced_77"
    out["route_cn"] = "横盘/箱体底部：G3 D1二次承接"
    out["source_family"] = "g3_range"
    out["signal_family"] = "balanced_77"
    out["g2_v2_buy_logic"] = "D1 second 30m acceptance + reclaim60 + amount<=1.2"
    out["g2_open_state"] = ""
    out["rank_key"] = 100.0
    out["exit_reason"] = d.get("exit_reason", "")
    return out.dropna(subset=["entry_date_ts", "policy_exit_date", "raw_net_ret"]).copy()


def route_candidates(g2: pd.DataFrame, g3: pd.DataFrame, variant: str) -> pd.DataFrame:
    if variant == "g2_v4_legcash_only":
        return g2.copy()
    if variant == "g3_range_only":
        return g3.copy()
    if variant == "router_g2_priority_plus_g3":
        d = pd.concat([g2, g3], ignore_index=True, sort=False)
        d["route_priority"] = d["route"].map({"g2_v4_strong": 0, "g3_range_balanced_77": 1}).fillna(9)
        d = d.sort_values(["entry_date_ts", "route_priority", "rank_key"], ascending=[True, True, False])
        return d.groupby("entry_date_ts", as_index=False).head(1).copy()
    raise ValueError(variant)


def build_exit_events(candidates: pd.DataFrame, profile: dict[str, Any], g2_legs: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    g2_leg_map = {k: g.sort_values("sell_date").copy() for k, g in g2_legs.groupby("candidate_key", sort=False)}
    for row in candidates.itertuples(index=False):
        key = str(getattr(row, "candidate_key"))
        route = str(getattr(row, "route"))
        events = []
        if route == "g2_v4_strong":
            legs = g2_leg_map.get(key, pd.DataFrame())
            for leg in legs.itertuples(index=False):
                ret = float(getattr(leg, "leg_return")) - float(profile["extra_cost"]) - float(profile["shock"])
                events.append(
                    {
                        "date": pd.Timestamp(getattr(leg, "sell_date")).normalize(),
                        "capital_ratio": float(getattr(leg, "capital_ratio")),
                        "net_return": ret,
                        "exit_reason": str(getattr(leg, "exit_reason")),
                    }
                )
        else:
            ret = float(getattr(row, "raw_net_ret")) - float(profile["extra_cost"]) - float(profile["shock"])
            events.append(
                {
                    "date": pd.Timestamp(getattr(row, "policy_exit_date")).normalize(),
                    "capital_ratio": 1.0,
                    "net_return": ret,
                    "exit_reason": str(getattr(row, "exit_reason", "g3_exit")),
                }
            )
        out[key] = events
    return out


def stake_weight(route: str) -> float:
    return 0.50 if route == "g2_v4_strong" else 0.20


def max_positions(route_mix: list[dict[str, Any]]) -> int:
    # G2 original strong engine is two 50% slots. G3 range keeps five 20% slots.
    g2_count = sum(1 for p in route_mix if p["route"] == "g2_v4_strong")
    g3_count = sum(1 for p in route_mix if p["route"] != "g2_v4_strong")
    return int(g2_count < 2 or g3_count < 5)


def simulate(candidates: pd.DataFrame, profile: dict[str, Any], g2_legs: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.sort_values(["entry_date_ts", "rank_key"], ascending=[True, False]).copy()
    exit_events = build_exit_events(d, profile, g2_legs)
    cal = calendar(d["entry_date_ts"].min(), d["policy_exit_date"].max())
    by_day = {pd.Timestamp(k).normalize(): g.copy() for k, g in d.groupby("entry_date_ts")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows = []
    for day in cal:
        new_open = []
        realized = 0.0
        for pos in open_pos:
            key = pos["candidate_key"]
            remaining_events = []
            for ev in pos["events"]:
                if pd.Timestamp(ev["date"]).normalize() <= day:
                    principal = pos["stake"] * float(ev["capital_ratio"])
                    proceeds = principal * (1.0 + float(ev["net_return"]))
                    cash += proceeds
                    pnl = proceeds - principal
                    realized += pnl
                    out = {k: v for k, v in pos.items() if k != "events"}
                    out.update(
                        {
                            "policy_exit_date": day,
                            "leg_capital_ratio": float(ev["capital_ratio"]),
                            "policy_net_ret": float(ev["net_return"]),
                            "exit_reason": ev["exit_reason"],
                            "realized_pnl": pnl,
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
            for row in todays.itertuples(index=False):
                if opened >= DAILY_OPEN_LIMIT:
                    continue
                route = str(getattr(row, "route"))
                if route == "g2_v4_strong" and sum(1 for p in open_pos if p["route"] == "g2_v4_strong") >= 2:
                    continue
                if route != "g2_v4_strong" and sum(1 for p in open_pos if p["route"] != "g2_v4_strong") >= 5:
                    continue
                equity_before = cash + sum(float(p["remaining_principal"]) for p in open_pos)
                stake = equity_before * stake_weight(route)
                if stake <= 0 or cash < stake:
                    continue
                key = str(getattr(row, "candidate_key"))
                events = exit_events.get(key, [])
                if not events:
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                pos["remaining_principal"] = stake
                pos["events"] = events
                cash -= stake
                open_pos.append(pos)
                opened += 1
        equity = cash + sum(float(p["remaining_principal"]) for p in open_pos)
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["remaining_principal"]) for p in open_pos),
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized,
            }
        )
    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> dict[str, Any]:
    # leg_count is exit legs; position_count is actual entries.
    if closed.empty:
        return {
            "variant": variant,
            "profile": profile,
            "position_count": 0,
            "leg_count": 0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "avg_leg_return": 0.0,
            "worst_leg": 0.0,
        }
    ret = pd.to_numeric(closed["policy_net_ret"], errors="coerce")
    return {
        "variant": variant,
        "profile": profile,
        "position_count": int(closed["candidate_key"].nunique()),
        "leg_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0),
        "max_drawdown": max_drawdown(curve["equity"]),
        "win_rate": float((ret > 0).mean()),
        "avg_leg_return": float(ret.mean()),
        "worst_leg": float(ret.min()),
    }


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> list[dict[str, Any]]:
    rows = []
    for name, (start, end) in WINDOWS.items():
        c = curve[pd.to_datetime(curve["date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        t = closed[pd.to_datetime(closed["entry_date_ts"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
        ret = pd.to_numeric(t.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "window": name,
                "position_count": int(t["candidate_key"].nunique()) if not t.empty else 0,
                "leg_count": int(len(t)),
                "return": float(c["equity"].iloc[-1] / c["equity"].iloc[0] - 1.0) if len(c) else 0.0,
                "max_drawdown": max_drawdown(c["equity"]) if len(c) else 0.0,
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return rows


def concentration(df: pd.DataFrame) -> pd.DataFrame:
    pos = df.groupby(["candidate_key", "code", "name", "route"], dropna=False).agg(
        entry_date_ts=("entry_date_ts", "first"),
        realized_pnl=("realized_pnl", "sum"),
        avg_leg_return=("policy_net_ret", "mean"),
    ).reset_index()
    d = pos.sort_values("realized_pnl", ascending=False)
    total = float(d["realized_pnl"].sum())
    rows = []
    for n in [0, 1, 3, 5, 10]:
        rest = d.iloc[n:]
        rows.append(
            {
                "exclude_top_n": n,
                "remaining_positions": int(len(rest)),
                "remaining_pnl": float(rest["realized_pnl"].sum()),
                "remaining_pnl_share": float(rest["realized_pnl"].sum() / total) if total else 0.0,
            }
        )
    return pd.DataFrame(rows)


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, routes: pd.DataFrame, conc: pd.DataFrame, top: pd.DataFrame, worst: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_leg_return",
        "worst_leg",
        "return",
        "remaining_pnl_share",
        "policy_net_ret",
        "avg_return",
    }
    money_cols = {"pnl", "remaining_pnl", "realized_pnl"}
    lines = [
        "# G3 市场路由腿级现金流复算 v1",
        "",
        "## 回测范围",
        "- 组合曲线：2020-01-01 至 2026-06-04。",
        "- G2 v4 扩周期源实际信号：2024-09-26 至 2026-05-20。",
        "- G3 横盘源：2020-02-06 至 2026-05-26。",
        "- G2 强势仓位按原始攻击口径近似：最多2个强势仓，单仓50%。",
        "- G3 横盘仓位按防守补位口径：最多5个横盘仓，单仓20%。",
        "- 本轮使用 G2 `trades.csv` 交易腿做现金流复算，较上一版 lot 聚合更接近分批止盈，但仍不是重新调用 G2 分钟线源码逐bar复算。",
        "",
        "## 英文策略名解释",
        "- `g2_v4_legcash_only`：只使用 G2 v4 强势源，并按交易腿现金流复算。",
        "- `g3_range_only`：只使用 G3 横盘 balanced_77。",
        "- `router_g2_priority_plus_g3`：同日优先 G2 强势源；没有 G2 时用 G3 横盘补位。",
        "- `base_net`：使用原交易腿净收益。",
        "- `cost100_approx`：每个退出腿额外扣 70bps，近似更高滑点。",
        "- `shock2_approx`：每个退出腿额外扣 2%，近似低开/执行冲击。",
        "",
        "## 总体结果",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗结果",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 路由构成",
        md_table(routes, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 路由集中度",
        md_table(conc, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最大盈利腿",
        md_table(top, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最大亏损腿",
        md_table(worst, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 判断",
        "- G2 v4 强势源必须成为 G3 强势环境的主收益发动机。",
        "- G3 横盘源在 2020-2024 空窗期有补位价值，但在 G2 强势源启动后只能作为低优先级补充。",
        "- 若要进入候选，下一步应把市场环境四态路由正式化，并用真实 G2 引擎重放强势源，而不是继续只做横盘调参。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g2 = load_g2_candidates()
    g3 = load_g3_candidates()
    g2_legs = pd.read_csv(OUT_DIR / "g2_source_trade_legs_normalized.csv", low_memory=False)
    g2.to_csv(OUT_DIR / "g2_candidates.csv", index=False, encoding="utf-8-sig")
    g3.to_csv(OUT_DIR / "g3_candidates.csv", index=False, encoding="utf-8-sig")
    summary_rows = []
    window_rows = []
    best_closed = pd.DataFrame()
    for variant in ["g2_v4_legcash_only", "g3_range_only", "router_g2_priority_plus_g3"]:
        base = route_candidates(g2, g3, variant)
        base.to_csv(OUT_DIR / f"{variant}_base_candidates.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            curve, closed = simulate(base, profile, g2_legs)
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_legs.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, variant, profile["profile"]))
            window_rows.extend(window_metrics(curve, closed, variant, profile["profile"]))
            if variant == "router_g2_priority_plus_g3" and profile["profile"] == "base_net":
                best_closed = closed.copy()
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    routes = best_closed.groupby(["route", "route_cn"], dropna=False).agg(
        position_count=("candidate_key", "nunique"),
        leg_count=("candidate_key", "size"),
        pnl=("realized_pnl", "sum"),
        avg_return=("policy_net_ret", "mean"),
    ).reset_index()
    routes.to_csv(OUT_DIR / "router_route_counts.csv", index=False, encoding="utf-8-sig")
    conc = concentration(best_closed)
    conc.to_csv(OUT_DIR / "router_concentration.csv", index=False, encoding="utf-8-sig")
    keep = ["entry_date_ts", "policy_exit_date", "code", "name", "route", "source_family", "leg_capital_ratio", "policy_net_ret", "realized_pnl", "exit_reason"]
    top = best_closed.sort_values("realized_pnl", ascending=False).head(20)[keep]
    worst = best_closed.sort_values("policy_net_ret", ascending=True).head(20)[keep]
    top.to_csv(OUT_DIR / "router_top_legs.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "router_worst_legs.csv", index=False, encoding="utf-8-sig")
    write_report(summary, windows, routes, conc, top, worst)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

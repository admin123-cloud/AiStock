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


ROOT = _PROJECT_ROOT
OUT_DIR = _report_path() / "gen3_market_router_g2v4_g3range_v1"
G2_RUN = _report_path() / "gen2_v2_complete_strategy" / "runs" / "probe_expand_2020_2026"
G2_SIGNALS = G2_RUN / "signals.csv"
G2_TRADES = G2_RUN / "trades.csv"
G3_RANGE = (
    _report_path()
    / "gen3_range_first_panic_d1_accept_stability_v1"
    / "d1_reclaim60_amt12__d3neg__cost30"
    / "closed_trades.csv"
)

INITIAL_CAPITAL = 150_000.0
SLOTS = 5
SLOT_PCT = 0.20
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


def trade_calendar(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    days = pd.bdate_range(start, end)
    return [pd.Timestamp(d).normalize() for d in days]


def load_g2_lots() -> pd.DataFrame:
    trades = pd.read_csv(G2_TRADES, low_memory=False)
    signals = pd.read_csv(G2_SIGNALS, low_memory=False)
    trades["buy_date"] = pd.to_datetime(trades["buy_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    trades["sell_date"] = pd.to_datetime(trades["sell_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    signals["entry_date"] = pd.to_datetime(signals["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    sig = signals[
        [
            "entry_date",
            "code",
            "source_family",
            "signal_family",
            "g2_v2_buy_logic",
            "g2_open_state",
        ]
    ].copy()
    group_cols = ["code", "name", "buy_date", "buy_datetime", "buy_price", "v4_rank", "v4_score"]
    lots = (
        trades.groupby(group_cols, dropna=False)
        .agg(
            policy_exit_date=("sell_date", "max"),
            source_capital=("capital", "first"),
            source_pnl=("pnl", "sum"),
            exit_reason=("exit_reason", lambda s: "|".join(sorted(set(map(str, s))))),
            sell_legs=("code", "size"),
        )
        .reset_index()
    )
    lots = lots.merge(sig, left_on=["buy_date", "code"], right_on=["entry_date", "code"], how="left")
    lots["entry_date_ts"] = pd.to_datetime(lots["buy_date"], errors="coerce").dt.normalize()
    lots["policy_exit_date"] = pd.to_datetime(lots["policy_exit_date"], errors="coerce").dt.normalize()
    lots["raw_net_ret"] = pd.to_numeric(lots["source_pnl"], errors="coerce") / pd.to_numeric(lots["source_capital"], errors="coerce")
    lots["route"] = "g2_v4_strong"
    lots["route_cn"] = "强势环境：G2 v4 volume5/big_bull"
    lots["rank_key"] = 1000.0 - pd.to_numeric(lots["v4_rank"], errors="coerce").fillna(999.0)
    lots["name"] = lots["name"].astype(str)
    return lots.dropna(subset=["entry_date_ts", "policy_exit_date", "raw_net_ret"]).copy()


def load_g3_range() -> pd.DataFrame:
    d = pd.read_csv(G3_RANGE, low_memory=False)
    out = pd.DataFrame()
    out["code"] = d["code"].astype(str)
    out["name"] = d["name"].astype(str)
    out["entry_date_ts"] = pd.to_datetime(d["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(d["policy_exit_date"], errors="coerce").dt.normalize()
    out["raw_net_ret"] = pd.to_numeric(d["policy_net_ret"], errors="coerce")
    out["exit_reason"] = d.get("exit_reason", "")
    out["source_family"] = "g3_range"
    out["signal_family"] = "balanced_77"
    out["g2_v2_buy_logic"] = "D1 second 30m acceptance + reclaim60 + amount<=1.2"
    out["g2_open_state"] = ""
    out["route"] = "g3_range_balanced_77"
    out["route_cn"] = "横盘/箱体底部：G3 D1二次承接"
    out["rank_key"] = 100.0
    return out.dropna(subset=["entry_date_ts", "policy_exit_date", "raw_net_ret"]).copy()


def apply_profile(candidates: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = candidates.copy()
    d["policy_net_ret"] = pd.to_numeric(d["raw_net_ret"], errors="coerce") - float(profile["extra_cost"]) - float(profile["shock"])
    return d.dropna(subset=["policy_net_ret"]).copy()


def simulate(candidates: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    d = candidates.sort_values(["entry_date_ts", "rank_key"], ascending=[True, False]).copy()
    cal = trade_calendar(d["entry_date_ts"].min(), d["policy_exit_date"].max())
    by_day = {pd.Timestamp(k).normalize(): g.copy() for k, g in d.groupby("entry_date_ts")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    rows = []
    for day in cal:
        still = []
        realized = 0.0
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["policy_net_ret"]))
                cash += exit_value
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
                realized += out["realized_pnl"]
            else:
                still.append(pos)
        open_pos = still
        opened = 0
        todays = by_day.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= DAILY_OPEN_LIMIT or len(open_pos) >= SLOTS:
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * SLOT_PCT
                if cash < stake or stake <= 0:
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1
        equity = cash + sum(float(p["stake"]) for p in open_pos)
        rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": sum(float(p["stake"]) for p in open_pos),
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized,
            }
        )
    curve = pd.DataFrame(rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> dict[str, Any]:
    ret = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "profile": profile,
        "trade_count": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / INITIAL_CAPITAL - 1.0) if len(curve) else 0.0,
        "max_drawdown": max_drawdown(curve["equity"]) if len(curve) else 0.0,
        "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
        "avg_trade_return": float(ret.mean()) if len(ret) else 0.0,
        "worst_trade": float(ret.min()) if len(ret) else 0.0,
    }


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, variant: str, profile: str) -> list[dict[str, Any]]:
    rows = []
    for name, (start, end) in WINDOWS.items():
        c0 = curve[pd.to_datetime(curve["date"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        t0 = closed[pd.to_datetime(closed["entry_date_ts"]).between(pd.Timestamp(start), pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
        ret = pd.to_numeric(t0.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "variant": variant,
                "profile": profile,
                "window": name,
                "trade_count": int(len(t0)),
                "return": float(c0["equity"].iloc[-1] / c0["equity"].iloc[0] - 1.0) if len(c0) else 0.0,
                "max_drawdown": max_drawdown(c0["equity"]) if len(c0) else 0.0,
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return rows


def route_candidates(g2: pd.DataFrame, g3: pd.DataFrame, variant: str) -> pd.DataFrame:
    if variant == "g2_v4_only":
        return g2.copy()
    if variant == "g3_range_only":
        return g3.copy()
    if variant == "router_g2_priority_plus_g3":
        d = pd.concat([g2, g3], ignore_index=True)
        d["route_priority"] = d["route"].map({"g2_v4_strong": 0, "g3_range_balanced_77": 1}).fillna(9)
        d = d.sort_values(["entry_date_ts", "route_priority", "rank_key"], ascending=[True, True, False])
        return d.groupby("entry_date_ts", as_index=False).head(1).copy()
    raise ValueError(variant)


def concentration(df: pd.DataFrame) -> pd.DataFrame:
    d = df.sort_values("realized_pnl", ascending=False).copy()
    total = float(pd.to_numeric(d.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum())
    rows = []
    for n in [0, 1, 3, 5, 10]:
        rest = d.iloc[n:].copy()
        ret = pd.to_numeric(rest.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
        pnl = float(pd.to_numeric(rest.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum())
        rows.append(
            {
                "exclude_top_n": n,
                "remaining_trades": int(len(rest)),
                "remaining_pnl": pnl,
                "remaining_pnl_share": pnl / total if total else 0.0,
                "remaining_avg_return": float(ret.mean()) if len(ret) else 0.0,
                "remaining_win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return pd.DataFrame(rows)


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, route_counts: pd.DataFrame, conc: pd.DataFrame, top: pd.DataFrame, worst: pd.DataFrame) -> None:
    pct_cols = {
        "total_return",
        "max_drawdown",
        "win_rate",
        "avg_trade_return",
        "worst_trade",
        "return",
        "remaining_pnl_share",
        "remaining_avg_return",
        "remaining_win_rate",
        "policy_net_ret",
    }
    money_cols = {"realized_pnl", "remaining_pnl"}
    lines = [
        "# G3 市场路由组合回测：G2 v4 强势 + G3 横盘 v1",
        "",
        "## 回测范围",
        "- 组合曲线：2020-01-01 至 2026-06-04。",
        "- G2 v4 强势源实际有信号日期：2024-09-26 至 2026-05-20。",
        "- G3 横盘源：balanced_77，2020-02-06 至 2026-05-26。",
        "- 仓位：初始资金150,000，5槽位，单槽20%，同日最多开1笔。",
        "- 本轮为路由近似复算：G2 分批止盈已聚合为单个 lot 收益；后续若进入候选，需要重建真实分批 slot 引擎。",
        "",
        "## 英文策略名解释",
        "- `g2_v4_only`：只使用 G2 v4 强势源，包含 volume5 和 big_bull，但本次实际成交以 G2 输出为准。",
        "- `g3_range_only`：只使用 G3 横盘 balanced_77 源。",
        "- `router_g2_priority_plus_g3`：同日优先 G2 v4 强势源；没有 G2 信号时，允许 G3 横盘源补位。",
        "- `base_net`：沿用原回测净收益口径。",
        "- `cost100_approx`：在原净收益上额外扣 70bps，近似从30bps压到100bps。",
        "- `shock2_approx`：在原净收益上每笔额外扣 2%，近似低开/执行冲击。",
        "",
        "## 总体结果",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗口结果",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 路由成交构成",
        md_table(route_counts),
        "",
        "## 最佳路由集中度",
        md_table(conc, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最佳路由最大盈利交易",
        md_table(top, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最佳路由最大亏损交易",
        md_table(worst, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 判断",
        "- 这个版本验证的是方向：强势收益应由 G2 v4 承担，G3 横盘只做补位。",
        "- 如果路由组合没有明显优于 G2 v4 单独版本，说明 G3 横盘补位质量仍不够，不能为了交易频率硬接。",
        "- 如果冲击口径显著恶化，下一步应先做 G2 v4 强势链路的执行压力和开仓门控，而不是继续叠加弱源。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    g2 = load_g2_lots()
    g3 = load_g3_range()
    g2.to_csv(OUT_DIR / "g2_v4_lot_candidates.csv", index=False, encoding="utf-8-sig")
    g3.to_csv(OUT_DIR / "g3_range_candidates.csv", index=False, encoding="utf-8-sig")
    summary_rows = []
    window_rows = []
    best_closed = pd.DataFrame()
    for variant in ["g2_v4_only", "g3_range_only", "router_g2_priority_plus_g3"]:
        base = route_candidates(g2, g3, variant)
        base.to_csv(OUT_DIR / f"{variant}_base_candidates.csv", index=False, encoding="utf-8-sig")
        for profile in PROFILES:
            candidates = apply_profile(base, profile)
            curve, closed = simulate(candidates)
            run_dir = OUT_DIR / f"{variant}__{profile['profile']}"
            run_dir.mkdir(parents=True, exist_ok=True)
            candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
            curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
            closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
            summary_rows.append(summarize(curve, closed, variant, str(profile["profile"])))
            window_rows.extend(window_metrics(curve, closed, variant, str(profile["profile"])))
            if variant == "router_g2_priority_plus_g3" and profile["profile"] == "base_net":
                best_closed = closed.copy()
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    route_counts = (
        best_closed.groupby(["route", "route_cn"], dropna=False)
        .agg(trade_count=("code", "size"), pnl=("realized_pnl", "sum"), avg_return=("policy_net_ret", "mean"))
        .reset_index()
    )
    route_counts.to_csv(OUT_DIR / "router_route_counts.csv", index=False, encoding="utf-8-sig")
    conc = concentration(best_closed)
    conc.to_csv(OUT_DIR / "router_concentration.csv", index=False, encoding="utf-8-sig")
    keep = ["entry_date_ts", "policy_exit_date", "code", "name", "route", "source_family", "policy_net_ret", "realized_pnl", "exit_reason"]
    top = best_closed.sort_values("realized_pnl", ascending=False).head(20)[keep]
    worst = best_closed.sort_values("policy_net_ret", ascending=True).head(20)[keep]
    top.to_csv(OUT_DIR / "router_top_trades.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "router_worst_trades.csv", index=False, encoding="utf-8-sig")
    write_report(summary, windows, route_counts, conc, top, worst)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

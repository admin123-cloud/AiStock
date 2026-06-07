from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "gen3_weak_rebound_g2v4_d1d2_legcash_v1"

G2_LEGS = ROOT / "reports" / "gen3_market_router_legcash_g2v4_g3range_v1" / "g2_source_trade_legs_normalized.csv"
AUDIT = ROOT / "reports" / "gen3_weak_rebound_g2v4_quality_v1" / "g2_v4_candidates_entry_quality.csv"
ROUTER_BALANCED = ROOT / "reports" / "gen3_market_router_fourstate_g2v4_g3range_v1" / "fourstate_balanced_base_candidates.csv"
ROUTER_UNKNOWN = ROOT / "reports" / "gen3_market_router_fourstate_g2v4_g3range_v1" / "fourstate_range_plus_unknown_pre2024_base_candidates.csv"

INITIAL_CAPITAL = 150_000.0
DAILY_OPEN_LIMIT = 1

WINDOWS = {
    "full_2020_2026": ("2020-01-01", "2026-06-04"),
    "g2_actual_2024_09_2026": ("2024-09-26", "2026-06-04"),
    "valid_2025": ("2025-01-01", "2025-12-31"),
    "blind_2026ytd": ("2026-01-01", "2026-06-04"),
}

PROFILES = [
    {"profile": "base_close30", "extra_cost": 0.0, "shock": 0.0},
    {"profile": "shock2", "extra_cost": 0.0, "shock": 0.02},
]

POLICIES = [
    "base_original",
    "weak_d1d2_full_exit",
    "weak_d1d2_half_keep_rest",
    "weak_d1d2_cancel_late_legs",
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


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    return float((equity / equity.cummax() - 1.0).min())


def calendar(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    return [pd.Timestamp(d).normalize() for d in pd.bdate_range(start, end)]


def load_inputs(router_path: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    candidates = pd.read_csv(router_path, low_memory=False)
    legs = pd.read_csv(G2_LEGS, low_memory=False)
    audit = pd.read_csv(AUDIT, low_memory=False)
    for col in ["entry_date_ts", "policy_exit_date", "buy_date"]:
        if col in candidates.columns:
            candidates[col] = pd.to_datetime(candidates[col], errors="coerce").dt.normalize()
    legs["sell_date"] = pd.to_datetime(legs["sell_date"], errors="coerce").dt.normalize()
    audit["entry_date_ts"] = pd.to_datetime(audit["entry_date_ts"], errors="coerce").dt.normalize()
    return candidates, legs, audit


def trigger_map(audit: pd.DataFrame, profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    d = audit.copy()
    d1 = pd.to_numeric(d["d1_close_ret"], errors="coerce").le(-0.03)
    d2 = pd.to_numeric(d["d2_close_ret"], errors="coerce").le(-0.03)
    weak = d["market_style"].eq("weak_rebound")
    for _, row in d[weak & (d1 | d2)].iterrows():
        key = str(row["candidate_key"])
        if bool(d1.loc[row.name]):
            date = pd.Timestamp(row["entry_date_ts"]) + pd.tseries.offsets.BDay(1)
            close_price = float(row["d1_close"])
            reason = "weak_rebound_d1_weak_confirm"
        else:
            date = pd.Timestamp(row["entry_date_ts"]) + pd.tseries.offsets.BDay(2)
            close_price = float(row["d2_close"])
            reason = "weak_rebound_d2_weak_confirm"
        buy_price = float(row["buy_price"])
        early_ret = close_price / buy_price - 1.0 - 0.003 - float(profile["shock"])
        out[key] = {"date": pd.Timestamp(date).normalize(), "early_ret": early_ret, "reason": reason}
    return out


def base_events_for_row(row: pd.Series, legs_map: dict[str, pd.DataFrame], profile: dict[str, Any]) -> list[dict[str, Any]]:
    key = str(row["candidate_key"])
    route = str(row["route"])
    events: list[dict[str, Any]] = []
    if route == "g2_v4_strong":
        legs = legs_map.get(key, pd.DataFrame())
        for _, leg in legs.sort_values("sell_date").iterrows():
            events.append(
                {
                    "date": pd.Timestamp(leg["sell_date"]).normalize(),
                    "capital_ratio": float(leg["capital_ratio"]),
                    "net_return": float(leg["leg_return"]) - float(profile["shock"]),
                    "exit_reason": str(leg["exit_reason"]),
                }
            )
    else:
        events.append(
            {
                "date": pd.Timestamp(row["policy_exit_date"]).normalize(),
                "capital_ratio": 1.0,
                "net_return": float(row["raw_net_ret"]) - float(profile["shock"]),
                "exit_reason": str(row.get("exit_reason", "g3_exit")),
            }
        )
    return events


def policy_events(row: pd.Series, legs_map: dict[str, pd.DataFrame], triggers: dict[str, dict[str, Any]], policy: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
    events = base_events_for_row(row, legs_map, profile)
    key = str(row["candidate_key"])
    if str(row["route"]) != "g2_v4_strong" or key not in triggers or policy == "base_original":
        return events
    trig = triggers[key]
    trig_date = pd.Timestamp(trig["date"]).normalize()
    early_ret = float(trig["early_ret"])
    reason = str(trig["reason"])
    original_after = [ev for ev in events if pd.Timestamp(ev["date"]).normalize() > trig_date]
    original_before = [ev for ev in events if pd.Timestamp(ev["date"]).normalize() <= trig_date]
    already_exited = sum(float(ev["capital_ratio"]) for ev in original_before)
    remaining = max(0.0, 1.0 - already_exited)
    if remaining <= 1e-9:
        return events
    if policy == "weak_d1d2_full_exit":
        return original_before + [{"date": trig_date, "capital_ratio": remaining, "net_return": early_ret, "exit_reason": reason + "_full_exit"}]
    if policy == "weak_d1d2_half_keep_rest":
        early_ratio = min(0.5, remaining)
        rest_scale = (remaining - early_ratio) / remaining if remaining else 0.0
        adjusted_after = []
        for ev in original_after:
            adjusted_after.append({**ev, "capital_ratio": float(ev["capital_ratio"]) * rest_scale})
        return original_before + [{"date": trig_date, "capital_ratio": early_ratio, "net_return": early_ret, "exit_reason": reason + "_half_exit"}] + adjusted_after
    if policy == "weak_d1d2_cancel_late_legs":
        # Keep any original profit-taking that happened before the weak confirmation; exit the rest immediately.
        return original_before + [{"date": trig_date, "capital_ratio": remaining, "net_return": early_ret, "exit_reason": reason + "_cancel_late_legs"}]
    raise ValueError(policy)


def stake_weight(route: str) -> float:
    return 0.50 if route == "g2_v4_strong" else 0.20


def simulate(candidates: pd.DataFrame, legs: pd.DataFrame, audit: pd.DataFrame, policy: str, profile: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = candidates.sort_values(["entry_date_ts", "route_priority", "rank_key"], ascending=[True, True, False]).copy()
    legs_map = {str(k): g.copy() for k, g in legs.groupby("candidate_key", sort=False)}
    triggers = trigger_map(audit, profile)
    event_map = {str(row["candidate_key"]): policy_events(row, legs_map, triggers, policy, profile) for _, row in d.iterrows()}
    cal = calendar(d["entry_date_ts"].min(), d["policy_exit_date"].max())
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
                stake = equity_before * stake_weight(route)
                if stake <= 0 or cash < stake:
                    continue
                key = str(row["candidate_key"])
                events = event_map.get(key, [])
                if not events:
                    continue
                cash -= stake
                pos = row.to_dict()
                pos.update({"stake": stake, "remaining_principal": stake, "events": events, "policy": policy, "profile": profile["profile"]})
                open_pos.append(pos)
                opened += 1
        equity = cash + sum(float(p["remaining_principal"]) for p in open_pos)
        curve.append({"date": day, "cash": cash, "open_principal": equity - cash, "equity": equity, "open_positions": len(open_pos)})
    return pd.DataFrame(curve), pd.DataFrame(closed)


def summarize(curve: pd.DataFrame, closed: pd.DataFrame, book: str, policy: str, profile: str) -> dict[str, Any]:
    ret = pd.to_numeric(closed.get("policy_net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "book": book,
        "policy": policy,
        "profile": profile,
        "positions": int(closed["candidate_key"].nunique()) if not closed.empty else 0,
        "legs": int(len(closed)),
        "total_return": float(curve["equity"].iloc[-1] / curve["equity"].iloc[0] - 1.0) if len(curve) else 0.0,
        "max_drawdown": max_drawdown(curve["equity"]) if len(curve) else 0.0,
        "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
        "avg_leg_return": float(ret.mean()) if len(ret) else 0.0,
        "bad5_rate": float((ret <= -0.05).mean()) if len(ret) else 0.0,
    }


def window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, book: str, policy: str, profile: str) -> list[dict[str, Any]]:
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
                "policy": policy,
                "profile": profile,
                "window": name,
                "positions": int(tt["candidate_key"].nunique()) if not tt.empty else 0,
                "legs": int(len(tt)),
                "return": float(cc["equity"].iloc[-1] / cc["equity"].iloc[0] - 1.0) if len(cc) else 0.0,
                "max_drawdown": max_drawdown(cc["equity"]) if len(cc) else 0.0,
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
            }
        )
    return rows


def route_summary(closed: pd.DataFrame, book: str, policy: str, profile: str) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    out = (
        closed.groupby(["route", "source_family", "market_style"], dropna=False)
        .agg(
            positions=("candidate_key", "nunique"),
            legs=("candidate_key", "size"),
            pnl=("realized_pnl", "sum"),
            avg_return=("policy_net_ret", "mean"),
        )
        .reset_index()
    )
    out.insert(0, "profile", profile)
    out.insert(0, "policy", policy)
    out.insert(0, "book", book)
    return out


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, routes: pd.DataFrame, worst: pd.DataFrame) -> None:
    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_leg_return", "bad5_rate", "return", "avg_return", "policy_net_ret"}
    money_cols = {"pnl", "realized_pnl"}
    lines = [
        "# G3 弱反弹 G2 D1/D2 腿级现金流复算 v1",
        "",
        "## 回测范围",
        "- 组合窗口：2020-01-01 至 2026-06-04。",
        "- G2 v4 实际信号：2024-09-26 至 2026-05-20；本轮只改弱反弹 G2 的 D1/D2 早期弱确认处理。",
        "- `balanced_router`：只使用四态有覆盖的主升/弱反弹 G2 + 标准震荡 G3，未知上下文不交易。",
        "- `range_unknown_router`：承认 2020-2023 四态缺口，未知上下文允许 G3 横盘补位。",
        "- 早退价为 D1/D2 日线收盘相对 G2 买入价，扣 30bps；`shock2` 额外扣 2% 冲击。",
        "",
        "## 英文名解释",
        "- `base_original`：保留原始 G2 分批退出腿。",
        "- `weak_d1d2_full_exit`：弱反弹 G2 若 D1/D2 收盘弱确认，剩余仓位全部按早期价退出。",
        "- `weak_d1d2_half_keep_rest`：弱反弹 G2 若 D1/D2 收盘弱确认，先退出 50%，剩余仓位继续按原 G2 退出腿走。",
        "- `weak_d1d2_cancel_late_legs`：弱反弹 G2 若 D1/D2 收盘弱确认，保留此前已发生的止盈腿，后续剩余仓位全部提前退出。",
        "- `balanced_router`：四态均衡路由，主升/弱反弹用 G2，标准震荡用 G3。",
        "- `range_unknown_router`：在 `balanced_router` 基础上，2020-2023 未知上下文允许 G3 横盘补位。",
        "",
        "## 总体结果",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗结果",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## 路由拆分",
        md_table(routes, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 最差交易腿",
        md_table(worst, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 阶段判断",
        "- 全退会明显降低弱反弹 G2 的进攻收益，不适合直接升级为候选。",
        "- 半退/取消后续腿是否优于原始，需要看 `balanced_router` 和 `range_unknown_router` 的回撤、坏腿率和 2025/2026 分窗是否同步改善。",
        "- 若半退只降低收益而不改善回撤，下一步应转向区分 D1/D2 弱确认后的分时承接，而不是再调退出比例。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    books = {"balanced_router": ROUTER_BALANCED, "range_unknown_router": ROUTER_UNKNOWN}
    summary_rows = []
    window_rows = []
    route_frames = []
    worst_frames = []
    for book, path in books.items():
        candidates, legs, audit = load_inputs(path)
        candidates.to_csv(OUT_DIR / f"{book}_base_candidates.csv", index=False, encoding="utf-8-sig")
        for policy in POLICIES:
            for profile in PROFILES:
                curve, closed = simulate(candidates, legs, audit, policy, profile)
                run_dir = OUT_DIR / f"{book}__{policy}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_legs.csv", index=False, encoding="utf-8-sig")
                summary_rows.append(summarize(curve, closed, book, policy, profile["profile"]))
                window_rows.extend(window_metrics(curve, closed, book, policy, profile["profile"]))
                route_frames.append(route_summary(closed, book, policy, profile["profile"]))
                if profile["profile"] == "base_close30":
                    keep = ["book", "policy", "profile", "entry_date_ts", "policy_exit_date", "code", "name", "route", "source_family", "market_style", "leg_capital_ratio", "policy_net_ret", "realized_pnl", "exit_reason"]
                    w = closed.sort_values("policy_net_ret").head(12).copy()
                    w.insert(0, "book", book)
                    worst_frames.append(w[[c for c in keep if c in w.columns]])
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    routes = pd.concat(route_frames, ignore_index=True) if route_frames else pd.DataFrame()
    worst = pd.concat(worst_frames, ignore_index=True) if worst_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "route_breakdown.csv", index=False, encoding="utf-8-sig")
    worst.to_csv(OUT_DIR / "worst_legs.csv", index=False, encoding="utf-8-sig")
    write_report(summary, windows, routes, worst)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

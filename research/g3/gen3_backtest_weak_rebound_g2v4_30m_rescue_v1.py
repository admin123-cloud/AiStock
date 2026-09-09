from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()


from pathlib import Path
import sys
from typing import Any

import numpy as np
import pandas as pd


ROOT = _PROJECT_ROOT
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen3_backtest_weak_rebound_g2v4_d1d2_legcash_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    POLICIES,
    PROFILES,
    ROUTER_BALANCED,
    ROUTER_UNKNOWN,
    WINDOWS,
    base_events_for_row,
    calendar,
    load_inputs,
    max_drawdown,
    md_table,
    money,
    pct,
    stake_weight,
    summarize,
    window_metrics,
)
from utils.market_warehouse import clickhouse_query_df  # noqa: E402


OUT_DIR = _report_path() / "gen3_weak_rebound_g2v4_30m_rescue_v1"

RESCUE_POLICIES = [
    "base_original",
    "weak_d1d2_full_exit",
    "weak_d1d2_30m_rescue_full_exit",
    "weak_d1d2_30m_rescue_half_exit",
]


def sql_literal(value: str) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def build_triggers(audit: pd.DataFrame, profile: dict[str, Any]) -> pd.DataFrame:
    d = audit.copy()
    d1 = pd.to_numeric(d["d1_close_ret"], errors="coerce").le(-0.03)
    d2 = pd.to_numeric(d["d2_close_ret"], errors="coerce").le(-0.03)
    weak = d["market_style"].eq("weak_rebound")
    rows = []
    for _, row in d[weak & (d1 | d2)].iterrows():
        if bool(d1.loc[row.name]):
            trigger_date = pd.Timestamp(row["entry_date_ts"]) + pd.tseries.offsets.BDay(1)
            close_price = float(row["d1_close"])
            reason = "weak_rebound_d1_weak_confirm"
        else:
            trigger_date = pd.Timestamp(row["entry_date_ts"]) + pd.tseries.offsets.BDay(2)
            close_price = float(row["d2_close"])
            reason = "weak_rebound_d2_weak_confirm"
        buy_price = float(row["buy_price"])
        rows.append(
            {
                "candidate_key": str(row["candidate_key"]),
                "code": str(row["code"]),
                "name": str(row["name"]),
                "entry_date_ts": pd.Timestamp(row["entry_date_ts"]).normalize(),
                "trigger_date": pd.Timestamp(trigger_date).normalize(),
                "early_ret": close_price / buy_price - 1.0 - 0.003 - float(profile["shock"]),
                "trigger_reason": reason,
            }
        )
    return pd.DataFrame(rows)


def load_30m_bars(triggers: pd.DataFrame) -> pd.DataFrame:
    if triggers.empty:
        return pd.DataFrame()
    codes = sorted(triggers["code"].dropna().astype(str).unique().tolist())
    start = triggers["trigger_date"].min().strftime("%Y-%m-%d")
    end = triggers["trigger_date"].max().strftime("%Y-%m-%d")
    parts: list[pd.DataFrame] = []
    for i in range(0, len(codes), 200):
        quoted = ",".join(sql_literal(c) for c in codes[i : i + 200])
        sql = f"""
        SELECT code, datetime, open, high, low, close, amount
        FROM kline_minute_30
        WHERE code IN ({quoted})
          AND datetime >= toDateTime({sql_literal(start + " 09:00:00")})
          AND datetime <= toDateTime({sql_literal(end + " 15:30:00")})
        ORDER BY code, datetime
        """
        part = clickhouse_query_df(sql)
        if not part.empty:
            parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["code"] = bars["code"].astype(str)
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trigger_date"] = bars["datetime"].dt.normalize()
    bars["bar_time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    bars = bars.dropna(subset=["code", "datetime", "open", "high", "low", "close"]).sort_values(["code", "trigger_date", "datetime"])
    g = bars.groupby(["code", "trigger_date"], sort=False)
    bars["prev_bar_high"] = g["high"].shift(1)
    bars["intraday_low_so_far"] = g["low"].cummin()
    bars["prev_intraday_low"] = g["intraday_low_so_far"].shift(1)
    bars["amount_ma3_prev"] = g["amount"].transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean())
    bar_range = bars["high"] - bars["low"]
    bars["bar_close_pos"] = np.where(bar_range > 0, (bars["close"] - bars["low"]) / bar_range, np.nan)
    bars["bar_ret"] = bars["close"] / bars["open"] - 1.0
    bars["amount_ratio3"] = bars["amount"] / bars["amount_ma3_prev"]
    return bars.replace([np.inf, -np.inf], np.nan)


def attach_30m_rescue(triggers: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    if triggers.empty:
        return triggers
    if bars.empty:
        out = triggers.copy()
        out["am_rescue"] = False
        return out
    joined = bars.merge(triggers[["candidate_key", "code", "trigger_date"]], on=["code", "trigger_date"], how="inner")
    joined = joined[joined["bar_time"].between("10:00:00", "11:30:00")].copy()
    if joined.empty:
        out = triggers.copy()
        out["am_rescue"] = False
        return out
    prior_low = joined["prev_intraday_low"].fillna(joined["low"])
    no_new_low = joined["low"] >= prior_low
    break_prev = joined["close"] > joined["prev_bar_high"]
    acceptance = (
        (joined["close"] > joined["open"])
        & joined["bar_close_pos"].ge(0.60)
        & joined["amount_ratio3"].fillna(0.0).ge(0.95)
        & (break_prev | no_new_low)
    )
    confirm = joined[acceptance].sort_values(["candidate_key", "datetime"]).groupby("candidate_key", as_index=False).first()
    keep = ["candidate_key", "datetime", "close", "bar_time", "bar_close_pos", "bar_ret", "amount_ratio3"]
    confirm = confirm[keep].rename(
        columns={
            "datetime": "rescue_30m_datetime",
            "close": "rescue_30m_close",
            "bar_time": "rescue_30m_time",
        }
    )
    confirm["am_rescue"] = True
    out = triggers.merge(confirm, on="candidate_key", how="left")
    out["am_rescue"] = out["am_rescue"].fillna(False).astype(bool)
    return out


def trigger_dict(audit: pd.DataFrame, profile: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], pd.DataFrame]:
    triggers = build_triggers(audit, profile)
    bars = load_30m_bars(triggers)
    tagged = attach_30m_rescue(triggers, bars)
    return {str(r["candidate_key"]): r.to_dict() for _, r in tagged.iterrows()}, tagged


def policy_events(row: pd.Series, legs_map: dict[str, pd.DataFrame], triggers: dict[str, dict[str, Any]], policy: str, profile: dict[str, Any]) -> list[dict[str, Any]]:
    events = base_events_for_row(row, legs_map, profile)
    key = str(row["candidate_key"])
    if str(row["route"]) != "g2_v4_strong" or key not in triggers or policy == "base_original":
        return events
    trig = triggers[key]
    if policy.startswith("weak_d1d2_30m_rescue") and bool(trig.get("am_rescue", False)):
        return events
    trig_date = pd.Timestamp(trig["trigger_date"]).normalize()
    early_ret = float(trig["early_ret"])
    reason = str(trig["trigger_reason"])
    original_before = [ev for ev in events if pd.Timestamp(ev["date"]).normalize() <= trig_date]
    original_after = [ev for ev in events if pd.Timestamp(ev["date"]).normalize() > trig_date]
    already_exited = sum(float(ev["capital_ratio"]) for ev in original_before)
    remaining = max(0.0, 1.0 - already_exited)
    if remaining <= 1e-9:
        return events
    if policy in {"weak_d1d2_full_exit", "weak_d1d2_30m_rescue_full_exit"}:
        return original_before + [{"date": trig_date, "capital_ratio": remaining, "net_return": early_ret, "exit_reason": reason + "_no30m_full_exit"}]
    if policy == "weak_d1d2_30m_rescue_half_exit":
        early_ratio = min(0.5, remaining)
        rest_scale = (remaining - early_ratio) / remaining if remaining else 0.0
        adjusted_after = [{**ev, "capital_ratio": float(ev["capital_ratio"]) * rest_scale} for ev in original_after]
        return original_before + [{"date": trig_date, "capital_ratio": early_ratio, "net_return": early_ret, "exit_reason": reason + "_no30m_half_exit"}] + adjusted_after
    raise ValueError(policy)


def simulate(candidates: pd.DataFrame, legs: pd.DataFrame, audit: pd.DataFrame, policy: str, profile: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    d = candidates.sort_values(["entry_date_ts", "route_priority", "rank_key"], ascending=[True, True, False]).copy()
    legs_map = {str(k): g.copy() for k, g in legs.groupby("candidate_key", sort=False)}
    triggers, trigger_tags = trigger_dict(audit, profile)
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
                    out.update({"policy_exit_date": day, "leg_capital_ratio": float(ev["capital_ratio"]), "policy_net_ret": float(ev["net_return"]), "exit_reason": ev["exit_reason"], "realized_pnl": proceeds - principal, "exit_value": proceeds})
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
                if opened >= 1:
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
    return pd.DataFrame(curve), pd.DataFrame(closed), trigger_tags


def route_summary(closed: pd.DataFrame, book: str, policy: str, profile: str) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    out = closed.groupby(["route", "source_family", "market_style"], dropna=False).agg(
        positions=("candidate_key", "nunique"),
        legs=("candidate_key", "size"),
        pnl=("realized_pnl", "sum"),
        avg_return=("policy_net_ret", "mean"),
    ).reset_index()
    out.insert(0, "profile", profile)
    out.insert(0, "policy", policy)
    out.insert(0, "book", book)
    return out


def write_report(summary: pd.DataFrame, windows: pd.DataFrame, triggers: pd.DataFrame, routes: pd.DataFrame) -> None:
    pct_cols = {"total_return", "max_drawdown", "win_rate", "avg_leg_return", "bad5_rate", "return", "avg_return", "trigger_rate", "rescue_rate"}
    money_cols = {"pnl"}
    lines = [
        "# G3 弱反弹 G2 D1/D2 + 30m 承接救援复算 v1",
        "",
        "## 回测范围",
        "- 组合窗口：2020-01-01 至 2026-06-04。",
        "- G2 v4 实际信号：2024-09-26 至 2026-05-20；本轮只处理弱反弹 G2 中 D1/D2 转弱样本。",
        "- 30m 承接只使用触发日早盘 10:00-11:30 可见的 30m K 线：阳线、收盘位置不弱、成交额不弱于前三根、且不再创新低或突破前一根高点。",
        "",
        "## 英文名解释",
        "- `weak_d1d2_30m_rescue_full_exit`：D1/D2 转弱后，如果早盘 30m 有承接就保留原 G2；没有承接才全退。",
        "- `weak_d1d2_30m_rescue_half_exit`：D1/D2 转弱后，如果早盘 30m 有承接就保留原 G2；没有承接则半退，剩余继续按原 G2。",
        "- `am_rescue`：早盘 30m 承接确认。",
        "",
        "## 总体结果",
        md_table(summary, pct_cols=pct_cols),
        "",
        "## 分窗结果",
        md_table(windows, pct_cols=pct_cols),
        "",
        "## D1/D2 触发与 30m 承接覆盖",
        md_table(triggers, pct_cols=pct_cols),
        "",
        "## 路由拆分",
        md_table(routes, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 阶段判断",
        "- 如果 `30m_rescue` 仍不能同时改善回撤和收益，说明 D1/D2 弱确认后的风险处理不能靠单个 30m 承接规则解决。",
        "- 更可能的方向是把弱反弹 G2 拆成 `volume5` 与 `big_bull` 两套处理，或者改成观察标签而非退出标签。",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    books = {"balanced_router": ROUTER_BALANCED, "range_unknown_router": ROUTER_UNKNOWN}
    summary_rows = []
    window_rows = []
    trigger_rows = []
    route_frames = []
    for book, path in books.items():
        candidates, legs, audit = load_inputs(path)
        for policy in RESCUE_POLICIES:
            for profile in PROFILES:
                curve, closed, tags = simulate(candidates, legs, audit, policy, profile)
                run_dir = OUT_DIR / f"{book}__{policy}__{profile['profile']}"
                run_dir.mkdir(parents=True, exist_ok=True)
                curve.to_csv(run_dir / "mtm_equity_curve.csv", index=False, encoding="utf-8-sig")
                closed.to_csv(run_dir / "closed_legs.csv", index=False, encoding="utf-8-sig")
                tags.to_csv(run_dir / "d1d2_30m_trigger_tags.csv", index=False, encoding="utf-8-sig")
                summary_rows.append(summarize(curve, closed, book, policy, profile["profile"]))
                window_rows.extend(window_metrics(curve, closed, book, policy, profile["profile"]))
                route_frames.append(route_summary(closed, book, policy, profile["profile"]))
                trigger_rows.append(
                    {
                        "book": book,
                        "policy": policy,
                        "profile": profile["profile"],
                        "triggered_positions": int(tags["candidate_key"].nunique()) if not tags.empty else 0,
                        "am_rescue_positions": int(tags.loc[tags.get("am_rescue", False), "candidate_key"].nunique()) if not tags.empty else 0,
                        "rescue_rate": float(tags.get("am_rescue", pd.Series(dtype=bool)).mean()) if not tags.empty else 0.0,
                    }
                )
    summary = pd.DataFrame(summary_rows)
    windows = pd.DataFrame(window_rows)
    triggers = pd.DataFrame(trigger_rows)
    routes = pd.concat(route_frames, ignore_index=True) if route_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    windows.to_csv(OUT_DIR / "window_metrics.csv", index=False, encoding="utf-8-sig")
    triggers.to_csv(OUT_DIR / "trigger_30m_rescue_summary.csv", index=False, encoding="utf-8-sig")
    routes.to_csv(OUT_DIR / "route_breakdown.csv", index=False, encoding="utf-8-sig")
    write_report(summary, windows, triggers, routes)
    print(f"written: {OUT_DIR}")


if __name__ == "__main__":
    main()

"""Research-only audit of the mainline leader/core/expansion chain.

This is intentionally an event study, not a backtest or an order source.  It
tests the user's original mainline intuition in a traceable sequence:
sector has leaders -> sector has liquid cores -> a non-leader expansion stock
approaches/breaks its prior high.  All security joins keep QMT exchange codes.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.research_main_wave_sector_score import _add_stock_features, _load_daily, _load_members
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


OUT = report_path("g3_mainline_role_chain_research_v1")
INDEX_CODE = "000001.SH"


def _safe(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _index_context(start: str, end: str) -> pd.DataFrame:
    d = clickhouse_query_df(
        """
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = ? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        [INDEX_CODE, start, end],
    )
    if d.empty:
        raise RuntimeError(f"missing canonical index data: {INDEX_CODE}")
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce")
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna().sort_values("trade_date")
    d["index_mom60"] = d["close"].pct_change(60)
    return d[["trade_date", "index_mom60"]]


def _add_event_features(daily: pd.DataFrame) -> pd.DataFrame:
    d = _add_stock_features(daily).copy()
    d = d.sort_values(["code_key", "trade_date"]).reset_index(drop=True)
    g = d.groupby("code_key", group_keys=False)
    d["high20_prev"] = g["high"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).max())
    d["mom5"] = g["close"].pct_change(5)
    d["next_open"] = g["open"].shift(-1)
    for horizon in (5, 10, 20):
        d[f"fwd_ret_{horizon}"] = g["close"].shift(-horizon) / d["next_open"] - 1.0
    d["amount_rank"] = d.groupby("trade_date")["amount20"].rank(pct=True)
    return d.replace([np.inf, -np.inf], np.nan)


def _build_chain(daily: pd.DataFrame, members: pd.DataFrame, index: pd.DataFrame, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = daily.merge(
        members[["stock_code_key", "sector_code", "sector_name", "member_count"]],
        left_on="code_key",
        right_on="stock_code_key",
        how="inner",
    ).merge(index, on="trade_date", how="left")
    d = d[(d["trade_date"] >= pd.Timestamp(start)) & (d["trade_date"] <= pd.Timestamp(end))].copy()
    d["leader"] = (
        d["mom20"].ge(0.12) & d["mom60"].ge(0.15) & d["close"].ge(d["ma20"]) & d["stock_amount_ratio5_20"].ge(1.0)
    )
    d["core"] = (
        d["amount_rank"].ge(0.80) & d["mom20"].between(0.05, 0.35) & d["close"].ge(d["ma20"]) & d["ma20"].ge(d["ma60"])
    )
    d["expansion"] = (
        d["mom20"].between(0.05, 0.35) & d["close"].ge(d["ma20"]) & d["ma20"].ge(d["ma60"])
        & d["stock_amount_ratio5_20"].between(1.0, 2.8) & d["close"].ge(d["high20_prev"] * 0.99) & d["close"].le(d["high20_prev"] * 1.06)
        & ~d["leader"]
    )
    keys = ["trade_date", "sector_code", "sector_name"]
    state = d.groupby(keys, as_index=False).agg(
        valid_members=("code_key", "nunique"),
        configured_members=("member_count", "max"),
        leader_count=("leader", "sum"),
        core_count=("core", "sum"),
        expansion_count=("expansion", "sum"),
        leader_mom20_top=("mom20", "max"),
        core_amount20_median=("amount20", "median"),
    )
    required = np.maximum(3, np.ceil(state["configured_members"].fillna(0) * 0.25))
    state["chain_confirmed"] = (
        state["valid_members"].ge(required) & state["leader_count"].ge(2) & state["core_count"].ge(1) & state["expansion_count"].ge(1)
    )
    state["chain_score"] = (
        (state["leader_count"].clip(upper=5) / 5.0) * 45.0
        + (state["core_count"].clip(upper=4) / 4.0) * 30.0
        + (state["expansion_count"].clip(upper=5) / 5.0) * 25.0
    )
    picks = d[d["expansion"]].merge(state[keys + ["chain_confirmed", "chain_score", "leader_count", "core_count", "expansion_count"]], on=keys, how="inner")
    picks = picks[picks["chain_confirmed"] & picks["index_mom60"].le(0.05)].copy()
    picks["entry_date"] = picks.groupby("code_key")["trade_date"].shift(-1)
    picks["expansion_score"] = (
        ((picks["close"] / picks["high20_prev"] - 0.99) / 0.07).clip(0, 1) * 35.0
        + ((picks["stock_amount_ratio5_20"] - 1.0) / 1.2).clip(0, 1) * 25.0
        + (picks["chain_score"] / 100.0) * 25.0
        + ((picks["mom5"] + 0.02) / 0.12).clip(0, 1) * 15.0
    )
    keep = [
        "trade_date", "entry_date", "code_key", "code_raw", "sector_code", "sector_name", "close", "high20_prev", "mom5", "mom20", "mom60",
        "stock_amount_ratio5_20", "index_mom60", "leader_count", "core_count", "expansion_count", "chain_score", "expansion_score", "fwd_ret_5", "fwd_ret_10", "fwd_ret_20",
    ]
    return state, picks[[c for c in keep if c in picks.columns]].sort_values(["trade_date", "expansion_score"], ascending=[True, False])


def _summary(picks: pd.DataFrame) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for horizon in (5, 10, 20):
        values = pd.to_numeric(picks.get(f"fwd_ret_{horizon}"), errors="coerce").dropna()
        result.append({
            "horizon": horizon,
            "events": int(len(values)),
            "avg_return": _safe(values.mean()),
            "median_return": _safe(values.median()),
            "win_rate": _safe((values > 0).mean()),
            "worst_return": _safe(values.min()),
        })
    return result


def _top_two_per_day(picks: pd.DataFrame) -> pd.DataFrame:
    return (
        picks.sort_values(["trade_date", "expansion_score", "chain_score"], ascending=[True, False, False])
        .groupby("trade_date", as_index=False)
        .head(2)
        .copy()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Research leader/core/expansion chain; never creates orders.")
    parser.add_argument("--start-date", default="2024-01-02")
    parser.add_argument("--end-date", default="2025-12-12")
    parser.add_argument("--min-members", type=int, default=5)
    args = parser.parse_args()
    load_start = (pd.Timestamp(args.start_date) - pd.Timedelta(days=180)).strftime("%Y-%m-%d")
    members = _load_members([2], args.min_members)
    daily = _add_event_features(_load_daily(load_start, args.end_date))
    state, picks = _build_chain(daily, members, _index_context(load_start, args.end_date), args.start_date, args.end_date)
    top_two = _top_two_per_day(picks)
    OUT.mkdir(parents=True, exist_ok=True)
    state.to_csv(OUT / "sector_chain_states.csv", index=False, encoding="utf-8-sig")
    picks.to_csv(OUT / "expansion_events.csv", index=False, encoding="utf-8-sig")
    top_two.to_csv(OUT / "top2_per_day_events.csv", index=False, encoding="utf-8-sig")
    payload = {
        "status": "completed", "index_code": INDEX_CODE, "period": [args.start_date, args.end_date], "members": int(len(members)),
        "sector_state_rows": int(len(state)), "confirmed_sector_days": int(state["chain_confirmed"].sum()), "events": int(len(picks)), "summary": _summary(picks),
        "top2_per_day": {"candidate_days": int(top_two["trade_date"].nunique()), "events": int(len(top_two)), "summary": _summary(top_two)},
        "limitations": "Current QMT sector membership is static rather than point-in-time; this is an event study only, not a portfolio backtest or order source.",
    }
    (OUT / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 主线龙头-中军-扩散启动研究 V1", "", f"- 指数口径：`{INDEX_CODE}`", f"- 区间：`{args.start_date}` 至 `{args.end_date}`", "- 只研究事件收益；未接入候选池、风控或下单。", "- 成分为当前 QMT 静态映射，不能直接作为实盘证明。", "", "| 持有日 | 事件数 | 均值 | 中位数 | 胜率 | 最差 |", "|---:|---:|---:|---:|---:|---:|"]
    for row in payload["summary"]:
        lines.append(f"| {row['horizon']} | {row['events']} | {row['avg_return'] or 0:.2%} | {row['median_return'] or 0:.2%} | {row['win_rate'] or 0:.2%} | {row['worst_return'] or 0:.2%} |")
    lines.extend(["", "## 二槽密度口径：每日综合分前二", "", "| 持有日 | 事件数 | 均值 | 中位数 | 胜率 | 最差 |", "|---:|---:|---:|---:|---:|---:|"])
    for row in payload["top2_per_day"]["summary"]:
        lines.append(f"| {row['horizon']} | {row['events']} | {row['avg_return'] or 0:.2%} | {row['median_return'] or 0:.2%} | {row['win_rate'] or 0:.2%} | {row['worst_return'] or 0:.2%} |")
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

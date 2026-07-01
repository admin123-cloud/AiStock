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

from scripts.gen3_build_four_path_candidates import (  # noqa: E402
    _add_index_features,
    _add_stock_features,
    _build_market_context,
    _load_index_daily,
    _load_stock_daily,
    _load_trade_dates,
    _with_entry_date,
)
from utils.paths import report_path


OUT_DIR = report_path("g3_five_strategies_from_scratch_v1")
INITIAL_CAPITAL = 1_000_000.0

STRATEGY_LABELS = {
    "institutional_score120_mainwave": "机构主升Score120",
    "old_g3_strong_breakout": "强势突破",
    "volume_runup_supplement": "量能续强补位",
    "range_weak_repair": "震荡弱势修复",
    "panic_capitulation_repair": "恐慌出清修复",
}

STRATEGY_PRIORITY = {
    "institutional_score120_mainwave": 0,
    "old_g3_strong_breakout": 1,
    "panic_capitulation_repair": 2,
    "range_weak_repair": 3,
    "volume_runup_supplement": 4,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
    except Exception:
        return default
    return x if math.isfinite(x) else default


def _pct(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:.1%}"


def _money(value: Any) -> str:
    try:
        x = float(value)
    except Exception:
        return "--"
    if not math.isfinite(x):
        return "--"
    return f"{x:,.0f}"


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, money_cols: set[str] | None = None, max_rows: int = 40) -> str:
    if df.empty:
        return "_无数据_"
    pct_cols = pct_cols or set()
    money_cols = money_cols or set()
    d = df.head(max_rows).copy()
    for col in pct_cols:
        if col in d.columns:
            d[col] = d[col].map(_pct)
    for col in money_cols:
        if col in d.columns:
            d[col] = d[col].map(_money)
    return d.to_markdown(index=False)


def _rank_pct(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").rank(pct=True).fillna(0.0)


def _build_sector_proxy(features: pd.DataFrame) -> pd.DataFrame:
    d = features.copy()
    d["code_prefix3"] = d["code"].astype(str).str[:3]
    d["sector_proxy"] = np.select(
        [
            d["code"].astype(str).str.startswith("688"),
            d["code"].astype(str).str.startswith("300"),
            d["code"].astype(str).str.startswith("60"),
            d["code"].astype(str).str.startswith("00"),
        ],
        ["科创板", "创业板", "沪市主板", "深市主板"],
        default=d["code_prefix3"],
    )
    d["stock_strong"] = (pd.to_numeric(d["mom20"], errors="coerce") >= 0.08) & (pd.to_numeric(d["close"], errors="coerce") > pd.to_numeric(d["ma20"], errors="coerce"))
    sector = (
        d.groupby(["trade_date", "sector_proxy"], dropna=False)
        .agg(
            sector_stock_count=("code", "count"),
            sector_strong_count=("stock_strong", "sum"),
            sector_avg_mom20=("mom20", "mean"),
            sector_avg_amount20=("amount20", "mean"),
        )
        .reset_index()
    )
    sector["sector_share_strong"] = sector["sector_strong_count"] / sector["sector_stock_count"].clip(lower=1)
    sector["sector_diffusion_score"] = (
        sector.groupby("trade_date")["sector_strong_count"].rank(pct=True).fillna(0.0) * 35.0
        + sector.groupby("trade_date")["sector_avg_mom20"].rank(pct=True).fillna(0.0) * 35.0
        + sector.groupby("trade_date")["sector_share_strong"].rank(pct=True).fillna(0.0) * 30.0
    )
    return d.merge(sector[["trade_date", "sector_proxy", "sector_diffusion_score"]], on=["trade_date", "sector_proxy"], how="left")


def _score_wave_style(d: pd.DataFrame) -> pd.Series:
    close_to_high60 = pd.to_numeric(d["close"], errors="coerce") / pd.to_numeric(d["high60"], errors="coerce") - 1.0
    amount_ratio20 = pd.to_numeric(d["amount_ratio20"], errors="coerce")
    mom20 = pd.to_numeric(d["mom20"], errors="coerce")
    mom60 = pd.to_numeric(d.get("runup_from_60d_low"), errors="coerce")
    range_pos60 = pd.to_numeric(d["range_pos60"], errors="coerce")
    ret5 = pd.to_numeric(d["mom5"], errors="coerce")
    capacity_score = (pd.to_numeric(d["amount20"], errors="coerce") / 100_000.0).clip(0.0, 1.0) * 20.0
    trend_score = (
        (pd.to_numeric(d["close"], errors="coerce") > pd.to_numeric(d["ma20"], errors="coerce")).astype(float) * 8.0
        + (pd.to_numeric(d["close"], errors="coerce") > pd.to_numeric(d["ma60"], errors="coerce")).astype(float) * 8.0
        + ((mom20 + 0.05) / 0.25).clip(0.0, 1.0).fillna(0.0) * 12.0
        + ((mom60 + 0.10) / 0.55).clip(0.0, 1.0).fillna(0.0) * 4.0
    )
    position_score = ((range_pos60 - 0.35) / 0.45).clip(0.0, 1.0).fillna(0.0) * 12.0 + ((close_to_high60 + 0.12) / 0.12).clip(0.0, 1.0).fillna(0.0) * 12.0
    volume_score = ((amount_ratio20 - 0.90) / 0.80).clip(0.0, 1.0).fillna(0.0) * 14.0
    acceleration_score = ((ret5 + 0.02) / 0.18).clip(0.0, 1.0).fillna(0.0) * 12.0
    sector_score = (pd.to_numeric(d.get("sector_diffusion_score"), errors="coerce").fillna(0.0) / 100.0).clip(0.0, 1.0) * 18.0
    return capacity_score + trend_score + position_score + volume_score + acceleration_score + sector_score


def _select_strategy_candidates(features: pd.DataFrame, ctx: pd.DataFrame, top_n: int, min_amount20: float) -> pd.DataFrame:
    d = _build_sector_proxy(features)
    d = d.merge(
        ctx[
            [
                "trade_date",
                "market_style",
                "up_rate",
                "big_down_rate",
                "limit_down_proxy_rate",
                "breadth_ma20",
                "breadth_ma60",
                "mom20",
                "mom60",
                "market_amount_ratio20",
            ]
        ].rename(columns={"mom20": "index_mom20", "mom60": "index_mom60"}),
        on="trade_date",
        how="left",
    )
    d = d[(pd.to_numeric(d["amount20"], errors="coerce") >= float(min_amount20)) & (pd.to_numeric(d["close"], errors="coerce") > 0)].copy()
    d = d[~d["code"].astype(str).str.endswith(".BJ")].copy()

    frames: list[pd.DataFrame] = []

    inst = d[
        (d["market_style"] == "standard_uptrend")
        & (d["close"] > d["ma20"])
        & (d["close"] > d["ma60"])
        & (d["mom20"] >= 0.08)
        & (d["range_pos60"] >= 0.55)
        & (d["amount_ratio20"].between(0.9, 3.5))
    ].copy()
    if not inst.empty:
        inst["trade_strategy"] = "institutional_score120_mainwave"
        inst["source_strategy_label"] = "机构主升Score120"
        inst["strategy_score"] = _score_wave_style(inst)
        inst = inst[
            (inst["strategy_score"] >= 120.0)
            & (pd.to_numeric(inst["sector_diffusion_score"], errors="coerce") >= 65.0)
            & (pd.to_numeric(inst["index_mom60"], errors="coerce") <= 0.05)
        ].copy()
        frames.append(inst)

    strong = d[
        (d["market_style"] == "standard_uptrend")
        & (d["close"] > d["ma20"])
        & (d["ma20"] > d["ma60"])
        & (d["mom10"] >= 0.05)
        & (d["mom20"] >= 0.12)
        & (d["close"] >= d["high20"])
        & (d["amount_ratio20"].between(1.10, 3.0))
        & (d["range_pos60"] >= 0.65)
        & (pd.to_numeric(d["index_mom60"], errors="coerce") <= 0.10)
    ].copy()
    if not strong.empty:
        strong["trade_strategy"] = "old_g3_strong_breakout"
        strong["source_strategy_label"] = "强势突破"
        strong["strategy_score"] = (
            0.30 * _rank_pct(strong["mom20"])
            + 0.25 * _rank_pct(strong["mom10"])
            + 0.25 * _rank_pct(strong["amount_ratio20"].clip(0, 3))
            + 0.20 * _rank_pct(strong["range_pos60"].clip(0, 1))
        )
        strong["strategy_score"] = strong["strategy_score"] * 100.0
        frames.append(strong)

    range_weak = d[
        (
            (
                (d["market_style"] == "weak_rebound")
                & (d["drawdown20"].between(-0.35, -0.08))
                & (d["close"] >= d["ma5"])
                & (d["close_position"] >= 0.55)
                & (d["amount_ratio20"].between(0.8, 3.0))
                & (d["range_pos60"].between(0.15, 0.65))
            )
            | (
                (d["market_style"] == "standard_range")
                & (d["big_down_rate"] >= 0.10)
                & (d["drawdown20"] <= -0.18)
                & (d["close_position"] >= 0.55)
                & (d["amount_ratio20"].between(1.0, 5.0))
                & (d["ret1"].between(-0.09, 0.05))
                & (d["range_pos60"] <= 0.55)
            )
        )
    ].copy()
    if not range_weak.empty:
        range_weak["trade_strategy"] = "range_weak_repair"
        range_weak["source_strategy_label"] = np.where(range_weak["market_style"].eq("standard_range"), "震荡恐慌修复", "旧G3弱势/震荡修复")
        range_weak["strategy_score"] = (
            0.28 * _rank_pct(range_weak["close_position"])
            + 0.24 * _rank_pct(range_weak["amount_ratio20"].clip(0, 3))
            + 0.24 * _rank_pct(-range_weak["drawdown20"])
            + 0.14 * _rank_pct(range_weak["mom5"])
            + 0.10 * _rank_pct(range_weak["lower_shadow_ratio"])
        ) * 100.0
        frames.append(range_weak)

    panic = d[
        (d["market_style"] == "standard_downtrend")
        & ((d["up_rate"] <= 0.25) | (d["big_down_rate"] >= 0.18) | (d["limit_down_proxy_rate"] >= 0.015))
        & ((d["drawdown10"] <= -0.12) | (d["drawdown20"] <= -0.20))
        & (d["close_position"] >= 0.35)
        & (d["lower_shadow_ratio"] >= 0.25)
        & (d["ret1"] > -0.095)
        & (d["amount_ratio20"].between(1.0, 4.5))
        & (d["range_pos60"] <= 0.45)
    ].copy()
    if not panic.empty:
        panic["trade_strategy"] = "panic_capitulation_repair"
        panic["source_strategy_label"] = "恐慌出清修复"
        panic["strategy_score"] = (
            0.35 * _rank_pct(-panic["drawdown10"])
            + 0.25 * _rank_pct(panic["lower_shadow_ratio"])
            + 0.25 * _rank_pct(panic["amount_ratio20"].clip(0, 4))
            + 0.15 * _rank_pct(panic["close_position"])
        ) * 100.0
        frames.append(panic)

    volume = d[
        (d["market_style"].isin(["standard_uptrend", "weak_rebound"]))
        & (d["close"] > d["ma20"])
        & (d["mom5"] >= 0.03)
        & (d["mom20"].between(0.03, 0.45))
        & (d["amount_ratio5"] >= 1.10)
        & (d["amount_ratio20"].between(1.0, 4.0))
        & (d["range_pos60"].between(0.45, 0.95))
    ].copy()
    if not volume.empty:
        volume["trade_strategy"] = "volume_runup_supplement"
        volume["source_strategy_label"] = np.where(pd.to_numeric(volume["sector_diffusion_score"], errors="coerce") >= 55.0, "G2量能续强+板块加分", "G2量能续强")
        volume["strategy_score"] = (
            0.30 * _rank_pct(volume["mom5"])
            + 0.25 * _rank_pct(volume["amount_ratio5"].clip(0, 4))
            + 0.20 * _rank_pct(volume["amount_ratio20"].clip(0, 4))
            + 0.15 * _rank_pct(volume["range_pos60"].clip(0, 1))
            + 0.10 * _rank_pct(volume["sector_diffusion_score"].fillna(0))
        ) * 100.0
        frames.append(volume)

    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out["trade_strategy_label"] = out["trade_strategy"].map(STRATEGY_LABELS)
    out = out[pd.to_numeric(out.get("index_mom60"), errors="coerce").fillna(0.0) <= 0.10].copy()
    out["strategy_priority"] = out["trade_strategy"].map(STRATEGY_PRIORITY).fillna(99).astype(int)
    out = out.sort_values(["trade_date", "strategy_priority", "strategy_score", "amount20"], ascending=[True, True, False, False])
    out["strategy_rank"] = out.groupby(["trade_date", "trade_strategy"]).cumcount() + 1
    out = out[out["strategy_rank"] <= int(top_n)].copy()
    return out


def _exit_contract(strategy: str, entry_price: float) -> dict[str, float]:
    if strategy == "institutional_score120_mainwave":
        return {"structure_stop_pct": -0.12, "hard_stop_pct": -0.12, "take_profit_pct": 0.12, "take_profit_ratio": 0.50, "max_hold_days": 12}
    if strategy == "volume_runup_supplement":
        return {"structure_stop_pct": -0.05, "hard_stop_pct": -0.05, "take_profit_pct": 0.10, "take_profit_ratio": 0.50, "max_hold_days": 8}
    if strategy == "panic_capitulation_repair":
        return {"structure_stop_pct": -0.05, "hard_stop_pct": -0.08, "take_profit_pct": 0.08, "take_profit_ratio": 0.50, "max_hold_days": 8}
    if strategy == "range_weak_repair":
        return {"structure_stop_pct": -0.06, "hard_stop_pct": -0.10, "take_profit_pct": 0.08, "take_profit_ratio": 0.50, "max_hold_days": 8}
    return {"structure_stop_pct": -0.10, "hard_stop_pct": -0.10, "take_profit_pct": 0.08, "take_profit_ratio": 0.50, "max_hold_days": 10}


def _simulate_trade(row: pd.Series, daily_by_code: dict[str, pd.DataFrame], calendar: list[pd.Timestamp]) -> dict[str, Any] | None:
    code = str(row.get("code") or "")
    entry_date = pd.Timestamp(row.get("entry_date")).normalize()
    bars = daily_by_code.get(code)
    if bars is None or bars.empty:
        return None
    future = bars[bars["trade_date_ts"].ge(entry_date)].head(20).copy()
    if future.empty:
        return None
    entry_bar = future.iloc[0]
    entry_price = _safe_float(entry_bar.get("open"))
    if entry_price <= 0:
        entry_price = _safe_float(entry_bar.get("close"))
    if entry_price <= 0:
        return None
    contract = _exit_contract(str(row.get("trade_strategy")), entry_price)
    hard_stop = entry_price * (1.0 + contract["hard_stop_pct"])
    structure_stop = entry_price * (1.0 + contract["structure_stop_pct"])
    take_profit = entry_price * (1.0 + contract["take_profit_pct"])
    max_hold = int(contract["max_hold_days"])
    took_profit = False
    exit_price = _safe_float(future.iloc[min(len(future), max_hold) - 1].get("close"))
    exit_date = pd.Timestamp(future.iloc[min(len(future), max_hold) - 1].get("trade_date_ts")).normalize()
    exit_reason = f"hold{max_hold}"
    for i, bar in enumerate(future.itertuples(index=False), start=1):
        low = _safe_float(getattr(bar, "low"))
        high = _safe_float(getattr(bar, "high"))
        close = _safe_float(getattr(bar, "close"))
        day = pd.Timestamp(getattr(bar, "trade_date_ts")).normalize()
        if low > 0 and low <= hard_stop:
            exit_price = hard_stop
            exit_date = day
            exit_reason = "hard_stop"
            break
        if low > 0 and low <= structure_stop:
            exit_price = structure_stop
            exit_date = day
            exit_reason = "structure_stop"
            break
        if not took_profit and high >= take_profit:
            took_profit = True
        if took_profit and i >= 2:
            prev_low = _safe_float(future.iloc[max(0, i - 2)].get("low"))
            if close > 0 and prev_low > 0 and close < prev_low:
                exit_price = close
                exit_date = day
                exit_reason = "prev_low_break_after_take_profit"
                break
        if i >= max_hold:
            exit_price = close
            exit_date = day
            exit_reason = f"hold{max_hold}"
            break
    gross_ret = exit_price / entry_price - 1.0 if entry_price > 0 else np.nan
    net_ret = gross_ret - 0.003
    out = row.to_dict()
    out.update(
        {
            "entry_date": entry_date.strftime("%Y-%m-%d"),
            "policy_exit_date": exit_date.strftime("%Y-%m-%d"),
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "gross_ret": gross_ret,
            "net_ret": net_ret,
            "exit_reason": exit_reason,
            "took_profit": took_profit,
            "structure_stop_pct": contract["structure_stop_pct"],
            "hard_stop_pct": contract["hard_stop_pct"],
            "take_profit_pct": contract["take_profit_pct"],
        }
    )
    return out


def _pick_daily(candidates: pd.DataFrame, slots: int = 2) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, day in candidates.groupby("entry_date"):
        day = day.sort_values(["strategy_priority", "strategy_score", "amount20"], ascending=[True, False, False])
        picked: list[dict[str, Any]] = []
        used_codes: set[str] = set()
        used_sector: set[str] = set()
        for item in day.to_dict("records"):
            code = str(item.get("code") or "")
            sector = str(item.get("sector_proxy") or "")
            strategy = str(item.get("trade_strategy") or "")
            if code in used_codes:
                continue
            if sector and sector in used_sector and strategy != "institutional_score120_mainwave":
                continue
            picked.append(item)
            used_codes.add(code)
            if sector:
                used_sector.add(sector)
            if len(picked) >= slots:
                break
        rows.extend(picked)
    return pd.DataFrame(rows)


def _simulate_portfolio(trades: pd.DataFrame, calendar: list[pd.Timestamp], slots: int, slot_pct: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_entry = {pd.Timestamp(k).normalize(): g.copy() for k, g in trades.groupby(pd.to_datetime(trades["entry_date"], errors="coerce"))}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    for day in calendar:
        realized = 0.0
        still: list[dict[str, Any]] = []
        for pos in open_pos:
            if pd.Timestamp(pos["policy_exit_date"]).normalize() <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                pnl = exit_value - float(pos["stake"])
                realized += pnl
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = pnl
                closed.append(out)
            else:
                still.append(pos)
        open_pos = still
        opened = 0
        todays = by_entry.get(day)
        if todays is not None and not todays.empty:
            for row in todays.sort_values(["strategy_priority", "strategy_score"], ascending=[True, False]).to_dict("records"):
                if len(open_pos) >= slots or opened >= slots:
                    break
                if any(str(p.get("code")) == str(row.get("code")) for p in open_pos):
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                heat_scale = 1.0
                stake = equity_before * slot_pct
                if cash < stake or stake <= 0:
                    continue
                row["position_scale"] = heat_scale
                row["stake"] = stake
                cash -= stake
                open_pos.append(row)
                opened += 1
        equity = cash + sum(float(p["stake"]) for p in open_pos)
        curve.append({"date": day.strftime("%Y-%m-%d"), "cash": cash, "reserved_principal": equity - cash, "equity": equity, "opened": opened, "open_positions": len(open_pos), "realized_pnl": realized})
    curve_df = pd.DataFrame(curve)
    if not curve_df.empty:
        curve_df["peak"] = curve_df["equity"].cummax()
        curve_df["drawdown"] = curve_df["equity"] / curve_df["peak"] - 1.0
        curve_df["ret_from_start"] = curve_df["equity"] / INITIAL_CAPITAL - 1.0
    return curve_df, pd.DataFrame(closed)


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    ret = pd.to_numeric(df.get("net_ret"), errors="coerce").dropna()
    pnl = pd.to_numeric(df.get("realized_pnl"), errors="coerce")
    return {
        "trade_count": int(len(df)),
        "win_rate": float((ret > 0).mean()) if len(ret) else None,
        "avg_ret": float(ret.mean()) if len(ret) else None,
        "median_ret": float(ret.median()) if len(ret) else None,
        "worst_ret": float(ret.min()) if len(ret) else None,
        "best_ret": float(ret.max()) if len(ret) else None,
        "loss_rate_le_5pct": float((ret <= -0.05).mean()) if len(ret) else None,
        "hard_loss_rate_le_10pct": float((ret <= -0.10).mean()) if len(ret) else None,
        "sum_pnl": float(pnl.sum()) if pnl.notna().any() else 0.0,
    }


def _group_metrics(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    rows = []
    for keys, part in df.groupby(cols, dropna=False):
        if not isinstance(keys, tuple):
            keys = (keys,)
        row = {col: key for col, key in zip(cols, keys)}
        row.update(_metrics(part))
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["trade_count", "sum_pnl"], ascending=[False, False]) if rows else pd.DataFrame()


def run(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = report_path("g3_five_strategies_from_scratch_v1")
    out_dir.mkdir(parents=True, exist_ok=True)
    trade_dates = _load_trade_dates(args.start_date, args.end_date)
    stocks = _load_stock_daily(args.start_date, args.end_date, max_codes=int(args.max_codes or 0))
    index = _load_index_daily(args.start_date, args.end_date)
    features = _add_stock_features(stocks)
    index_features = _add_index_features(index)
    ctx = _build_market_context(features, index_features, min_amount20=float(args.min_amount20))
    candidates = _select_strategy_candidates(features, ctx, top_n=int(args.top_n), min_amount20=float(args.min_amount20))
    candidates = _with_entry_date(candidates, trade_dates)
    candidates = candidates[pd.to_datetime(candidates["entry_date"], errors="coerce").between(pd.Timestamp(args.start_date), pd.Timestamp(args.end_date))].copy()
    selected = _pick_daily(candidates, slots=int(args.slots))

    daily_for_exit = stocks.copy()
    daily_for_exit["trade_date_ts"] = pd.to_datetime(daily_for_exit["trade_date"], errors="coerce").dt.normalize()
    daily_by_code = {code: g.sort_values("trade_date_ts").copy() for code, g in daily_for_exit.groupby("code", sort=False)}
    sim_rows = []
    for row in selected.to_dict("records"):
        item = _simulate_trade(pd.Series(row), daily_by_code, [pd.Timestamp(x) for x in trade_dates])
        if item is not None:
            sim_rows.append(item)
    trade_events = pd.DataFrame(sim_rows)
    calendar = [pd.Timestamp(x).normalize() for x in trade_dates if pd.Timestamp(args.start_date) <= pd.Timestamp(x) <= pd.Timestamp(args.end_date)]
    curve, closed = _simulate_portfolio(trade_events, calendar, slots=int(args.slots), slot_pct=float(args.slot_pct)) if not trade_events.empty else (pd.DataFrame(), pd.DataFrame())

    candidates.to_csv(out_dir / "all_strategy_candidates.csv", index=False, encoding="utf-8-sig")
    selected.to_csv(out_dir / "selected_daily_candidates.csv", index=False, encoding="utf-8-sig")
    trade_events.to_csv(out_dir / "simulated_trade_events.csv", index=False, encoding="utf-8-sig")
    closed.to_csv(out_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
    curve.to_csv(out_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    ctx.to_csv(out_dir / "market_context.csv", index=False, encoding="utf-8-sig")

    strategy_metrics = _group_metrics(closed, ["trade_strategy", "trade_strategy_label"]) if not closed.empty else pd.DataFrame()
    source_metrics = _group_metrics(closed, ["trade_strategy_label", "source_strategy_label"]) if not closed.empty else pd.DataFrame()
    exit_metrics = _group_metrics(closed, ["trade_strategy_label", "exit_reason"]) if not closed.empty else pd.DataFrame()
    strategy_metrics.to_csv(out_dir / "strategy_metrics.csv", index=False, encoding="utf-8-sig")
    source_metrics.to_csv(out_dir / "source_metrics.csv", index=False, encoding="utf-8-sig")
    exit_metrics.to_csv(out_dir / "exit_metrics.csv", index=False, encoding="utf-8-sig")

    final_equity = float(curve["equity"].iloc[-1]) if not curve.empty else INITIAL_CAPITAL
    max_dd = float(curve["drawdown"].min()) if not curve.empty else 0.0
    meta = {
        "version": "g3_five_strategies_from_scratch_v1",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "max_codes": int(args.max_codes or 0),
        "source": "clickhouse:kline_daily + stocks; no historical closed_trades as input",
        "candidate_rows": int(len(candidates)),
        "selected_rows": int(len(selected)),
        "closed_trades": int(len(closed)),
        "final_equity": final_equity,
        "total_return": final_equity / INITIAL_CAPITAL - 1.0,
        "max_drawdown": max_dd,
        "output_dir": str(out_dir),
        "note": "30m confirmation is not applied in this first from-scratch run; exits use daily OHLC proxy for unified strategy contracts.",
    }
    (out_dir / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    report = [
        "# G3 五策略从零重建历史回测 v1",
        "",
        "## 口径",
        "",
        "- 输入：从 ClickHouse `kline_daily` 与 `stocks` 重新拉取历史股票/指数日线，重新计算特征、市场状态、候选、买点和卖点。",
        "- 未使用既有 `closed_trades` 作为输入；既有成交文件只用于此前归因审计，不进入本次回测。",
        "- 策略数：5 个正式交易策略，`旧G3强势突破` 已更名为 `强势突破`。",
        "- 限制：本轮先使用日线 OHLC 代理执行统一止损/止盈合同，暂未启用全历史 30m 确认过滤。",
        "",
        "## 总览",
        "",
        f"- 候选数：{meta['candidate_rows']}",
        f"- 每日选中候选：{meta['selected_rows']}",
        f"- 已关闭交易：{meta['closed_trades']}",
        f"- 总收益：{_pct(meta['total_return'])}",
        f"- 最大回撤：{_pct(meta['max_drawdown'])}",
        f"- 最终权益：{_money(meta['final_equity'])}",
        "",
        "## 策略表现",
        "",
        _md_table(strategy_metrics, {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct"}, {"sum_pnl"}),
        "",
        "## 来源子状态表现",
        "",
        _md_table(source_metrics, {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret", "loss_rate_le_5pct", "hard_loss_rate_le_10pct"}, {"sum_pnl"}),
        "",
        "## 退出原因",
        "",
        _md_table(exit_metrics, {"win_rate", "avg_ret", "median_ret", "worst_ret", "best_ret"}, {"sum_pnl"}),
        "",
    ]
    (out_dir / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")
    return meta


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest unified G3 five strategies from raw historical daily data.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-19")
    parser.add_argument("--max-codes", type=int, default=0)
    parser.add_argument("--min-amount20", type=float, default=30000.0)
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--slots", type=int, default=2)
    parser.add_argument("--slot-pct", type=float, default=0.50)
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(run(parse_args()), ensure_ascii=False, indent=2))

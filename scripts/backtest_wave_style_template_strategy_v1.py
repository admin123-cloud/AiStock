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

from scripts.research_main_wave_sector_score import _code6, _load_members  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("wave_style_template_strategy_backtest_v1")
LEARN_DIR = report_path("profitable_wave_stock_style_learning_v1")
INDEX_CODE = "999999.SH"
INITIAL_CAPITAL = 1_000_000.0


def _safe_float(value: Any, digits: int = 6) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _pct(value: Any) -> str:
    x = _safe_float(value)
    if x is None:
        return ""
    return f"{x:.2%}"


def _load_stocks() -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT code, name, industry, quit, st
        FROM stocks
        WHERE code != ''
          AND type = 'stock'
          AND quit = 0
          AND (st = 0 OR st IS NULL)
        """
    )
    if df.empty:
        raise RuntimeError("stocks table has no active stock rows")
    df["code_raw"] = df["code"].astype(str)
    df["code6"] = df["code"].map(_code6)
    df["stock_name"] = df["name"].astype(str)
    df["industry"] = df["industry"].fillna("").astype(str)
    return df[["code_raw", "code6", "stock_name", "industry"]].drop_duplicates("code6")


def _load_daily(start_date: str, end_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT code, trade_date, open, high, low, close, volume, amount, change_pct, turnover_rate
        FROM kline_daily
        WHERE trade_date BETWEEN ? AND ?
          AND close > 0
          AND change_pct > -50
          AND change_pct < 50
        ORDER BY code, trade_date
        """,
        [start_date, end_date],
    )
    if df.empty:
        raise RuntimeError("kline_daily query returned no rows")
    df["code_raw"] = df["code"].astype(str)
    df["code6"] = df["code"].map(_code6)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.normalize()
    for col in ["open", "high", "low", "close", "volume", "amount", "change_pct", "turnover_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code6", "trade_date", "open", "high", "low", "close"])
    df = df.sort_values(["code6", "trade_date", "amount"])
    df = df.groupby(["code6", "trade_date"], as_index=False).tail(1)
    return df.sort_values(["code6", "trade_date"]).reset_index(drop=True)


def _trade_calendar(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    df = clickhouse_query_df(
        """
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")],
    )
    if df.empty:
        return []
    return pd.to_datetime(df["trade_date"], errors="coerce").dropna().dt.normalize().tolist()


def _load_index(start_date: str, end_date: str) -> pd.DataFrame:
    df = clickhouse_query_df(
        """
        SELECT trade_date, close
        FROM kline_daily
        WHERE code = ?
          AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        [INDEX_CODE, start_date, end_date],
    )
    if df.empty:
        return pd.DataFrame()
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
    df["index_ret_from_start"] = df["close"] / df["close"].iloc[0] - 1.0
    return df


def _load_index_features(start_date: str, end_date: str) -> pd.DataFrame:
    preload = (pd.Timestamp(start_date) - pd.Timedelta(days=160)).strftime("%Y-%m-%d")
    df = _load_index(preload, end_date)
    if df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"close": "index_close"})
    df["index_ma20"] = df["index_close"].rolling(20, min_periods=15).mean()
    df["index_ma60"] = df["index_close"].rolling(60, min_periods=40).mean()
    df["index_mom20"] = df["index_close"].pct_change(20)
    df["index_mom60"] = df["index_close"].pct_change(60)
    return df[["trade_date", "index_close", "index_ma20", "index_ma60", "index_mom20", "index_mom60"]]


def _load_learned_sector_focus() -> pd.DataFrame:
    path = LEARN_DIR / "institutional_winners.csv"
    if not path.exists():
        return pd.DataFrame(columns=["l2_sector_name", "learned_count", "learned_avg_return"])
    d = pd.read_csv(path, encoding="utf-8-sig")
    if d.empty or "l2_sector_name" not in d.columns:
        return pd.DataFrame(columns=["l2_sector_name", "learned_count", "learned_avg_return"])
    d["wave_return"] = pd.to_numeric(d.get("wave_return"), errors="coerce")
    out = (
        d.dropna(subset=["l2_sector_name"])
        .groupby("l2_sector_name", as_index=False)
        .agg(learned_count=("code6", "count"), learned_avg_return=("wave_return", "mean"))
    )
    return out


def _add_features(daily: pd.DataFrame, max_hold: int) -> pd.DataFrame:
    d = daily.copy().sort_values(["code6", "trade_date"]).reset_index(drop=True)
    g = d.groupby("code6", group_keys=False)
    d["ret1"] = d["close"] / g["close"].shift(1) - 1.0
    d["ma20"] = g["close"].rolling(20, min_periods=15).mean().reset_index(level=0, drop=True)
    d["ma60"] = g["close"].rolling(60, min_periods=40).mean().reset_index(level=0, drop=True)
    d["high60"] = g["high"].rolling(60, min_periods=30).max().reset_index(level=0, drop=True)
    d["high120"] = g["high"].rolling(120, min_periods=60).max().reset_index(level=0, drop=True)
    d["low120"] = g["low"].rolling(120, min_periods=60).min().reset_index(level=0, drop=True)
    d["amount5"] = g["amount"].rolling(5, min_periods=3).mean().reset_index(level=0, drop=True)
    d["amount20"] = g["amount"].rolling(20, min_periods=10).mean().reset_index(level=0, drop=True)
    d["amount60"] = g["amount"].rolling(60, min_periods=30).mean().reset_index(level=0, drop=True)
    d["mom20"] = g["close"].pct_change(20)
    d["mom60"] = g["close"].pct_change(60)
    d["range_pos120"] = (d["close"] - d["low120"]) / (d["high120"] - d["low120"]).replace(0, np.nan)
    d["close_to_high60"] = d["close"] / d["high60"].replace(0, np.nan) - 1.0
    d["above_ma20"] = d["close"] >= d["ma20"]
    d["above_ma60"] = d["close"] >= d["ma60"]
    d["amount_ratio20_60"] = d["amount20"] / d["amount60"].replace(0, np.nan)
    d["amount5_20"] = d["amount5"] / d["amount20"].replace(0, np.nan)
    d["ret5"] = g["close"].pct_change(5)
    d["ret20"] = g["close"].pct_change(20)
    d["limit_up_day"] = pd.to_numeric(d["change_pct"], errors="coerce") >= 9.5
    d["big_up_day"] = pd.to_numeric(d["change_pct"], errors="coerce") >= 5.0
    d["limit_up_days20"] = g["limit_up_day"].rolling(20, min_periods=1).sum().reset_index(level=0, drop=True)
    d["big_up_days20"] = g["big_up_day"].rolling(20, min_periods=1).sum().reset_index(level=0, drop=True)
    d["max_dd20"] = d["close"] / g["high"].rolling(20, min_periods=5).max().reset_index(level=0, drop=True) - 1.0
    d["next_entry_date"] = g["trade_date"].shift(-1)
    d["next_open"] = g["open"].shift(-1)
    for hold in sorted(set([5, 10, 20, int(max_hold)])):
        d[f"exit_date_{hold}"] = g["trade_date"].shift(-hold)
        d[f"exit_close_{hold}"] = g["close"].shift(-hold)
        d[f"gross_ret_nextopen_h{hold}"] = d[f"exit_close_{hold}"] / d["next_open"] - 1.0
    return d.replace([np.inf, -np.inf], np.nan)


def _attach_sector(daily: pd.DataFrame) -> pd.DataFrame:
    members = _load_members([2], 8)
    l2 = members[members["level"].astype("Int64").eq(2)].copy()
    l2 = l2.sort_values(["stock_code6", "sector_code"]).drop_duplicates("stock_code6")
    out = daily.merge(
        l2[["stock_code6", "sector_code", "sector_name"]].rename(
            columns={"stock_code6": "code6", "sector_code": "l2_sector_code", "sector_name": "l2_sector_name"}
        ),
        on="code6",
        how="left",
    )
    learned = _load_learned_sector_focus()
    out = out.merge(learned, on="l2_sector_name", how="left")
    return out


def _score_candidates(df: pd.DataFrame, use_learned_sector: bool) -> pd.DataFrame:
    d = df.copy()
    d["amount_rank"] = d.groupby("trade_date")["amount20"].rank(pct=True)
    d["learned_sector_bonus"] = 0.0
    if use_learned_sector:
        d["learned_sector_bonus"] = (pd.to_numeric(d.get("learned_count", 0), errors="coerce").fillna(0) / 8.0).clip(0.0, 1.0)
    d["capacity_score"] = d["amount_rank"].fillna(0.0) * 20.0
    d["trend_score"] = (
        d["above_ma20"].fillna(False).astype(float) * 8.0
        + d["above_ma60"].fillna(False).astype(float) * 8.0
        + ((d["mom20"] + 0.05) / 0.25).clip(0.0, 1.0).fillna(0.0) * 8.0
        + ((d["mom60"] + 0.10) / 0.35).clip(0.0, 1.0).fillna(0.0) * 8.0
    )
    d["position_score"] = (
        ((d["range_pos120"] - 0.35) / 0.45).clip(0.0, 1.0).fillna(0.0) * 12.0
        + ((d["close_to_high60"] + 0.15) / 0.15).clip(0.0, 1.0).fillna(0.0) * 12.0
    )
    d["volume_score"] = (
        ((d["amount_ratio20_60"] - 0.90) / 0.80).clip(0.0, 1.0).fillna(0.0) * 10.0
        + ((d["amount5_20"] - 0.90) / 0.80).clip(0.0, 1.0).fillna(0.0) * 10.0
    )
    d["acceleration_score"] = (
        ((d["ret5"] + 0.02) / 0.18).clip(0.0, 1.0).fillna(0.0) * 8.0
        + ((d["change_pct"] + 1.0) / 8.0).clip(0.0, 1.0).fillna(0.0) * 6.0
        + ((d["big_up_days20"] - 1.0) / 5.0).clip(0.0, 1.0).fillna(0.0) * 6.0
    )
    d["learned_sector_score"] = d["learned_sector_bonus"] * 12.0
    d["climax_penalty"] = (
        (pd.to_numeric(d["limit_up_days20"], errors="coerce").fillna(0) / 4.0).clip(0.0, 1.0) * 10.0
        + ((d["ret20"] - 0.65) / 0.45).clip(0.0, 1.0).fillna(0.0) * 8.0
    )
    d["wave_style_score"] = (
        d["capacity_score"]
        + d["trend_score"]
        + d["position_score"]
        + d["volume_score"]
        + d["acceleration_score"]
        + d["learned_sector_score"]
        - d["climax_penalty"]
    )

    institution_trend = (
        (d["amount_rank"] >= 0.70)
        & d["above_ma20"].fillna(False)
        & d["above_ma60"].fillna(False)
        & (d["close_to_high60"] >= -0.10)
        & (d["limit_up_days20"] <= 2)
        & (d["max_dd20"].fillna(-1.0) >= -0.22)
    )
    breakout_accel = (
        (d["amount_rank"] >= 0.70)
        & (d["close_to_high60"] >= -0.08)
        & (d["amount5_20"] >= 1.10)
        & (d["ret5"].fillna(0) >= 0.03)
        & (d["change_pct"].between(1.5, 9.3))
    )
    capacity_theme = (
        (d["amount_rank"] >= 0.70)
        & (d["ret20"].fillna(0) >= 0.08)
        & (d["big_up_days20"] >= 2)
        & ((d["learned_sector_bonus"] > 0) if use_learned_sector else True)
    )
    too_late = (d["ret20"].fillna(0) >= 0.75) | (d["limit_up_days20"] >= 5)
    d["template_label"] = "watch"
    d.loc[institution_trend, "template_label"] = "institution_trend_setup"
    d.loc[breakout_accel, "template_label"] = "breakout_acceleration"
    d.loc[capacity_theme & ~breakout_accel, "template_label"] = "capacity_theme_mainwave"
    d.loc[too_late, "template_label"] = "possible_climax"
    return d


def _build_signal_candidates(
    df: pd.DataFrame,
    args: argparse.Namespace,
    hold_days: int,
    use_learned_sector: bool,
    strict: bool,
    market_gate: str,
) -> pd.DataFrame:
    d = _score_candidates(df, use_learned_sector=use_learned_sector)
    labels = ["institution_trend_setup", "breakout_acceleration", "capacity_theme_mainwave"]
    mask = (
        (d["trade_date"] >= pd.Timestamp(args.start_date))
        & (d["trade_date"] <= pd.Timestamp(args.end_date))
        & ~d["code_raw"].astype(str).str.endswith(".BJ")
        & d["template_label"].isin(labels)
        & d["next_entry_date"].notna()
        & d[f"exit_close_{hold_days}"].notna()
        & d["next_open"].gt(0)
    )
    if strict:
        mask &= (
            (d["wave_style_score"] >= float(args.strict_min_score))
            & (d["amount_rank"] >= 0.80)
            & (d["ret20"].fillna(0) < 0.55)
            & (d["limit_up_days20"].fillna(0) <= 4)
        )
    else:
        mask &= (
            (d["wave_style_score"] >= float(args.min_score))
            & (d["amount_rank"] >= 0.70)
            & (d["ret20"].fillna(0) < 0.75)
            & (d["limit_up_days20"].fillna(0) <= 4)
        )
    if market_gate == "index_trend":
        mask &= (
            (pd.to_numeric(d.get("index_close"), errors="coerce") >= pd.to_numeric(d.get("index_ma20"), errors="coerce"))
            & (pd.to_numeric(d.get("index_mom20"), errors="coerce") >= 0.0)
        )
    elif market_gate == "index_bigwave":
        mask &= (
            (pd.to_numeric(d.get("index_close"), errors="coerce") >= pd.to_numeric(d.get("index_ma20"), errors="coerce"))
            & (pd.to_numeric(d.get("index_ma20"), errors="coerce") >= pd.to_numeric(d.get("index_ma60"), errors="coerce") * 0.995)
            & (pd.to_numeric(d.get("index_mom20"), errors="coerce") >= 0.01)
        )
    elif market_gate == "none":
        pass
    else:
        raise ValueError(f"unknown market_gate={market_gate}")
    out = d[mask].copy()
    out["entry_date"] = pd.to_datetime(out["next_entry_date"]).dt.normalize()
    out["entry_price"] = pd.to_numeric(out["next_open"], errors="coerce")
    out["policy_exit_date"] = pd.to_datetime(out[f"exit_date_{hold_days}"]).dt.normalize()
    out["exit_price"] = pd.to_numeric(out[f"exit_close_{hold_days}"], errors="coerce")
    out["gross_ret"] = pd.to_numeric(out[f"gross_ret_nextopen_h{hold_days}"], errors="coerce")
    out["net_ret"] = out["gross_ret"] - float(args.cost_bps) / 10000.0
    out["hold_days"] = int(hold_days)
    out["rank_key"] = (
        pd.to_numeric(out["wave_style_score"], errors="coerce").fillna(0.0)
        + pd.to_numeric(out.get("learned_sector_bonus", 0.0), errors="coerce").fillna(0.0) * 8.0
        + pd.to_numeric(out["ret5"], errors="coerce").fillna(0.0) * 10.0
    )
    return out.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False]).reset_index(drop=True)


def _simulate(candidates: pd.DataFrame, slots: int, slot_pct: float, daily_open_limit: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    start = candidates["entry_date"].min()
    end = candidates["policy_exit_date"].max()
    calendar = _trade_calendar(start, end)
    by_entry = {day: g.copy() for day, g in candidates.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    for day in calendar:
        realized_pnl = 0.0
        still_open: list[dict[str, Any]] = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized_pnl += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open

        opened = 0
        skipped_slots = 0
        skipped_daily_limit = 0
        todays = by_entry.get(day)
        if todays is not None:
            for row in todays.itertuples(index=False):
                if opened >= daily_open_limit:
                    skipped_daily_limit += 1
                    continue
                if len(open_pos) >= slots:
                    skipped_slots += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * float(slot_pct)
                if stake <= 0 or cash < stake:
                    skipped_slots += 1
                    continue
                pos = row._asdict()
                pos["stake"] = stake
                cash -= stake
                open_pos.append(pos)
                opened += 1

        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped_slots": skipped_slots,
                "skipped_daily_limit": skipped_daily_limit,
                "realized_pnl": realized_pnl,
            }
        )
    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def _max_drawdown(s: pd.Series) -> float:
    if s.empty:
        return 0.0
    return float((s / s.cummax() - 1.0).min())


def _window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, variant: str, window: str, start: str, end: str) -> dict[str, Any]:
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    iw = index_df[(index_df["trade_date"] >= pd.Timestamp(start)) & (index_df["trade_date"] <= pd.Timestamp(end))].copy()
    if cw.empty:
        return {"variant": variant, "window": window, "closed": 0}
    strat_ret = float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0)
    index_ret = float(iw["close"].iloc[-1] / iw["close"].iloc[0] - 1.0) if len(iw) >= 2 else np.nan
    net = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "variant": variant,
        "window": window,
        "closed": int(len(tw)),
        "unique_codes": int(tw["code_raw"].nunique()) if not tw.empty else 0,
        "strategy_ret": strat_ret,
        "index_ret": index_ret,
        "excess_ret": strat_ret - index_ret if pd.notna(index_ret) else np.nan,
        "max_drawdown": _max_drawdown(cw["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "mean_trade_ret": float(net.mean()) if len(net) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "avg_open_positions": float(cw["open_positions"].mean()),
        "max_open_positions": int(cw["open_positions"].max()),
    }


def _annual_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, variant: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    years = sorted(pd.to_datetime(curve["date"]).dt.year.dropna().unique().tolist()) if not curve.empty else []
    for year in years:
        rows.append(_window_metrics(curve, closed, index_df, variant, str(int(year)), f"{int(year)}-01-01", f"{int(year)}-12-31"))
    return pd.DataFrame(rows)


def _route_summary(closed: pd.DataFrame, variant: str) -> pd.DataFrame:
    if closed.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for label, g in closed.groupby("template_label", dropna=False):
        ret = pd.to_numeric(g["net_ret"], errors="coerce")
        rows.append(
            {
                "variant": variant,
                "template_label": str(label),
                "closed": int(len(g)),
                "avg_ret": float(ret.mean()),
                "win_rate": float((ret > 0).mean()),
                "pnl": float(pd.to_numeric(g["realized_pnl"], errors="coerce").sum()) if "realized_pnl" in g.columns else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values("pnl", ascending=False)


def _md_table(df: pd.DataFrame, pct_cols: set[str] | None = None, limit: int | None = None) -> str:
    if df.empty:
        return "_无数据_"
    d = df.head(limit).copy() if limit else df.copy()
    pct_cols = pct_cols or set()
    rows: list[dict[str, Any]] = []
    for _, row in d.iterrows():
        item: dict[str, Any] = {}
        for col in d.columns:
            v = row[col]
            if col in pct_cols:
                item[col] = _pct(v)
            elif isinstance(v, float):
                item[col] = f"{v:.4f}"
            else:
                item[col] = "" if pd.isna(v) else str(v)
        rows.append(item)
    return pd.DataFrame(rows).to_markdown(index=False)


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    holds = [int(x.strip()) for x in str(args.hold_days).split(",") if x.strip()]
    max_hold = max(holds)
    load_start = (pd.Timestamp(args.start_date) - pd.Timedelta(days=300)).strftime("%Y-%m-%d")
    load_end = (pd.Timestamp(args.end_date) + pd.Timedelta(days=max_hold * 3 + 20)).strftime("%Y-%m-%d")

    stocks = _load_stocks()
    raw = _load_daily(load_start, load_end)
    raw = raw.merge(stocks[["code6", "stock_name", "industry"]], on="code6", how="inner")
    feat = _attach_sector(_add_features(raw, max_hold=max_hold))
    index_df = _load_index(args.start_date, args.end_date)

    variants: list[tuple[str, bool, bool, str]] = []
    for hold in holds:
        variants.append((f"shape_only_h{hold}", False, False, "none"))
        variants.append((f"shape_only_strict_h{hold}", False, True, "none"))
        variants.append((f"learned_sector_h{hold}", True, False, "none"))
        variants.append((f"learned_sector_strict_h{hold}", True, True, "none"))
        variants.append((f"learned_sector_trend_h{hold}", True, False, "index_trend"))
        variants.append((f"learned_sector_bigwave_h{hold}", True, False, "index_bigwave"))
        variants.append((f"learned_sector_bigwave_strict_h{hold}", True, True, "index_bigwave"))

    summary_rows: list[dict[str, Any]] = []
    annual_frames: list[pd.DataFrame] = []
    route_frames: list[pd.DataFrame] = []
    candidate_meta: list[dict[str, Any]] = []
    windows = {
        "full": (args.start_date, args.end_date),
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "blind_2026ytd": ("2026-01-01", args.end_date),
        "post_2024_09": ("2024-09-24", args.end_date),
    }

    index_features = _load_index_features(load_start, load_end)
    if not index_features.empty:
        feat = feat.merge(index_features, on="trade_date", how="left")

    for variant, use_learned, strict, market_gate in variants:
        hold = int(variant.rsplit("h", 1)[-1])
        candidates = _build_signal_candidates(feat, args, hold, use_learned_sector=use_learned, strict=strict, market_gate=market_gate)
        candidate_meta.append(
            {
                "variant": variant,
                "hold_days": hold,
                "use_learned_sector": use_learned,
                "strict": strict,
                "market_gate": market_gate,
                "candidate_rows": int(len(candidates)),
                "candidate_days": int(candidates["entry_date"].nunique()) if not candidates.empty else 0,
            }
        )
        run_dir = OUT_DIR / variant
        run_dir.mkdir(parents=True, exist_ok=True)
        candidates.to_csv(run_dir / "candidates.csv", index=False, encoding="utf-8-sig")
        curve, closed = _simulate(candidates, slots=int(args.slots), slot_pct=float(args.slot_pct), daily_open_limit=int(args.daily_open_limit))
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        for window, (start, end) in windows.items():
            summary_rows.append(_window_metrics(curve, closed, index_df, variant, window, start, end))
        annual_frames.append(_annual_metrics(curve, closed, index_df, variant))
        route = _route_summary(closed, variant)
        if not route.empty:
            route_frames.append(route)

    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    route_summary = pd.concat(route_frames, ignore_index=True) if route_frames else pd.DataFrame()
    meta = pd.DataFrame(candidate_meta)
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")
    route_summary.to_csv(OUT_DIR / "route_summary.csv", index=False, encoding="utf-8-sig")
    meta.to_csv(OUT_DIR / "candidate_meta.csv", index=False, encoding="utf-8-sig")

    full_focus = summary[summary["window"].eq("full")].sort_values("excess_ret", ascending=False)
    annual_focus = annual[annual["variant"].isin(full_focus.head(4)["variant"].tolist())].sort_values(["variant", "window"])
    pct_cols = {"strategy_ret", "index_ret", "excess_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "avg_ret"}
    lines = [
        "# 波段赢家模式交易策略历史回测 v1",
        "",
        "## 回测定义",
        "",
        f"- 区间：{args.start_date} 至 {args.end_date}",
        "- 信号：T 日收盘形态可见，T+1 开盘买入，固定持有 5/10/20 日后收盘卖出。",
        f"- 成本：单笔扣 {float(args.cost_bps):.0f} bps。",
        f"- 仓位：{int(args.slots)} 槽，每槽 {float(args.slot_pct):.0%}，每日最多开 {int(args.daily_open_limit)} 笔。",
        "- `shape_only` 不使用上一波行业学习；`learned_sector` 使用 2024-09 至 2025-03 机构赢家行业作研究加分，属于带样本学习口径。",
        "",
        "## Full 结果排序",
        "",
        _md_table(full_focus, pct_cols=pct_cols),
        "",
        "## 候选规模",
        "",
        _md_table(meta),
        "",
        "## 前四名分年表现",
        "",
        _md_table(annual_focus, pct_cols=pct_cols),
        "",
        "## 模板贡献",
        "",
        _md_table(route_summary, pct_cols={"avg_ret", "win_rate"}, limit=80),
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")

    result = {
        "status": "completed",
        "start_date": args.start_date,
        "end_date": args.end_date,
        "hold_days": holds,
        "cost_bps": float(args.cost_bps),
        "slots": int(args.slots),
        "slot_pct": float(args.slot_pct),
        "daily_rows": int(len(raw)),
        "feature_rows": int(len(feat)),
        "out_dir": str(OUT_DIR),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest wave-style template strategy.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    parser.add_argument("--hold-days", default="5,10,20")
    parser.add_argument("--cost-bps", type=float, default=30.0)
    parser.add_argument("--slots", type=int, default=5)
    parser.add_argument("--slot-pct", type=float, default=0.20)
    parser.add_argument("--daily-open-limit", type=int, default=1)
    parser.add_argument("--min-score", type=float, default=88.0)
    parser.add_argument("--strict-min-score", type=float, default=100.0)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

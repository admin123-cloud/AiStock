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

from scripts.backtest_wave_style_template_strategy_v1 import (  # noqa: E402
    INITIAL_CAPITAL,
    _load_index,
    _max_drawdown,
    _md_table,
    _pct,
    _trade_calendar,
)
from utils.paths import report_path  # noqa: E402


SOURCE_DIR = report_path("wave_style_template_strategy_backtest_v1")
OUT_DIR = report_path("wave_style_model_scheduler_v1")


def _safe_float(value: Any, digits: int = 6) -> Any:
    try:
        x = float(value)
    except Exception:
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, digits)


def _load_model_trades(source_dir: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for path in sorted(source_dir.glob("*/closed_trades.csv")):
        variant = path.parent.name
        df = pd.read_csv(path, encoding="utf-8-sig")
        if df.empty:
            continue
        keep = [
            "code",
            "code6",
            "code_raw",
            "stock_name",
            "l2_sector_name",
            "template_label",
            "trade_date",
            "entry_date",
            "entry_price",
            "policy_exit_date",
            "exit_price",
            "gross_ret",
            "net_ret",
            "hold_days",
            "rank_key",
            "wave_style_score",
            "amount_rank",
            "index_close",
            "index_ma20",
            "index_ma60",
            "index_mom20",
            "index_mom60",
        ]
        cols = [c for c in keep if c in df.columns]
        df = df[cols].copy()
        df["variant"] = variant
        frames.append(df)
    if not frames:
        raise RuntimeError(f"no closed_trades.csv found under {source_dir}")
    out = pd.concat(frames, ignore_index=True)
    out["entry_date"] = pd.to_datetime(out["entry_date"], errors="coerce").dt.normalize()
    out["policy_exit_date"] = pd.to_datetime(out["policy_exit_date"], errors="coerce").dt.normalize()
    out["net_ret"] = pd.to_numeric(out["net_ret"], errors="coerce")
    out["rank_key"] = pd.to_numeric(out.get("rank_key"), errors="coerce").fillna(0.0)
    out["amount_rank"] = pd.to_numeric(out.get("amount_rank"), errors="coerce").fillna(0.0)
    out = out.dropna(subset=["entry_date", "policy_exit_date", "net_ret"])
    out = out.sort_values(["entry_date", "variant", "rank_key", "amount_rank"], ascending=[True, True, False, False])
    return out.reset_index(drop=True)


def _score_variants(
    all_trades: pd.DataFrame,
    day: pd.Timestamp,
    lookback_days: int,
    min_trades: int,
    allow_variants: set[str] | None,
    min_win_rate: float | None = None,
    min_mean_ret: float | None = None,
    min_worst_ret: float | None = None,
    max_bad_loss_rate: float | None = None,
) -> pd.DataFrame:
    start = day - pd.Timedelta(days=int(lookback_days))
    hist = all_trades[(all_trades["policy_exit_date"] < day) & (all_trades["policy_exit_date"] >= start)].copy()
    if allow_variants is not None:
        hist = hist[hist["variant"].isin(allow_variants)]
    if hist.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for variant, g in hist.groupby("variant"):
        ret = pd.to_numeric(g["net_ret"], errors="coerce").dropna()
        if len(ret) < min_trades:
            continue
        total_ret = float(np.prod(1.0 + ret.clip(lower=-0.95)) - 1.0)
        mean_ret = float(ret.mean())
        win_rate = float((ret > 0).mean())
        worst_ret = float(ret.min())
        bad_loss_rate = float((ret <= -0.12).mean())
        if min_win_rate is not None and win_rate < float(min_win_rate):
            continue
        if min_mean_ret is not None and mean_ret < float(min_mean_ret):
            continue
        if min_worst_ret is not None and worst_ret < float(min_worst_ret):
            continue
        if max_bad_loss_rate is not None and bad_loss_rate > float(max_bad_loss_rate):
            continue
        recent_count = int(len(ret))
        stability = min(recent_count / max(float(min_trades * 3), 1.0), 1.0)
        score = total_ret * 0.55 + mean_ret * 4.0 + (win_rate - 0.5) * 0.35 + stability * 0.15 + min(worst_ret, 0.0) * 0.25
        rows.append(
            {
                "variant": variant,
                "score": score,
                "lookback_trades": recent_count,
                "lookback_total_ret": total_ret,
                "lookback_mean_ret": mean_ret,
                "lookback_win_rate": win_rate,
                "lookback_worst_ret": worst_ret,
                "lookback_bad_loss_rate": bad_loss_rate,
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)


def _market_gate_mask(df: pd.DataFrame, gate: str) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=bool)
    if gate == "none":
        return pd.Series(True, index=df.index)
    close = pd.to_numeric(df.get("index_close"), errors="coerce")
    ma20 = pd.to_numeric(df.get("index_ma20"), errors="coerce")
    ma60 = pd.to_numeric(df.get("index_ma60"), errors="coerce")
    mom20 = pd.to_numeric(df.get("index_mom20"), errors="coerce")
    if gate == "index_trend":
        return (close >= ma20) & (mom20 >= 0.0)
    if gate == "index_bigwave":
        return (close >= ma20) & (ma20 >= ma60 * 0.995) & (mom20 >= 0.01)
    raise ValueError(f"unknown execution_market_gate={gate}")


def _passes_recent_trade_cooldown(
    all_trades: pd.DataFrame,
    day: pd.Timestamp,
    variant: str,
    recent_trades: int,
    min_sum_ret: float,
    min_mean_ret: float,
) -> tuple[bool, dict[str, float]]:
    if recent_trades <= 0:
        return True, {"cooldown_count": 0.0, "cooldown_sum_ret": 0.0, "cooldown_mean_ret": 0.0}
    hist = all_trades[(all_trades["variant"].eq(variant)) & (all_trades["policy_exit_date"] < day)].copy()
    hist = hist.sort_values("policy_exit_date").tail(int(recent_trades))
    ret = pd.to_numeric(hist.get("net_ret", pd.Series(dtype=float)), errors="coerce").dropna()
    if len(ret) < recent_trades:
        return False, {"cooldown_count": float(len(ret)), "cooldown_sum_ret": float(ret.sum()) if len(ret) else 0.0, "cooldown_mean_ret": float(ret.mean()) if len(ret) else 0.0}
    sum_ret = float(ret.sum())
    mean_ret = float(ret.mean())
    ok = (sum_ret >= float(min_sum_ret)) and (mean_ret >= float(min_mean_ret))
    return ok, {"cooldown_count": float(len(ret)), "cooldown_sum_ret": sum_ret, "cooldown_mean_ret": mean_ret}


def _simulate_scheduler(
    all_trades: pd.DataFrame,
    calendar: list[pd.Timestamp],
    *,
    scheduler_name: str,
    lookback_days: int,
    min_trades: int,
    score_threshold: float,
    require_positive_total: bool,
    allow_pattern: str,
    execution_market_gate: str,
    cooldown_recent_trades: int,
    cooldown_min_sum_ret: float,
    cooldown_min_mean_ret: float,
    score_min_win_rate: float | None,
    score_min_mean_ret: float | None,
    score_min_worst_ret: float | None,
    score_max_bad_loss_rate: float | None,
    slots: int,
    slot_pct: float,
    daily_open_limit: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if allow_pattern == "learned_only":
        allow_variants = {v for v in all_trades["variant"].unique() if v.startswith("learned_sector")}
    elif allow_pattern == "trend_or_mainwave":
        allow_variants = {
            v
            for v in all_trades["variant"].unique()
            if v in {"learned_sector_h20", "learned_sector_strict_h20", "learned_sector_trend_h10", "learned_sector_trend_h20"}
        }
    elif allow_pattern == "all":
        allow_variants = None
    else:
        raise ValueError(f"unknown allow_pattern={allow_pattern}")

    by_entry = {day: g.copy() for day, g in all_trades.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []

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

        scores = _score_variants(
            all_trades,
            day,
            lookback_days,
            min_trades,
            allow_variants,
            min_win_rate=score_min_win_rate,
            min_mean_ret=score_min_mean_ret,
            min_worst_ret=score_min_worst_ret,
            max_bad_loss_rate=score_max_bad_loss_rate,
        )
        selected = ""
        selected_score = np.nan
        selected_total = np.nan
        selected_mean = np.nan
        selected_win = np.nan
        selected_cooldown_sum = np.nan
        selected_cooldown_mean = np.nan
        if not scores.empty:
            for _, top in scores.iterrows():
                pass_score = float(top["score"]) >= float(score_threshold)
                pass_total = (not require_positive_total) or float(top["lookback_total_ret"]) > 0.0
                cooldown_ok, cooldown_meta = _passes_recent_trade_cooldown(
                    all_trades,
                    day,
                    str(top["variant"]),
                    cooldown_recent_trades,
                    cooldown_min_sum_ret,
                    cooldown_min_mean_ret,
                )
                if pass_score and pass_total and cooldown_ok:
                    selected = str(top["variant"])
                    selected_score = float(top["score"])
                    selected_total = float(top["lookback_total_ret"])
                    selected_mean = float(top["lookback_mean_ret"])
                    selected_win = float(top["lookback_win_rate"])
                    selected_cooldown_sum = float(cooldown_meta["cooldown_sum_ret"])
                    selected_cooldown_mean = float(cooldown_meta["cooldown_mean_ret"])
                    break

        opened = 0
        skipped_slots = 0
        skipped_daily_limit = 0
        if selected:
            todays = by_entry.get(day)
            if todays is not None:
                todays = todays[todays["variant"].eq(selected)].copy()
                todays = todays[_market_gate_mask(todays, execution_market_gate)].copy()
                todays = todays.sort_values(["rank_key", "amount_rank"], ascending=[False, False])
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
                    pos["scheduler"] = scheduler_name
                    pos["selected_variant"] = selected
                    pos["selected_score"] = selected_score
                    pos["selected_total"] = selected_total
                    pos["selected_mean"] = selected_mean
                    pos["selected_win_rate"] = selected_win
                    pos["selected_cooldown_sum_ret"] = selected_cooldown_sum
                    pos["selected_cooldown_mean_ret"] = selected_cooldown_mean
                    pos["stake"] = stake
                    cash -= stake
                    open_pos.append(pos)
                    opened += 1

        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day,
                "scheduler": scheduler_name,
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "skipped_slots": skipped_slots,
                "skipped_daily_limit": skipped_daily_limit,
                "realized_pnl": realized_pnl,
                "selected_variant": selected,
                "selected_score": selected_score,
                "selected_total": selected_total,
                "selected_mean": selected_mean,
                "selected_win_rate": selected_win,
                "selected_cooldown_sum_ret": selected_cooldown_sum,
                "selected_cooldown_mean_ret": selected_cooldown_mean,
            }
        )
        decision_rows.append(
            {
                "date": day,
                "scheduler": scheduler_name,
                "selected_variant": selected,
                "selected_score": selected_score,
                "selected_total": selected_total,
                "selected_mean": selected_mean,
                "selected_win_rate": selected_win,
                "selected_cooldown_sum_ret": selected_cooldown_sum,
                "selected_cooldown_mean_ret": selected_cooldown_mean,
                "score_rows": int(len(scores)),
            }
        )

    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    decisions = pd.DataFrame(decision_rows)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df, decisions


def _window_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, name: str, window: str, start: str, end: str) -> dict[str, Any]:
    cw = curve[(curve["date"] >= pd.Timestamp(start)) & (curve["date"] <= pd.Timestamp(end))].copy()
    tw = closed[(closed["entry_date"] >= pd.Timestamp(start)) & (closed["entry_date"] <= pd.Timestamp(end))].copy() if not closed.empty else pd.DataFrame()
    iw = index_df[(index_df["trade_date"] >= pd.Timestamp(start)) & (index_df["trade_date"] <= pd.Timestamp(end))].copy()
    if cw.empty:
        return {"model": name, "window": window, "closed": 0}
    strat_ret = float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0)
    index_ret = float(iw["close"].iloc[-1] / iw["close"].iloc[0] - 1.0) if len(iw) >= 2 else np.nan
    net = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    return {
        "model": name,
        "window": window,
        "closed": int(len(tw)),
        "unique_codes": int(tw["code_raw"].nunique()) if (not tw.empty and "code_raw" in tw.columns) else 0,
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


def _annual_metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, name: str) -> pd.DataFrame:
    years = sorted(pd.to_datetime(curve["date"]).dt.year.dropna().unique().tolist()) if not curve.empty else []
    rows = [_window_metrics(curve, closed, index_df, name, str(int(y)), f"{int(y)}-01-01", f"{int(y)}-12-31") for y in years]
    return pd.DataFrame(rows)


def _selection_summary(decisions: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    if decisions.empty:
        return pd.DataFrame()
    active = decisions[decisions["selected_variant"].astype(str).ne("")].copy()
    rows: list[dict[str, Any]] = []
    for variant, g in active.groupby("selected_variant"):
        trades = closed[closed["selected_variant"].eq(variant)] if not closed.empty and "selected_variant" in closed.columns else pd.DataFrame()
        ret = pd.to_numeric(trades.get("net_ret", pd.Series(dtype=float)), errors="coerce")
        rows.append(
            {
                "selected_variant": variant,
                "selected_days": int(len(g)),
                "closed": int(len(trades)),
                "avg_ret": float(ret.mean()) if len(ret) else 0.0,
                "win_rate": float((ret > 0).mean()) if len(ret) else 0.0,
                "pnl": float(pd.to_numeric(trades.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()) if len(trades) else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values("pnl", ascending=False)


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_trades = _load_model_trades(SOURCE_DIR)
    start = pd.Timestamp(args.start_date)
    end = pd.Timestamp(args.end_date)
    all_trades = all_trades[(all_trades["entry_date"] >= start) & (all_trades["entry_date"] <= end)].copy()
    calendar = _trade_calendar(start, all_trades["policy_exit_date"].max())
    index_df = _load_index(args.start_date, args.end_date)

    schedulers = [
        {
            "name": "scheduler_offensive_120d",
            "lookback_days": 120,
            "min_trades": 5,
            "score_threshold": 0.0,
            "require_positive_total": True,
            "allow_pattern": "learned_only",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_balanced_240d",
            "lookback_days": 240,
            "min_trades": 8,
            "score_threshold": 0.03,
            "require_positive_total": True,
            "allow_pattern": "learned_only",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_balanced_240d_index_trend",
            "lookback_days": 240,
            "min_trades": 8,
            "score_threshold": 0.03,
            "require_positive_total": True,
            "allow_pattern": "learned_only",
            "execution_market_gate": "index_trend",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_focus_240d",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 0.02,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_focus_240d_score075",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 0.75,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_focus_240d_score100",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 1.00,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_focus_240d_score100_aggr25",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 1.00,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
            "slot_pct": 0.25,
        },
        {
            "name": "scheduler_focus_240d_score120_aggr25",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 1.20,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
            "slot_pct": 0.25,
        },
        {
            "name": "scheduler_focus_240d_score120_aggr25_win52",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 1.20,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
            "slot_pct": 0.25,
            "score_min_win_rate": 0.52,
        },
        {
            "name": "scheduler_focus_240d_score120_aggr25_mean10",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 1.20,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
            "slot_pct": 0.25,
            "score_min_mean_ret": 0.10,
        },
        {
            "name": "scheduler_focus_240d_score120_aggr25_loss25",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 1.20,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
            "slot_pct": 0.25,
            "score_max_bad_loss_rate": 0.25,
        },
        {
            "name": "scheduler_focus_240d_score120_aggr25_win52_mean08",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 1.20,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
            "slot_pct": 0.25,
            "score_min_win_rate": 0.52,
            "score_min_mean_ret": 0.08,
        },
        {
            "name": "scheduler_focus_240d_cool5",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 0.02,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 5,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_focus_240d_cool8",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 0.02,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 8,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_focus_240d_index_trend",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 0.02,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "index_trend",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_focus_240d_index_bigwave",
            "lookback_days": 240,
            "min_trades": 6,
            "score_threshold": 0.02,
            "require_positive_total": True,
            "allow_pattern": "trend_or_mainwave",
            "execution_market_gate": "index_bigwave",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
        {
            "name": "scheduler_allmodels_180d",
            "lookback_days": 180,
            "min_trades": 8,
            "score_threshold": 0.02,
            "require_positive_total": True,
            "allow_pattern": "all",
            "execution_market_gate": "none",
            "cooldown_recent_trades": 0,
            "cooldown_min_sum_ret": 0.0,
            "cooldown_min_mean_ret": 0.0,
        },
    ]

    windows = {
        "full": (args.start_date, args.end_date),
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "post_2024_09": ("2024-09-24", args.end_date),
        "blind_2026ytd": ("2026-01-01", args.end_date),
    }

    summary_rows: list[dict[str, Any]] = []
    annual_frames: list[pd.DataFrame] = []
    selection_frames: list[pd.DataFrame] = []

    for cfg in schedulers:
        name = cfg["name"]
        run_dir = OUT_DIR / name
        run_dir.mkdir(parents=True, exist_ok=True)
        curve, closed, decisions = _simulate_scheduler(
            all_trades,
            calendar,
            scheduler_name=name,
            lookback_days=int(cfg["lookback_days"]),
            min_trades=int(cfg["min_trades"]),
            score_threshold=float(cfg["score_threshold"]),
            require_positive_total=bool(cfg["require_positive_total"]),
            allow_pattern=str(cfg["allow_pattern"]),
            execution_market_gate=str(cfg["execution_market_gate"]),
            cooldown_recent_trades=int(cfg["cooldown_recent_trades"]),
            cooldown_min_sum_ret=float(cfg["cooldown_min_sum_ret"]),
            cooldown_min_mean_ret=float(cfg["cooldown_min_mean_ret"]),
            score_min_win_rate=cfg.get("score_min_win_rate"),
            score_min_mean_ret=cfg.get("score_min_mean_ret"),
            score_min_worst_ret=cfg.get("score_min_worst_ret"),
            score_max_bad_loss_rate=cfg.get("score_max_bad_loss_rate"),
            slots=int(args.slots),
            slot_pct=float(cfg.get("slot_pct", args.slot_pct)),
            daily_open_limit=int(args.daily_open_limit),
        )
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        decisions.to_csv(run_dir / "daily_decisions.csv", index=False, encoding="utf-8-sig")
        for window, (w_start, w_end) in windows.items():
            summary_rows.append(_window_metrics(curve, closed, index_df, name, window, w_start, w_end))
        annual_frames.append(_annual_metrics(curve, closed, index_df, name))
        sel = _selection_summary(decisions, closed)
        if not sel.empty:
            sel.insert(0, "scheduler", name)
            selection_frames.append(sel)

    summary = pd.DataFrame(summary_rows)
    annual = pd.concat(annual_frames, ignore_index=True) if annual_frames else pd.DataFrame()
    selection = pd.concat(selection_frames, ignore_index=True) if selection_frames else pd.DataFrame()
    summary.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "annual_summary.csv", index=False, encoding="utf-8-sig")
    selection.to_csv(OUT_DIR / "selection_summary.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(schedulers).to_csv(OUT_DIR / "scheduler_config.csv", index=False, encoding="utf-8-sig")

    pct_cols = {"strategy_ret", "index_ret", "excess_ret", "max_drawdown", "win_rate", "mean_trade_ret", "worst_trade", "avg_ret"}
    full = summary[summary["window"].eq("full")].sort_values("excess_ret", ascending=False)
    post = summary[summary["window"].eq("post_2024_09")].sort_values("excess_ret", ascending=False)
    blind = summary[summary["window"].eq("blind_2026ytd")].sort_values("excess_ret", ascending=False)
    best_names = full.head(2)["model"].tolist()
    annual_focus = annual[annual["model"].isin(best_names)].sort_values(["model", "window"]) if not annual.empty else pd.DataFrame()

    lines = [
        "# 波段赢家模型调度器回测 v1",
        "",
        "## 定义",
        "",
        f"- 调度区间：{args.start_date} 至 {args.end_date}",
        "- 输入：上一层 21 个波段赢家模板模型的已开仓交易明细。",
        "- 调度：每日只使用该日前已经退出的模型交易作为学习样本，按近 120/180/240 日收益、胜率、均值和稳定性选择一个模型。",
        "- 执行：当天只执行被选中模型当天已经给出的候选交易；可叠加指数趋势/大波段门控；未达到正收益门槛时空仓。",
        f"- 仓位：{int(args.slots)} 槽，每槽 {float(args.slot_pct):.0%}，每日最多开 {int(args.daily_open_limit)} 笔。",
        "",
        "## 全周期结果",
        "",
        _md_table(full, pct_cols=pct_cols),
        "",
        "## 2024-09 后结果",
        "",
        _md_table(post, pct_cols=pct_cols),
        "",
        "## 2026 样本外窗口",
        "",
        _md_table(blind, pct_cols=pct_cols),
        "",
        "## 最优调度器分年",
        "",
        _md_table(annual_focus, pct_cols=pct_cols),
        "",
        "## 模型选择贡献",
        "",
        _md_table(selection, pct_cols={"avg_ret", "win_rate"}, limit=80),
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8")

    result = {
        "status": "completed",
        "source_dir": str(SOURCE_DIR),
        "out_dir": str(OUT_DIR),
        "trade_rows": int(len(all_trades)),
        "schedulers": [x["name"] for x in schedulers],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest rolling scheduler over wave-style template models.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    parser.add_argument("--slots", type=int, default=5)
    parser.add_argument("--slot-pct", type=float, default=0.20)
    parser.add_argument("--daily-open-limit", type=int, default=1)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

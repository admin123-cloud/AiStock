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

from scripts.backtest_g3_five_strategies_from_scratch_v1 import _md_table  # noqa: E402
from scripts.backtest_wave_style_template_strategy_v1 import INITIAL_CAPITAL, _load_index, _max_drawdown, _trade_calendar  # noqa: E402
from scripts.gen3_backtest_strong_volume5_confirm_d3_execution_stress_v1 import _ret_from_price, _sql_literal  # noqa: E402
from utils.market_warehouse import clickhouse_query_df  # noqa: E402
from utils.paths import report_path  # noqa: E402


SOURCE = report_path("score120_mom60_gate_revalidation_v1", "score120_diff65_m30_ma20_source_signals.csv")
OUT_DIR = report_path("score120_mainwave_risk_contract_variants_v1")

COST_BPS = 30.0
MOM60_LIMIT = 0.05
M30_WEAK_RET = -0.03
M30_CUTOFF = "10:30:00"


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value) if math.isfinite(float(value)) else None
    if pd.isna(value):
        return None
    return str(value)


def _load_signals(start: str, end: str) -> pd.DataFrame:
    if not SOURCE.exists():
        raise FileNotFoundError(f"missing source: {SOURCE}; run audit_score120_mom60_gate_revalidation_v1.py first")
    d = pd.read_csv(SOURCE, encoding="utf-8-sig")
    for col in ["trade_date", "entry_date", "policy_exit_date"]:
        d[col] = pd.to_datetime(d[col], errors="coerce").dt.normalize()
    for col in [
        "entry_price",
        "exit_price",
        "net_ret",
        "rank_key",
        "amount_rank",
        "sector_diffusion_score",
        "m30_close_above_ma20",
        "index_close",
        "index_ma20",
        "index_mom20",
        "index_mom60",
    ]:
        d[col] = pd.to_numeric(d.get(col), errors="coerce")
    d["code_raw"] = d.get("code_raw", d.get("code")).astype(str)
    d = d[(d["entry_date"] >= pd.Timestamp(start)) & (d["entry_date"] <= pd.Timestamp(end))].copy()
    d = d[d["index_mom60"] <= MOM60_LIMIT].copy()
    d = d.dropna(subset=["entry_date", "policy_exit_date", "entry_price", "net_ret", "index_mom60"])
    d["original_policy_exit_date"] = d["policy_exit_date"]
    d["original_net_ret"] = d["net_ret"]
    d["risk_exit_note"] = "base_policy"
    return d.sort_values(["entry_date", "rank_key", "amount_rank"], ascending=[True, False, False]).reset_index(drop=True)


def _next_trade_date_map(calendar: list[pd.Timestamp]) -> dict[pd.Timestamp, pd.Timestamp]:
    return {calendar[i]: calendar[i + 1] for i in range(len(calendar) - 1)}


def _add_d1_d2(signals: pd.DataFrame, calendar: list[pd.Timestamp]) -> pd.DataFrame:
    nxt = _next_trade_date_map(calendar)
    d = signals.copy()
    d["d1_date"] = d["entry_date"].map(nxt)
    d["d2_date"] = d["d1_date"].map(nxt)
    return d


def _load_30m_bars(signals: pd.DataFrame) -> pd.DataFrame:
    pairs: set[tuple[str, str]] = set()
    for row in signals.itertuples(index=False):
        for day in [getattr(row, "d1_date", pd.NaT), getattr(row, "d2_date", pd.NaT)]:
            if pd.notna(day):
                pairs.add((str(row.code_raw), pd.Timestamp(day).strftime("%Y-%m-%d")))
    if not pairs:
        return pd.DataFrame()
    codes = sorted({code for code, _ in pairs})
    dates = sorted({day for _, day in pairs})
    parts: list[pd.DataFrame] = []
    for di in range(0, len(dates), 80):
        date_chunk = dates[di : di + 80]
        date_list = ",".join(f"toDate({_sql_literal(day)})" for day in date_chunk)
        for ci in range(0, len(codes), 250):
            code_chunk = codes[ci : ci + 250]
            quoted = ",".join(_sql_literal(code) for code in code_chunk)
            sql = f"""
            SELECT code, datetime, open, high, low, close, volume, amount
            FROM kline_minute_30
            WHERE code IN ({quoted})
              AND toDate(datetime) IN ({date_list})
            ORDER BY code, datetime
            """
            part = clickhouse_query_df(sql)
            if not part.empty:
                parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trade_date"] = bars["datetime"].dt.normalize()
    bars["time_text"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["code", "datetime", "trade_date", "open", "high", "low", "close"]).sort_values(["code", "datetime"])


def _cutoff_context(g: pd.DataFrame, entry_price: float) -> dict[str, Any]:
    if g.empty or entry_price <= 0:
        return {"status": "missing"}
    usable = g[g["time_text"] <= M30_CUTOFF].sort_values("datetime")
    if usable.empty:
        return {"status": "no_cutoff_bar", "bar_count": int(len(g))}
    bar = usable.iloc[-1]
    return {
        "status": "ok",
        "datetime": pd.Timestamp(bar["datetime"]),
        "close": float(bar["close"]),
        "close_ret": float(bar["close"]) / float(entry_price) - 1.0,
        "low_ret": float(usable["low"].min()) / float(entry_price) - 1.0,
    }


def _apply_m30_weak_exit(signals: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    d = signals.copy()
    d["m30_exit_datetime"] = pd.NaT
    d["m30_exit_ret"] = pd.NA
    d["m30_d1_1030_ret"] = pd.NA
    d["m30_d2_1030_ret"] = pd.NA
    by_key = {(str(code), pd.Timestamp(day).normalize()): g.copy() for (code, day), g in bars.groupby(["code", "trade_date"])}
    for idx, row in d.iterrows():
        code = str(row["code_raw"])
        entry_price = float(row["entry_price"])
        old_exit = pd.Timestamp(row["policy_exit_date"]).normalize()
        chosen: dict[str, Any] | None = None
        chosen_label = ""
        for label, date_col in [("d1", "d1_date"), ("d2", "d2_date")]:
            day = row.get(date_col)
            if pd.isna(day):
                continue
            day = pd.Timestamp(day).normalize()
            if day >= old_exit:
                continue
            ctx = _cutoff_context(by_key.get((code, day), pd.DataFrame()), entry_price)
            if label == "d1":
                d.at[idx, "m30_d1_1030_ret"] = ctx.get("close_ret", pd.NA)
            else:
                d.at[idx, "m30_d2_1030_ret"] = ctx.get("close_ret", pd.NA)
            if ctx.get("status") == "ok" and float(ctx["close_ret"]) <= M30_WEAK_RET:
                chosen = ctx
                chosen_label = f"{label}_1030_weak3_exit"
                break
        if not chosen:
            continue
        exit_ret = _ret_from_price(float(chosen["close"]), entry_price, COST_BPS)
        if exit_ret is None:
            continue
        d.at[idx, "policy_exit_date"] = pd.Timestamp(chosen["datetime"]).normalize()
        d.at[idx, "net_ret"] = exit_ret
        d.at[idx, "m30_exit_datetime"] = chosen["datetime"]
        d.at[idx, "m30_exit_ret"] = exit_ret
        d.at[idx, "risk_exit_note"] = chosen_label
    return d


def _apply_m30_persistent_fail_exit(signals: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    d = signals.copy()
    d["m30_exit_datetime"] = pd.NaT
    d["m30_exit_ret"] = pd.NA
    d["m30_d1_1030_ret"] = pd.NA
    d["m30_d2_1030_ret"] = pd.NA
    d["m30_d2_repair"] = False
    by_key = {(str(code), pd.Timestamp(day).normalize()): g.copy() for (code, day), g in bars.groupby(["code", "trade_date"])}
    for idx, row in d.iterrows():
        code = str(row["code_raw"])
        entry_price = float(row["entry_price"])
        old_exit = pd.Timestamp(row["policy_exit_date"]).normalize()
        d1 = row.get("d1_date")
        d2 = row.get("d2_date")
        if pd.isna(d1) or pd.isna(d2):
            continue
        d1 = pd.Timestamp(d1).normalize()
        d2 = pd.Timestamp(d2).normalize()
        if d2 >= old_exit:
            continue
        d1_ctx = _cutoff_context(by_key.get((code, d1), pd.DataFrame()), entry_price)
        d2_ctx = _cutoff_context(by_key.get((code, d2), pd.DataFrame()), entry_price)
        d.at[idx, "m30_d1_1030_ret"] = d1_ctx.get("close_ret", pd.NA)
        d.at[idx, "m30_d2_1030_ret"] = d2_ctx.get("close_ret", pd.NA)
        if d1_ctx.get("status") != "ok" or d2_ctx.get("status") != "ok":
            continue
        if float(d1_ctx["close_ret"]) > M30_WEAK_RET:
            continue
        d1_bars = by_key.get((code, d1), pd.DataFrame())
        d2_bars = by_key.get((code, d2), pd.DataFrame())
        d1_usable = d1_bars[d1_bars["time_text"] <= M30_CUTOFF] if not d1_bars.empty else pd.DataFrame()
        d2_usable = d2_bars[d2_bars["time_text"] <= M30_CUTOFF] if not d2_bars.empty else pd.DataFrame()
        if d1_usable.empty or d2_usable.empty:
            continue
        repaired = float(d2_ctx["close"]) > float(d1_ctx["close"]) or float(d2_usable["high"].max()) > float(d1_usable["high"].max())
        d.at[idx, "m30_d2_repair"] = bool(repaired)
        if repaired:
            continue
        exit_ret = _ret_from_price(float(d2_ctx["close"]), entry_price, COST_BPS)
        if exit_ret is None:
            continue
        d.at[idx, "policy_exit_date"] = pd.Timestamp(d2_ctx["datetime"]).normalize()
        d.at[idx, "net_ret"] = exit_ret
        d.at[idx, "m30_exit_datetime"] = d2_ctx["datetime"]
        d.at[idx, "m30_exit_ret"] = exit_ret
        d.at[idx, "risk_exit_note"] = "d1_weak_d2_no_repair_exit"
    return d


def _simulate(
    signals: pd.DataFrame,
    calendar: list[pd.Timestamp],
    variant: dict[str, Any],
    slots: int,
    daily_open_limit: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    by_entry = {day: g.copy() for day, g in signals.groupby("entry_date")}
    cash = INITIAL_CAPITAL
    open_pos: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    consecutive_losses = 0
    cooldown_until: pd.Timestamp | None = None
    cooldown_min_idx: int | None = None
    cooldown_max_idx: int | None = None
    cooldown_mode = str(variant.get("cooldown_mode") or "fixed")

    def recovery_ok(todays: pd.DataFrame | None) -> bool:
        if todays is None or todays.empty:
            return False
        top = todays.sort_values(["rank_key", "amount_rank"], ascending=[False, False]).iloc[0]
        mom60_ok = float(top.get("index_mom60", np.nan)) <= MOM60_LIMIT
        mom20 = pd.to_numeric(pd.Series([top.get("index_mom20")]), errors="coerce").iloc[0]
        index_close = pd.to_numeric(pd.Series([top.get("index_close")]), errors="coerce").iloc[0]
        index_ma20 = pd.to_numeric(pd.Series([top.get("index_ma20")]), errors="coerce").iloc[0]
        index_repaired = (pd.notna(mom20) and float(mom20) >= 0.0) or (
            pd.notna(index_close) and pd.notna(index_ma20) and float(index_close) >= float(index_ma20)
        )
        diffusion_ok = float(top.get("sector_diffusion_score", np.nan)) >= 65.0
        m30_ok = float(top.get("m30_close_above_ma20", np.nan)) >= 0.0
        return bool(mom60_ok and index_repaired and diffusion_ok and m30_ok)

    for day_idx, day in enumerate(calendar):
        realized = 0.0
        still_open: list[dict[str, Any]] = []
        day_closed: list[dict[str, Any]] = []
        for pos in open_pos:
            if pos["policy_exit_date"] <= day:
                exit_value = float(pos["stake"]) * (1.0 + float(pos["net_ret"]))
                cash += exit_value
                realized += exit_value - float(pos["stake"])
                out = pos.copy()
                out["exit_value"] = exit_value
                out["realized_pnl"] = exit_value - float(pos["stake"])
                out["exit_date_actual"] = day
                closed.append(out)
                day_closed.append(out)
            else:
                still_open.append(pos)
        open_pos = still_open

        if variant.get("cooldown_after_losses"):
            for out in day_closed:
                if float(out["net_ret"]) < 0:
                    consecutive_losses += 1
                else:
                    consecutive_losses = 0
                if consecutive_losses >= int(variant["cooldown_after_losses"]):
                    if cooldown_mode == "dynamic_recovery":
                        cooldown_min_idx = min(day_idx + int(variant.get("min_cooldown_days", 3)), len(calendar) - 1)
                        cooldown_max_idx = min(day_idx + int(variant.get("max_cooldown_days", 15)), len(calendar) - 1)
                        cooldown_until = calendar[cooldown_max_idx]
                    else:
                        until_idx = min(day_idx + int(variant.get("cooldown_days", 0)), len(calendar) - 1)
                        cooldown_until = calendar[until_idx]
                    consecutive_losses = 0

        opened = 0
        skipped_cooldown = 0
        skipped_capacity = 0
        todays = by_entry.get(day)
        released_by_recovery = False
        released_by_max = False
        in_cooldown = cooldown_until is not None and day <= cooldown_until
        if in_cooldown and cooldown_mode == "dynamic_recovery":
            if cooldown_min_idx is not None and day_idx <= cooldown_min_idx:
                in_cooldown = True
            elif cooldown_max_idx is not None and day_idx > cooldown_max_idx:
                in_cooldown = False
                released_by_max = True
                cooldown_until = None
                cooldown_min_idx = None
                cooldown_max_idx = None
            elif recovery_ok(todays):
                in_cooldown = False
                released_by_recovery = True
                cooldown_until = None
                cooldown_min_idx = None
                cooldown_max_idx = None
            else:
                in_cooldown = True
        if todays is not None:
            todays = todays.sort_values(["rank_key", "amount_rank"], ascending=[False, False])
            for _, row in todays.iterrows():
                if in_cooldown:
                    skipped_cooldown += 1
                    continue
                if opened >= daily_open_limit or len(open_pos) >= slots:
                    skipped_capacity += 1
                    continue
                equity_before = cash + sum(float(p["stake"]) for p in open_pos)
                stake = equity_before * float(variant["slot_pct"])
                if stake <= 0 or cash < stake:
                    skipped_capacity += 1
                    continue
                pos = row.to_dict()
                pos["variant"] = variant["variant"]
                pos["slot_pct"] = float(variant["slot_pct"])
                pos["stake"] = stake
                pos["cooldown_enabled"] = bool(variant.get("cooldown_after_losses"))
                cash -= stake
                open_pos.append(pos)
                opened += 1

        reserved = sum(float(p["stake"]) for p in open_pos)
        equity = cash + reserved
        curve_rows.append(
            {
                "date": day,
                "variant": variant["variant"],
                "cash": cash,
                "reserved_principal": reserved,
                "equity": equity,
                "open_positions": len(open_pos),
                "opened": opened,
                "realized_pnl": realized,
                "in_cooldown": bool(in_cooldown),
                "cooldown_until": cooldown_until,
                "cooldown_mode": cooldown_mode,
                "released_by_recovery": bool(released_by_recovery),
                "released_by_max": bool(released_by_max),
                "skipped_cooldown": skipped_cooldown,
                "skipped_capacity": skipped_capacity,
            }
        )
    curve = pd.DataFrame(curve_rows)
    closed_df = pd.DataFrame(closed)
    if not curve.empty:
        curve["peak"] = curve["equity"].cummax()
        curve["drawdown"] = curve["equity"] / curve["peak"] - 1.0
        curve["ret_from_start"] = curve["equity"] / INITIAL_CAPITAL - 1.0
    return curve, closed_df


def _metrics(curve: pd.DataFrame, closed: pd.DataFrame, index_df: pd.DataFrame, variant: str, window: str, start: str, end: str) -> dict[str, Any]:
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    cw = curve[(curve["date"] >= start_ts) & (curve["date"] <= end_ts)].copy()
    tw = closed[(closed["entry_date"] >= start_ts) & (closed["entry_date"] <= end_ts)].copy() if not closed.empty else pd.DataFrame()
    iw = index_df[(index_df["trade_date"] >= start_ts) & (index_df["trade_date"] <= end_ts)].copy()
    if cw.empty:
        return {"variant": variant, "window": window, "closed": 0}
    net = pd.to_numeric(tw.get("net_ret", pd.Series(dtype=float)), errors="coerce")
    original = pd.to_numeric(tw.get("original_net_ret", pd.Series(dtype=float)), errors="coerce")
    index_ret = float(iw["close"].iloc[-1] / iw["close"].iloc[0] - 1.0) if len(iw) >= 2 else math.nan
    strategy_ret = float(cw["equity"].iloc[-1] / cw["equity"].iloc[0] - 1.0)
    triggered = tw[tw.get("risk_exit_note", pd.Series(dtype=str)).astype(str).ne("base_policy")] if not tw.empty else pd.DataFrame()
    return {
        "variant": variant,
        "window": window,
        "closed": int(len(tw)),
        "strategy_ret": strategy_ret,
        "index_ret": index_ret,
        "excess_ret": strategy_ret - index_ret if math.isfinite(index_ret) else math.nan,
        "max_drawdown": _max_drawdown(cw["equity"]),
        "win_rate": float((net > 0).mean()) if len(net) else 0.0,
        "mean_trade_ret": float(net.mean()) if len(net) else 0.0,
        "original_mean_trade_ret": float(original.mean()) if len(original) else 0.0,
        "worst_trade": float(net.min()) if len(net) else 0.0,
        "avg_slot_pct": float(pd.to_numeric(tw.get("slot_pct", pd.Series(dtype=float)), errors="coerce").mean()) if len(tw) else 0.0,
        "avg_open_positions": float(cw["open_positions"].mean()),
        "cooldown_days": int(cw["in_cooldown"].sum()) if "in_cooldown" in cw.columns else 0,
        "released_by_recovery_days": int(cw["released_by_recovery"].sum()) if "released_by_recovery" in cw.columns else 0,
        "released_by_max_days": int(cw["released_by_max"].sum()) if "released_by_max" in cw.columns else 0,
        "m30_exit_trades": int(len(triggered)),
        "sum_pnl": float(pd.to_numeric(tw.get("realized_pnl", pd.Series(dtype=float)), errors="coerce").sum()) if len(tw) else 0.0,
    }


def _variant_specs() -> list[dict[str, Any]]:
    return [
        {"variant": "le5_slot50_base", "slot_pct": 0.50, "use_m30_exit": False},
        {"variant": "le5_slot33_base", "slot_pct": 0.33, "use_m30_exit": False},
        {"variant": "le5_slot25_base", "slot_pct": 0.25, "use_m30_exit": False},
        {"variant": "le5_slot50_cd2_5d", "slot_pct": 0.50, "use_m30_exit": False, "cooldown_after_losses": 2, "cooldown_days": 5},
        {"variant": "le5_slot50_cd2_15d", "slot_pct": 0.50, "use_m30_exit": False, "cooldown_after_losses": 2, "cooldown_days": 15},
        {
            "variant": "le5_slot50_cd2_dynamic_m3_max15",
            "slot_pct": 0.50,
            "use_m30_exit": False,
            "cooldown_after_losses": 2,
            "cooldown_mode": "dynamic_recovery",
            "min_cooldown_days": 3,
            "max_cooldown_days": 15,
        },
        {"variant": "le5_slot33_cd2_5d", "slot_pct": 0.33, "use_m30_exit": False, "cooldown_after_losses": 2, "cooldown_days": 5},
        {"variant": "le5_slot25_cd2_5d", "slot_pct": 0.25, "use_m30_exit": False, "cooldown_after_losses": 2, "cooldown_days": 5},
        {"variant": "le5_slot50_m30weak3", "slot_pct": 0.50, "m30_exit_mode": "weak3"},
        {"variant": "le5_slot33_m30weak3", "slot_pct": 0.33, "m30_exit_mode": "weak3"},
        {"variant": "le5_slot25_m30weak3", "slot_pct": 0.25, "m30_exit_mode": "weak3"},
        {"variant": "le5_slot25_m30weak3_cd2_5d", "slot_pct": 0.25, "m30_exit_mode": "weak3", "cooldown_after_losses": 2, "cooldown_days": 5},
        {"variant": "le5_slot50_m30persist", "slot_pct": 0.50, "m30_exit_mode": "persistent_fail"},
        {"variant": "le5_slot33_m30persist", "slot_pct": 0.33, "m30_exit_mode": "persistent_fail"},
        {"variant": "le5_slot25_m30persist", "slot_pct": 0.25, "m30_exit_mode": "persistent_fail"},
        {
            "variant": "le5_slot25_m30persist_cd2_5d",
            "slot_pct": 0.25,
            "m30_exit_mode": "persistent_fail",
            "cooldown_after_losses": 2,
            "cooldown_days": 5,
        },
    ]


def run(args: argparse.Namespace) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw = _load_signals(args.start_date, args.end_date)
    calendar = _trade_calendar(pd.Timestamp(args.start_date), max(pd.Timestamp(args.end_date), raw["policy_exit_date"].max()) + pd.Timedelta(days=10))
    raw = _add_d1_d2(raw, calendar)
    bars = _load_30m_bars(raw) if bool(args.with_m30_exit) else pd.DataFrame()
    m30_signal_sets = {"none": raw.copy()}
    if not bars.empty:
        m30_signal_sets["weak3"] = _apply_m30_weak_exit(raw, bars)
        m30_signal_sets["persistent_fail"] = _apply_m30_persistent_fail_exit(raw, bars)
    index_df = _load_index(args.start_date, args.end_date)
    windows = {
        "full": (args.start_date, args.end_date),
        "train_2020_2023": ("2020-01-01", "2023-12-31"),
        "valid_2024_2025": ("2024-01-01", "2025-12-31"),
        "post_2024_09": ("2024-09-24", args.end_date),
        "blind_2026ytd": ("2026-01-01", args.end_date),
    }

    raw.to_csv(OUT_DIR / "le5_source_signals.csv", index=False, encoding="utf-8-sig")
    if not bars.empty:
        bars.to_csv(OUT_DIR / "loaded_30m_bars_d1d2.csv", index=False, encoding="utf-8-sig")
        m30_signal_sets["weak3"].to_csv(OUT_DIR / "le5_source_signals_with_m30_weak3_exit.csv", index=False, encoding="utf-8-sig")
        m30_signal_sets["persistent_fail"].to_csv(
            OUT_DIR / "le5_source_signals_with_m30_persistent_fail_exit.csv",
            index=False,
            encoding="utf-8-sig",
        )

    summary_rows: list[dict[str, Any]] = []
    annual_rows: list[dict[str, Any]] = []
    for spec in _variant_specs():
        mode = str(spec.get("m30_exit_mode") or "none")
        if mode != "none" and not bool(args.with_m30_exit):
            continue
        signals = m30_signal_sets.get(mode, raw)
        run_dir = OUT_DIR / str(spec["variant"])
        run_dir.mkdir(parents=True, exist_ok=True)
        curve, closed = _simulate(signals, calendar, spec, int(args.slots), int(args.daily_open_limit))
        curve.to_csv(run_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(run_dir / "closed_trades.csv", index=False, encoding="utf-8-sig")
        for window, (start, end) in windows.items():
            summary_rows.append(_metrics(curve, closed, index_df, str(spec["variant"]), window, start, end))
        for year in sorted(pd.to_datetime(curve["date"]).dt.year.dropna().unique()):
            annual_rows.append(_metrics(curve, closed, index_df, str(spec["variant"]), str(int(year)), f"{int(year)}-01-01", f"{int(year)}-12-31"))

    summary = pd.DataFrame(summary_rows)
    annual = pd.DataFrame(annual_rows)
    summary.to_csv(OUT_DIR / "risk_variant_summary.csv", index=False, encoding="utf-8-sig")
    annual.to_csv(OUT_DIR / "risk_variant_annual_summary.csv", index=False, encoding="utf-8-sig")

    pct_cols = {
        "strategy_ret",
        "index_ret",
        "excess_ret",
        "max_drawdown",
        "win_rate",
        "mean_trade_ret",
        "original_mean_trade_ret",
        "worst_trade",
        "avg_slot_pct",
        "avg_open_positions",
    }
    money_cols = {"sum_pnl"}
    full = summary[summary["window"].eq("full")].sort_values(["max_drawdown", "strategy_ret"], ascending=[False, False])
    post = summary[summary["window"].eq("post_2024_09")].sort_values(["max_drawdown", "strategy_ret"], ascending=[False, False])
    blind = summary[summary["window"].eq("blind_2026ytd")].sort_values(["max_drawdown", "strategy_ret"], ascending=[False, False])
    valid = summary[summary["window"].eq("valid_2024_2025")].sort_values(["max_drawdown", "strategy_ret"], ascending=[False, False])
    m30_weak_triggered = (
        int(m30_signal_sets.get("weak3", raw)["risk_exit_note"].astype(str).ne("base_policy").sum())
        if "risk_exit_note" in m30_signal_sets.get("weak3", raw).columns
        else 0
    )
    m30_persist_triggered = (
        int(m30_signal_sets.get("persistent_fail", raw)["risk_exit_note"].astype(str).ne("base_policy").sum())
        if "risk_exit_note" in m30_signal_sets.get("persistent_fail", raw).columns
        else 0
    )
    lines = [
        "# Score120 机构主升风险合同变体复核 v1",
        "",
        "## 口径",
        "",
        "- 信号：score120 + 主线扩散>=65 + 信号日30m站上MA20 + `index_mom60<=5%`。",
        "- 资金曲线：沿用前次复核口径，持仓期间按本金占用估值，盈亏在策略出场日体现。",
        "- 30m弱势退出：买入后D1/D2的10:30 30m收盘相对入场价跌幅<=-3%时提前退出，交易成本按30bps估算。",
        "- 连续亏损冷却：连续2笔亏损后暂停新增5个交易日，只管理已有持仓。",
        "",
        f"- 机构主升有效信号数：{len(raw)}",
        f"- 30m弱势退出触发数：{m30_weak_triggered}",
        f"- 30m二次不修复退出触发数：{m30_persist_triggered}",
        "",
        "## 全周期",
        "",
        _md_table(full, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 2024-2025 验证窗口",
        "",
        _md_table(valid, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 2024-09 后高波动窗口",
        "",
        _md_table(post, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 2026 样本外窗口",
        "",
        _md_table(blind, pct_cols=pct_cols, money_cols=money_cols),
        "",
        "## 分年明细",
        "",
        _md_table(annual.sort_values(["variant", "window"]), pct_cols=pct_cols, money_cols=money_cols, max_rows=100),
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    result = {
        "status": "completed",
        "out_dir": str(OUT_DIR),
        "source": str(SOURCE),
        "signals": int(len(raw)),
        "m30_weak_exit_triggered": m30_weak_triggered,
        "m30_persistent_fail_exit_triggered": m30_persist_triggered,
        "variants": summary["variant"].dropna().astype(str).unique().tolist() if not summary.empty else [],
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit score120 institutional mainwave risk-contract variants.")
    parser.add_argument("--start-date", default="2020-01-01")
    parser.add_argument("--end-date", default="2026-06-17")
    parser.add_argument("--slots", type=int, default=2)
    parser.add_argument("--daily-open-limit", type=int, default=2)
    parser.add_argument("--with-m30-exit", action="store_true", default=True)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

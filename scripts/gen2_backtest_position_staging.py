from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen2_backtest_open_v1_intraday_risk import Lot, _sell_trade  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import (  # noqa: E402
    _json_default,
    _load_benchmark,
    _load_signals,
    _load_trade_dates,
    _pct,
    _safe_price,
    _sort_signals,
)
from scripts.gen2_backtest_risk_cool_dynamic_circuit import _policy_params  # noqa: E402
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _load_minute_bars, _process_lot_exits  # noqa: E402
from scripts.gen2_sweep_open_v1_semantic_exits import _load_daily_ohlc  # noqa: E402


DEFAULT_OUTPUT_DIR = ROOT / "reports" / "gen2_position_staging_backtest"
SYSTEMS = {
    "g2_v3_user_v2_two_stop_cd3_skip": {
        "source": ROOT / "reports" / "gen2_risk_cool_dynamic_circuit_user_v2_cap_v2" / "sources" / "risk_cool_base.parquet",
        "policy": "two_stop_cd3_skip",
        "note": "G2 V3 User V2 two_stop_cd3 from the Gen2 backtest selector",
    },
    "g2_alpha191_t1_keep80_stop_cd3_skip": {
        "source": ROOT / "reports" / "gen2_alpha191_t1_keep80_stop_cd3_full" / "variant_sources" / "alpha191_gate_keep80.parquet",
        "policy": "stop_cd3_skip",
        "note": "G2 Alpha191 T-1 keep80 from the Gen2 backtest selector",
    },
    "g2_alpha191_t1_keep90_stop_cd3_skip": {
        "source": ROOT / "reports" / "gen2_alpha191_t1_keep90_stop_cd3_full" / "variant_sources" / "alpha191_gate_keep90.parquet",
        "policy": "stop_cd3_skip",
        "note": "G2 Alpha191 T-1 keep90 from the Gen2 backtest selector",
    },
}


@dataclass
class AddonWatch:
    code: str
    name: str
    signal_date: str
    addon_date: str
    base_price: float
    v4_rank: int
    v4_score: float
    hold_days: int


def _daily_return_map(daily: pd.DataFrame) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    if daily.empty:
        return out
    d = daily.sort_values(["code", "trade_date"]).copy()
    d["prev_close"] = d.groupby("code", sort=False)["close"].shift(1)
    d["day_ret"] = pd.to_numeric(d["close"], errors="coerce") / pd.to_numeric(d["prev_close"], errors="coerce") - 1.0
    for row in d.dropna(subset=["day_ret"]).itertuples(index=False):
        out[(str(row.code), str(row.trade_date))] = float(row.day_ret)
    return out


def _equity(cash: float, lots: list[Lot], price_map: dict[tuple[str, str], float], date: str) -> tuple[float, float]:
    market_value = 0.0
    for lot in lots:
        close = _safe_price(price_map, lot.code, date) or lot.buy_price
        market_value += lot.shares * close
    return cash + market_value, market_value


def _code_capital(lots: list[Lot], code: str) -> float:
    return float(sum(lot.capital for lot in lots if lot.code == code))


def _make_lot(
    code: str,
    name: str,
    buy_datetime: pd.Timestamp,
    raw_price: float,
    capital: float,
    v4_rank: int,
    v4_score: float,
    trade_dates: list[str],
    date_idx: dict[str, int],
    hold_days: int,
    reason: str,
) -> Optional[Lot]:
    buy_date = buy_datetime.strftime("%Y-%m-%d")
    if buy_date not in date_idx:
        return None
    buy_price = float(raw_price) * 1.0005
    if capital <= 0 or buy_price <= 0 or not np.isfinite(buy_price):
        return None
    sell_idx = min(date_idx[buy_date] + int(hold_days), len(trade_dates) - 1)
    buy_fee = capital * 2.5 / 10000.0
    lot = Lot(
        code=code,
        name=name,
        buy_date=buy_date,
        buy_datetime=buy_datetime,
        sell_date=trade_dates[sell_idx],
        buy_price=buy_price,
        shares=(capital - buy_fee) / buy_price,
        capital=capital,
        v4_rank=int(v4_rank),
        v4_score=float(v4_score),
        peak_price=buy_price,
    )
    lot.entry_reason = reason
    return lot


def _find_down_addon_bar(day_bars: pd.DataFrame, base_dt: pd.Timestamp) -> Optional[pd.Series]:
    if day_bars.empty:
        return None
    scan = day_bars[pd.to_datetime(day_bars["datetime"]) > base_dt].sort_values("datetime").copy()
    if len(scan) < 3:
        return None
    rows = list(scan.itertuples(index=False))
    for i in range(1, len(rows) - 1):
        prev_bar = rows[i - 1]
        mid = rows[i]
        right = rows[i + 1]
        mid_low = float(getattr(mid, "low"))
        if mid_low >= float(getattr(prev_bar, "low")) or mid_low >= float(getattr(right, "low")):
            continue
        if float(getattr(right, "close")) <= float(getattr(mid, "close")):
            continue
        return scan.iloc[i + 1]
    return None


def _find_up_addon_bar(day_bars: pd.DataFrame, day_ret: float, max_up_ret: float) -> Optional[pd.Series]:
    if day_bars.empty or not math.isfinite(float(day_ret)):
        return None
    if day_ret <= 0 or day_ret > float(max_up_ret):
        return None
    day = day_bars.sort_values("datetime").copy()
    if day.empty:
        return None
    last = day.iloc[-1]
    first = day.iloc[0]
    day_high = float(pd.to_numeric(day["high"], errors="coerce").max())
    day_low = float(pd.to_numeric(day["low"], errors="coerce").min())
    if day_high <= day_low:
        return None
    close_pos = (float(last["close"]) - day_low) / (day_high - day_low)
    if close_pos < 0.55:
        return None
    if float(last["close"]) < float(first["close"]):
        return None
    return last


def _apply_addons(
    date: str,
    watches: list[AddonWatch],
    lots: list[Lot],
    cash: float,
    trade_dates: list[str],
    date_idx: dict[str, int],
    price_map: dict[tuple[str, str], float],
    bars_by_code_date: dict[tuple[str, str], pd.DataFrame],
    day_ret_map: dict[tuple[str, str], float],
    addon_ledger: list[dict[str, Any]],
    tranche_weight: float,
    max_position_weight: float,
    max_up_ret: float,
) -> tuple[list[AddonWatch], float]:
    remaining: list[AddonWatch] = []
    held_codes = {lot.code for lot in lots}
    for watch in watches:
        if watch.addon_date != date:
            if watch.addon_date > date:
                remaining.append(watch)
            continue
        if watch.code not in held_codes or cash <= 0:
            addon_ledger.append({"date": date, "code": watch.code, "action": "skip", "reason": "not_held_or_no_cash"})
            continue
        day_ret = day_ret_map.get((watch.code, date))
        day_bars = bars_by_code_date.get((watch.code, date), pd.DataFrame())
        addon_bar: Optional[pd.Series] = None
        addon_reason = ""
        if day_ret is not None and day_ret < 0:
            addon_bar = _find_down_addon_bar(day_bars, pd.Timestamp(f"{date} 09:30:00"))
            addon_reason = "down_next_day_30m_bottom_fractal"
        elif day_ret is not None and day_ret > 0:
            addon_bar = _find_up_addon_bar(day_bars, day_ret, max_up_ret)
            addon_reason = "up_next_day_moderate_close_strength"
        if addon_bar is None:
            addon_ledger.append(
                {
                    "date": date,
                    "code": watch.code,
                    "action": "skip",
                    "reason": "addon_condition_not_met",
                    "day_ret": day_ret,
                }
            )
            continue

        equity, _ = _equity(cash, lots, price_map, date)
        existing = _code_capital(lots, watch.code)
        max_code_capital = equity * float(max_position_weight)
        target_addon = equity * float(tranche_weight)
        capital = min(cash, target_addon, max(0.0, max_code_capital - existing))
        if capital <= 0:
            addon_ledger.append({"date": date, "code": watch.code, "action": "skip", "reason": "position_cap_reached"})
            continue
        lot = _make_lot(
            code=watch.code,
            name=watch.name,
            buy_datetime=pd.Timestamp(addon_bar["datetime"]),
            raw_price=float(addon_bar["close"]),
            capital=capital,
            v4_rank=watch.v4_rank,
            v4_score=watch.v4_score,
            trade_dates=trade_dates,
            date_idx=date_idx,
            hold_days=watch.hold_days,
            reason=addon_reason,
        )
        if lot is None:
            addon_ledger.append({"date": date, "code": watch.code, "action": "skip", "reason": "invalid_lot"})
            continue
        cash -= capital
        lots.append(lot)
        addon_ledger.append(
            {
                "date": date,
                "code": watch.code,
                "action": "buy",
                "reason": addon_reason,
                "day_ret": day_ret,
                "buy_datetime": lot.buy_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "capital": capital,
                "price": lot.buy_price,
            }
        )
    return remaining, cash


def _segment_rows(curve_df: pd.DataFrame) -> list[dict[str, Any]]:
    d = curve_df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce")
    windows = [
        ("train_2024h2_2025q1", "2024-07-09", "2025-03-31"),
        ("validation_2025q2_q4", "2025-04-01", "2025-12-31"),
        ("blind_2026ytd", "2026-01-01", "2026-05-21"),
    ]
    rows: list[dict[str, Any]] = []
    for name, start, end in windows:
        g = d[(d["date"] >= pd.Timestamp(start)) & (d["date"] <= pd.Timestamp(end))].copy()
        if g.empty:
            continue
        first = float(g.iloc[0]["strategy_equity"])
        last = float(g.iloc[-1]["strategy_equity"])
        rows.append({"segment": name, "return": last / first - 1.0 if first else None, "max_drawdown": float(g["drawdown"].min())})
    return rows


def run_one(
    name: str,
    source: Path,
    policy: str,
    output_dir: Path,
    start_date: str,
    end_date: str,
    tranche_weight: float,
    max_position_weight: float,
    initial_cash: float,
    max_codes: int,
    max_new_codes_per_day: int,
    hold_days: int,
    max_up_ret: float,
    position_model: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(source, start_date, end_date)
    signals["confirm_datetime"] = pd.to_datetime(signals["confirm_datetime"], errors="coerce")
    signals = signals.dropna(subset=["confirm_datetime"]).copy()
    trade_dates = _load_trade_dates(start_date, end_date)
    if not trade_dates:
        raise RuntimeError("No trade dates available.")
    date_idx = {date: i for i, date in enumerate(trade_dates)}
    signals = signals[signals["entry_date"].isin(date_idx)].copy()
    signals = _sort_signals(signals, "trigger_time").reset_index(drop=True)
    signals_by_date = {date: g.copy() for date, g in signals.groupby("entry_date", sort=True)}

    codes = signals["code"].dropna().astype(str).unique().tolist()
    daily = _load_daily_ohlc(codes, start_date, end_date)
    price_map = {(row.code, row.trade_date): float(row.close) for row in daily.itertuples(index=False)}
    prev_low_map = {
        (row.code, row.trade_date): float(row.prev_low)
        for row in daily.itertuples(index=False)
        if row.prev_low is not None and np.isfinite(row.prev_low)
    }
    day_ret_map = _daily_return_map(daily)
    bars30 = _load_minute_bars(codes, start_date, end_date, 30)
    bars_by_code_date = {
        (code, date): g.sort_values("datetime").copy()
        for (code, date), g in bars30.groupby(["code", "bar_date"], sort=False)
    }
    fill_profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")
    cooldown_days, cooldown_action, trigger_stop_count = _policy_params(policy)

    cash = float(initial_cash)
    lots: list[Lot] = []
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    addon_ledger: list[dict[str, Any]] = []
    watches: list[AddonWatch] = []
    cooldown_until_idx = -1
    consecutive_stop_events = 0

    for date in trade_dates:
        idx = date_idx[date]
        stop_loss_codes_today: set[str] = set()
        next_lots: list[Lot] = []
        for lot in lots:
            before_trades = len(trades)
            day_bars = bars_by_code_date.get((lot.code, date), pd.DataFrame())
            if not day_bars.empty:
                day_bars = day_bars[pd.to_datetime(day_bars["datetime"]) > lot.buy_datetime].copy()
            lot_after, cash = _process_lot_exits(
                lot=lot,
                bars30=day_bars,
                semantic_bars=day_bars,
                cash=cash,
                trades=trades,
                prev_low=prev_low_map.get((lot.code, date)),
                profile=fill_profile,
                gap_confirm_bars=day_bars,
                sell_slippage_bps=5.0,
                commission_bps=2.5,
                stamp_tax_bps=5.0,
            )
            for trade in trades[before_trades:]:
                if str(trade.get("exit_reason")) == "stop_loss_30m":
                    stop_loss_codes_today.add(str(trade.get("code")))
            if lot_after is None:
                continue
            if date >= lot_after.sell_date:
                close_price = _safe_price(price_map, lot_after.code, date)
                if close_price is not None:
                    trade = _sell_trade(
                        lot=lot_after,
                        sell_datetime=pd.Timestamp(f"{date} 15:00:00"),
                        raw_price=close_price,
                        ratio=1.0,
                        reason="time_exit",
                        sell_slippage_bps=5.0,
                        commission_bps=2.5,
                        stamp_tax_bps=5.0,
                    )
                    cash += float(trade["proceeds"])
                    trades.append({k: v for k, v in trade.items() if k != "proceeds"})
                else:
                    next_lots.append(lot_after)
            else:
                next_lots.append(lot_after)
        lots = next_lots

        stop_events_today = len(stop_loss_codes_today)
        if stop_events_today:
            consecutive_stop_events += int(stop_events_today)
            if cooldown_days > 0 and consecutive_stop_events >= trigger_stop_count:
                cooldown_until_idx = max(cooldown_until_idx, idx + cooldown_days)
                consecutive_stop_events = 0
        else:
            consecutive_stop_events = 0

        if position_model == "staged":
            watches, cash = _apply_addons(
                date=date,
                watches=watches,
                lots=lots,
                cash=cash,
                trade_dates=trade_dates,
                date_idx=date_idx,
                price_map=price_map,
                bars_by_code_date=bars_by_code_date,
                day_ret_map=day_ret_map,
                addon_ledger=addon_ledger,
                tranche_weight=tranche_weight,
                max_position_weight=max_position_weight,
                max_up_ret=max_up_ret,
            )
        else:
            watches = []

        cooldown_active = idx <= cooldown_until_idx
        buy_allowed = True
        if cooldown_active and cooldown_action == "skip":
            buy_allowed = False

        bought_new = 0
        today = signals_by_date.get(date)
        if buy_allowed and today is not None and not today.empty:
            held_codes = {lot.code for lot in lots}
            slots = max(0, int(max_codes) - len(held_codes))
            buy_count = min(slots, int(max_new_codes_per_day))
            if buy_count > 0 and cash > 0:
                candidates = today[~today["code"].isin(held_codes)].head(buy_count)
                for row in candidates.itertuples(index=False):
                    equity, _ = _equity(cash, lots, price_map, date)
                    capital = min(cash, equity * float(tranche_weight))
                    if cooldown_active and cooldown_action == "half":
                        capital *= 0.5
                    if capital <= 0:
                        continue
                    raw_price = float(getattr(row, "entry_price"))
                    lot = _make_lot(
                        code=str(getattr(row, "code")),
                        name=str(getattr(row, "name")),
                        buy_datetime=pd.Timestamp(getattr(row, "confirm_datetime")),
                        raw_price=raw_price,
                        capital=capital,
                        v4_rank=int(getattr(row, "v4_rank")),
                        v4_score=float(getattr(row, "v4_score")),
                        trade_dates=trade_dates,
                        date_idx=date_idx,
                        hold_days=hold_days,
                        reason=f"{position_model}_entry",
                    )
                    if lot is None:
                        continue
                    cash -= capital
                    lots.append(lot)
                    bought_new += 1
                    next_idx = date_idx[date] + 1
                    if position_model == "staged" and next_idx < len(trade_dates):
                        watches.append(
                            AddonWatch(
                                code=lot.code,
                                name=lot.name,
                                signal_date=date,
                                addon_date=trade_dates[next_idx],
                                base_price=lot.buy_price,
                                v4_rank=lot.v4_rank,
                                v4_score=lot.v4_score,
                                hold_days=hold_days,
                            )
                        )

        equity, market_value = _equity(cash, lots, price_map, date)
        curve.append(
            {
                "date": date,
                "cash": cash,
                "market_value": market_value,
                "equity": equity,
                "holding_lots": len(lots),
                "holding_codes": len({lot.code for lot in lots}),
                "watch_count": len(watches),
            }
        )
        ledger.append(
            {
                "date": date,
                "cooldown_active": bool(cooldown_active),
                "buy_allowed": bool(buy_allowed),
                "stop_events_today": int(stop_events_today),
                "bought_new_codes": int(bought_new),
            }
        )

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)
    ledger_df = pd.DataFrame(ledger)
    addon_df = pd.DataFrame(addon_ledger)
    if not curve_df.empty:
        curve_df["strategy_equity"] = curve_df["equity"] / float(initial_cash)
        curve_df["peak"] = curve_df["strategy_equity"].cummax()
        curve_df["drawdown"] = curve_df["strategy_equity"] / curve_df["peak"] - 1.0
    benchmark = _load_benchmark(start_date, end_date, "000852.SH")
    if not benchmark.empty:
        curve_df = curve_df.merge(benchmark, on="date", how="left")

    total_return = float(curve_df["strategy_equity"].iloc[-1] - 1.0) if not curve_df.empty else 0.0
    benchmark_return = (
        float(curve_df["benchmark_equity"].dropna().iloc[-1] - 1.0)
        if "benchmark_equity" in curve_df.columns and not curve_df["benchmark_equity"].dropna().empty
        else None
    )
    summary = {
        "schema_version": 1,
        "system": name,
        "source": str(source),
        "policy": policy,
        "position_model": position_model,
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": float(initial_cash),
        "tranche_weight": float(tranche_weight),
        "max_position_weight": float(max_position_weight),
        "max_codes": int(max_codes),
        "max_new_codes_per_day": int(max_new_codes_per_day),
        "hold_days": int(hold_days),
        "max_up_ret_for_addon": float(max_up_ret),
        "signal_count": int(len(signals)),
        "trade_count": int(len(trades_df)),
        "addon_buy_count": int((addon_df["action"] == "buy").sum()) if not addon_df.empty and "action" in addon_df.columns else 0,
        "final_equity": float(curve_df["equity"].iloc[-1]) if not curve_df.empty else float(initial_cash),
        "total_return": total_return,
        "benchmark_return": benchmark_return,
        "excess_return": None if benchmark_return is None else total_return - benchmark_return,
        "max_drawdown": float(curve_df["drawdown"].min()) if "drawdown" in curve_df.columns else 0.0,
        "win_rate": float((trades_df["return"] > 0).mean()) if not trades_df.empty else None,
        "avg_trade_return": float(trades_df["return"].mean()) if not trades_df.empty else None,
        "exit_reason_counts": trades_df["exit_reason"].fillna("").astype(str).value_counts().to_dict() if not trades_df.empty else {},
    }
    signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    curve_df.to_csv(output_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    ledger_df.to_csv(output_dir / "decision_ledger.csv", index=False, encoding="utf-8-sig")
    addon_df.to_csv(output_dir / "addon_ledger.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(_segment_rows(curve_df)).to_csv(output_dir / "segment_summary.csv", index=False, encoding="utf-8-sig")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return summary


def _write_report(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    model = str(rows[0].get("position_model", "")) if rows else ""
    if model == "single":
        intro = [
            "Only the entry sizing path is changed: each new signal buys one fixed tranche and no follow-up add-on is created.",
            "Sell, stop-loss, take-profit, previous-low exit and cooldown policies stay aligned with each system source.",
        ]
    else:
        intro = [
            "Only the entry sizing path is changed: initial signal buys 25% equity, then the next trading day may add 25%.",
            "Down next day adds on a confirmed 30m bottom fractal. Up next day adds only on moderate positive close strength.",
            "Sell, stop-loss, take-profit, previous-low exit and cooldown policies stay aligned with each system source.",
        ]
    lines = [
        "# G2 Position Staging Backtest",
        "",
        *intro,
        "",
        "| system | policy | signals | trades | addons | total | excess | max_dd | win | avg_trade |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| {row['system']} | {row['policy']} | {row['signal_count']} | {row['trade_count']} | {row['addon_buy_count']} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | "
            f"{_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    requested = [x.strip() for x in str(args.systems).split(",") if x.strip()]
    if not requested:
        requested = list(SYSTEMS)
    missing = sorted(set(requested) - set(SYSTEMS))
    if missing:
        raise ValueError(f"Unknown systems: {missing}")
    rows: list[dict[str, Any]] = []
    for name in requested:
        spec = SYSTEMS[name]
        source = Path(spec["source"])
        if not source.exists():
            raise FileNotFoundError(source)
        row = run_one(
            name=name,
            source=source,
            policy=str(spec["policy"]),
            output_dir=output_dir / name,
            start_date=str(args.start_date),
            end_date=str(args.end_date),
            tranche_weight=float(args.tranche_weight),
            max_position_weight=float(args.max_position_weight),
            initial_cash=float(args.initial_cash),
            max_codes=int(args.max_codes),
            max_new_codes_per_day=int(args.max_new_codes_per_day),
            hold_days=int(args.hold_days),
            max_up_ret=float(args.max_up_ret),
            position_model=str(args.position_model),
        )
        row["note"] = spec["note"]
        rows.append(row)
    summary_df = pd.DataFrame(rows).sort_values(["total_return", "max_drawdown"], ascending=[False, False])
    summary_df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "rows": summary_df.where(pd.notna(summary_df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "findings.md", "runs": "./<system>/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_report(output_dir, summary_df.to_dict("records"))
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest current G2 systems with position-only staged entry.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--systems", default=",".join(SYSTEMS), help="Comma-separated system keys.")
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--initial-cash", type=float, default=150000.0)
    parser.add_argument("--tranche-weight", type=float, default=0.25)
    parser.add_argument("--max-position-weight", type=float, default=0.50)
    parser.add_argument("--max-codes", type=int, default=2)
    parser.add_argument("--max-new-codes-per-day", type=int, default=1)
    parser.add_argument("--hold-days", type=int, default=10)
    parser.add_argument("--max-up-ret", type=float, default=0.06)
    parser.add_argument("--position-model", choices=["staged", "single"], default="staged")
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

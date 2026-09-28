from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from api.gen2_strategy import GEN2_OPEN_RULE_V1  # noqa: E402
from scripts.gen2_backtest_open_v1_intraday_risk import Lot, _sell_trade  # noqa: E402
from scripts.gen2_backtest_open_v1_portfolio import (  # noqa: E402
    SORT_MODES,
    _json_default,
    _load_benchmark,
    _load_signals,
    _load_trade_dates,
    _pct,
    _safe_price,
    _sort_signals,
)
from scripts.gen2_compare_prev_low_exit_fills import FillProfile, _load_minute_bars, _process_lot_exits  # noqa: E402
from scripts.gen2_sweep_open_v1_semantic_exits import _load_daily_ohlc  # noqa: E402

DEFAULT_SIGNAL_SOURCE = REPO_ROOT / "reports" / "gen2_v4_factor_profile" / "pullback_signal_factor_labeled.parquet"
DEFAULT_STATE_DAILY = REPO_ROOT / "reports" / "gen2_open_state_research_full" / "g2_open_state_daily.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_pullback_holding_timing_sweep"


@dataclass(frozen=True)
class HoldingTimingProfile:
    key: str
    note: str
    exit_rule: str = "none"


def _profiles() -> list[HoldingTimingProfile]:
    return [
        HoldingTimingProfile("baseline", "no holding-level market timing", "none"),
        HoldingTimingProfile("d1_open_exit_not_normal", "if previous close state is not NORMAL, exit holdings at next open", "not_normal"),
        HoldingTimingProfile("d1_open_exit_off", "if previous close state is OFF, exit holdings at next open", "off"),
        HoldingTimingProfile("d1_open_exit_csi1000_below_ma20", "if previous close CSI1000 is below MA20, exit holdings at next open", "csi1000_below_ma20"),
        HoldingTimingProfile(
            "d1_open_exit_csi1000_below_ma20_or_slope_down",
            "if previous close CSI1000 is below MA20 or MA20 slope5 <= 0, exit holdings at next open",
            "csi1000_below_ma20_or_slope_down",
        ),
    ]


def _load_state_daily(path: Path, start_date: str, end_date: str, trade_dates: list[str]) -> dict[str, dict[str, Any]]:
    df = pd.read_csv(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = df[df["trade_date"] <= end_date].copy().sort_values("trade_date").reset_index(drop=True)
    for col in ["csi1000_close", "csi1000_ma20", "csi1000_ma20_slope5"]:
        d[col] = pd.to_numeric(d.get(col), errors="coerce")
    d["prev_g2_open_state"] = d["g2_open_state"].shift(1)
    d["prev_csi1000_close"] = d["csi1000_close"].shift(1)
    d["prev_csi1000_ma20"] = d["csi1000_ma20"].shift(1)
    d["prev_csi1000_ma20_slope5"] = d["csi1000_ma20_slope5"].shift(1)
    d = d[d["trade_date"].isin(trade_dates)].copy()
    d = d[d["trade_date"] >= start_date].copy()
    return {
        str(row.trade_date): {
            "prev_g2_open_state": str(row.prev_g2_open_state) if pd.notna(row.prev_g2_open_state) else "",
            "prev_csi1000_close": float(row.prev_csi1000_close) if pd.notna(row.prev_csi1000_close) else np.nan,
            "prev_csi1000_ma20": float(row.prev_csi1000_ma20) if pd.notna(row.prev_csi1000_ma20) else np.nan,
            "prev_csi1000_ma20_slope5": float(row.prev_csi1000_ma20_slope5) if pd.notna(row.prev_csi1000_ma20_slope5) else np.nan,
        }
        for row in d.itertuples(index=False)
    }


def _market_exit_allowed(state: dict[str, Any] | None, rule: str) -> bool:
    if rule == "none" or not state:
        return False
    prev_state = str(state.get("prev_g2_open_state") or "")
    close = float(state.get("prev_csi1000_close", np.nan))
    ma20 = float(state.get("prev_csi1000_ma20", np.nan))
    slope5 = float(state.get("prev_csi1000_ma20_slope5", np.nan))
    below_ma20 = np.isfinite(close) and np.isfinite(ma20) and close < ma20
    slope_down = np.isfinite(slope5) and slope5 <= 0
    if rule == "not_normal":
        return prev_state != "NORMAL"
    if rule == "off":
        return prev_state == "OFF"
    if rule == "csi1000_below_ma20":
        return below_ma20
    if rule == "csi1000_below_ma20_or_slope_down":
        return below_ma20 or slope_down
    return False


def _run_profile(
    signal_source: Path,
    state_daily: Path,
    output_dir: Path,
    profile: HoldingTimingProfile,
    start_date: str,
    end_date: str,
    sort_mode: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(signal_source, start_date, end_date)
    signals["confirm_datetime"] = pd.to_datetime(signals["confirm_datetime"], errors="coerce")
    signals = signals.dropna(subset=["confirm_datetime"]).copy()
    trade_dates = _load_trade_dates(start_date, end_date)
    if not trade_dates:
        raise RuntimeError("No trade dates available for backtest.")
    date_idx = {date: i for i, date in enumerate(trade_dates)}
    signals = signals[signals["entry_date"].isin(date_idx)].copy()
    signals = _sort_signals(signals, sort_mode).reset_index(drop=True)
    signals_by_date = {date: g.copy() for date, g in signals.groupby("entry_date", sort=True)}

    states = _load_state_daily(state_daily, start_date, end_date, trade_dates)
    codes = signals["code"].dropna().astype(str).unique().tolist()
    daily = _load_daily_ohlc(codes, start_date, end_date)
    price_map = {(row.code, row.trade_date): float(row.close) for row in daily.itertuples(index=False)}
    open_map = {
        (row.code, row.trade_date): float(row.open)
        for row in daily.itertuples(index=False)
        if row.open is not None and np.isfinite(row.open) and float(row.open) > 0
    }
    prev_low_map = {
        (row.code, row.trade_date): float(row.prev_low)
        for row in daily.itertuples(index=False)
        if row.prev_low is not None and np.isfinite(row.prev_low)
    }
    bars30 = _load_minute_bars(codes, start_date, end_date, 30)
    bars30_by_code_date = {
        (code, date): g.sort_values("datetime").copy()
        for (code, date), g in bars30.groupby(["code", "bar_date"], sort=False)
    }
    fill_profile = FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close")

    cash = 150000.0
    lots: List[Lot] = []
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []

    for date in trade_dates:
        next_lots: List[Lot] = []
        force_market_exit = _market_exit_allowed(states.get(date), profile.exit_rule)
        for lot in lots:
            if force_market_exit:
                open_price = _safe_price(open_map, lot.code, date)
                if open_price is not None:
                    trade = _sell_trade(
                        lot=lot,
                        sell_datetime=pd.Timestamp(f"{date} 09:30:00"),
                        raw_price=open_price,
                        ratio=1.0,
                        reason=f"market_timing_{profile.exit_rule}_d1_open",
                        sell_slippage_bps=5.0,
                        commission_bps=2.5,
                        stamp_tax_bps=5.0,
                    )
                    cash += float(trade["proceeds"])
                    trades.append({k: v for k, v in trade.items() if k != "proceeds"})
                    continue

            day_bars30 = bars30_by_code_date.get((lot.code, date), pd.DataFrame())
            if not day_bars30.empty:
                day_bars30 = day_bars30[day_bars30["datetime"] > lot.buy_datetime].copy()
            lot_after, cash = _process_lot_exits(
                lot=lot,
                bars30=day_bars30,
                semantic_bars=day_bars30,
                cash=cash,
                trades=trades,
                prev_low=prev_low_map.get((lot.code, date)),
                profile=fill_profile,
                gap_confirm_bars=day_bars30,
                sell_slippage_bps=5.0,
                commission_bps=2.5,
                stamp_tax_bps=5.0,
            )
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

        today = signals_by_date.get(date)
        if today is not None and not today.empty:
            held_codes = {lot.code for lot in lots}
            slots = max(0, 2 - len(lots))
            buy_count = min(slots, 1)
            if buy_count > 0 and cash > 0:
                candidates = today[~today["code"].isin(held_codes)].head(buy_count)
                market_value_for_size = 0.0
                for lot in lots:
                    close = _safe_price(price_map, lot.code, date) or lot.buy_price
                    market_value_for_size += lot.shares * close
                sizing_equity = cash + market_value_for_size
                target_capital = min(sizing_equity / 2.0, sizing_equity * 0.50)
                for row in candidates.itertuples(index=False):
                    capital_weight = float(getattr(row, "capital_weight", 1.0))
                    if not np.isfinite(capital_weight):
                        capital_weight = 1.0
                    capital_weight = max(0.0, min(capital_weight, 1.0))
                    capital_each = min(cash, target_capital * capital_weight)
                    if capital_each <= 0:
                        break
                    buy_price = float(getattr(row, "entry_price")) * 1.0005
                    if buy_price <= 0 or not np.isfinite(buy_price):
                        continue
                    entry_idx = date_idx[date]
                    sell_idx = min(entry_idx + 10, len(trade_dates) - 1)
                    buy_fee = capital_each * 2.5 / 10000.0
                    shares = (capital_each - buy_fee) / buy_price
                    cash -= capital_each
                    lots.append(
                        Lot(
                            code=str(getattr(row, "code")),
                            name=str(getattr(row, "name")),
                            buy_date=date,
                            buy_datetime=getattr(row, "confirm_datetime"),
                            sell_date=trade_dates[sell_idx],
                            buy_price=buy_price,
                            shares=shares,
                            capital=capital_each,
                            v4_rank=int(getattr(row, "v4_rank")),
                            v4_score=float(getattr(row, "v4_score")),
                            peak_price=buy_price,
                        )
                    )

        market_value = 0.0
        for lot in lots:
            close = _safe_price(price_map, lot.code, date) or lot.buy_price
            market_value += lot.shares * close
        equity = cash + market_value
        curve.append({"date": date, "cash": cash, "market_value": market_value, "equity": equity, "holding_count": len(lots)})

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)
    if not curve_df.empty:
        curve_df["strategy_equity"] = curve_df["equity"] / 150000.0
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
        "rule": GEN2_OPEN_RULE_V1,
        "profile": profile.__dict__,
        "sort_mode": sort_mode,
        "start_date": start_date,
        "end_date": end_date,
        "signal_count": int(len(signals)),
        "trade_count": int(len(trades_df)),
        "final_equity": float(curve_df["equity"].iloc[-1]) if not curve_df.empty else 150000.0,
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
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return summary


def _write_findings(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# G2 Pullback Holding Timing Sweep",
        "",
        "Entry signal source remains the intraday NORMAL pullback signal set. Holding timing uses previous close market state and exits at the next open to avoid close-price lookahead.",
        "",
        "| sort | profile | signals | trades | total | benchmark | excess | max_dd | win | avg_trade | note | exits |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['sort_mode']} | {row['profile']} | {int(row.get('signal_count') or 0)} | {int(row.get('trade_count') or 0)} | "
            f"{_pct(row.get('total_return'))} | {_pct(row.get('benchmark_return'))} | {_pct(row.get('excess_return'))} | "
            f"{_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | "
            f"{row.get('note', '')} | {row.get('exit_reason_counts') or ''} |"
        )
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def run(
    signal_source: Path,
    state_daily: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    sort_modes: list[str],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for sort_mode in sort_modes:
        if sort_mode not in SORT_MODES:
            raise ValueError(f"Unsupported sort mode: {sort_mode}")
        for profile in _profiles():
            summary = _run_profile(
                signal_source=signal_source,
                state_daily=state_daily,
                output_dir=output_dir / "runs" / sort_mode / profile.key,
                profile=profile,
                start_date=start_date,
                end_date=end_date,
                sort_mode=sort_mode,
            )
            rows.append(
                {
                    "sort_mode": sort_mode,
                    "profile": profile.key,
                    "note": profile.note,
                    "signal_count": summary.get("signal_count"),
                    "trade_count": summary.get("trade_count"),
                    "total_return": summary.get("total_return"),
                    "benchmark_return": summary.get("benchmark_return"),
                    "excess_return": summary.get("excess_return"),
                    "max_drawdown": summary.get("max_drawdown"),
                    "win_rate": summary.get("win_rate"),
                    "avg_trade_return": summary.get("avg_trade_return"),
                    "final_equity": summary.get("final_equity"),
                    "exit_reason_counts": json.dumps(summary.get("exit_reason_counts", {}), ensure_ascii=False, sort_keys=True),
                }
            )

    df = pd.DataFrame(rows)
    df["score"] = df["total_return"].fillna(0.0) + df["max_drawdown"].fillna(0.0) * 0.50
    df = df.sort_values(["sort_mode", "score", "total_return"], ascending=[True, False, False]).reset_index(drop=True)
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    ordered_rows = df.where(pd.notna(df), None).to_dict("records")
    _write_findings(output_dir, ordered_rows)
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "signal_source": str(signal_source),
        "state_daily": str(state_daily),
        "rows": ordered_rows,
        "outputs": {"summary": "summary.csv", "findings": "findings.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep holding-level market timing exits for G2 pullback signals.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--state-daily", default=str(DEFAULT_STATE_DAILY))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--sort-modes", default="trigger_time,volume_ratio")
    args = parser.parse_args()
    sort_modes = [x.strip() for x in str(args.sort_modes).split(",") if x.strip()]
    payload = run(
        signal_source=Path(args.signal_source),
        state_daily=Path(args.state_daily),
        output_dir=Path(args.output_dir),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        sort_modes=sort_modes,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

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
    DEFAULT_SIGNAL_SOURCE,
    _json_default,
    _load_benchmark,
    _load_signals,
    _load_trade_dates,
    _pct,
    _safe_price,
    _sort_signals,
)
from scripts.gen2_sweep_open_v1_semantic_exits import _load_daily_ohlc  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_prev_low_exit_fill_compare"


@dataclass(frozen=True)
class FillProfile:
    name: str
    gap_open_mode: str = "prev_low"
    intraday_mode: str = "prev_low"


def _load_minute_bars(codes: List[str], start_date: str, end_date: str, period: int) -> pd.DataFrame:
    from utils.market_warehouse import clickhouse_client

    if not codes:
        return pd.DataFrame()
    table = {15: "kline_minute_15", 30: "kline_minute_30"}[int(period)]
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, datetime, open, high, low, close, volume
        FROM {table}
        WHERE code IN ({quoted})
          AND datetime >= toDateTime('{start_date} 09:30:00')
          AND datetime <= toDateTime('{end_date} 15:00:00')
        ORDER BY code, datetime
        """
    )
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["bar_date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["code", "datetime", "open", "high", "low", "close"]).reset_index(drop=True)


def _prev_low_exit_from_bars(
    lot: Lot,
    bars: pd.DataFrame,
    prev_low: Optional[float],
    profile: FillProfile,
    gap_confirm_bars: Optional[pd.DataFrame] = None,
) -> tuple[Optional[pd.Timestamp], Optional[float], Optional[str]]:
    if not lot.take_profit_done or prev_low is None or not np.isfinite(prev_low):
        return None, None, None
    if bars.empty:
        return None, None, None

    threshold = float(prev_low)
    first = bars.iloc[0]
    first_open = float(first["open"])
    gap_opened = first_open < threshold
    suppress_until: Optional[pd.Timestamp] = None
    if gap_opened:
        if profile.gap_open_mode == "open":
            return pd.Timestamp(first["datetime"]), first_open, "weak_prev_day_low_gap_open"
        if profile.gap_open_mode in {"confirm_15m", "confirm_30m"}:
            confirm_source = gap_confirm_bars if gap_confirm_bars is not None and not gap_confirm_bars.empty else bars
            confirm = confirm_source[pd.to_datetime(confirm_source["datetime"]) > lot.buy_datetime].copy()
            if not confirm.empty:
                confirm_row = confirm.iloc[0]
                confirm_dt = pd.Timestamp(confirm_row["datetime"])
                confirm_close = float(confirm_row["close"])
                if confirm_close < threshold:
                    return confirm_dt, confirm_close, f"weak_prev_day_low_gap_{profile.gap_open_mode}_close"
                suppress_until = confirm_dt

    scan_bars = bars
    if suppress_until is not None:
        scan_bars = bars[pd.to_datetime(bars["datetime"]) > suppress_until].copy()
    hit = scan_bars[pd.to_numeric(scan_bars["low"], errors="coerce") <= threshold]
    if hit.empty:
        return None, None, None
    row = hit.iloc[0]
    dt = pd.Timestamp(row["datetime"])
    if profile.intraday_mode == "prev_low_minus_0p5":
        return dt, threshold * 0.995, "weak_prev_day_low_break_minus_0p5"
    if profile.intraday_mode == "bar_close_15m":
        return dt, float(row["close"]), "weak_prev_day_low_break_15m_close"
    if profile.intraday_mode == "bar_close":
        return dt, float(row["close"]), "weak_prev_day_low_break_30m_close"
    return dt, threshold, "weak_prev_day_low_break_30m"


def _process_lot_exits(
    lot: Lot,
    bars30: pd.DataFrame,
    semantic_bars: pd.DataFrame,
    cash: float,
    trades: List[Dict[str, Any]],
    prev_low: Optional[float],
    profile: FillProfile,
    gap_confirm_bars: Optional[pd.DataFrame],
    sell_slippage_bps: float,
    commission_bps: float,
    stamp_tax_bps: float,
    stop_loss_pct: float = 0.05,
    take_profit_pct: float = 0.10,
    take_profit_sell_ratio: float = 0.50,
) -> tuple[Optional[Lot], float]:
    if bars30.empty and semantic_bars.empty:
        return lot, cash

    events: list[dict[str, Any]] = []
    stop_price = lot.buy_price * (1.0 - abs(float(stop_loss_pct)))
    take_price = lot.buy_price * (1.0 + abs(float(take_profit_pct)))

    for bar in bars30.itertuples(index=False):
        dt = pd.Timestamp(getattr(bar, "datetime"))
        high = float(getattr(bar, "high"))
        low = float(getattr(bar, "low"))
        close = float(getattr(bar, "close"))
        lot.peak_price = max(lot.peak_price, high)
        if low <= stop_price:
            events.append({"dt": dt, "price": stop_price, "reason": "stop_loss_30m", "ratio": 1.0, "priority": 1})
            break
        if not lot.take_profit_done and high >= take_price:
            events.append(
                {
                    "dt": dt,
                    "price": take_price,
                    "reason": "take_profit_partial_30m",
                    "ratio": float(take_profit_sell_ratio),
                    "priority": 2,
                }
            )
            break
        lot.peak_price = max(lot.peak_price, close)

    semantic_dt, semantic_price, semantic_reason = _prev_low_exit_from_bars(
        lot,
        semantic_bars,
        prev_low,
        profile,
        gap_confirm_bars=gap_confirm_bars,
    )
    if semantic_dt is not None and semantic_price is not None and semantic_reason is not None:
        events.append({"dt": semantic_dt, "price": semantic_price, "reason": semantic_reason, "ratio": 1.0, "priority": 3})

    if not events:
        return lot, cash
    event = sorted(events, key=lambda item: (item["dt"], item["priority"]))[0]
    trade = _sell_trade(
        lot=lot,
        sell_datetime=event["dt"],
        raw_price=float(event["price"]),
        ratio=float(event["ratio"]),
        reason=str(event["reason"]),
        sell_slippage_bps=sell_slippage_bps,
        commission_bps=commission_bps,
        stamp_tax_bps=stamp_tax_bps,
    )
    cash += float(trade["proceeds"])
    trades.append({k: v for k, v in trade.items() if k != "proceeds"})
    lot.shares -= float(trade["shares"])
    lot.capital -= float(trade["capital"])
    if event["reason"] == "take_profit_partial_30m":
        lot.take_profit_done = True
        remaining_bars30 = bars30[pd.to_datetime(bars30["datetime"]) > event["dt"]].copy()
        remaining_semantic = semantic_bars[pd.to_datetime(semantic_bars["datetime"]) > event["dt"]].copy()
        return _process_lot_exits(
            lot=lot,
            bars30=remaining_bars30,
            semantic_bars=remaining_semantic,
            cash=cash,
            trades=trades,
            prev_low=prev_low,
            profile=profile,
            gap_confirm_bars=gap_confirm_bars,
            sell_slippage_bps=sell_slippage_bps,
            commission_bps=commission_bps,
            stamp_tax_bps=stamp_tax_bps,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            take_profit_sell_ratio=take_profit_sell_ratio,
        )
    return None, cash


def _run_profile(
    signal_source: Path,
    output_dir: Path,
    profile: FillProfile,
    start_date: str,
    end_date: str,
    sort_mode: str = "trigger_time",
) -> Dict[str, Any]:
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

    codes = signals["code"].dropna().astype(str).unique().tolist()
    daily = _load_daily_ohlc(codes, start_date, end_date)
    price_map = {(row.code, row.trade_date): float(row.close) for row in daily.itertuples(index=False)}
    prev_low_map = {
        (row.code, row.trade_date): float(row.prev_low)
        for row in daily.itertuples(index=False)
        if row.prev_low is not None and np.isfinite(row.prev_low)
    }
    bars30 = _load_minute_bars(codes, start_date, end_date, 30)
    needs_15m = profile.intraday_mode == "bar_close_15m" or profile.gap_open_mode == "confirm_15m"
    bars15 = _load_minute_bars(codes, start_date, end_date, 15) if needs_15m else pd.DataFrame()
    bars30_by_code_date = {
        (code, date): g.sort_values("datetime").copy()
        for (code, date), g in bars30.groupby(["code", "bar_date"], sort=False)
    }
    bars15_by_code_date = (
        {
            (code, date): g.sort_values("datetime").copy()
            for (code, date), g in bars15.groupby(["code", "bar_date"], sort=False)
        }
        if not bars15.empty
        else {}
    )

    cash = 150000.0
    lots: List[Lot] = []
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []

    for date in trade_dates:
        next_lots: List[Lot] = []
        for lot in lots:
            day_bars30 = bars30_by_code_date.get((lot.code, date), pd.DataFrame())
            if not day_bars30.empty:
                day_bars30 = day_bars30[day_bars30["datetime"] > lot.buy_datetime].copy()
            if profile.intraday_mode == "bar_close_15m":
                semantic_bars = bars15_by_code_date.get((lot.code, date), pd.DataFrame())
                if not semantic_bars.empty:
                    semantic_bars = semantic_bars[semantic_bars["datetime"] > lot.buy_datetime].copy()
            else:
                semantic_bars = day_bars30
            if profile.gap_open_mode == "confirm_15m":
                gap_confirm_bars = bars15_by_code_date.get((lot.code, date), pd.DataFrame())
                if not gap_confirm_bars.empty:
                    gap_confirm_bars = gap_confirm_bars[gap_confirm_bars["datetime"] > lot.buy_datetime].copy()
            elif profile.gap_open_mode == "confirm_30m":
                gap_confirm_bars = day_bars30
            else:
                gap_confirm_bars = None
            lot_after, cash = _process_lot_exits(
                lot=lot,
                bars30=day_bars30,
                semantic_bars=semantic_bars,
                cash=cash,
                trades=trades,
                prev_low=prev_low_map.get((lot.code, date)),
                profile=profile,
                gap_confirm_bars=gap_confirm_bars,
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


def _profiles() -> list[FillProfile]:
    return [
        FillProfile("baseline_prev_low_fill", gap_open_mode="prev_low", intraday_mode="prev_low"),
        FillProfile("gap_open_at_open__intraday_prev_low", gap_open_mode="open", intraday_mode="prev_low"),
        FillProfile("gap_confirm_15m_close__intraday_prev_low", gap_open_mode="confirm_15m", intraday_mode="prev_low"),
        FillProfile("gap_confirm_30m_close__intraday_prev_low", gap_open_mode="confirm_30m", intraday_mode="prev_low"),
        FillProfile("gap_open_at_open__intraday_minus_0p5", gap_open_mode="open", intraday_mode="prev_low_minus_0p5"),
        FillProfile("gap_open_at_open__intraday_15m_close", gap_open_mode="open", intraday_mode="bar_close_15m"),
        FillProfile("gap_open_at_open__intraday_30m_close", gap_open_mode="open", intraday_mode="bar_close"),
        FillProfile("gap_confirm_30m_close__intraday_30m_close", gap_open_mode="confirm_30m", intraday_mode="bar_close"),
    ]


def _write_report(output_dir: Path, df: pd.DataFrame) -> None:
    lines = [
        "# G2 Prev-Low Exit Fill Compare",
        "",
        "Only the post-profit previous-day-low exit fill model changes. Entry, position sizing, stop-loss, take-profit and max holding days remain unchanged.",
        "",
        "| profile | trades | total | excess | max_dd | win | avg_trade | exits |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['profile']} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {row.get('exit_reason_counts') or ''} |"
        )
    (output_dir / "fill_compare_report.md").write_text("\n".join(lines), encoding="utf-8")


def run_compare(output_dir: Path, signal_source: Path, start_date: str, end_date: str) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for profile in _profiles():
        summary = _run_profile(
            signal_source=signal_source,
            output_dir=output_dir / "runs" / profile.name,
            profile=profile,
            start_date=start_date,
            end_date=end_date,
        )
        rows.append(
            {
                "profile": profile.name,
                "gap_open_mode": profile.gap_open_mode,
                "intraday_mode": profile.intraday_mode,
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
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    _write_report(output_dir, df)
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "report": "fill_compare_report.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare previous-day-low exit fill assumptions for G2 Attack V1.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    args = parser.parse_args()
    payload = run_compare(Path(args.output_dir), Path(args.signal_source), str(args.start_date), str(args.end_date))
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

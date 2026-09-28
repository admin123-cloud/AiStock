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
    SORT_MODES,
    _json_default,
    _load_benchmark,
    _load_signals,
    _load_trade_dates,
    _pct,
    _safe_price,
    _sort_signals,
)

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_open_v1_semantic_exits"


@dataclass(frozen=True)
class ExitProfile:
    name: str
    stop_mode: str = "pct5"
    use_ma20_break: bool = False
    use_prev_day_low_break: bool = False
    bear_body_pct: Optional[float] = None
    bear_volume_ratio: Optional[float] = None
    giveback_pct: Optional[float] = None
    min_peak_profit_pct: float = 0.15
    min_exit_profit_pct: float = 0.05


def _load_daily_ohlc(codes: List[str], start_date: str, end_date: str) -> pd.DataFrame:
    from utils.market_warehouse import clickhouse_client

    if not codes:
        return pd.DataFrame()
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, open, high, low, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["code", "trade_date", "close"]).reset_index(drop=True)
    df["prev_low"] = df.groupby("code")["low"].shift(1)
    return df


def _load_minute_bars(codes: List[str], start_date: str, end_date: str) -> pd.DataFrame:
    from utils.market_warehouse import clickhouse_client

    if not codes:
        return pd.DataFrame()
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, datetime, open, high, low, close, volume
        FROM kline_minute_30
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
    df = df.dropna(subset=["code", "datetime", "open", "high", "low", "close"]).reset_index(drop=True)
    df["ma20_30m"] = df.groupby("code")["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    df["volume_ma20_30m"] = df.groupby("code")["volume"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    return df


def _semantic_exit(
    lot: Lot,
    bar: Any,
    profile: ExitProfile,
    prev_low: Optional[float],
) -> tuple[Optional[str], Optional[float]]:
    if not lot.take_profit_done:
        return None, None

    low = float(getattr(bar, "low"))
    close = float(getattr(bar, "close"))
    open_px = float(getattr(bar, "open"))
    high = float(getattr(bar, "high"))
    volume = float(getattr(bar, "volume")) if getattr(bar, "volume") is not None else np.nan
    ma20 = getattr(bar, "ma20_30m")
    vol_ma20 = getattr(bar, "volume_ma20_30m")
    lot_peak = max(float(lot.peak_price), high)

    if profile.use_prev_day_low_break and prev_low is not None and np.isfinite(prev_low) and low <= float(prev_low):
        return "weak_prev_day_low_break_30m", float(prev_low)

    if profile.use_ma20_break and ma20 is not None and np.isfinite(ma20) and close < float(ma20):
        return "weak_ma20_break_30m", close

    if profile.bear_body_pct is not None and profile.bear_volume_ratio is not None:
        body_ret = close / open_px - 1.0 if open_px > 0 else 0.0
        vol_ratio = volume / float(vol_ma20) if vol_ma20 is not None and np.isfinite(vol_ma20) and float(vol_ma20) > 0 else np.nan
        if body_ret <= -abs(float(profile.bear_body_pct)) and np.isfinite(vol_ratio) and vol_ratio >= float(profile.bear_volume_ratio):
            return "weak_bear_volume_bar_30m", close

    if profile.giveback_pct is not None:
        min_peak = lot.buy_price * (1.0 + abs(float(profile.min_peak_profit_pct)))
        trigger = lot_peak * (1.0 - abs(float(profile.giveback_pct)))
        min_exit = lot.buy_price * (1.0 + abs(float(profile.min_exit_profit_pct)))
        if lot_peak >= min_peak and low <= trigger and trigger >= min_exit:
            return "weak_profit_giveback_30m", trigger

    return None, None


def _process_intraday_exits(
    lot: Lot,
    bars: pd.DataFrame,
    cash: float,
    trades: List[Dict[str, Any]],
    profile: ExitProfile,
    prev_low: Optional[float],
    fractal_low: Optional[float],
    sell_slippage_bps: float,
    commission_bps: float,
    stamp_tax_bps: float,
) -> tuple[Optional[Lot], float]:
    if bars.empty:
        return lot, cash

    if profile.stop_mode == "fractal_low" and fractal_low is not None and np.isfinite(fractal_low) and 0 < float(fractal_low) < lot.buy_price:
        stop_price = float(fractal_low)
        stop_reason = "stop_loss_fractal_low_30m"
    else:
        stop_price = lot.buy_price * 0.95
        stop_reason = "stop_loss_30m"
    take_price = lot.buy_price * 1.10

    for bar in bars.itertuples(index=False):
        dt = getattr(bar, "datetime")
        high = float(getattr(bar, "high"))
        low = float(getattr(bar, "low"))
        close = float(getattr(bar, "close"))
        lot.peak_price = max(lot.peak_price, high)

        exit_reason = ""
        exit_price: Optional[float] = None
        sell_ratio = 0.0

        if low <= stop_price:
            exit_reason = stop_reason
            exit_price = stop_price
            sell_ratio = 1.0
        elif not lot.take_profit_done and high >= take_price:
            exit_reason = "take_profit_partial_30m"
            exit_price = take_price
            sell_ratio = 0.5
        else:
            semantic_reason, semantic_price = _semantic_exit(lot, bar, profile, prev_low)
            if semantic_reason and semantic_price is not None:
                exit_reason = semantic_reason
                exit_price = semantic_price
                sell_ratio = 1.0

        if sell_ratio > 0 and exit_price is not None:
            trade = _sell_trade(
                lot=lot,
                sell_datetime=dt,
                raw_price=exit_price,
                ratio=sell_ratio,
                reason=exit_reason,
                sell_slippage_bps=sell_slippage_bps,
                commission_bps=commission_bps,
                stamp_tax_bps=stamp_tax_bps,
            )
            cash += float(trade["proceeds"])
            trades.append({k: v for k, v in trade.items() if k != "proceeds"})
            lot.shares -= float(trade["shares"])
            lot.capital -= float(trade["capital"])
            if exit_reason == "take_profit_partial_30m":
                lot.take_profit_done = True
                continue
            return None, cash

        lot.peak_price = max(lot.peak_price, close)

    return lot if lot.shares > 1e-9 and lot.capital > 1e-6 else None, cash


def _profiles() -> list[ExitProfile]:
    return [
        ExitProfile("hold10_no_semantic"),
        ExitProfile("weak_ma20", use_ma20_break=True),
        ExitProfile("weak_prev_low", use_prev_day_low_break=True),
        ExitProfile("weak_prev_low_stop_fractal", stop_mode="fractal_low", use_prev_day_low_break=True),
        ExitProfile("weak_bear_vol_mild", bear_body_pct=0.02, bear_volume_ratio=1.5),
        ExitProfile("weak_bear_vol_strict", bear_body_pct=0.03, bear_volume_ratio=1.8),
        ExitProfile(
            "weak_prev_low_or_bear_vol_strict",
            use_prev_day_low_break=True,
            bear_body_pct=0.03,
            bear_volume_ratio=1.8,
        ),
        ExitProfile("weak_giveback8", giveback_pct=0.08),
        ExitProfile("weak_giveback10", giveback_pct=0.10),
        ExitProfile("weak_giveback12", giveback_pct=0.12),
        ExitProfile(
            "weak_combo_balanced",
            use_ma20_break=True,
            use_prev_day_low_break=True,
            bear_body_pct=0.03,
            bear_volume_ratio=1.8,
            giveback_pct=0.10,
        ),
    ]


def _diagnostics(run_dir: Path) -> dict[str, Any]:
    trades = pd.read_csv(run_dir / "trades.csv")
    if trades.empty:
        return {}
    trades["pnl"] = pd.to_numeric(trades["pnl"], errors="coerce")
    trades["return"] = pd.to_numeric(trades["return"], errors="coerce")
    gross_profit = float(trades.loc[trades["pnl"] > 0, "pnl"].sum())
    top5_profit = float(trades.sort_values("pnl", ascending=False).head(5)["pnl"].sum())
    return {
        "median_trade_return": float(trades["return"].median()),
        "p90_trade_return": float(trades["return"].quantile(0.9)),
        "top5_profit_share": top5_profit / gross_profit if gross_profit > 0 else None,
        "exit_reason_counts": trades["exit_reason"].fillna("").astype(str).value_counts().to_dict(),
    }


def run_profile(
    signal_source: Path,
    output_dir: Path,
    profile: ExitProfile,
    start_date: str,
    end_date: str,
    hold_days: int,
    buy_slippage_bps: float = 5.0,
    sell_slippage_bps: float = 5.0,
    commission_bps: float = 2.5,
    stamp_tax_bps: float = 5.0,
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
    minute = _load_minute_bars(codes, start_date, end_date)
    minute_by_code_date = {
        (code, date): g.sort_values("datetime").copy()
        for (code, date), g in minute.groupby(["code", "bar_date"], sort=False)
    }

    cash = 150000.0
    lots: List[Lot] = []
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []

    for date in trade_dates:
        next_lots: List[Lot] = []
        for lot in lots:
            bars = minute_by_code_date.get((lot.code, date), pd.DataFrame())
            if not bars.empty:
                bars = bars[bars["datetime"] > lot.buy_datetime].copy()
            lot_after, cash = _process_intraday_exits(
                lot=lot,
                bars=bars,
                cash=cash,
                trades=trades,
                profile=profile,
                prev_low=prev_low_map.get((lot.code, date)),
                fractal_low=getattr(lot, "fractal_low", None),
                sell_slippage_bps=sell_slippage_bps,
                commission_bps=commission_bps,
                stamp_tax_bps=stamp_tax_bps,
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
                        sell_slippage_bps=sell_slippage_bps,
                        commission_bps=commission_bps,
                        stamp_tax_bps=stamp_tax_bps,
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
                    capital_each = min(cash, target_capital)
                    if capital_each <= 0:
                        break
                    raw_buy_price = float(getattr(row, "entry_price"))
                    buy_price = raw_buy_price * (1.0 + float(buy_slippage_bps) / 10000.0)
                    if buy_price <= 0 or not np.isfinite(buy_price):
                        continue
                    entry_idx = date_idx[date]
                    sell_idx = min(entry_idx + int(hold_days), len(trade_dates) - 1)
                    buy_fee = capital_each * float(commission_bps) / 10000.0
                    shares = (capital_each - buy_fee) / buy_price
                    cash -= capital_each
                    lot = Lot(
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
                    setattr(lot, "fractal_low", float(getattr(row, "fractal_low", np.nan)))
                    lots.append(lot)

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
        "risk_model": "semantic_post_profit_exit",
        "exit_profile": profile.__dict__,
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": 150000.0,
        "max_positions": 2,
        "max_buys_per_day": 1,
        "hold_days": int(hold_days),
        "buy_slippage_bps": float(buy_slippage_bps),
        "sell_slippage_bps": float(sell_slippage_bps),
        "commission_bps": float(commission_bps),
        "stamp_tax_bps": float(stamp_tax_bps),
        "sort_mode": "trigger_time",
        "max_position_weight": 0.50,
        "signal_count": int(len(signals)),
        "trade_count": int(len(trades_df)),
        "final_equity": float(curve_df["equity"].iloc[-1]) if not curve_df.empty else 150000.0,
        "total_return": total_return,
        "benchmark_code": "000852.SH",
        "benchmark_return": benchmark_return,
        "excess_return": None if benchmark_return is None else total_return - benchmark_return,
        "max_drawdown": float(curve_df["drawdown"].min()) if "drawdown" in curve_df.columns else 0.0,
        "win_rate": float((trades_df["return"] > 0).mean()) if not trades_df.empty else None,
        "avg_trade_return": float(trades_df["return"].mean()) if not trades_df.empty else None,
        "outputs": {"curve": "equity_curve.csv", "trades": "trades.csv", "signals": "signals.csv"},
    }
    signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
    curve_df.to_csv(output_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    return summary


def run_sweep(output_dir: Path, signal_source: Path, start_date: str, end_date: str, hold_days: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for profile in _profiles():
        run_dir = output_dir / "runs" / profile.name
        summary = run_profile(signal_source, run_dir, profile, start_date, end_date, hold_days)
        diag = _diagnostics(run_dir)
        rows.append(
            {
                "profile": profile.name,
                "hold_days": int(hold_days),
                "signal_count": summary.get("signal_count"),
                "trade_count": summary.get("trade_count"),
                "total_return": summary.get("total_return"),
                "benchmark_return": summary.get("benchmark_return"),
                "excess_return": summary.get("excess_return"),
                "max_drawdown": summary.get("max_drawdown"),
                "win_rate": summary.get("win_rate"),
                "avg_trade_return": summary.get("avg_trade_return"),
                "median_trade_return": diag.get("median_trade_return"),
                "p90_trade_return": diag.get("p90_trade_return"),
                "top5_profit_share": diag.get("top5_profit_share"),
                "exit_reason_counts": json.dumps(diag.get("exit_reason_counts", {}), ensure_ascii=False, sort_keys=True),
                "final_equity": summary.get("final_equity"),
            }
        )

    df = pd.DataFrame(rows)
    df["semantic_score"] = (
        df["excess_return"].fillna(0.0)
        + df["total_return"].fillna(0.0) * 0.25
        + df["max_drawdown"].fillna(0.0) * 0.75
        + df["p90_trade_return"].fillna(0.0) * 0.20
    )
    df = df.sort_values(["semantic_score", "total_return"], ascending=[False, False])
    df.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    payload = {
        "schema_version": 1,
        "start_date": start_date,
        "end_date": end_date,
        "hold_days": int(hold_days),
        "rows": df.where(pd.notna(df), None).to_dict("records"),
        "outputs": {"summary": "summary.csv", "conclusion": "semantic_exit_conclusion.md", "runs": "runs/"},
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_conclusion(output_dir, df)
    return payload


def _write_conclusion(output_dir: Path, df: pd.DataFrame) -> None:
    lines = [
        "# G2 Open V1 Semantic Exit Sweep",
        "",
        "All semantic exits are post-profit exits: they only become active after +10% half take-profit. Before that, only the 30m -5% hard stop is active.",
        "",
        "| profile | trades | total | excess | max_dd | win | avg | median | p90 | top5_profit_share | exits |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in df.to_dict("records"):
        lines.append(
            f"| {row['profile']} | {int(row.get('trade_count') or 0)} | {_pct(row.get('total_return'))} | {_pct(row.get('excess_return'))} | {_pct(row.get('max_drawdown'))} | {_pct(row.get('win_rate'))} | {_pct(row.get('avg_trade_return'))} | {_pct(row.get('median_trade_return'))} | {_pct(row.get('p90_trade_return'))} | {_pct(row.get('top5_profit_share'))} | {row.get('exit_reason_counts') or ''} |"
        )
    (output_dir / "semantic_exit_conclusion.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep semantic post-profit exits for G2 Open V1.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--hold-days", type=int, default=10)
    args = parser.parse_args()
    payload = run_sweep(
        output_dir=Path(args.output_dir),
        signal_source=Path(args.signal_source),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        hold_days=int(args.hold_days),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

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

DEFAULT_SIGNAL_SOURCE = REPO_ROOT / "reports" / "gen2_30m_fractal_restart_expanded_w2" / "fractal_triggers.parquet"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "gen2_open_v1_portfolio_backtest"
SORT_MODES = {"rank", "score", "trigger_time", "volume_ratio", "g2_v2"}


@dataclass
class Lot:
    code: str
    name: str
    buy_date: str
    sell_date: str
    buy_price: float
    shares: float
    capital: float
    v4_rank: int
    v4_score: float
    peak_price: float
    take_profit_done: bool = False


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if pd.isna(value):
        return None
    return str(value)


def _load_signals(path: Path, start_date: str, end_date: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    rule = GEN2_OPEN_RULE_V1
    d = df[
        (df["pattern"] == rule["pattern"])
        & (df["g2_open_state"] == rule["g2_open_state"])
        & (df["trigger_type"] == rule["trigger_type"])
    ].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.strftime("%Y-%m-%d")
    d = d[(d["entry_date"] >= start_date) & (d["entry_date"] <= end_date)].copy()
    d["v4_rank"] = pd.to_numeric(d["v4_rank"], errors="coerce").fillna(999).astype(int)
    d["v4_score"] = pd.to_numeric(d["v4_score"], errors="coerce").fillna(0.0)
    d["entry_price"] = pd.to_numeric(d["entry_price"], errors="coerce")
    d["confirm_datetime"] = pd.to_datetime(d.get("confirm_datetime"), errors="coerce")
    d["confirm_amount"] = pd.to_numeric(d.get("confirm_amount"), errors="coerce")
    d["confirm_amount_ma5_prev"] = pd.to_numeric(d.get("confirm_amount_ma5_prev"), errors="coerce")
    d["volume_ratio"] = np.where(
        d["confirm_amount_ma5_prev"] > 0,
        d["confirm_amount"] / d["confirm_amount_ma5_prev"],
        np.nan,
    )
    d = d.dropna(subset=["entry_date", "code", "entry_price"])
    return _sort_signals(d, "rank").reset_index(drop=True)


def _sort_signals(signals: pd.DataFrame, sort_mode: str) -> pd.DataFrame:
    mode = sort_mode if sort_mode in SORT_MODES else "rank"
    d = signals.copy()
    if mode == "g2_v2":
        if "g2_v2_family_priority" not in d.columns:
            d["g2_v2_family_priority"] = 99
        cols = ["entry_date", "g2_v2_family_priority", "v4_score", "v4_rank", "confirm_datetime", "code"]
        asc = [True, True, False, True, True, True]
    elif mode == "score":
        cols = ["entry_date", "v4_score", "v4_rank", "confirm_datetime", "code"]
        asc = [True, False, True, True, True]
    elif mode == "trigger_time":
        cols = ["entry_date", "confirm_datetime", "v4_rank", "v4_score", "code"]
        asc = [True, True, True, False, True]
    elif mode == "volume_ratio":
        cols = ["entry_date", "volume_ratio", "v4_rank", "v4_score", "code"]
        asc = [True, False, True, False, True]
    else:
        cols = ["entry_date", "v4_rank", "v4_score", "confirm_datetime", "code"]
        asc = [True, True, False, True, True]
    return d.sort_values(cols, ascending=asc, na_position="last")


def _load_daily_prices(codes: List[str], start_date: str, end_date: str) -> pd.DataFrame:
    from utils.market_warehouse import clickhouse_client

    if not codes:
        return pd.DataFrame()
    quoted = ", ".join([f"'{code}'" for code in sorted(set(codes))])
    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT code, trade_date, close
        FROM kline_daily
        WHERE code IN ({quoted})
          AND trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY code, trade_date
        """
    )
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df.dropna(subset=["code", "trade_date", "close"]).reset_index(drop=True)


def _load_trade_dates(start_date: str, end_date: str) -> List[str]:
    from utils.market_warehouse import clickhouse_client

    ch = clickhouse_client()
    df = ch.query_df(
        f"""
        SELECT DISTINCT trade_date
        FROM kline_daily
        WHERE trade_date BETWEEN '{start_date}' AND '{end_date}'
        ORDER BY trade_date
        """
    )
    return [pd.Timestamp(x).strftime("%Y-%m-%d") for x in df["trade_date"].tolist()]


def _load_benchmark(start_date: str, end_date: str, benchmark_code: str) -> pd.DataFrame:
    daily = _load_daily_prices([benchmark_code], start_date, end_date)
    if daily.empty:
        return pd.DataFrame(columns=["date", "benchmark_equity"])
    d = daily.rename(columns={"trade_date": "date"}).copy()
    first = float(d["close"].iloc[0])
    d["benchmark_equity"] = d["close"] / first if first > 0 else np.nan
    return d[["date", "benchmark_equity"]]


def _safe_price(price_map: Dict[tuple[str, str], float], code: str, date: str) -> Optional[float]:
    value = price_map.get((code, date))
    if value is None or not np.isfinite(value) or value <= 0:
        return None
    return float(value)


def _sell_lot(
    lot: Lot,
    date: str,
    raw_price: float,
    ratio: float,
    reason: str,
    sell_slippage_bps: float,
    commission_bps: float,
    stamp_tax_bps: float,
) -> Dict[str, Any]:
    sell_ratio = max(0.0, min(float(ratio), 1.0))
    shares = lot.shares * sell_ratio
    capital = lot.capital * sell_ratio
    sell_price = float(raw_price) * (1.0 - float(sell_slippage_bps) / 10000.0)
    gross = shares * sell_price
    sell_fee = gross * (float(commission_bps) + float(stamp_tax_bps)) / 10000.0
    proceeds = gross - sell_fee
    return {
        "code": lot.code,
        "name": lot.name,
        "buy_date": lot.buy_date,
        "sell_date": date,
        "buy_price": lot.buy_price,
        "sell_price": sell_price,
        "raw_sell_price": float(raw_price),
        "sell_ratio": sell_ratio,
        "shares": shares,
        "capital": capital,
        "sell_fee": sell_fee,
        "pnl": proceeds - capital,
        "return": proceeds / capital - 1.0 if capital > 0 else np.nan,
        "exit_reason": reason,
        "v4_rank": lot.v4_rank,
        "v4_score": lot.v4_score,
        "proceeds": proceeds,
    }


def run(
    signal_source: Path,
    output_dir: Path,
    start_date: str,
    end_date: str,
    initial_cash: float,
    max_positions: int,
    max_buys_per_day: int,
    hold_days: int,
    benchmark_code: str,
    buy_slippage_bps: float = 5.0,
    sell_slippage_bps: float = 5.0,
    commission_bps: float = 2.5,
    stamp_tax_bps: float = 5.0,
    stop_loss_pct: Optional[float] = None,
    take_profit_pct: Optional[float] = None,
    take_profit_sell_ratio: float = 0.5,
    trailing_stop_pct: Optional[float] = None,
    sort_mode: str = "rank",
    max_position_weight: float = 0.0,
) -> Dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = _load_signals(signal_source, start_date, end_date)
    trade_dates = _load_trade_dates(start_date, end_date)
    if not trade_dates:
        raise RuntimeError("No trade dates available for backtest.")

    date_idx = {date: i for i, date in enumerate(trade_dates)}
    signals = signals[signals["entry_date"].isin(date_idx)].copy()
    signals = _sort_signals(signals, sort_mode).reset_index(drop=True)
    signals_by_date = {date: g.copy() for date, g in signals.groupby("entry_date", sort=True)}
    prices = _load_daily_prices(signals["code"].dropna().astype(str).unique().tolist(), start_date, end_date)
    price_map = {(row.code, row.trade_date): float(row.close) for row in prices.itertuples(index=False)}

    cash = float(initial_cash)
    lots: List[Lot] = []
    trades: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []

    for date in trade_dates:
        next_lots: List[Lot] = []
        for lot in lots:
            close_price = _safe_price(price_map, lot.code, date)
            if close_price is None:
                next_lots.append(lot)
                continue

            lot.peak_price = max(float(lot.peak_price), close_price)
            current_return = close_price / lot.buy_price - 1.0
            drawdown_from_peak = close_price / lot.peak_price - 1.0 if lot.peak_price > 0 else 0.0
            exit_reason = ""
            sell_ratio = 0.0
            if stop_loss_pct is not None and current_return <= -abs(float(stop_loss_pct)):
                exit_reason = "stop_loss"
                sell_ratio = 1.0
            elif (
                take_profit_pct is not None
                and not lot.take_profit_done
                and current_return >= abs(float(take_profit_pct))
                and 0 < float(take_profit_sell_ratio) < 1
            ):
                exit_reason = "take_profit_partial"
                sell_ratio = float(take_profit_sell_ratio)
            elif (
                trailing_stop_pct is not None
                and lot.take_profit_done
                and drawdown_from_peak <= -abs(float(trailing_stop_pct))
            ):
                exit_reason = "trailing_stop"
                sell_ratio = 1.0
            elif date >= lot.sell_date:
                exit_reason = "time_exit"
                sell_ratio = 1.0

            if sell_ratio > 0:
                trade = _sell_lot(
                    lot=lot,
                    date=date,
                    raw_price=close_price,
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
                if exit_reason == "take_profit_partial":
                    lot.take_profit_done = True
                if lot.shares > 1e-9 and lot.capital > 1e-6:
                    next_lots.append(lot)
            else:
                next_lots.append(lot)
        lots = next_lots

        today = signals_by_date.get(date)
        if today is not None and not today.empty:
            held_codes = {lot.code for lot in lots}
            slots = max(0, int(max_positions) - len(lots))
            buy_count = min(slots, int(max_buys_per_day))
            if buy_count > 0 and cash > 0:
                candidates = today[~today["code"].isin(held_codes)].head(buy_count)
                sizing_market_value = 0.0
                for lot in lots:
                    close = _safe_price(price_map, lot.code, date) or lot.buy_price
                    sizing_market_value += lot.shares * close
                sizing_equity = cash + sizing_market_value
                target_capital = sizing_equity / float(max_positions) if int(max_positions) > 0 else cash
                if float(max_position_weight) > 0:
                    target_capital = min(target_capital, sizing_equity * float(max_position_weight))
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
                    sell_date = trade_dates[sell_idx]
                    buy_fee = capital_each * float(commission_bps) / 10000.0
                    shares = (capital_each - buy_fee) / buy_price
                    cash -= capital_each
                    lots.append(
                        Lot(
                            code=str(getattr(row, "code")),
                            name=str(getattr(row, "name")),
                            buy_date=date,
                            sell_date=sell_date,
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
        curve.append(
            {
                "date": date,
                "cash": cash,
                "market_value": market_value,
                "equity": equity,
                "holding_count": len(lots),
            }
        )

    curve_df = pd.DataFrame(curve)
    trades_df = pd.DataFrame(trades)
    if not curve_df.empty:
        curve_df["strategy_equity"] = curve_df["equity"] / float(initial_cash)
        curve_df["peak"] = curve_df["strategy_equity"].cummax()
        curve_df["drawdown"] = curve_df["strategy_equity"] / curve_df["peak"] - 1.0
    benchmark = _load_benchmark(start_date, end_date, benchmark_code)
    if not benchmark.empty:
        curve_df = curve_df.merge(benchmark, on="date", how="left")

    total_return = float(curve_df["strategy_equity"].iloc[-1] - 1.0) if not curve_df.empty else 0.0
    max_drawdown = float(curve_df["drawdown"].min()) if "drawdown" in curve_df.columns else 0.0
    win_rate = float((trades_df["return"] > 0).mean()) if not trades_df.empty else None
    avg_trade_return = float(trades_df["return"].mean()) if not trades_df.empty else None
    benchmark_return = (
        float(curve_df["benchmark_equity"].dropna().iloc[-1] - 1.0)
        if "benchmark_equity" in curve_df.columns and not curve_df["benchmark_equity"].dropna().empty
        else None
    )
    summary = {
        "schema_version": 1,
        "rule": GEN2_OPEN_RULE_V1,
        "start_date": start_date,
        "end_date": end_date,
        "initial_cash": float(initial_cash),
        "max_positions": int(max_positions),
        "max_buys_per_day": int(max_buys_per_day),
        "hold_days": int(hold_days),
        "sort_mode": sort_mode if sort_mode in SORT_MODES else "rank",
        "max_position_weight": float(max_position_weight),
        "signal_count": int(len(signals)),
        "trade_count": int(len(trades_df)),
        "final_equity": float(curve_df["equity"].iloc[-1]) if not curve_df.empty else float(initial_cash),
        "total_return": total_return,
        "benchmark_code": benchmark_code,
        "buy_slippage_bps": float(buy_slippage_bps),
        "sell_slippage_bps": float(sell_slippage_bps),
        "commission_bps": float(commission_bps),
        "stamp_tax_bps": float(stamp_tax_bps),
        "stop_loss_pct": None if stop_loss_pct is None else float(stop_loss_pct),
        "take_profit_pct": None if take_profit_pct is None else float(take_profit_pct),
        "take_profit_sell_ratio": float(take_profit_sell_ratio),
        "trailing_stop_pct": None if trailing_stop_pct is None else float(trailing_stop_pct),
        "benchmark_return": benchmark_return,
        "excess_return": None if benchmark_return is None else total_return - benchmark_return,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "avg_trade_return": avg_trade_return,
        "outputs": {
            "curve": "equity_curve.csv",
            "trades": "trades.csv",
            "signals": "signals.csv",
            "findings": "findings.md",
        },
    }

    signals.to_csv(output_dir / "signals.csv", index=False, encoding="utf-8-sig")
    curve_df.to_csv(output_dir / "equity_curve.csv", index=False, encoding="utf-8-sig")
    trades_df.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8-sig")
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    _write_findings(output_dir, summary)
    return summary


def _pct(value: Any) -> str:
    if value is None:
        return ""
    return f"{float(value) * 100:.2f}%"


def _write_findings(output_dir: Path, summary: Dict[str, Any]) -> None:
    lines = [
        "# G2 Open V1 Portfolio Backtest",
        "",
        f"Window: {summary['start_date']} to {summary['end_date']}",
        f"Rule: `{summary['rule']['rule_id']}`",
        "",
        "## Summary",
        "",
        f"- Total return: {_pct(summary.get('total_return'))}",
        f"- Benchmark return ({summary.get('benchmark_code')}): {_pct(summary.get('benchmark_return'))}",
        f"- Excess return: {_pct(summary.get('excess_return'))}",
        f"- Max drawdown: {_pct(summary.get('max_drawdown'))}",
        f"- Trades: {summary.get('trade_count')}",
        f"- Win rate: {_pct(summary.get('win_rate'))}",
        f"- Avg trade return: {_pct(summary.get('avg_trade_return'))}",
        "",
        "## Parameters",
        "",
        f"- Initial cash: {summary.get('initial_cash')}",
        f"- Max positions: {summary.get('max_positions')}",
        f"- Max buys per day: {summary.get('max_buys_per_day')}",
        f"- Hold days: {summary.get('hold_days')}",
        f"- Sort mode: {summary.get('sort_mode')}",
        f"- Max position weight: {_pct(summary.get('max_position_weight')) if summary.get('max_position_weight') else ''}",
        f"- Buy slippage bps: {summary.get('buy_slippage_bps')}",
        f"- Sell slippage bps: {summary.get('sell_slippage_bps')}",
        f"- Commission bps: {summary.get('commission_bps')}",
        f"- Stamp tax bps: {summary.get('stamp_tax_bps')}",
        f"- Stop loss: {_pct(summary.get('stop_loss_pct'))}",
        f"- Take profit: {_pct(summary.get('take_profit_pct'))}",
        f"- Take profit sell ratio: {_pct(summary.get('take_profit_sell_ratio'))}",
        f"- Trailing stop: {_pct(summary.get('trailing_stop_pct'))}",
    ]
    (output_dir / "findings.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest G2 Open V1 fixed-hold portfolio.")
    parser.add_argument("--signal-source", default=str(DEFAULT_SIGNAL_SOURCE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--start-date", default="2024-07-09")
    parser.add_argument("--end-date", default="2026-05-21")
    parser.add_argument("--initial-cash", type=float, default=150000.0)
    parser.add_argument("--max-positions", type=int, default=3)
    parser.add_argument("--max-buys-per-day", type=int, default=3)
    parser.add_argument("--hold-days", type=int, default=3)
    parser.add_argument("--benchmark-code", default="000852.SH")
    parser.add_argument("--sort-mode", default="rank", choices=sorted(SORT_MODES))
    parser.add_argument("--max-position-weight", type=float, default=0.0)
    parser.add_argument("--buy-slippage-bps", type=float, default=5.0)
    parser.add_argument("--sell-slippage-bps", type=float, default=5.0)
    parser.add_argument("--commission-bps", type=float, default=2.5)
    parser.add_argument("--stamp-tax-bps", type=float, default=5.0)
    parser.add_argument("--stop-loss-pct", type=float, default=None)
    parser.add_argument("--take-profit-pct", type=float, default=None)
    parser.add_argument("--take-profit-sell-ratio", type=float, default=0.5)
    parser.add_argument("--trailing-stop-pct", type=float, default=None)
    args = parser.parse_args()
    payload = run(
        signal_source=Path(args.signal_source),
        output_dir=Path(args.output_dir),
        start_date=str(args.start_date),
        end_date=str(args.end_date),
        initial_cash=float(args.initial_cash),
        max_positions=int(args.max_positions),
        max_buys_per_day=int(args.max_buys_per_day),
        hold_days=int(args.hold_days),
        benchmark_code=str(args.benchmark_code),
        buy_slippage_bps=float(args.buy_slippage_bps),
        sell_slippage_bps=float(args.sell_slippage_bps),
        commission_bps=float(args.commission_bps),
        stamp_tax_bps=float(args.stamp_tax_bps),
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        take_profit_sell_ratio=float(args.take_profit_sell_ratio),
        trailing_stop_pct=args.trailing_stop_pct,
        sort_mode=str(args.sort_mode),
        max_position_weight=float(args.max_position_weight),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()

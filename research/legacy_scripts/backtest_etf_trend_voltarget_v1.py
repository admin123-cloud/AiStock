"""Research-only executable ETF trend/volatility-target backtest."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import report_path


def _download(symbol: str, start: str, end: str, auto_adjust: bool) -> pd.DataFrame:
    data = yf.download(symbol, start=start, end=end, auto_adjust=auto_adjust, progress=False, threads=False)
    if data.empty:
        raise RuntimeError(f"no yfinance data for {symbol}")
    data.columns = [c[0] if isinstance(c, tuple) else c for c in data.columns]
    data = data.reset_index()
    data["trade_date"] = pd.to_datetime(data["Date"]).dt.tz_localize(None)
    data = data.rename(columns={"Open": "open", "Close": "close"})
    return data[["trade_date", "open", "close"]].dropna().sort_values("trade_date")


def run(args: argparse.Namespace) -> dict:
    rows = []
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for symbol in [x.strip() for x in args.symbols.split(",") if x.strip()]:
        d = _download(symbol, args.start, args.end, args.auto_adjust)
        d["entry_to_next_open"] = d["open"].shift(-1) / d["open"] - 1.0
        d["realized_vol"] = d["entry_to_next_open"].rolling(args.vol_window).std().shift(1) * np.sqrt(252.0)
        d["signal"] = (
            d["close"].rolling(args.fast).mean() > d["close"].rolling(args.slow).mean()
        ).shift(1).fillna(False)
        scale = (args.target_vol / d["realized_vol"]).clip(upper=1.0).fillna(0.0)
        change = d["signal"].astype(int).diff().abs().fillna(0.0)
        # Charge a switch using the larger of the outgoing and incoming
        # exposure so an exit is not under-costed when volatility changes.
        active_scale = scale.where(d["signal"], 0.0)
        trade_scale = pd.concat([active_scale, active_scale.shift(1)], axis=1).max(axis=1).fillna(0.0)
        d["position_scale"] = active_scale
        d["strategy_return"] = d["entry_to_next_open"].fillna(0.0) * active_scale
        d["strategy_return"] -= args.cost_bps / 10000.0 * change * trade_scale
        d["symbol"] = symbol
        d.to_parquet(output / f"{symbol.replace('.', '_')}.parquet", index=False)
        equity = (1.0 + d["strategy_return"]).cumprod()
        row = {
            "symbol": symbol,
            "rows": int(len(d)),
            "start": str(d.trade_date.min().date()),
            "end": str(d.trade_date.max().date()),
            "total_return": float(equity.iloc[-1] - 1.0),
            "max_drawdown": float((equity / equity.cummax() - 1.0).min()),
            "timing_valid": True,
            "execution_valid": False,
            "price_basis": "yfinance_auto_adjusted_ohlc" if args.auto_adjust else "yfinance_raw_ohlc",
        }
        for name, lo, hi in [
            ("train_2013_2019", "2013-01-01", "2019-12-31"),
            ("validation_2020_2023", "2020-01-01", "2023-12-31"),
            ("blind_2024_2026", "2024-01-01", "2026-09-24"),
        ]:
            part = d[(d.trade_date >= lo) & (d.trade_date <= hi)]["strategy_return"]
            part_equity = (1.0 + part).cumprod()
            row[f"{name}_return"] = float(part_equity.iloc[-1] - 1.0) if len(part_equity) else None
            row[f"{name}_max_drawdown"] = float((part_equity / part_equity.cummax() - 1.0).min()) if len(part_equity) else None
        rows.append(row)
    summary = {
        "research_only": True,
        "timing_valid": True,
        "execution_valid": False,
        "timing": "prior_close_signal_next_open_entry_following_open_exit",
        "fast": args.fast,
        "slow": args.slow,
        "target_vol": args.target_vol,
        "vol_window": args.vol_window,
        "cost_bps": args.cost_bps,
        "auto_adjust": args.auto_adjust,
        "rows": rows,
        "promotion_status": "research_shadow_only",
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS")
    parser.add_argument("--start", default="2013-01-01")
    parser.add_argument("--end", default="2026-09-25")
    parser.add_argument("--fast", type=int, default=60)
    parser.add_argument("--slow", type=int, default=250)
    parser.add_argument("--vol-window", type=int, default=20)
    parser.add_argument("--target-vol", type=float, default=0.10)
    parser.add_argument("--cost-bps", type=float, default=100.0)
    parser.add_argument("--raw", dest="auto_adjust", action="store_false", help="use raw executable OHLC instead of adjusted OHLC")
    parser.set_defaults(auto_adjust=True)
    parser.add_argument("--output-dir", default=str(report_path("etf_trend_voltarget_shadow_v1")))
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2))

"""Record ETF trend strategy observations without placing orders."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
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
    return data[["trade_date", "open", "close", "Volume"]].rename(columns={"Volume": "volume"}).dropna().sort_values("trade_date")


def _observation(d: pd.DataFrame, fast: int, slow: int, target_vol: float) -> dict:
    d = d.copy()
    d["open_to_open"] = d["open"].pct_change()
    rv = d["open_to_open"].rolling(20).std().iloc[-1] * np.sqrt(252.0)
    fast_ma = d["close"].rolling(fast).mean().iloc[-1]
    slow_ma = d["close"].rolling(slow).mean().iloc[-1]
    signal = bool(pd.notna(fast_ma) and pd.notna(slow_ma) and fast_ma > slow_ma)
    scale = float(min(target_vol / rv, 1.0)) if pd.notna(rv) and rv > 0 else 0.0
    traded_value = (d["close"] * d["volume"]).rolling(20).median().iloc[-1]
    return {
        "signal": signal,
        "position_scale": scale if signal else 0.0,
        "realized_vol": float(rv) if pd.notna(rv) else None,
        "fast_ma": float(fast_ma) if pd.notna(fast_ma) else None,
        "slow_ma": float(slow_ma) if pd.notna(slow_ma) else None,
        "median_20d_traded_value": float(traded_value) if pd.notna(traded_value) else None,
        "liquidity_gate": bool(pd.notna(traded_value) and traded_value >= 100_000_000),
    }


def run(args: argparse.Namespace) -> dict:
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    ledger = Path(args.ledger)
    history = []
    if ledger.exists():
        history = json.loads(ledger.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            raise ValueError("paper ledger must be a JSON list")
    prior = history[-1] if history else None
    observations = {}
    as_of = None
    for symbol in symbols:
        d = _download(symbol, args.start, args.end, args.auto_adjust)
        if d.empty:
            raise RuntimeError(f"empty data for {symbol}")
        latest = str(d["trade_date"].iloc[-1].date())
        as_of = latest if as_of is None else min(as_of, latest)
        v2 = _observation(d, 60, 250, args.target_vol)
        v3 = _observation(d, 40, 250, args.target_vol)
        prior_v4 = (((prior or {}).get("observations") or {}).get(symbol) or {}).get("v4_ma40_250_entry_fixed") or {}
        if v3["signal"]:
            held_scale = prior_v4.get("held_position_scale", 0.0) if prior_v4.get("signal") else v3["position_scale"]
        else:
            held_scale = 0.0
        v4 = dict(v3)
        v4["entry_scale_if_new"] = v3["position_scale"]
        v4["held_position_scale"] = float(held_scale)
        v4["action"] = "hold" if v3["signal"] and prior_v4.get("signal") else ("enter" if v3["signal"] else "exit_or_cash")
        observations[symbol] = {
            "v2_ma60_250": v2,
            "v3_ma40_250": v3,
            "v4_ma40_250_entry_fixed": v4,
            "rows": int(len(d)),
            "latest_source_date": latest,
        }
    record = {
        "as_of": as_of,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "execution_valid": False,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "price_basis": "yfinance_auto_adjusted_ohlc" if args.auto_adjust else "yfinance_raw_ohlc",
        "target_vol": args.target_vol,
        "symbols": symbols,
        "observations": observations,
    }
    ledger.parent.mkdir(parents=True, exist_ok=True)
    history = [x for x in history if x.get("as_of") != as_of]
    history.append(record)
    history.sort(key=lambda x: x.get("as_of", ""))
    ledger.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS,511010.SS,518880.SS")
    p.add_argument("--start", default="2013-01-01")
    p.add_argument("--end", default="2026-09-25")
    p.add_argument("--target-vol", type=float, default=0.10)
    p.add_argument("--raw", dest="auto_adjust", action="store_false", help="use raw executable OHLC")
    p.set_defaults(auto_adjust=False)
    p.add_argument("--ledger", default=str(report_path("etf_trend_voltarget_forward_paper_v1", "ledger.json")))
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

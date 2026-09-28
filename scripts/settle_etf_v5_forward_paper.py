"""Settle one-period returns for v5 paper observations when future opens exist."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import yfinance as yf


def _opens(symbol: str, start: str, end: str) -> pd.Series:
    d = yf.download(symbol, start=start, end=end, auto_adjust=False, progress=False, threads=False)
    if d.empty:
        raise RuntimeError(f"no data for {symbol}")
    d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
    idx = pd.to_datetime(d.index).tz_localize(None)
    return pd.Series(d["Open"].to_numpy(dtype=float), index=idx).sort_index()


def run(args: argparse.Namespace) -> dict:
    ledger = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
    symbols = ledger[0]["symbols"] if ledger else []
    price = {s: _opens(s, args.start, args.end) for s in symbols}
    settlements = []
    for record in ledger:
        as_of = pd.Timestamp(record["as_of"])
        row = {"as_of": record["as_of"], "status": "settled", "symbols": {}}
        for symbol in symbols:
            dates = price[symbol].index
            future = dates[dates > as_of]
            obs = record["observations"][symbol]["v4_ma40_250_entry_fixed"]
            item = {"action": obs.get("action"), "held_position_scale": obs.get("held_position_scale", 0.0)}
            if obs.get("action") in {"exit_or_cash", "cash"} or float(obs.get("held_position_scale", 0.0)) <= 0:
                item["status"] = "no_position"
            elif len(future) < 2:
                row["status"] = "pending_future_open"
                item["status"] = "pending"
            else:
                entry_date, exit_date = future[0], future[1]
                item.update({
                    "status": "settled",
                    "entry_date": str(entry_date.date()),
                    "exit_date": str(exit_date.date()),
                    "entry_open": float(price[symbol].loc[entry_date]),
                    "exit_open": float(price[symbol].loc[exit_date]),
                    "gross_open_to_open_return": float(price[symbol].loc[exit_date] / price[symbol].loc[entry_date] - 1.0),
                })
            row["symbols"][symbol] = item
        settlements.append(row)
    out = {"research_only": True, "execution_valid": False, "price_basis": "yfinance_raw_ohlc", "settlements": settlements}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ledger", required=True)
    p.add_argument("--start", default="2026-09-01")
    p.add_argument("--end", default="2026-09-29")
    p.add_argument("--output", required=True)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

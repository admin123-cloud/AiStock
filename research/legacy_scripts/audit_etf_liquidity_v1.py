"""Audit simple ETF traded-value liquidity gates from raw Yahoo OHLCV."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yfinance as yf


def run(args: argparse.Namespace) -> dict:
    rows = []
    for symbol in [x.strip() for x in args.symbols.split(",") if x.strip()]:
        d = yf.download(symbol, start=args.start, end=args.end, auto_adjust=False, progress=False, threads=False)
        if d.empty:
            raise RuntimeError(f"no data for {symbol}")
        d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
        value = (d["Close"] * d["Volume"]).dropna()
        rows.append({
            "symbol": symbol,
            "days": int(len(value)),
            "median_daily_value": float(value.median()),
            "p10_daily_value": float(value.quantile(0.10)),
            "latest_daily_value": float(value.iloc[-1]),
            "passes_p10_gate": bool(value.quantile(0.10) >= args.min_p10_value),
        })
    result = {"research_only": True, "price_basis": "raw_close_times_raw_volume", "min_p10_value": args.min_p10_value, "rows": rows}
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS,511010.SS,518880.SS")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2026-09-25")
    p.add_argument("--min-p10-value", type=float, default=100_000_000.0)
    p.add_argument("--output", required=True)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

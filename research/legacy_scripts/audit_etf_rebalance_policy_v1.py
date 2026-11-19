"""Audit lower-turnover ETF volatility-target position policies."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _one(path: Path, fast: int, slow: int, target: float, policy: str, cost_bps: float) -> pd.Series:
    d = pd.read_parquet(path, columns=["trade_date", "open", "close"]).sort_values("trade_date").set_index("trade_date")
    oo = d["open"].shift(-1) / d["open"] - 1.0
    rv = oo.rolling(20).std().shift(1) * np.sqrt(252.0)
    signal = (d["close"].rolling(fast).mean() > d["close"].rolling(slow).mean()).shift(1).fillna(False)
    desired = (target / rv).clip(upper=1.0).fillna(0.0).where(signal, 0.0)
    if policy == "daily":
        pos = desired
    elif policy == "weekly":
        pos = desired.where((np.arange(len(d)) % 5 == 0) | signal.ne(signal.shift(1)), np.nan).ffill().where(signal, 0.0)
    elif policy == "entry_fixed":
        pos = pd.Series(0.0, index=d.index)
        previous = False
        held = 0.0
        for idx, active in signal.items():
            if not active:
                held = 0.0
            elif not previous:
                held = float(desired.loc[idx])
            pos.loc[idx] = held
            previous = bool(active)
    else:
        raise ValueError(policy)
    turnover = pos.diff().abs().fillna(pos.abs())
    return oo.fillna(0.0) * pos - cost_bps / 10000.0 * turnover


def _metrics(s: pd.Series) -> dict:
    eq = (1.0 + s).cumprod()
    return {"return": float(eq.iloc[-1] - 1.0), "max_drawdown": float((eq / eq.cummax() - 1.0).min())}


def run(args: argparse.Namespace) -> dict:
    root = Path(args.input_dir)
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    paths = [root / f"{s.replace('.', '_')}.parquet" for s in symbols]
    rows = []
    for policy in ["daily", "weekly", "entry_fixed"]:
        basket = pd.concat([_one(p, args.fast, args.slow, args.target_vol, policy, args.cost_bps) for p in paths], axis=1).fillna(0.0).mean(axis=1)
        row = {"policy": policy, "fast": args.fast, "slow": args.slow, "target_vol": args.target_vol, "cost_bps": args.cost_bps}
        for name, lo, hi in [("full", "2013-01-01", "2026-09-24"), ("validation_2020_2023", "2020-01-01", "2023-12-31"), ("blind_2024_2026", "2024-01-01", "2026-09-24")]:
            row[name] = _metrics(basket[(basket.index >= lo) & (basket.index <= hi)])
        rows.append(row)
    out = {"research_only": True, "symbols": symbols, "results": rows}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS")
    p.add_argument("--fast", type=int, default=40)
    p.add_argument("--slow", type=int, default=250)
    p.add_argument("--target-vol", type=float, default=0.10)
    p.add_argument("--cost-bps", type=float, default=100.0)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

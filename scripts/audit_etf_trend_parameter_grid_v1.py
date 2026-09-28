"""Research-only parameter grid audit for the fixed ETF trend contract.

The grid is diagnostic: training selects a candidate, while validation and
blind windows are reported without being used to choose parameters.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _series(path: Path, fast: int, slow: int, target_vol: float, cost_bps: float) -> pd.Series:
    d = pd.read_parquet(path, columns=["trade_date", "open", "close"])
    d = d.sort_values("trade_date").set_index("trade_date")
    oo = d["open"].shift(-1) / d["open"] - 1.0
    rv = oo.rolling(20).std().shift(1) * np.sqrt(252.0)
    signal = (d["close"].rolling(fast).mean() > d["close"].rolling(slow).mean()).shift(1).fillna(False)
    scale = (target_vol / rv).clip(upper=1.0).fillna(0.0)
    active = scale.where(signal, 0.0)
    change = signal.astype(int).diff().abs().fillna(0.0)
    trade_scale = pd.concat([active, active.shift(1)], axis=1).max(axis=1).fillna(0.0)
    return oo.fillna(0.0) * active - cost_bps / 10000.0 * change * trade_scale


def _metrics(s: pd.Series) -> dict:
    eq = (1.0 + s).cumprod()
    return {
        "return": float(eq.iloc[-1] - 1.0),
        "max_drawdown": float((eq / eq.cummax() - 1.0).min()),
    }


def run(args: argparse.Namespace) -> dict:
    root = Path(args.input_dir)
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    paths = [root / f"{s.replace('.', '_')}.parquet" for s in symbols]
    results = []
    for fast in args.fast_values:
        for slow in args.slow_values:
            if fast >= slow:
                continue
            for target in args.target_values:
                parts = [_series(p, fast, slow, target, args.cost_bps) for p in paths]
                basket = pd.concat(parts, axis=1).fillna(0.0).mean(axis=1)
                row = {"fast": fast, "slow": slow, "target_vol": target, "cost_bps": args.cost_bps}
                for name, lo, hi in [
                    ("train_2013_2019", "2013-01-01", "2019-12-31"),
                    ("validation_2020_2023", "2020-01-01", "2023-12-31"),
                    ("blind_2024_2026", "2024-01-01", "2026-09-24"),
                    ("full", "2013-01-01", "2026-09-24"),
                ]:
                    row[name] = _metrics(basket[(basket.index >= lo) & (basket.index <= hi)])
                results.append(row)
    # Training-only ranking. The other windows remain untouched diagnostics.
    ranked = sorted(results, key=lambda r: (r["train_2013_2019"]["return"], r["train_2013_2019"]["max_drawdown"]), reverse=True)
    out = {"research_only": True, "selection_window": "train_2013_2019", "symbols": symbols, "results": results, "training_ranked_top10": ranked[:10]}
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS")
    p.add_argument("--fast-values", nargs="+", type=int, default=[40, 60, 80, 100])
    p.add_argument("--slow-values", nargs="+", type=int, default=[180, 250, 300, 350])
    p.add_argument("--target-values", nargs="+", type=float, default=[0.05, 0.075, 0.10])
    p.add_argument("--cost-bps", type=float, default=100.0)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

"""Audit fixed portfolio drawdown throttles on the raw-lot v4 model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from audit_etf_lot_rounding_v1 import _load


def simulate(frames: dict[str, pd.DataFrame], initial: float, cost_bps: float, lot: int, mode: str) -> dict:
    common = sorted(set.intersection(*(set(x.index) for x in frames.values())))
    prices = pd.DataFrame({s: frames[s].loc[common, "open"] for s in frames})
    base = pd.DataFrame({s: frames[s].loc[common, "position_scale"] for s in frames}).fillna(0.0)
    equity, peak = initial, initial
    shares = {s: 0 for s in frames}
    previous_scales = pd.Series(0.0, index=frames)
    curve = []
    for i, date in enumerate(prices.index[:-1]):
        drawdown = equity / peak - 1.0
        if mode == "half10" and drawdown <= -0.10:
            risk = 0.5
        elif mode == "cash15" and drawdown <= -0.15:
            risk = 0.0
        else:
            risk = 1.0
        current = base.loc[date] * risk
        px = prices.loc[date]
        if (current == previous_scales).all():
            new_shares = shares.copy()
        else:
            target = equity * current / len(frames)
            new_shares = {s: int(max(target[s], 0.0) // (px[s] * lot)) * lot for s in frames}
        turnover = sum(abs(new_shares[s] - shares[s]) * px[s] for s in frames)
        equity -= turnover * cost_bps / 10000.0
        next_px = prices.iloc[i + 1]
        equity += sum(new_shares[s] * (next_px[s] - px[s]) for s in frames)
        shares, previous_scales = new_shares, current
        peak = max(peak, equity)
        curve.append((date, equity))
    series = pd.Series(dict(curve)).sort_index()
    out = {"mode": mode, "return": float(series.iloc[-1] / initial - 1.0), "max_drawdown": float((series / series.cummax() - 1.0).min())}
    for name, lo, hi in [("validation_2020_2023", "2020-01-01", "2023-12-31"), ("blind_2024_2026", "2024-01-01", "2026-09-24")]:
        p = series[(series.index >= lo) & (series.index <= hi)]
        out[name] = float(p.iloc[-1] / p.iloc[0] - 1.0) if len(p) > 1 else None
    return out


def run(args: argparse.Namespace) -> dict:
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    frames = {s: _load(Path(args.input_dir) / f"{s.replace('.', '_')}.parquet") for s in symbols}
    out = {"research_only": True, "cost_bps": args.cost_bps, "rows": [simulate(frames, args.initial, args.cost_bps, args.lot, m) for m in ["none", "half10", "cash15"]]}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS,511010.SS,518880.SS")
    p.add_argument("--initial", type=float, default=500000)
    p.add_argument("--cost-bps", type=float, default=200.0)
    p.add_argument("--lot", type=int, default=100)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

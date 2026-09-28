"""Audit ETF lot-size rounding for the v4 entry-fixed portfolio."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _load(path: Path, target_vol: float = 0.10, confirm_days: int = 1, fast: int = 40, slow: int = 250) -> pd.DataFrame:
    d = pd.read_parquet(path, columns=["trade_date", "open", "close"]).sort_values("trade_date").set_index("trade_date")
    oo = d["open"].shift(-1) / d["open"] - 1.0
    rv = oo.rolling(20).std().shift(1) * np.sqrt(252.0)
    raw_signal = d["close"].rolling(fast).mean() > d["close"].rolling(slow).mean()
    confirmed = raw_signal.rolling(confirm_days).sum() == confirm_days
    signal = confirmed.shift(1).fillna(False)
    desired = (target_vol / rv).clip(upper=1.0).fillna(0.0).where(signal, 0.0)
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
    return pd.DataFrame({"open": d["open"], "position_scale": pos})


def _simulate(frames: dict[str, pd.DataFrame], initial: float, cost_bps: float, lot: int, weights: list[float]) -> dict:
    common = sorted(set.intersection(*(set(x.index) for x in frames.values())))
    prices = pd.DataFrame({s: frames[s].loc[common, "open"] for s in frames})
    scales = pd.DataFrame({s: frames[s].loc[common, "position_scale"] for s in frames}).fillna(0.0)
    equity = float(initial)
    shares = {s: 0 for s in frames}
    previous_scales = pd.Series(0.0, index=frames)
    curve = []
    for i, date in enumerate(prices.index[:-1]):
        px = prices.loc[date]
        current_scales = scales.loc[date]
        if (current_scales == previous_scales).all():
            new_shares = shares.copy()
        else:
            target = equity * current_scales * pd.Series(weights, index=frames)
            new_shares = {s: int(max(target[s], 0.0) // (px[s] * lot)) * lot for s in frames}
        turnover = sum(abs(new_shares[s] - shares[s]) * px[s] for s in frames)
        equity -= turnover * cost_bps / 10000.0
        next_px = prices.iloc[i + 1]
        equity += sum(new_shares[s] * (next_px[s] - px[s]) for s in frames)
        shares = new_shares
        previous_scales = current_scales
        curve.append((date, equity))
    curve = pd.Series(dict(curve)).sort_index()
    dd = curve / curve.cummax() - 1.0
    daily = curve.pct_change().dropna()
    years = max(len(curve) / 252.0, 1.0 / 252.0)
    annualized = float((curve.iloc[-1] / initial) ** (1.0 / years) - 1.0)
    ann_vol = float(daily.std() * np.sqrt(252.0)) if len(daily) > 1 else 0.0
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252.0)) if len(daily) > 1 and daily.std() > 0 else 0.0
    max_dd = float(dd.min())
    out = {"initial": initial, "final": float(curve.iloc[-1]), "return": float(curve.iloc[-1] / initial - 1.0), "annualized_return": annualized, "annualized_volatility": ann_vol, "sharpe": sharpe, "calmar": annualized / abs(max_dd) if max_dd < 0 else None, "max_drawdown": max_dd, "common_days": int(len(curve)), "lot": lot, "cost_bps": cost_bps}
    for name, lo, hi in [("validation_2020_2023", "2020-01-01", "2023-12-31"), ("blind_2024_2026", "2024-01-01", "2026-09-24")]:
        part = curve[(curve.index >= lo) & (curve.index <= hi)]
        out[name] = float(part.iloc[-1] / part.iloc[0] - 1.0) if len(part) > 1 else None
    return out


def run(args: argparse.Namespace) -> dict:
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    frames = {s: _load(Path(args.input_dir) / f"{s.replace('.', '_')}.parquet", args.target_vol, args.confirm_days, args.fast, args.slow) for s in symbols}
    weights = args.weights if args.weights else [1.0 / len(symbols)] * len(symbols)
    if len(weights) != len(symbols) or abs(sum(weights) - 1.0) > 1e-6:
        raise ValueError("weights must match symbols and sum to 1")
    rows = [_simulate(frames, initial, args.cost_bps, args.lot, weights) for initial in args.initial_capital]
    out = {"research_only": True, "symbols": symbols, "weights": weights, "rows": rows}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS,511010.SS,518880.SS")
    p.add_argument("--initial-capital", nargs="+", type=float, default=[100000, 500000, 1000000])
    p.add_argument("--cost-bps", type=float, default=200.0)
    p.add_argument("--target-vol", type=float, default=0.10)
    p.add_argument("--confirm-days", type=int, default=1)
    p.add_argument("--fast", type=int, default=40)
    p.add_argument("--slow", type=int, default=250)
    p.add_argument("--weights", nargs="+", type=float, default=None)
    p.add_argument("--lot", type=int, default=100)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

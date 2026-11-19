"""Summarize an equal-weight ETF basket from per-symbol research parquet files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def summarize(args: argparse.Namespace) -> dict:
    root = Path(args.input_dir)
    frames = []
    for symbol in [x.strip() for x in args.symbols.split(",") if x.strip()]:
        path = root / f"{symbol.replace('.', '_')}.parquet"
        if not path.exists():
            raise FileNotFoundError(path)
        frame = pd.read_parquet(path, columns=["trade_date", "strategy_return", "position_scale"])
        frame = frame.set_index("trade_date")[["strategy_return", "position_scale"]]
        frame.columns = pd.MultiIndex.from_product([[symbol], frame.columns])
        frames.append(frame)
    data = pd.concat(frames, axis=1).fillna(0.0)
    returns = data.xs("strategy_return", axis=1, level=1).mean(axis=1)
    target = data.xs("position_scale", axis=1, level=1) / len(frames)
    turnover = target.diff().abs().sum(axis=1).fillna(target.iloc[0].abs().sum())
    rebalance_cost = args.rebalance_cost_bps / 10000.0 * turnover
    returns = returns - rebalance_cost
    equity = (1.0 + returns).cumprod()
    result = {
        "research_only": True,
        "execution_valid": False,
        "allocation": "equal_weight",
        "symbols": [x.strip() for x in args.symbols.split(",") if x.strip()],
        "source_directory": str(root),
        "rows": int(len(returns)),
        "total_return": float(equity.iloc[-1] - 1.0),
        "max_drawdown": float((equity / equity.cummax() - 1.0).min()),
        "rebalance_cost_included": False,
        "rebalance_cost_bps": args.rebalance_cost_bps,
        "average_daily_turnover": float(turnover.mean()),
        "total_turnover": float(turnover.sum()),
    }
    result["rebalance_cost_included"] = bool(args.rebalance_cost_bps > 0)
    for name, lo, hi in [
        ("train_2013_2019", "2013-01-01", "2019-12-31"),
        ("validation_2020_2023", "2020-01-01", "2023-12-31"),
        ("blind_2024_2026", "2024-01-01", "2026-09-24"),
    ]:
        part = returns[(returns.index >= lo) & (returns.index <= hi)]
        part_equity = (1.0 + part).cumprod()
        result[f"{name}_return"] = float(part_equity.iloc[-1] - 1.0) if len(part) else None
        result[f"{name}_max_drawdown"] = float((part_equity / part_equity.cummax() - 1.0).min()) if len(part) else None
    returns.rename("basket_return").to_csv(root / "basket_equity_curve.csv", header=True)
    (root / "basket_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS")
    parser.add_argument("--rebalance-cost-bps", type=float, default=0.0)
    print(json.dumps(summarize(parser.parse_args()), ensure_ascii=False, indent=2))

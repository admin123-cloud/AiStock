"""Report rolling subwindow behavior for the v4 entry-fixed basket."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from audit_etf_rebalance_policy_v1 import _one, _metrics


def run(args: argparse.Namespace) -> dict:
    root = Path(args.input_dir)
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    paths = [root / f"{s.replace('.', '_')}.parquet" for s in symbols]
    basket = pd.concat([_one(p, 40, 250, 0.10, "entry_fixed", args.cost_bps) for p in paths], axis=1).fillna(0.0).mean(axis=1)
    windows = [
        ("2014_2016", "2014-01-01", "2016-12-31"),
        ("2017_2019", "2017-01-01", "2019-12-31"),
        ("2020_2022", "2020-01-01", "2022-12-31"),
        ("2023_2024", "2023-01-01", "2024-12-31"),
        ("2025_2026", "2025-01-01", "2026-09-24"),
    ]
    result = {"research_only": True, "cost_bps": args.cost_bps, "windows": {name: _metrics(basket[(basket.index >= lo) & (basket.index <= hi)]) for name, lo, hi in windows}}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS,511010.SS,518880.SS")
    p.add_argument("--cost-bps", type=float, default=200.0)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

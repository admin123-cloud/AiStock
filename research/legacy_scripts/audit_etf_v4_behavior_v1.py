"""Report trade behavior and return concentration for the v4 raw-price model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def run(args: argparse.Namespace) -> dict:
    rows = []
    for symbol in [x.strip() for x in args.symbols.split(",") if x.strip()]:
        d = pd.read_parquet(Path(args.input_dir) / f"{symbol.replace('.', '_')}.parquet", columns=["trade_date", "open", "close"]).sort_values("trade_date").set_index("trade_date")
        oo = d["open"].shift(-1) / d["open"] - 1.0
        rv = oo.rolling(20).std().shift(1) * np.sqrt(252.0)
        signal = (d["close"].rolling(40).mean() > d["close"].rolling(250).mean()).shift(1).fillna(False)
        desired = (0.10 / rv).clip(upper=1.0).fillna(0.0).where(signal, 0.0)
        pos = pd.Series(0.0, index=d.index)
        prev = False
        held = 0.0
        entries = exits = 0
        for idx, active in signal.items():
            if not active:
                if prev:
                    exits += 1
                held = 0.0
            elif not prev:
                entries += 1
                held = float(desired.loc[idx])
            pos.loc[idx] = held
            prev = bool(active)
        active = pos > 0
        contribution = (oo.fillna(0.0) * pos).sum() / len([x for x in args.symbols.split(',') if x.strip()])
        rows.append({
            "symbol": symbol,
            "entries": entries,
            "exits": exits,
            "active_days": int(active.sum()),
            "active_fraction": float(active.mean()),
            "mean_active_scale": float(pos[active].mean()) if active.any() else 0.0,
            "gross_contribution": float(contribution),
        })
    total = sum(x["gross_contribution"] for x in rows)
    for row in rows:
        row["contribution_share"] = row["gross_contribution"] / total if total else 0.0
    out = {"research_only": True, "rows": rows, "total_gross_contribution": float(total)}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--symbols", default="510300.SS,159919.SZ,510500.SS,511010.SS,518880.SS")
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

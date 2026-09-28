"""Research-only monthly ETF momentum rotation audit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def run(args: argparse.Namespace) -> dict:
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()]
    frames = []
    for s in symbols:
        d = pd.read_parquet(Path(args.input_dir) / f"{s.replace('.', '_')}.parquet", columns=["trade_date", "open", "close"]).set_index("trade_date").sort_index()
        frames.append(d.rename(columns={"open": f"open_{s}", "close": f"close_{s}"}))
    data = pd.concat(frames, axis=1).dropna()
    opens = data[[f"open_{s}" for s in symbols]]
    closes = data[[f"close_{s}" for s in symbols]]
    close_names = {f"close_{s}": s for s in symbols}
    rows = []
    for top_k in args.top_k:
        for every in args.rebalance_every:
            for cost_bps in args.cost_bps:
                pos = pd.DataFrame(0.0, index=data.index, columns=symbols)
                for i in range(len(data)):
                    if i == 0 or i % every == 0:
                        # The signal is shifted one session: only prior close is used.
                        mom = closes.iloc[max(0, i - 1)] / closes.iloc[max(0, i - 61)] - 1.0 if i >= 61 else pd.Series(np.nan, index=closes.columns)
                        valid = mom.dropna().sort_values(ascending=False)
                        chosen = [close_names[x] for x in valid[valid > 0].head(top_k).index]
                        if chosen:
                            pos.loc[pos.index[i], chosen] = 1.0 / len(chosen)
                    elif i > 0:
                        pos.iloc[i] = pos.iloc[i - 1]
                oo = opens.shift(-1) / opens - 1.0
                gross = sum(oo[f"open_{s}"].fillna(0.0) * pos[s] for s in symbols)
                turnover = pos.diff().abs().sum(axis=1).fillna(pos.iloc[0].abs().sum())
                ret = gross - cost_bps / 10000.0 * turnover
                eq = (1.0 + ret).cumprod()
                dd = eq / eq.cummax() - 1.0
                row = {"top_k": top_k, "rebalance_every": every, "cost_bps": cost_bps, "return": float(eq.iloc[-1] - 1.0), "max_drawdown": float(dd.min())}
                for name, lo, hi in [("validation_2020_2023", "2020-01-01", "2023-12-31"), ("blind_2024_2026", "2024-01-01", "2026-09-24")]:
                    part = ret[(ret.index >= lo) & (ret.index <= hi)]
                    pe = (1.0 + part).cumprod()
                    row[name] = float(pe.iloc[-1] - 1.0) if len(pe) else None
                rows.append(row)
    out = {"research_only": True, "symbols": symbols, "rows": rows}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--symbols", default="510300.SS,510500.SS,511010.SS,518880.SS")
    p.add_argument("--top-k", nargs="+", type=int, default=[1, 2])
    p.add_argument("--rebalance-every", nargs="+", type=int, default=[20, 40])
    p.add_argument("--cost-bps", nargs="+", type=float, default=[200, 300])
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

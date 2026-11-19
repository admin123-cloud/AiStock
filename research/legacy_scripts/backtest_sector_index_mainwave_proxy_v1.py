"""Research-only sector-index mainwave proxy.

This deliberately does not select stocks. It tests whether the sector layer
has predictive value before a dated constituent snapshot is used for leaders.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


def run(args: argparse.Namespace) -> dict:
    df = clickhouse_query_df(
        f"""SELECT code, trade_date, open, close
             FROM sector_kline_daily
             WHERE trade_date BETWEEN '{args.start}' AND '{args.end}'"""
    )
    if df.empty:
        raise RuntimeError("sector_kline_daily is empty")
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for c in ["open", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["trade_date", "open", "close"])
    # Some sector snapshots contain duplicate code/date rows; keep the latest
    # warehouse value instead of silently failing or double counting it.
    close = df.pivot_table(index="trade_date", columns="code", values="close", aggfunc="last").sort_index()
    op = df.pivot_table(index="trade_date", columns="code", values="open", aggfunc="last").reindex(close.index)
    mom20 = close / close.shift(args.fast) - 1.0
    mom60 = close / close.shift(args.slow) - 1.0
    positions = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    for i in range(args.slow + 1, len(close)):
        if i % args.rebalance != 0:
            positions.iloc[i] = positions.iloc[i - 1]
            continue
        score = mom20.iloc[i - 1].where(mom60.iloc[i - 1] > 0).dropna().sort_values(ascending=False)
        chosen = list(score.head(args.top_k).index)
        if chosen:
            positions.iloc[i, positions.columns.get_indexer(chosen)] = 1.0 / len(chosen)
    oo = op.shift(-1) / op - 1.0
    gross = (oo * positions).sum(axis=1, min_count=1).fillna(0.0)
    turnover = positions.diff().abs().sum(axis=1).fillna(positions.iloc[0].abs().sum())
    ret = gross - (args.cost_bps / 10000.0) * turnover
    equity = (1 + ret).cumprod()
    dd = equity / equity.cummax() - 1.0
    out = {
        "research_only": True,
        "proxy_only": True,
        "data": {"start": args.start, "end": args.end, "rows": int(len(df)), "sector_codes": int(close.shape[1])},
        "params": {"fast": args.fast, "slow": args.slow, "rebalance_sessions": args.rebalance, "top_k": args.top_k, "cost_bps": args.cost_bps},
        "return": float(equity.iloc[-1] - 1),
        "max_drawdown": float(dd.min()),
    }
    for name, lo, hi in [("validation_2020_2023", "2020-01-01", "2023-12-31"), ("blind_2024_2026", "2024-01-01", args.end)]:
        part = ret[(ret.index >= lo) & (ret.index <= hi)]
        out[name] = float((1 + part).cumprod().iloc[-1] - 1) if len(part) else None
    target = report_path("sector_mainwave_leader_rebuild_v2") / "sector_index_proxy.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default="2026-08-12")
    p.add_argument("--fast", type=int, default=20)
    p.add_argument("--slow", type=int, default=60)
    p.add_argument("--rebalance", type=int, default=20)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--cost-bps", type=float, default=20)
    print(json.dumps(run(p.parse_args()), ensure_ascii=False, indent=2))

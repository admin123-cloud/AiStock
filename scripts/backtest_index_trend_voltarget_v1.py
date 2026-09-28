"""Research-only T+1 index trend strategy with volatility targeting.

Signal is confirmed at the prior close. A position is held from the next
session open to the following session open, so the timing is compatible with
T+1 when mapped to a real ETF. Index results are proxies until ETF data is
available.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


def run(args: argparse.Namespace) -> dict:
    codes = [x.strip() for x in args.codes.split(",") if x.strip()]
    quoted = ",".join("'" + x.replace("'", "''") + "'" for x in codes)
    d = clickhouse_query_df(
        f"SELECT code, trade_date, open, close FROM kline_daily "
        f"WHERE code IN ({quoted}) AND trade_date BETWEEN ? AND ? ORDER BY code, trade_date",
        [args.start, args.end],
    )
    if d.empty:
        raise RuntimeError("no index rows")
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce")
    for col in ["open", "close"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.dropna(subset=["code", "trade_date", "open", "close"]).sort_values(["code", "trade_date"])
    frames = []
    for code, g in d.groupby("code", sort=False):
        g = g.set_index("trade_date").sort_index().copy()
        g["entry_to_next_open"] = g["open"].shift(-1) / g["open"] - 1.0
        g["realized_vol"] = g["entry_to_next_open"].rolling(args.vol_window).std().shift(1) * np.sqrt(252.0)
        g["fast_ma"] = g["close"].rolling(args.fast).mean()
        g["slow_ma"] = g["close"].rolling(args.slow).mean()
        g["signal"] = (g["fast_ma"] > g["slow_ma"]).shift(1).fillna(False)
        scale = (args.target_vol / g["realized_vol"]).clip(upper=1.0).fillna(0.0)
        change = g["signal"].astype(int).diff().abs().fillna(0.0)
        g["strategy_return"] = g["entry_to_next_open"].fillna(0.0).where(g["signal"], 0.0) * scale
        g["strategy_return"] -= args.cost_bps / 10000.0 * change * scale
        g["code"] = code
        frames.append(g.reset_index()[["code", "trade_date", "strategy_return", "signal", "realized_vol"]])
    out = pd.concat(frames, ignore_index=True)
    result_rows = []
    for code, g in out.groupby("code", sort=False):
        g = g.sort_values("trade_date")
        eq = (1.0 + g["strategy_return"]).cumprod()
        result_rows.append(
            {
                "code": code,
                "rows": int(len(g)),
                "signal_rows": int(g["signal"].sum()),
                "total_return": float(eq.iloc[-1] - 1.0),
                "max_drawdown": float((eq / eq.cummax() - 1.0).min()),
                "execution_valid": False,
                "proxy_only_reason": "index_proxy_requires_real_ETF_price_and_fill_audit",
            }
        )
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_dir / "daily_proxy_returns.parquet", index=False)
    summary = {
        "research_only": True,
        "execution_valid": False,
        "timing": "prior_close_signal_next_open_entry_following_open_exit",
        "cost_bps": args.cost_bps,
        "target_vol": args.target_vol,
        "fast": args.fast,
        "slow": args.slow,
        "vol_window": args.vol_window,
        "rows": result_rows,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", default="000300.SH,000016.SH,000905.SH,000852.SH")
    parser.add_argument("--start", default="2005-01-01")
    parser.add_argument("--end", default="2026-09-24")
    parser.add_argument("--fast", type=int, default=60)
    parser.add_argument("--slow", type=int, default=250)
    parser.add_argument("--vol-window", type=int, default=20)
    parser.add_argument("--target-vol", type=float, default=0.05)
    parser.add_argument("--cost-bps", type=float, default=100.0)
    parser.add_argument("--output-dir", default=str(report_path("index_trend_voltarget_shadow_v1")))
    print(json.dumps(run(parser.parse_args()), ensure_ascii=False, indent=2))

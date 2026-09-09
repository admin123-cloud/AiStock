from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import _load_daily, _load_index_features, _simulate
from utils.paths import report_path


OUT_DIR = report_path("breakout_initiation_hypothesis_v1")
START, END = "2020-01-01", "2026-05-15"


def _metrics(df: pd.DataFrame, window: str) -> dict[str, object]:
    ret = pd.to_numeric(df.get("net_ret"), errors="coerce").dropna()
    return {"window": window, "trades": len(ret), "avg_net_ret": ret.mean() if len(ret) else None, "win_rate": (ret > 0).mean() if len(ret) else None, "worst_trade": ret.min() if len(ret) else None, "sum_trade_ret": ret.sum() if len(ret) else None}


def _features() -> pd.DataFrame:
    d = _load_daily("2019-07-01", END).sort_values(["code6", "trade_date"]).copy()
    g = d.groupby("code6", group_keys=False)
    d["ma20"] = g["close"].rolling(20, min_periods=15).mean().reset_index(level=0, drop=True)
    d["ma60"] = g["close"].rolling(60, min_periods=40).mean().reset_index(level=0, drop=True)
    d["close20"] = g["close"].shift(20); d["close60"] = g["close"].shift(60)
    d["mom20"] = d["close"] / d["close20"] - 1; d["mom60"] = d["close"] / d["close60"] - 1
    d["high20_prev"] = g["high"].shift(1).rolling(20, min_periods=15).max().reset_index(level=0, drop=True)
    d["high60_prev"] = g["high"].shift(1).rolling(60, min_periods=40).max().reset_index(level=0, drop=True)
    d["high10_prev"] = g["high"].shift(1).rolling(10, min_periods=8).max().reset_index(level=0, drop=True)
    d["low10_prev"] = g["low"].shift(1).rolling(10, min_periods=8).min().reset_index(level=0, drop=True)
    d["base_range10"] = d["high10_prev"] / d["low10_prev"] - 1
    d["amount20"] = g["amount"].rolling(20, min_periods=15).mean().reset_index(level=0, drop=True)
    d["amount_ratio"] = d["amount"] / d["amount20"]
    d["ret1"] = d["close"] / g["close"].shift(1) - 1
    d["next_entry_date"] = g["trade_date"].shift(-1); d["next_open"] = g["open"].shift(-1)
    d["exit_date"] = g["trade_date"].shift(-10); d["exit_close"] = g["close"].shift(-10)
    return d.replace([np.inf, -np.inf], np.nan)


def _run_variant(d: pd.DataFrame, name: str, squeeze: bool) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    base = (
        d["trade_date"].between(pd.Timestamp(START), pd.Timestamp(END)) & ~d["code_raw"].astype(str).str.endswith(".BJ")
        & d["next_entry_date"].notna() & d["exit_close"].notna() & d["next_open"].gt(0)
        & d["close"].gt(d["ma20"]) & d["ma20"].gt(d["ma60"])
        & d["mom20"].between(.08, .35) & d["mom60"].between(.15, 1.00)
        & d["close"].ge(d["high20_prev"] * .995) & d["close"].ge(d["high60_prev"] * .97)
        & d["amount20"].ge(30000) & d["amount_ratio"].ge(1.20) & d["ret1"].between(.01, .06)
        & d["index_mom60"].le(.05)
    )
    if squeeze:
        base &= d["base_range10"].le(.20)
    out = d[base].copy()
    out["entry_date"] = pd.to_datetime(out["next_entry_date"]).dt.normalize(); out["entry_price"] = out["next_open"]
    out["policy_exit_date"] = pd.to_datetime(out["exit_date"]).dt.normalize(); out["exit_price"] = out["exit_close"]
    out["gross_ret"] = out["exit_price"] / out["entry_price"] - 1; out["net_ret"] = out["gross_ret"] - .003; out["hold_days"] = 10
    # Daily prices occasionally contain unadjusted corporate-action artifacts.  They are not executable breakout returns.
    out = out[out["gross_ret"].between(-0.50, 1.00)].copy()
    out["signal_score"] = out.groupby("trade_date")["amount_ratio"].rank(pct=True) * .45 + out.groupby("trade_date")["mom20"].rank(pct=True) * .35 + out.groupby("trade_date")["base_range10"].rank(pct=True, ascending=False) * .20
    out = out.sort_values(["entry_date", "signal_score", "amount_ratio"], ascending=[True, False, False])
    curve, closed = _simulate(out, slots=2, slot_pct=.50, daily_open_limit=1)
    return out, closed, curve


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = _features().merge(_load_index_features("2019-07-01", END), on="trade_date", how="left")
    summaries = []
    for name, squeeze in [("breakout_base", False), ("breakout_squeeze10", True)]:
        candidates, closed, curve = _run_variant(d, name, squeeze)
        candidates.to_csv(OUT_DIR / f"{name}_candidates.csv", index=False, encoding="utf-8-sig")
        closed.to_csv(OUT_DIR / f"{name}_closed_trades.csv", index=False, encoding="utf-8-sig")
        curve.to_csv(OUT_DIR / f"{name}_equity_curve.csv", index=False, encoding="utf-8-sig")
        for label, start, end in [("train_2020_2023", "2020-01-01", "2023-12-31"), ("validation_2024_2025", "2024-01-01", "2025-12-31"), ("blind_2026", "2026-01-01", END), ("full", START, END)]:
            x = closed[pd.to_datetime(closed["entry_date"]).between(start, end)] if not closed.empty else closed
            row = _metrics(x, label); row["variant"] = name; row["candidate_rows"] = len(candidates); row["candidate_days"] = candidates["entry_date"].nunique() if not candidates.empty else 0; summaries.append(row)
    summary = pd.DataFrame(summaries)
    summary.to_csv(OUT_DIR / "window_summary.csv", index=False, encoding="utf-8-sig")
    meta = {"status": "completed", "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "live_eligible": False, "limitations": "daily-only proxy; requires independent 30m acceptance and sector/mainline validation"}
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "REPORT_CN.md").write_text("# 前高突破启动研究 v1\n\n仅研究，不进入G3。\n\n" + summary.to_markdown(index=False) + "\n\n必须在30分钟承接、板块共振和独立样本验证后才可评估。\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

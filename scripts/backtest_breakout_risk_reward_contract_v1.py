from __future__ import annotations

"""Daily-proxy risk/reward audit for the research-only breakout entries.

This deliberately holds the entry list fixed.  It answers whether a clear
small-loss / let-winners-run exit contract improves the payoff shape; it does
not optimise the entry thresholds and it is not a live-trading implementation.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_wave_style_template_strategy_v1 import _load_daily
from utils.paths import report_path


SOURCE_DIR = report_path("breakout_initiation_hypothesis_v1")
OUT_DIR = report_path("breakout_risk_reward_contract_v1")
FEE = 0.003
WINDOWS = [
    ("train_2020_2023", "2020-01-01", "2023-12-31"),
    ("validation_2024_2025", "2024-01-01", "2025-12-31"),
    ("blind_2026", "2026-01-01", "2026-05-15"),
    ("full", "2020-01-01", "2026-05-15"),
]
# These are predeclared contracts, not a parameter search.  All cap a loss at
# roughly 5%, take half at +10/+12%, and give the remaining half room to run.
CONTRACTS = {
    "rr_a_8d_5stop_10tp": {"stop": 0.05, "tp": 0.10, "max_hold": 8},
    "rr_b_12d_5stop_12tp": {"stop": 0.05, "tp": 0.12, "max_hold": 12},
    "rr_c_20d_5stop_12tp": {"stop": 0.05, "tp": 0.12, "max_hold": 20},
}


def _num(v: object) -> float:
    try:
        x = float(v)
        return x if np.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _simulate(row: pd.Series, bars: pd.DataFrame, contract: dict[str, float]) -> dict[str, object] | None:
    entry_date = pd.Timestamp(row["entry_date"]).normalize()
    entry = _num(row["entry_price"])
    if entry <= 0:
        return None
    if bars.empty or "trade_date" not in bars:
        return None
    future = bars[bars["trade_date"].ge(entry_date)].head(int(contract["max_hold"]))
    if future.empty:
        return None
    # The invalidation is the tighter of a 5% loss and a close back below the
    # breakout level (with a 1% buffer).  Cap it below entry to avoid treating
    # a gap-up opening as a fictional no-loss stop.
    breakout_level = _num(row.get("high20_prev"))
    stop = entry * (1.0 - contract["stop"])
    if breakout_level > 0:
        stop = max(stop, breakout_level * 0.99)
    stop = min(stop, entry * 0.995)
    tp = entry * (1.0 + contract["tp"])
    realised_weighted = 0.0
    remaining = 1.0
    took_profit = False
    reason = f"hold{int(contract['max_hold'])}"
    exit_date = pd.Timestamp(future.iloc[-1]["trade_date"]).normalize()
    runner_exit = _num(future.iloc[-1]["close"])
    for pos, bar in enumerate(future.itertuples(index=False)):
        low, high, close = _num(bar.low), _num(bar.high), _num(bar.close)
        day = pd.Timestamp(bar.trade_date).normalize()
        # Conservative daily-bar convention: a stop has priority if both stop
        # and take-profit are touched on the same unknown intraday path.
        if low and low <= stop:
            realised_weighted += remaining * (stop / entry - 1.0)
            remaining = 0.0; runner_exit = stop; exit_date = day; reason = "breakout_invalidation_stop"
            break
        if not took_profit and high >= tp:
            realised_weighted += 0.5 * (tp / entry - 1.0)
            remaining = 0.5; took_profit = True
        if took_profit and pos >= 1:
            prev_low = _num(future.iloc[pos - 1]["low"])
            if prev_low > 0 and close < prev_low:
                realised_weighted += remaining * (close / entry - 1.0)
                runner_exit = close; remaining = 0.0; exit_date = day; reason = "runner_prev_low_break"
                break
        if pos == len(future) - 1:
            realised_weighted += remaining * (close / entry - 1.0)
            runner_exit = close; remaining = 0.0
    out = row.to_dict()
    out.update({
        "exit_date": exit_date.strftime("%Y-%m-%d"), "net_ret": realised_weighted - FEE,
        "gross_ret": realised_weighted, "exit_reason": reason, "took_profit": took_profit,
        "stop_price": stop, "take_profit_price": tp, "runner_exit_price": runner_exit,
    })
    return out


def _metrics(x: pd.DataFrame, window: str) -> dict[str, object]:
    r = pd.to_numeric(x.get("net_ret"), errors="coerce").dropna()
    wins, losses = r[r > 0], r[r <= 0]
    avg_win, avg_loss = wins.mean(), losses.mean()
    payoff = (avg_win / abs(avg_loss)) if len(wins) and len(losses) and avg_loss else np.nan
    win_rate = (r > 0).mean() if len(r) else np.nan
    expectation = r.mean() if len(r) else np.nan
    breakeven = (1.0 / (1.0 + payoff)) if pd.notna(payoff) and payoff > 0 else np.nan
    return {
        "window": window, "trades": len(r), "win_rate": win_rate, "avg_win": avg_win,
        "avg_loss": avg_loss, "payoff_ratio": payoff, "expectation": expectation,
        "breakeven_win_rate": breakeven, "worst_trade": r.min() if len(r) else np.nan,
        "stop_rate": (x.get("exit_reason") == "breakout_invalidation_stop").mean() if len(x) else np.nan,
        "tp_rate": x.get("took_profit", pd.Series(dtype=bool)).fillna(False).mean() if len(x) else np.nan,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # The already selected closed-trade files preserve the original daily slot
    # constraints.  Load price paths only for their codes, then apply exits.
    source = []
    for variant in ("breakout_base", "breakout_squeeze10"):
        x = pd.read_csv(SOURCE_DIR / f"{variant}_closed_trades.csv", dtype={"code6": str})
        x["code6"] = x["code6"].astype(str).str.zfill(6)
        x["variant"] = variant
        source.append(x)
    entries = pd.concat(source, ignore_index=True)
    entries["entry_date"] = pd.to_datetime(entries["entry_date"]).dt.normalize()
    daily = _load_daily("2020-01-01", "2026-06-30").copy()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"]).dt.normalize()
    daily["code6"] = daily["code6"].astype(str).str.zfill(6)
    need = set(entries["code6"])
    daily = daily[daily["code6"].isin(need)].sort_values(["code6", "trade_date"])
    paths = {str(k).zfill(6): v for k, v in daily.groupby("code6")}
    all_closed, rows = [], []
    for variant, group in entries.groupby("variant"):
        for contract_name, contract in CONTRACTS.items():
            simulated = [_simulate(r, paths.get(str(r["code6"]).zfill(6), pd.DataFrame()), contract) for _, r in group.iterrows()]
            closed = pd.DataFrame([r for r in simulated if r is not None])
            closed["contract"] = contract_name; closed["variant"] = variant
            all_closed.append(closed)
            for label, start, end in WINDOWS:
                sample = closed[closed["entry_date"].between(pd.Timestamp(start), pd.Timestamp(end))]
                metric = _metrics(sample, label); metric.update({"variant": variant, "contract": contract_name}); rows.append(metric)
    closed_all = pd.concat(all_closed, ignore_index=True)
    summary = pd.DataFrame(rows)
    closed_all.to_csv(OUT_DIR / "closed_trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "window_payoff_summary.csv", index=False, encoding="utf-8-sig")
    meta = {"status": "completed", "generated_at": datetime.now().isoformat(timespec="seconds"), "live_eligible": False, "limitations": "fixed historical entry list; daily OHLC proxy uses conservative stop priority; no 30m execution, sector/mainline, or slippage validation"}
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    report = "# 前高突破：小亏大赚退出合同研究 v1\n\n仅研究，不进入 G3。固定原始入选交易，不调入场阈值；日线 OHLC 下，同日触发止损和止盈时按止损优先处理。\n\n" + summary.to_markdown(index=False) + "\n\n判定必须同时看训练、验证、盲测的期望值与平均盈亏比，不能只挑一段最高收益。\n"
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

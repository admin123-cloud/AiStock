from __future__ import annotations

"""Executable-price proxy for accepted 10:30 breakout entries.

Entry is the completed 10:30 30m bar close.  Stops/profit targets are checked
only on later 30m bars that day, then daily bars on subsequent sessions.  This
is still a proxy (no tick ordering/slippage/limit-board model), but unlike the
association audit it does not inherit a next-open entry price.
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


SOURCE = report_path("breakout_entry_30m_acceptance_v1", "audited_entries.csv")
OUT_DIR = report_path("breakout_1030_execution_proxy_v1")
FEE = 0.003
WINDOWS = [("train_2020_2023", "2020-01-01", "2023-12-31"), ("validation_2024_2025", "2024-01-01", "2025-12-31"), ("blind_2026", "2026-01-01", "2026-05-15"), ("full", "2020-01-01", "2026-05-15")]


def _num(v: object) -> float:
    try:
        x = float(v)
        return x if np.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _simulate(row: pd.Series, m30: pd.DataFrame, daily: pd.DataFrame, stop_pct: float = 0.05) -> dict[str, object] | None:
    entry_date = pd.Timestamp(row["entry_date"]).normalize()
    entry = _num(row["m30_cutoff_close"])
    if entry <= 0:
        return None
    breakout = _num(row.get("high20_prev"))
    stop = max(entry * (1.0 - float(stop_pct)), breakout * .99) if breakout > 0 else entry * (1.0 - float(stop_pct))
    stop = min(stop, entry * .995)
    tp = entry * 1.10
    realised, remaining, took_profit = 0.0, 1.0, False
    reason, exit_date, runner_exit = "hold8", entry_date, entry

    def process_bar(low: float, high: float, close: float, day: pd.Timestamp, prev_low: float | None = None) -> bool:
        nonlocal realised, remaining, took_profit, reason, exit_date, runner_exit
        if low > 0 and low <= stop:
            realised += remaining * (stop / entry - 1.0); remaining = 0.0
            reason = "breakout_invalidation_stop"; exit_date = day; runner_exit = stop; return True
        if not took_profit and high >= tp:
            realised += .5 * (tp / entry - 1.0); remaining = .5; took_profit = True
        if took_profit and prev_low and close < prev_low:
            realised += remaining * (close / entry - 1.0); remaining = 0.0
            reason = "runner_prev_low_break"; exit_date = day; runner_exit = close; return True
        return False

    # Only bars completed after the 10:30 entry can cause an entry-day exit.
    first_day = m30[(m30["trade_date"] == entry_date) & (m30["time"] > "10:30:00")].sort_values("datetime")
    for pos, bar in enumerate(first_day.itertuples(index=False)):
        prev_low = _num(first_day.iloc[pos - 1]["low"]) if took_profit and pos else None
        if process_bar(_num(bar.low), _num(bar.high), _num(bar.close), entry_date, prev_low):
            break
    if remaining > 0:
        future = daily[daily["trade_date"] > entry_date].head(7).reset_index(drop=True)
        for pos, bar in enumerate(future.itertuples(index=False)):
            day = pd.Timestamp(bar.trade_date).normalize()
            prev_low = _num(future.iloc[pos - 1]["low"]) if took_profit and pos else None
            if process_bar(_num(bar.low), _num(bar.high), _num(bar.close), day, prev_low):
                break
            if pos == len(future) - 1:
                realised += remaining * (_num(bar.close) / entry - 1.0); remaining = 0.0
                exit_date = day; runner_exit = _num(bar.close)
    if remaining > 0:  # no subsequent bar: close the proxy at entry rather than invent a future fill
        realised += remaining * (_num(row["m30_cutoff_close"]) / entry - 1.0); remaining = 0.0
        reason = "insufficient_future_bars"
    out = row.to_dict()
    out.update({"entry_price": entry, "exit_date": exit_date.strftime("%Y-%m-%d"), "net_ret": realised - FEE, "gross_ret": realised, "exit_reason": reason, "took_profit": took_profit, "stop_price": stop, "take_profit_price": tp, "runner_exit_price": runner_exit})
    return out


def _metrics(x: pd.DataFrame) -> dict[str, object]:
    r = pd.to_numeric(x["net_ret"], errors="coerce").dropna(); w, l = r[r > 0], r[r <= 0]
    return {"trades": len(r), "win_rate": (r > 0).mean() if len(r) else np.nan, "expectation": r.mean() if len(r) else np.nan, "avg_win": w.mean() if len(w) else np.nan, "avg_loss": l.mean() if len(l) else np.nan, "payoff_ratio": w.mean() / abs(l.mean()) if len(w) and len(l) else np.nan, "worst_trade": r.min() if len(r) else np.nan, "stop_rate": (x["exit_reason"] == "breakout_invalidation_stop").mean() if len(x) else np.nan, "tp_rate": x["took_profit"].fillna(False).mean() if len(x) else np.nan}


def _load_inputs() -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    d = pd.read_csv(SOURCE, dtype={"code6": str})
    d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.normalize()
    d = d[(d["m30_status"] == "ok") & (d["m30_native_accept"] == True)].copy()
    d["code6"] = d["code6"].astype(str).str.zfill(6)
    daily = _load_daily("2020-01-01", "2026-06-30").copy(); daily["trade_date"] = pd.to_datetime(daily["trade_date"]).dt.normalize(); daily["code6"] = daily["code6"].astype(str).str.zfill(6)
    paths = {str(k): v.sort_values("trade_date") for k, v in daily[daily["code6"].isin(set(d["code6"]))].groupby("code6")}
    m30 = pd.read_csv(SOURCE).iloc[0:0]  # schema placeholder is intentionally not used
    # Re-query all entry-day bars from the warehouse through the audited entry codes.
    from utils.market_warehouse import clickhouse_query_df
    parts = []
    for code, g in d.groupby("code"):
        start, end = g["entry_date"].min().strftime("%Y-%m-%d"), g["entry_date"].max().strftime("%Y-%m-%d")
        qcode = "'" + str(code).replace("'", "''") + "'"
        q = f"SELECT code, datetime, open, high, low, close FROM kline_minute_30 WHERE code={qcode} AND toDate(datetime) BETWEEN toDate('{start}') AND toDate('{end}') ORDER BY datetime"
        part = clickhouse_query_df(q)
        if not part.empty: parts.append(part)
    m30 = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["code", "datetime", "open", "high", "low", "close"])
    m30["datetime"] = pd.to_datetime(m30["datetime"], errors="coerce"); m30["trade_date"] = m30["datetime"].dt.normalize(); m30["time"] = m30["datetime"].dt.strftime("%H:%M:%S")
    for c in ["open", "high", "low", "close"]: m30[c] = pd.to_numeric(m30[c], errors="coerce")
    mpaths = {str(k): v.dropna(subset=["datetime", "close"]).sort_values("datetime") for k, v in m30.groupby("code")}
    return d, mpaths, paths


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d, mpaths, paths = _load_inputs()
    closed = pd.DataFrame([z for _, r in d.iterrows() if (z := _simulate(r, mpaths.get(str(r["code"]), pd.DataFrame()), paths.get(str(r["code6"]), pd.DataFrame()))) is not None])
    rows = []
    for label, start, end in WINDOWS:
        sample = closed[closed["entry_date"].between(start, end)]; metric = _metrics(sample); metric["window"] = label; rows.append(metric)
    summary = pd.DataFrame(rows)
    closed.to_csv(OUT_DIR / "closed_trades.csv", index=False, encoding="utf-8-sig"); summary.to_csv(OUT_DIR / "payoff_summary.csv", index=False, encoding="utf-8-sig")
    meta = {"status": "completed", "generated_at": datetime.now().isoformat(timespec="seconds"), "live_eligible": False, "entry": "accepted entry-day 10:30 30m close", "limitations": "30m OHLC proxy; unknown intra-bar order is stop-first; no tick slippage, price limits, or sector/mainline validation"}
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "REPORT_CN.md").write_text("# 前高突破：10:30执行价代理回放 v1\n\n仅研究，不进入G3。入场为通过30m承接确认后的10:30收盘价；止损优先。\n\n" + summary.to_markdown(index=False) + "\n", encoding="utf-8")
    print(json.dumps({**meta, "trades": len(closed)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

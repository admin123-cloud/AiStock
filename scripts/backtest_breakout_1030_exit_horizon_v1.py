from __future__ import annotations

"""Compare predeclared exit horizons on the identical executable breakout entries."""

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_breakout_1030_execution_proxy_v1 import _num
from scripts.backtest_wave_style_template_strategy_v1 import _load_daily
from utils.paths import report_path


OUT_DIR = report_path("breakout_1030_exit_horizon_v1")
FEE = 0.003
CONTRACTS = {"hold5": 5, "hold8": 8, "hold12": 12}
WINDOWS = [("validation_2025_local", "2025-01-01", "2025-12-31"), ("blind_2026", "2026-01-01", "2026-05-15"), ("local_2025_2026", "2025-01-01", "2026-05-15")]
LOCAL_30M_ROOT = Path(r"F:\Stock\AiStockData\data\warehouse\qmt_datadir_minute_v1")


def _simulate(row: pd.Series, m30: pd.DataFrame, daily: pd.DataFrame, sessions: int) -> dict | None:
    entry_date = pd.Timestamp(row["entry_date"]).normalize()
    entry = _num(row["m30_cutoff_close"])
    if entry <= 0:
        return None
    breakout = _num(row.get("high20_prev"))
    stop = max(entry * .95, breakout * .99) if breakout > 0 else entry * .95
    stop = min(stop, entry * .995)
    tp = entry * 1.10
    realized, remaining, took_profit = 0.0, 1.0, False
    reason, exit_date, runner_exit = f"hold{sessions}", entry_date, entry

    def process(low: float, high: float, close: float, day: pd.Timestamp, previous_low: float | None) -> bool:
        nonlocal realized, remaining, took_profit, reason, exit_date, runner_exit
        if low > 0 and low <= stop:
            realized += remaining * (stop / entry - 1.0); remaining = 0.0
            reason = "breakout_invalidation_stop"; exit_date = day; runner_exit = stop
            return True
        if not took_profit and high >= tp:
            realized += .5 * (tp / entry - 1.0); remaining = .5; took_profit = True
        if took_profit and previous_low and close < previous_low:
            realized += remaining * (close / entry - 1.0); remaining = 0.0
            reason = "runner_prev_low_break"; exit_date = day; runner_exit = close
            return True
        return False

    same_day = m30[(m30["trade_date"] == entry_date) & (m30["time"] > "10:30:00")].sort_values("datetime").reset_index(drop=True)
    for pos, bar in enumerate(same_day.itertuples(index=False)):
        previous_low = _num(same_day.iloc[pos - 1]["low"]) if took_profit and pos else None
        if process(_num(bar.low), _num(bar.high), _num(bar.close), entry_date, previous_low):
            break
    if remaining > 0:
        future = daily[daily["trade_date"] > entry_date].head(max(sessions - 1, 0)).reset_index(drop=True)
        for pos, bar in enumerate(future.itertuples(index=False)):
            day = pd.Timestamp(bar.trade_date).normalize()
            previous_low = _num(future.iloc[pos - 1]["low"]) if took_profit and pos else None
            if process(_num(bar.low), _num(bar.high), _num(bar.close), day, previous_low):
                break
            if pos == len(future) - 1:
                realized += remaining * (_num(bar.close) / entry - 1.0); remaining = 0.0
                exit_date = day; runner_exit = _num(bar.close)
    if remaining > 0:
        realized += remaining * (_num(row["m30_cutoff_close"]) / entry - 1.0); remaining = 0.0
        reason = "insufficient_future_bars"
    out = row.to_dict()
    out.update({"entry_price": entry, "exit_date": exit_date.strftime("%Y-%m-%d"), "net_ret": realized - FEE, "gross_ret": realized, "exit_reason": reason, "took_profit": took_profit, "stop_price": stop, "take_profit_price": tp, "runner_exit_price": runner_exit, "contract": f"hold{sessions}"})
    return out


def _metrics(frame: pd.DataFrame) -> dict:
    ret = pd.to_numeric(frame["net_ret"], errors="coerce").dropna()
    wins, losses = ret[ret > 0], ret[ret <= 0]
    return {"trades": int(len(ret)), "expectation": float(ret.mean()) if len(ret) else np.nan, "win_rate": float((ret > 0).mean()) if len(ret) else np.nan, "avg_win": float(wins.mean()) if len(wins) else np.nan, "avg_loss": float(losses.mean()) if len(losses) else np.nan, "payoff_ratio": float(wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else np.nan, "stop_rate": float(frame["exit_reason"].eq("breakout_invalidation_stop").mean()) if len(frame) else np.nan, "tp_rate": float(frame["took_profit"].mean()) if len(frame) else np.nan}


def _load_inputs_fast() -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    import pyarrow.dataset as ds

    source = report_path("breakout_entry_30m_acceptance_v1", "audited_entries.csv")
    entries = pd.read_csv(source, dtype={"code6": str})
    entries["entry_date"] = pd.to_datetime(entries["entry_date"], errors="coerce").dt.normalize()
    entries = entries[(entries["m30_status"] == "ok") & (entries["m30_native_accept"] == True) & (entries["entry_date"] >= pd.Timestamp("2025-01-01"))].copy()
    entries["code6"] = entries["code6"].astype(str).str.zfill(6)
    daily = _load_daily("2025-01-01", "2026-06-30").copy()
    daily["trade_date"] = pd.to_datetime(daily["trade_date"], errors="coerce").dt.normalize()
    daily["code6"] = daily["code6"].astype(str).str.zfill(6)
    paths = {str(k): x.sort_values("trade_date") for k, x in daily[daily["code6"].isin(set(entries["code6"]))].groupby("code6")}
    dataset = ds.dataset(LOCAL_30M_ROOT, format="parquet", partitioning="hive")
    end = min(entries["entry_date"].max() + pd.Timedelta(days=20), pd.Timestamp("2026-06-30")).strftime("%Y-%m-%d")
    expression = (
        (ds.field("period") == "30m")
        & ds.field("code").isin(sorted(entries["code"].astype(str).unique()))
        & (ds.field("trade_date") >= "2025-01-01")
        & (ds.field("trade_date") <= end)
    )
    bars = dataset.to_table(filter=expression, columns=["code", "datetime", "trade_date", "open", "high", "low", "close"]).to_pandas()
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trade_date"] = bars["datetime"].dt.normalize()
    bars["time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    mpaths = {str(k): x.dropna(subset=["datetime", "close"]).sort_values("datetime") for k, x in bars.groupby("code")}
    return entries, mpaths, paths


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    entries, mpaths, paths = _load_inputs_fast()
    all_closed, rows = [], []
    for name, sessions in CONTRACTS.items():
        closed = pd.DataFrame([out for _, row in entries.iterrows() if (out := _simulate(row, mpaths.get(str(row["code"]), pd.DataFrame()), paths.get(str(row["code6"]), pd.DataFrame()), sessions)) is not None])
        closed["contract"] = name
        all_closed.append(closed)
        for window, start, end in WINDOWS:
            metric = _metrics(closed[closed["entry_date"].between(pd.Timestamp(start), pd.Timestamp(end))])
            rows.append({"contract": name, "window": window, **metric})
    summary = pd.DataFrame(rows)
    combined = pd.concat(all_closed, ignore_index=True)
    combined.to_csv(OUT_DIR / "closed_trades.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "payoff_summary.csv", index=False, encoding="utf-8-sig")
    meta = {"status": "completed", "research_only": True, "generated_at": datetime.now().isoformat(timespec="seconds"), "same_entries_per_contract": int(len(entries)), "contracts": CONTRACTS, "entry": "same accepted 10:30 30m close", "coverage": "local QMT parquet covers 2025-01 onward only; this is a partial validation plus blind replay", "limitations": "30m OHLC stop-first proxy; no tick slippage, price limits, or portfolio capacity simulation"}
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    report = ["# 前高突破启动：同样本退出期限审计 v1", "", "所有合同使用同一批已通过 30m 承接的入场，不减少候选；差异只来自退出期限。", "", summary.to_markdown(index=False), ""]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8", newline="\n")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

"""Audit the native 30m acceptance state after a daily breakout signal.

The original daily study entered at the following open.  This audit does not
pretend that its returns are the returns of a 10:30 purchase.  It only tests
whether the predeclared native G3 semantic (30m close >= 30m MA20) separates
the payoff outcomes of that fixed entry list.  A later execution replay must
still price the actual 10:30 entry and same-day stop path.
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

from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


SOURCE = report_path("breakout_risk_reward_contract_v1", "closed_trades.csv")
OUT_DIR = report_path("breakout_entry_30m_acceptance_v1")
WINDOWS = [
    ("train_2020_2023", "2020-01-01", "2023-12-31"),
    ("validation_2024_2025", "2024-01-01", "2025-12-31"),
    ("blind_2026", "2026-01-01", "2026-05-15"),
    ("full", "2020-01-01", "2026-05-15"),
]
CUTOFF = "10:30:00"


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _load_bars(entries: pd.DataFrame) -> pd.DataFrame:
    """Fetch just enough history for MA20 and the entry morning, per code."""
    parts: list[pd.DataFrame] = []
    for code, g in entries.groupby("code"):
        start = (g["entry_date"].min() - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        end = g["entry_date"].max().strftime("%Y-%m-%d")
        part = clickhouse_query_df(
            f"""
            SELECT code, datetime, open, high, low, close, amount
            FROM kline_minute_30
            WHERE code = {_quote(code)}
              AND toDate(datetime) BETWEEN toDate({_quote(start)}) AND toDate({_quote(end)})
            ORDER BY datetime
            """
        )
        if not part.empty:
            parts.append(part)
    bars = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if bars.empty:
        return bars
    bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
    bars["trade_date"] = bars["datetime"].dt.normalize()
    bars["time"] = bars["datetime"].dt.strftime("%H:%M:%S")
    for col in ["open", "high", "low", "close", "amount"]:
        bars[col] = pd.to_numeric(bars[col], errors="coerce")
    return bars.dropna(subset=["datetime", "close"]).sort_values(["code", "datetime"])


def _features(entries: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    by_code = {str(k): x.reset_index(drop=True) for k, x in bars.groupby("code")} if not bars.empty else {}
    rows = []
    for idx, row in entries.iterrows():
        x = by_code.get(str(row["code"]))
        base = {"_idx": idx, "m30_status": "missing"}
        if x is None:
            rows.append(base); continue
        until = x[(x["trade_date"] == row["entry_date"]) & (x["time"] <= CUTOFF)].copy()
        hist = x[x["datetime"] <= (row["entry_date"] + pd.Timedelta(hours=10, minutes=30))].tail(20)
        if until.empty or len(hist) < 20:
            base["m30_status"] = "insufficient_history"; rows.append(base); continue
        last = until.iloc[-1]
        ma20 = float(hist["close"].mean())
        day_open = float(until.iloc[0]["open"])
        low = float(until["low"].min())
        base.update({
            "m30_status": "ok", "m30_cutoff_close": float(last["close"]),
            "m30_ma20": ma20, "m30_close_above_ma20": float(last["close"]) / ma20 - 1.0 if ma20 else np.nan,
            "m30_morning_ret": float(last["close"]) / day_open - 1.0 if day_open else np.nan,
            "m30_morning_drawdown": low / day_open - 1.0 if day_open else np.nan,
            # This is deliberately the existing native semantic, not a new tuned threshold.
            "m30_native_accept": bool(float(last["close"]) >= ma20),
        })
        rows.append(base)
    return entries.join(pd.DataFrame(rows).set_index("_idx"))


def _metrics(x: pd.DataFrame) -> dict[str, object]:
    r = pd.to_numeric(x["net_ret"], errors="coerce").dropna()
    wins, losses = r[r > 0], r[r <= 0]
    return {
        "trades": len(r), "win_rate": (r > 0).mean() if len(r) else np.nan,
        "expectation": r.mean() if len(r) else np.nan, "avg_win": wins.mean() if len(wins) else np.nan,
        "avg_loss": losses.mean() if len(losses) else np.nan,
        "payoff_ratio": wins.mean() / abs(losses.mean()) if len(wins) and len(losses) else np.nan,
        "worst_trade": r.min() if len(r) else np.nan,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    d = pd.read_csv(SOURCE, dtype={"code6": str})
    d = d[(d["variant"] == "breakout_base") & (d["contract"] == "rr_a_8d_5stop_10tp")].copy()
    d["entry_date"] = pd.to_datetime(d["entry_date"]).dt.normalize()
    d["code"] = d.get("code_raw", d.get("code")).astype(str)
    bars = _load_bars(d)
    audited = _features(d, bars)
    rows = []
    for label, start, end in WINDOWS:
        part = audited[audited["entry_date"].between(start, end)]
        for state, sample in [("all", part), ("m30_native_accept", part[part["m30_native_accept"] == True]), ("m30_native_reject", part[part["m30_native_accept"] == False]), ("m30_missing", part[part["m30_status"] != "ok"])]:
            result = _metrics(sample); result.update({"window": label, "state": state}); rows.append(result)
    summary = pd.DataFrame(rows)
    audited.to_csv(OUT_DIR / "audited_entries.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(OUT_DIR / "payoff_by_30m_state.csv", index=False, encoding="utf-8-sig")
    meta = {"status": "completed", "generated_at": datetime.now().isoformat(timespec="seconds"), "live_eligible": False, "semantics": "entry-day 10:30 30m close >= trailing 20 bars MA20", "limitation": "outcomes retain the prior daily next-open entry prices; this is an association audit, not executable 10:30-entry performance"}
    (OUT_DIR / "summary.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    report = "# 前高突破：30分钟承接关联审计 v1\n\n仅研究。采用 G3 已有的原生语义：入场日 10:30 的30分钟收盘价不低于过去20根30分钟K线均线。\n\n**重要：** 本表仍沿用前序日线研究的次日开盘入场收益，所以只能说明承接状态与结果的关联，不能代表10:30实际买入的收益。\n\n" + summary.to_markdown(index=False) + "\n"
    (OUT_DIR / "REPORT_CN.md").write_text(report, encoding="utf-8")
    print(json.dumps({**meta, "entries": int(len(audited)), "m30_ok": int((audited["m30_status"] == "ok").sum())}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

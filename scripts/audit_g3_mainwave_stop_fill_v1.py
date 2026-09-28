"""Reconstruct post-2024-09 hard-stop price paths from stored 30-minute bars.

The result distinguishes observable stop-trigger fills from an intentionally
adverse bar-low bound.  It is research-only and never treats a bar low as a
guaranteed executable price.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.backtest_g3_recalled_mainwave_contract_v1 as replay
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


OUT_DIR = report_path("g3_mainwave_stop_fill_audit_v1")
TRADES_PATH = report_path("g3_mainwave_execution_stress_v1", "veto_plus_2loss_3day_cooldown_trades.csv")
START = pd.Timestamp("2024-09-24")


def _load_bars(stops: pd.DataFrame) -> pd.DataFrame:
    clauses = []
    for row in stops.itertuples():
        code = str(row.code_raw).replace("'", "''")
        day = pd.Timestamp(row.exit_date).strftime("%Y-%m-%d")
        clauses.append(f"(code='{code}' AND toDate(datetime)=toDate('{day}'))")
    if not clauses:
        return pd.DataFrame()
    sql = (
        "SELECT code,datetime,open,high,low,close,volume FROM kline_minute_30 WHERE "
        + " OR ".join(clauses)
        + " ORDER BY code,datetime"
    )
    bars = clickhouse_query_df(sql)
    if not bars.empty:
        bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
        for column in ["open", "high", "low", "close", "volume"]:
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
    return bars


def _reconstruct(stops: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for row in stops.itertuples():
        part = bars[(bars["code"].astype(str).eq(str(row.code_raw))) & (bars["datetime"].dt.normalize().eq(pd.Timestamp(row.exit_date).normalize()))].copy()
        if pd.Timestamp(row.exit_date).normalize() == pd.Timestamp(row.entry_datetime).normalize():
            part = part[part["datetime"] > pd.Timestamp(row.entry_datetime)]
        breached = part[part["low"] <= float(row.hard_stop)]
        record = row._asdict()
        record.update({"bar_coverage": len(part), "first_breach_datetime": None, "trigger_fill_price": None,
                       "bar_low_bound_price": None, "gap_below_stop": None, "stop_fill_gap": None})
        if not breached.empty:
            bar = breached.sort_values("datetime").iloc[0]
            trigger_fill = min(float(bar["open"]), float(row.hard_stop))
            record.update({
                "first_breach_datetime": pd.Timestamp(bar["datetime"]).strftime("%Y-%m-%d %H:%M:%S"),
                "trigger_fill_price": trigger_fill,
                "bar_low_bound_price": float(bar["low"]),
                "gap_below_stop": bool(float(bar["open"]) < float(row.hard_stop)),
                "stop_fill_gap": trigger_fill / float(row.entry_price) - 1.0,
                "bar_low_bound_ret": float(bar["low"]) / float(row.entry_price) - 1.0,
            })
        rows.append(record)
    return pd.DataFrame(rows)


def _curve_metrics(trades: pd.DataFrame) -> dict:
    _, funded = replay._two_slot_funded_curve(trades)
    return {**replay._summary(trades), **funded}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    trades = pd.read_csv(TRADES_PATH, encoding="utf-8-sig")
    trades["entry_datetime"] = pd.to_datetime(trades["entry_datetime"], errors="coerce")
    trades["exit_date"] = pd.to_datetime(trades["exit_date"], errors="coerce")
    post = trades[trades["entry_datetime"].ge(START)].copy()
    stops = post[post["exit_reason"].eq("hard_stop")].copy()
    bars = _load_bars(stops)
    audit = _reconstruct(stops, bars)
    audit.to_csv(OUT_DIR / "post_2024_09_hard_stop_fill_audit.csv", index=False, encoding="utf-8-sig")
    observed = audit[audit["trigger_fill_price"].notna()].copy()
    trigger = post.copy()
    worst = post.copy()
    observed_by_key = observed.set_index(["code_raw", "entry_datetime"])
    for frame, field in [(trigger, "trigger_fill_price"), (worst, "bar_low_bound_price")]:
        for idx, trade in frame[frame["exit_reason"].eq("hard_stop")].iterrows():
            key = (trade["code_raw"], pd.Timestamp(trade["entry_datetime"]))
            if key in observed_by_key.index:
                price = float(observed_by_key.loc[key, field])
                frame.loc[idx, "net_ret"] = price / float(trade["entry_price"]) - 1.0 - replay.FEE
    summary = {
        "status": "completed", "research_only": True, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope": "weak-rebound-veto plus two-loss-three-day-cooldown, entries from 2024-09-24",
        "hard_stop_trades": int(len(stops)), "observable_30m_breach": int(len(observed)),
        "unconfirmed_by_30m_data": int(len(stops) - len(observed)),
        "gap_below_stop_count": int(observed["gap_below_stop"].sum()) if len(observed) else 0,
        "base": _curve_metrics(post), "observable_trigger_fill": _curve_metrics(trigger),
        "adverse_first_breach_bar_low_bound": _curve_metrics(worst),
        "limitations": "Trigger fill uses min(first breach bar open, stop), still not a broker fill. Bar-low bound is deliberately adverse and not executable-price evidence. 30m absence is reported, never imputed.",
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# G3机构主升硬止损30分钟成交偏差审计 v1", "", "- 仅研究，不改运行合同、影子账本或下单链路。",
             "- `observable_trigger_fill`：首根触发K线的 `min(open, stop)`；`bar_low_bound`：该根最低价的故意悲观边界，不能视为可成交价。", "",
             "## 结果", "", "```json", json.dumps(summary, ensure_ascii=False, indent=2), "```", ""]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

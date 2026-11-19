"""Daily forward-paper ledger for panic_stock_reversal_shadow_v3.

The updater deliberately separates signal generation (after the signal-day close)
from next-session execution and settlement. It never reads next-day OHLCV while
creating a signal.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_sector_first_pullback_panic_v1 import _load_market_breadth
from utils.market_warehouse import clickhouse_query_df
from utils.paths import runtime_path


STRATEGY_ID = "panic_stock_reversal_shadow_v3"
DOWN_RATIO = 0.60
TOP = 6
COOLDOWN = 5
COST_BPS = 50.0
GAP_MIN = -0.09
GAP_MAX = -0.05


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"strategy_id": STRATEGY_ID, "records": [], "signal_dates": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError(f"invalid paper ledger: {path}")
    data.setdefault("strategy_id", STRATEGY_ID)
    data.setdefault("signal_dates", [])
    return data


def _write_outputs(path: Path, ledger: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = ledger.get("records", [])
    frame = pd.DataFrame(rows)
    frame.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    settled = frame[frame.get("status", pd.Series(dtype=str)).eq("settled")].copy() if not frame.empty else frame
    if not settled.empty:
        settled["trade_date"] = pd.to_datetime(settled["signal_date"])
        event = settled.groupby("trade_date")["net_return"].mean().sort_index()
        equity = (1.0 + event).cumprod()
        curve = pd.DataFrame({"trade_date": event.index, "strategy_return": event.values, "equity": equity.values})
        curve.to_csv(path.with_name("equity_curve.csv"), index=False)
        summary = {
            "strategy_id": STRATEGY_ID,
            "settled_positions": int(len(settled)),
            "settled_events": int(len(event)),
            "total_return": float(equity.iloc[-1] - 1.0),
            "max_drawdown": float((equity / equity.cummax() - 1.0).min()),
        }
        if "gross_return" not in settled.columns:
            settled["gross_return"] = settled["net_return"] + float(ledger.get("cost_bps", COST_BPS)) / 10000.0
        stress = settled.assign(stress_return=settled["gross_return"] - 0.01).groupby("trade_date")["stress_return"].mean().sort_index()
        stress_equity = (1.0 + stress).cumprod()
        summary.update(
            {
                "cost_bps": float(ledger.get("cost_bps", COST_BPS)),
                "stress_100bps_total_return": float(stress_equity.iloc[-1] - 1.0),
                "stress_100bps_max_drawdown": float((stress_equity / stress_equity.cummax() - 1.0).min()),
            }
        )
    else:
        pd.DataFrame(columns=["trade_date", "strategy_return", "equity"]).to_csv(
            path.with_name("equity_curve.csv"), index=False
        )
        summary = {
            "strategy_id": STRATEGY_ID,
            "settled_positions": 0,
            "settled_events": 0,
            "total_return": 0.0,
            "max_drawdown": 0.0,
            "cost_bps": float(ledger.get("cost_bps", COST_BPS)),
            "stress_100bps_total_return": 0.0,
            "stress_100bps_max_drawdown": 0.0,
        }
    summary.update(
        {
            "planned_positions": int(sum(r.get("status") == "planned" for r in rows)),
            "unfilled_positions": int(sum(str(r.get("status", "")).startswith("unfilled") for r in rows)),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
        }
    )
    path.with_name("summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def _calendar(start: str, end: str) -> list[pd.Timestamp]:
    df = clickhouse_query_df(
        "SELECT trade_date FROM kline_daily WHERE code = '000300.SH' AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [start, end],
    )
    return sorted(pd.Timestamp(x) for x in pd.to_datetime(df["trade_date"], errors="coerce").dropna().unique())


def _latest_daily_date() -> str:
    value = clickhouse_query_df("SELECT max(trade_date) AS latest FROM kline_daily").iloc[0]["latest"]
    if pd.isna(value):
        raise RuntimeError("kline_daily has no latest trade date")
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _market_gate(as_of: str, start: str) -> bool:
    idx = clickhouse_query_df(
        """
        SELECT trade_date, close FROM kline_daily
        WHERE code = '000300.SH' AND trade_date BETWEEN ? AND ? ORDER BY trade_date
        """,
        [start, as_of],
    )
    if idx.empty:
        return False
    idx["trade_date"] = pd.to_datetime(idx["trade_date"], errors="coerce")
    idx["close"] = pd.to_numeric(idx["close"], errors="coerce")
    idx = idx.dropna().sort_values("trade_date")
    return bool(len(idx) >= 60 and idx.iloc[-1]["close"] >= idx["close"].rolling(60).mean().iloc[-1])


def _signal_candidates(as_of: str, warmup_start: str) -> pd.DataFrame:
    breadth = _load_market_breadth(warmup_start, as_of)
    breadth["trade_date"] = pd.to_datetime(breadth["trade_date"])
    row = breadth[breadth["trade_date"].eq(pd.Timestamp(as_of))]
    if row.empty or float(row.iloc[-1]["down_ratio"]) < DOWN_RATIO or not _market_gate(as_of, warmup_start):
        return pd.DataFrame()
    sql = """
    WITH features AS (
      SELECT code, trade_date, close,
        lagInFrame(close, 15) OVER (
          PARTITION BY code ORDER BY trade_date
          ROWS BETWEEN 15 PRECEDING AND 15 PRECEDING
        ) AS close_15d
      FROM kline_daily
      WHERE code IN (SELECT code FROM stocks WHERE type = 'stock')
        AND trade_date BETWEEN toDate(?) AND toDate(?)
    )
    SELECT code, trade_date, close, close_15d
    FROM features
    WHERE trade_date = toDate(?) AND close > 0 AND close_15d > 0
    """
    d = clickhouse_query_df(sql, [warmup_start, as_of, as_of])
    if d.empty:
        return d
    d["trade_date"] = pd.to_datetime(d["trade_date"], errors="coerce")
    for col in ["close", "close_15d"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["runup_15d"] = d["close"] / d["close_15d"] - 1.0
    # Gap is only observable on the next session. Keep the full momentum
    # candidate set here; _settle applies the gap filter and then Top6 rank.
    d = d[d["runup_15d"] >= 0.20].sort_values("runup_15d", ascending=False).copy()
    d["signal_date"] = pd.Timestamp(as_of).strftime("%Y-%m-%d")
    return d


def _settle(records: list[dict[str, Any]], as_of: str, cost_bps: float) -> None:
    pending = [r for r in records if r.get("status") == "planned" and r.get("entry_date", "") <= as_of]
    if not pending:
        return
    pairs = ",".join(
        f"('{r['code']}', toDate('{r['entry_date']}'))" for r in pending
    )
    bars = clickhouse_query_df(
        f"SELECT code, trade_date, open, close, volume, amount FROM kline_daily WHERE (code, trade_date) IN ({pairs})"
    )
    if not bars.empty:
        bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce")
    if bars.empty:
        for rec in pending:
            rec["status"] = "unfilled_missing_data"
        return
    bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    bars = bars.rename(columns={"trade_date": "entry_date"})
    bar_frame = bars.set_index(["code", "entry_date"])
    for signal_date in sorted({r["signal_date"] for r in pending}):
        group = [r for r in pending if r["signal_date"] == signal_date]
        valid: list[tuple[dict[str, Any], Any]] = []
        for rec in group:
            key = (rec["code"], rec["entry_date"])
            if key not in bar_frame.index:
                rec["status"] = "unfilled_missing_data"
                continue
            bar = bar_frame.loc[key]
            gap = float(bar["open"]) / float(rec["signal_close"]) - 1.0
            rec["observed_gap"] = gap
            if not (GAP_MIN <= gap <= GAP_MAX):
                rec["status"] = "unfilled_gap"
                continue
            valid.append((rec, bar))
        valid.sort(key=lambda item: float(item[0].get("runup_15d", 0.0)), reverse=True)
        selected = valid[:TOP]
        for rec, bar in valid[TOP:]:
            rec["status"] = "unfilled_rank_capacity"
        for rec, bar in selected:
            if float(bar["volume"] or 0) <= 0 or float(bar["amount"] or 0) <= 0:
                rec["status"] = "unfilled_no_turnover"
                continue
            limit_pct = 0.195 if rec["code"].startswith(("300", "688")) else 0.095
            if float(bar["open"]) / float(rec["signal_close"]) - 1.0 <= -limit_pct + 0.002:
                rec["status"] = "unfilled_limit_down_like"
                continue
            rec.update(
                {
                    "status": "settled",
                    "actual_entry_open": float(bar["open"]),
                    "exit_close": float(bar["close"]),
                    "gross_return": float(bar["close"]) / float(bar["open"]) - 1.0,
                    "net_return": float(bar["close"]) / float(bar["open"]) - 1.0 - cost_bps / 10000.0,
                    "cost_bps": cost_bps,
                    "settled_at": as_of,
                }
            )


def run(as_of: str, ledger_path: Path, cost_bps: float = COST_BPS) -> dict[str, Any]:
    raise RuntimeError(
        "Paper execution disabled: newly purchased A-share stocks cannot be sold "
        "on the entry day (T+1). Historical ledger is a non-executable price proxy."
    )
    as_of_ts = pd.Timestamp(as_of)
    warmup_start = (as_of_ts - pd.Timedelta(days=120)).strftime("%Y-%m-%d")
    ledger = _read_ledger(ledger_path)
    records = ledger["records"]
    existing_cost = ledger.get("cost_bps")
    if existing_cost is not None and abs(float(existing_cost) - cost_bps) > 1e-9:
        raise ValueError(f"ledger cost_bps={existing_cost} differs from requested cost_bps={cost_bps}")
    ledger["cost_bps"] = cost_bps
    previous_as_of = ledger.get("last_as_of")
    if previous_as_of and pd.Timestamp(as_of) < pd.Timestamp(previous_as_of):
        raise ValueError(f"as-of {as_of} is earlier than existing ledger last_as_of {previous_as_of}")
    calendar = _calendar(warmup_start, as_of)
    # Resolve signals recorded after a close but before the next session was
    # present in the warehouse.
    for rec in records:
        if rec.get("status") == "planned" and not rec.get("entry_date"):
            future = [d for d in calendar if d > pd.Timestamp(rec["signal_date"])]
            if future:
                rec["entry_date"] = future[0].strftime("%Y-%m-%d")
    _settle(records, as_of, cost_bps)
    signal_ts = as_of_ts
    if signal_ts in calendar and str(as_of) not in set(ledger.get("signal_dates", [])):
        idx = calendar.index(signal_ts)
        next_date = calendar[idx + 1].strftime("%Y-%m-%d") if idx + 1 < len(calendar) else ""
        last_dates = sorted(pd.Timestamp(x) for x in ledger.get("signal_dates", []))
        if last_dates:
            last_idx = max((i for i, d in enumerate(calendar) if d <= last_dates[-1]), default=-1)
            allowed = idx - last_idx >= COOLDOWN + 1
        else:
            allowed = True
        candidates = pd.DataFrame()
        if allowed:
            candidates = _signal_candidates(as_of, warmup_start)
            for row in candidates.itertuples():
                records.append(
                    {
                        "strategy_id": STRATEGY_ID,
                        "signal_date": as_of,
                        "entry_date": next_date,
                        "code": row.code,
                        "signal_close": float(row.close),
                        "runup_15d": float(row.runup_15d),
                        "status": "planned",
                        "formal_buy_signal": False,
                        "auto_order_allowed": False,
                    }
                )
        if allowed and not candidates.empty:
            ledger.setdefault("signal_dates", []).append(as_of)
    ledger["last_as_of"] = as_of
    ledger["strategy_id"] = STRATEGY_ID
    _write_outputs(ledger_path, ledger)
    return json.loads(ledger_path.with_name("summary.json").read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", help="signal-day close date, YYYY-MM-DD; defaults to latest kline_daily date")
    parser.add_argument("--ledger-path", default=str(runtime_path("panic_stock_reversal_shadow_v3", "paper_ledger.json")))
    parser.add_argument("--cost-bps", type=float, default=COST_BPS, help="base round-trip cost used for net paper return")
    args = parser.parse_args()
    as_of = args.as_of or _latest_daily_date()
    print(json.dumps(run(as_of, Path(args.ledger_path), args.cost_bps), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

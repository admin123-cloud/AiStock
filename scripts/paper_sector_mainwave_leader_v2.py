"""Forward paper ledger for the sector mainwave/leader shadow contract."""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path

LEDGER_DIR = report_path("sector_mainwave_leader_forward_v2")
LEDGER = LEDGER_DIR / "ledger.json"


def _next_dates(dates: pd.Series, start: pd.Timestamp, n: int) -> pd.Timestamp | None:
    vals = sorted(pd.Timestamp(x) for x in dates.dropna().unique() if pd.Timestamp(x) > start)
    return vals[n - 1] if len(vals) >= n else None


def advance_rows(ledger: list[dict], dates: pd.Series, px: pd.DataFrame) -> None:
    """Resolve pending dates on every run; prices are diagnostics, never fills."""
    for row in ledger:
        row.update(research_only=True, execution_valid=False, auto_order_allowed=False,
                   evidence_type="open_price_event_proxy_not_verified_fill")
        row.setdefault("recorded_at", datetime.now(ZoneInfo("Asia/Shanghai")).isoformat())
        row["is_independent_forward"] = False  # Existing scans have no pre-entry timestamp proof.
        for key in ["entry_open", "exit_3d_open", "exit_5d_open",
                    "net_return_3d_200bps", "net_return_5d_200bps"]:
            row[key] = None
        row["cost_scenarios"] = {}
        e = _next_dates(dates, pd.Timestamp(row["signal_date"]), 1)
        row["entry_date"] = e.strftime("%Y-%m-%d") if e is not None else None
        row["status"] = "pending_future_entry_date"
        if e is None:
            continue
        p = px[px.code.eq(row["code"])].set_index("trade_date")["open"]
        if p.index.has_duplicates:
            row["status"] = "blocked_duplicate_prices"
            continue
        def valid(date):
            return date in p.index and pd.notna(p.loc[date]) and math.isfinite(float(p.loc[date])) and float(p.loc[date]) > 0
        row["status"] = "pending_entry_open"
        if not valid(e):
            continue
        row["entry_open"] = float(p.loc[e])
        row["status"] = "pending_exit_open"
        for horizon in (3, 5):
            x = _next_dates(dates, e, horizon)
            row[f"exit_{horizon}d_date"] = x.strftime("%Y-%m-%d") if x is not None else None
            if x is None or not valid(x):
                continue
            gross = float(p.loc[x] / p.loc[e] - 1)
            row[f"exit_{horizon}d_open"] = float(p.loc[x])
            row[f"net_return_{horizon}d_200bps"] = gross - .02
            row["cost_scenarios"][str(horizon)] = {str(bps): gross - bps / 10000 for bps in (100, 200, 300)}
            row["status"] = f"price_proxy_settled_{horizon}d"


def run(as_of: str | None = None) -> dict:
    scan_path = report_path("sector_mainwave_leader_rebuild_v2") / "current_shadow_scan.json"
    scan = json.loads(scan_path.read_text(encoding="utf-8"))
    if as_of and pd.Timestamp(as_of) != pd.Timestamp(scan["as_of"]):
        raise ValueError("as_of must match the frozen scan date")
    signal_date = pd.Timestamp(as_of or scan["as_of"])
    sectors = pd.DataFrame(scan["sector_candidates"])
    leaders = pd.DataFrame(scan["leaders"])
    if sectors.empty or leaders.empty:
        raise RuntimeError("current scan has no candidates")
    sectors = sectors.sort_values(["breadth", "avg_ret15"], ascending=False)
    selected = []
    for sector in sectors.sector_code.tolist():
        sub = leaders[leaders.sector_code.eq(sector)].sort_values(["ret15", "amount20"], ascending=False)
        if not sub.empty:
            selected.append(sub.iloc[0].to_dict())
        if len(selected) >= 3:
            break
    # Use the warehouse trading dates and keep signal/entry/settlement separate.
    dates = clickhouse_query_df("SELECT DISTINCT trade_date FROM kline_daily ORDER BY trade_date")["trade_date"]
    dates = pd.to_datetime(dates, errors="coerce").dropna()
    entry_date = _next_dates(dates, signal_date, 1)
    entry_available = entry_date is not None
    if entry_date is None:
        # Never substitute the signal date: that would create an invalid T+0 fill.
        entry_date = signal_date
    rows = []
    for item in selected:
        rows.append({
            "signal_date": signal_date.strftime("%Y-%m-%d"),
            "entry_date": entry_date.strftime("%Y-%m-%d") if entry_available else None,
            "sector_code": str(item["sector_code"]),
            "code": str(item["code"]),
            "signal_ret15": float(item["ret15"]),
            "signal_amount20": float(item["amount20"]),
            "status": "pending_entry_open" if entry_available else "pending_future_entry_date",
            "entry_open": None,
            "exit_3d_open": None,
            "exit_5d_open": None,
            "net_return_3d_200bps": None,
            "net_return_5d_200bps": None,
        })
    ledger = []
    if LEDGER.exists():
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    if not any(str(x.get("signal_date")) == signal_date.strftime("%Y-%m-%d") for x in ledger):
        ledger.extend(rows)
    # Settle only when both entry and exit opens exist.
    codes = sorted({r["code"] for r in ledger})
    if codes:
        quoted = ",".join("'" + c.replace("'", "\\'") + "'" for c in codes)
        px = clickhouse_query_df(f"SELECT code, trade_date, open FROM kline_daily WHERE code IN ({quoted}) AND trade_date >= '2026-07-01'")
        px["trade_date"] = pd.to_datetime(px["trade_date"], errors="coerce")
        px["open"] = pd.to_numeric(px["open"], errors="coerce")
        advance_rows(ledger, dates, px)
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    if LEDGER.exists():
        backup = LEDGER_DIR / ("ledger_before_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
        backup.write_bytes(LEDGER.read_bytes())
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    tmp.replace(LEDGER)
    return {"research_only": True, "ledger": str(LEDGER), "records": len(ledger), "latest_signal_date": signal_date.strftime("%Y-%m-%d"), "pending": sum(x.get("status", "").startswith("pending") for x in ledger)}


if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("--as-of"); print(json.dumps(run(p.parse_args().as_of), ensure_ascii=False, indent=2))

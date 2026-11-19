"""Validate WStock 5m by aggregating to a daily bar and comparing to the formal
daily table (stock.kline_daily), which is maintained from an independent feed.

This is the strongest available check: if WStock's 48 bars aggregate to the same
OHLC as the formal daily bar, the intraday series is coherent with the daily
record even when it disagrees with our purchased 5m feed.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import report_path

QUEUE = "stock.purchased_history_data_review_items"
RUN_ID = "purchased_history_review_20260918_5m_source_closeout_v14"
WSTOCK = Path(r"E:\AStockHistory\wstock\输出\来源归档\out_2006")


def wstock_day(code: str, day: str):
    symbol, market = code.split(".")
    path = WSTOCK / market / f"{market}{symbol}.csv"
    if not path.exists():
        return None
    rows = []
    r = csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig", errors="replace")))
    next(r, None)
    for c in r:
        if len(c) < 7:
            continue
        d, _, _hm = c[0].strip().partition(" ")
        if d != day:
            continue
        rows.append((float(c[1]), float(c[2]), float(c[3]), float(c[4])))  # O,L,H,C
    if not rows:
        return None
    return {
        "open": rows[0][0],
        "high": max(x[2] for x in rows),
        "low": min(x[1] for x in rows),
        "close": rows[-1][3],
        "bars": len(rows),
    }


def main() -> int:
    year = int(sys.argv[1]) if len(sys.argv) > 1 else 2006
    tail = sys.argv[2] if len(sys.argv) > 2 else "6"

    client = clickhouse_client()
    q = client.query(
        f"""
        SELECT code, toString(trade_date) AS d
        FROM {QUEUE}
        WHERE run_id = %(run)s AND toYear(trade_date) = %(year)s
        GROUP BY code, d ORDER BY d, code
        """,
        parameters={"run": RUN_ID, "year": year},
    ).result_rows
    q = [r for r in q if str(r[0])[5] == tail]

    tally = Counter()
    details = []
    for code, day in q:
        code, day = str(code), str(day)
        w = wstock_day(code, day)
        if w is None:
            tally["no_wstock"] += 1
            continue
        d = client.query(
            "SELECT open, high, low, close FROM stock.kline_daily "
            "WHERE code = %(c)s AND trade_date = toDate(%(d)s)",
            parameters={"c": code, "d": day},
        ).result_rows
        if not d:
            tally["no_formal_daily"] += 1
            continue
        o, h, l, c = (float(x) for x in d[0])
        # compare with a small tolerance: WStock prints 3dp, daily 2-4dp
        tol = 0.011
        ok = abs(w["open"] - o) <= tol and abs(w["high"] - h) <= tol and abs(w["low"] - l) <= tol and abs(w["close"] - c) <= tol
        tally["daily_match" if ok else "daily_diff"] += 1
        if not ok:
            details.append({
                "code": code, "trade_date": day,
                "wstock": w,
                "formal_daily": {"open": o, "high": h, "low": l, "close": c},
                "delta": {
                    "open": round(w["open"] - o, 4),
                    "high": round(w["high"] - h, 4),
                    "low": round(w["low"] - l, 4),
                    "close": round(w["close"] - c, 4),
                },
            })

    payload = {"run_id": RUN_ID, "year": year, "tail": tail, "rows": len(q), "tally": dict(tally), "mismatches": details}
    out = report_path("purchased_history", "wstock", f"wstock_vs_formal_daily_{year}_tail{tail}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"rows": len(q), "tally": dict(tally), "report": str(out)}, ensure_ascii=False, indent=2))
    for m in details[:5]:
        print("  DIFF", m["code"], m["trade_date"], "wstock=", m["wstock"], "daily=", m["formal_daily"], "delta=", m["delta"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

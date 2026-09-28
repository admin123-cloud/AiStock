"""Independent read-back verification of the staged WStock repair data.

Re-reads the stage table and re-derives the daily aggregate straight from
ClickHouse (not from the in-memory build), then compares against the formal
daily table and against the raw wsCSV files on disk. This closes the loop:
build -> stage -> read-back -> cross-check.
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

STAGE = "stock.wstock_5m_repair_stage"
WSTOCK = Path(r"E:\AStockHistory\wstock\输出\来源归档\out_2006")
RUN = sys.argv[1] if len(sys.argv) > 1 else ""


def main() -> int:
    client = clickhouse_client()
    if not RUN:
        rows = client.query(
            f"SELECT run_id FROM {STAGE} GROUP BY run_id ORDER BY max(verified_at) DESC LIMIT 1"
        ).result_rows
        run = str(rows[0][0])
    else:
        run = RUN
    print(f"verifying run_id = {run}")

    agg = client.query(
        f"""
        SELECT code, trade_date, count() AS bars,
               argMin(open, datetime) AS o,
               max(high) AS h, min(low) AS l,
               argMax(close, datetime) AS c,
               sum(volume) AS v, sum(amount) AS a,
               countIf(open>0 AND high>0 AND low>0 AND close>0) AS valid_bars,
               uniqExact(datetime) AS uniq_bars
        FROM {STAGE}
        WHERE run_id = %(run)s
        GROUP BY code, trade_date
        ORDER BY trade_date, code
        """,
        parameters={"run": run},
    ).result_rows

    tally = Counter()
    diffs = []
    for code, day, bars, o, h, l, c, v, a, valid, uniq in agg:
        code, day = str(code), str(day)
        tally["code_days"] += 1
        if bars == 48 and uniq == 48 and valid == 48:
            tally["structurally_complete"] += 1
        else:
            tally["structurally_bad"] += 1
            diffs.append({"code": code, "trade_date": day, "bars": bars, "uniq": uniq, "valid": valid})
            continue

        d = client.query(
            "SELECT open, high, low, close FROM stock.kline_daily "
            "WHERE code = %(c)s AND trade_date = toDate(%(d)s)",
            parameters={"c": code, "d": day},
        ).result_rows
        if not d:
            tally["no_formal_daily"] += 1
            continue
        fo, fh, fl, fc = (float(x) for x in d[0])
        delta = {"open": round(float(o) - fo, 4), "high": round(float(h) - fh, 4),
                 "low": round(float(l) - fl, 4), "close": round(float(c) - fc, 4)}
        ok = all(abs(x) <= 0.011 for x in delta.values())
        tally["daily_match" if ok else "daily_diff"] += 1
        if not ok:
            diffs.append({"code": code, "trade_date": day, "delta": delta})

    # Also verify one day against the raw wsCSV to prove values survived intact.
    probe = agg[0]
    code, day = str(probe[0]), str(probe[1])
    symbol, market = code.split(".")
    path = WSTOCK / market / f"{market}{symbol}.csv"
    raw_close = {}
    if path.exists():
        r = csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig", errors="replace")))
        next(r, None)
        for cells in r:
            if len(cells) >= 7:
                d0, _, hm = cells[0].strip().partition(" ")
                if d0 == day:
                    raw_close[hm[:5]] = float(cells[4])
    stage_close = {
        str(t)[11:16]: float(cc)
        for t, cc in client.query(
            f"SELECT datetime, close FROM {STAGE} WHERE run_id=%(r)s AND code=%(c)s AND trade_date=toDate(%(d)s) ORDER BY datetime",
            parameters={"r": run, "c": code, "d": day},
        ).result_rows
    }
    matched = sum(1 for k, v in stage_close.items() if abs(raw_close.get(k, -1) - v) < 1e-9)

    payload = {
        "run_id": run, "tally": dict(tally),
        "raw_vs_stage_probe": {"code": code, "trade_date": day,
                               "stage_bars": len(stage_close), "raw_bars": len(raw_close),
                               "exact_matches": matched},
        "issues": diffs[:20],
    }
    out = report_path("purchased_history", "wstock", f"readback_{run}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload | {"report": str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

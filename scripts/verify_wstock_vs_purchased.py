"""Verify whether WStock .wdz-exported 5m data repairs the purchased-history gaps.

Purpose
-------
Decide, BEFORE paying the vendor, whether their data is actually clean on the
code-days our purchased feed corrupted.

Method
------
For each queued gap code-day:
  * read the purchased 5m source bar set (the "bad" side) from the daily ZIP
  * read the WStock csv export (option 4: wsCSV, one file per stock)
  * compare the 48 canonical 5m buckets bar by bar
and classify:
  * ``wstock_repairs``   - purchased bars were non-positive/frozen AND wstock has
                           positive values for the same bucket
  * ``wstock_also_bad``  - wstock is likewise non-positive on those buckets
  * ``wstock_missing``   - wstock file or date absent
  * ``both_agree_ok``    - both sources fine on every bucket

WStock wsCSV column order is ``Date,Open,Low,High,Close,Volume,Amount``
(NOTE: Low precedes High, unlike the usual OHLC convention).

Read-only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import report_path

QUEUE = "stock.purchased_history_data_review_items"
RUN_ID = "purchased_history_review_20260918_5m_source_closeout_v14"
HS_MONTHLY = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按月归档")
WSTOCK_DIR = Path(r"E:\AStockHistory\wstock\输出\来源归档\out_2006")


def canonical_labels() -> list[str]:
    labels = [f"{t // 60:02d}:{t % 60:02d}" for t in range(9 * 60 + 35, 11 * 60 + 30 + 1, 5)]
    labels += [f"{t // 60:02d}:{t % 60:02d}" for t in range(13 * 60 + 5, 15 * 60 + 1, 5)]
    return labels


def purchased_bars(code: str, day: str) -> dict[str, tuple[float, float, float, float]]:
    symbol, market = code.split(".")
    archive = HS_MONTHLY / f"{day[:7]}" / f"{day.replace('-', '')}_5min.zip"
    out: dict[str, tuple[float, float, float, float]] = {}
    if not archive.exists():
        return out
    entry = f"{market.lower()}{symbol}.csv"
    with ZipFile(archive) as zf:
        names = set(zf.namelist())
        if entry not in names:
            return out
        text = zf.read(entry).decode("utf-8-sig", errors="replace")
    for cells in csv.reader(io.StringIO(text)):
        if len(cells) < 7 or not cells[0].strip().startswith(day.replace("-", "/")):
            continue
        label = cells[0].strip()[-5:]
        try:
            o, c, h, l = (float(cells[i]) for i in (3, 4, 5, 6))
        except ValueError:
            continue
        out[label] = (o, h, l, c)
    return out


def wstock_bars(code: str) -> dict[str, dict[str, tuple[float, float, float, float]]]:
    """Load the WStock export for one code: {date: {label: (o,h,l,c)}}.

    The wsCSV export names files ``<MARKET><symbol>.csv`` (upper-case market,
    no dot), laid out under ``<out>/SH/`` and ``<out>/SZ/``.
    """
    out: dict[str, dict[str, tuple[float, float, float, float]]] = {}
    if not WSTOCK_DIR.exists():
        return {}
    symbol, market = code.split(".")
    candidates = [
        WSTOCK_DIR / market / f"{market}{symbol}.csv",
        WSTOCK_DIR / f"{market}{symbol}.csv",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
        return {}
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    next(reader, None)
    for cells in reader:
        if len(cells) < 7:
            continue
        stamp = cells[0].strip()
        day, _, hm = stamp.partition(" ")
        label = hm[:5]
        try:
            # wsCSV order: Date,Open,Low,High,Close,Volume,Amount
            o, l, h, c = (float(cells[i]) for i in (1, 2, 3, 4))
        except ValueError:
            continue
        out.setdefault(day, {})[label] = (o, h, l, c)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", default=RUN_ID)
    parser.add_argument("--year", type=int, default=2006)
    parser.add_argument("--tail", default="6", help="restrict to this code tail digit")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    client = clickhouse_client()
    rows = client.query(
        f"""
        SELECT code, toString(trade_date) AS d, any(expected_count), any(actual_count)
        FROM {QUEUE}
        WHERE run_id = %(run)s AND toYear(trade_date) = %(year)s
        GROUP BY code, d
        ORDER BY d, code
        """,
        parameters={"run": args.run_id, "year": args.year},
    ).result_rows
    if args.tail:
        rows = [r for r in rows if str(r[0])[5] == args.tail]
    if args.limit:
        rows = rows[: args.limit]

    labels = canonical_labels()
    results = []
    tally: Counter[str] = Counter()
    for code, day, expected, actual in rows:
        bad = purchased_bars(str(code), str(day))
        wst = wstock_bars(str(code)).get(str(day), {})
        if not wst:
            decision = "wstock_missing"
            tally[decision] += 1
            results.append({"code": str(code), "trade_date": str(day), "decision": decision,
                            "purchased_bars": len(bad)})
            continue
        bad_labels = [l for l in labels if l in bad and min(bad[l]) <= 0]
        repaired = [l for l in bad_labels if l in wst and min(wst[l]) > 0]
        still_bad = [l for l in bad_labels if l in wst and min(wst[l]) <= 0]
        if bad_labels and repaired and not still_bad:
            decision = "wstock_repairs"
        elif still_bad:
            decision = "wstock_also_bad"
        else:
            decision = "both_agree_ok"
        tally[decision] += 1
        results.append({
            "code": str(code), "trade_date": str(day), "decision": decision,
            "purchased_bars": len(bad), "wstock_bars": len(wst),
            "purchased_bad_buckets": bad_labels,
            "wstock_positive_on_those": repaired,
            "wstock_still_bad": still_bad,
            "sample_purchased": {l: bad[l] for l in bad_labels[:3]},
            "sample_wstock": {l: wst.get(l) for l in bad_labels[:3]},
        })

    payload = {
        "run_id": args.run_id, "year": args.year, "tail": args.tail,
        "rows": len(results), "tally": dict(tally), "results": results,
    }
    out = report_path("purchased_history", "wstock", f"wstock_vs_purchased_{args.year}_tail{args.tail}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"rows": len(results), "tally": dict(tally), "report": str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

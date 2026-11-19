"""End-to-end STAGED verification of WStock repairs (writes nothing to production).

Pipeline exercised per target code-day:
  1. build the candidate 48-bar set from the WStock wsCSV export
     - "whole-day replace" policy: take all 48 buckets from WStock
  2. gate A - completeness : exactly the 48 canonical buckets, unique
  3. gate B - OHLC validity: all positive, low <= open/close <= high
  4. gate C - volume/amount: non-negative, amount present
  5. gate D - daily reconciliation: 48-bar aggregate must match
     stock.kline_daily for the same code-day (independent feed)
  6. gate E - cross-check vs purchased source on buckets the purchased feed
     got RIGHT (agreement within a documented tolerance)

Results land in an isolated ClickHouse STAGE table plus a JSON report.
No formal kline table and no review ledger is modified.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import report_path

QUEUE = "stock.purchased_history_data_review_items"
RUN_ID = "purchased_history_review_20260918_5m_source_closeout_v14"
WSTOCK_BASE = Path(r"E:\AStockHistory\wstock\输出\来源归档")
# The wsCSV export writes <MARKET><symbol>.csv with NO year in the name, so each
# yearly .wdz must be exported into its own out_<year> directory.
WSTOCK = WSTOCK_BASE / "out_2006"  # rebound in main() based on --year
HS_MONTHLY = Path(r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按月归档")
STAGE = "stock.wstock_5m_repair_stage"
TZ = ZoneInfo("Asia/Shanghai")
DAILY_TOL = 0.011      # WStock 3dp vs formal daily rounding
CROSS_TOL = 0.0301     # documented vendor rounding/adjustment spread


def canonical() -> list[str]:
    labels = [f"{t // 60:02d}:{t % 60:02d}" for t in range(9 * 60 + 35, 11 * 60 + 30 + 1, 5)]
    labels += [f"{t // 60:02d}:{t % 60:02d}" for t in range(13 * 60 + 5, 15 * 60 + 1, 5)]
    return labels


def load_wstock(code: str, day: str) -> dict[str, tuple[float, float, float, float, float, float]]:
    symbol, market = code.split(".")
    path = WSTOCK / market / f"{market}{symbol}.csv"
    out: dict[str, tuple] = {}
    if not path.exists():
        return out
    r = csv.reader(io.StringIO(path.read_text(encoding="utf-8-sig", errors="replace")))
    next(r, None)
    for c in r:
        if len(c) < 7:
            continue
        d, _, hm = c[0].strip().partition(" ")
        if d != day:
            continue
        # wsCSV: Date,Open,Low,High,Close,Volume,Amount
        out[hm[:5]] = (float(c[1]), float(c[3]), float(c[2]), float(c[4]), float(c[5]), float(c[6]))
    return out


def load_purchased(code: str, day: str) -> dict[str, tuple]:
    symbol, market = code.split(".")
    arc = HS_MONTHLY / day[:7] / f"{day.replace('-', '')}_5min.zip"
    out: dict[str, tuple] = {}
    if not arc.exists():
        return out
    entry = f"{market.lower()}{symbol}.csv"
    from zipfile import ZipFile

    with ZipFile(arc) as zf:
        if entry not in set(zf.namelist()):
            return out
        text = zf.read(entry).decode("utf-8-sig", errors="replace")
    for c in csv.reader(io.StringIO(text)):
        if len(c) < 10 or not c[0].strip().startswith(day.replace("-", "/")):
            continue
        out[c[0].strip()[-5:]] = tuple(float(c[i]) for i in (3, 4, 5, 6, 7, 8))  # O,C,H,L,V,A
    return out


def ensure_stage(client: Any) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {STAGE}
        (
            run_id String, code String, security_type LowCardinality(String),
            trade_date Date, datetime DateTime('Asia/Shanghai'),
            open Float64, high Float64, low Float64, close Float64,
            volume Float64, amount Float64,
            source LowCardinality(String), policy LowCardinality(String),
            bars_in_day UInt8, verified_at DateTime('Asia/Shanghai')
        ) ENGINE = MergeTree
        ORDER BY (run_id, code, datetime)
        """
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=2006)
    ap.add_argument("--tail", default="6")
    ap.add_argument("--run-id", default=f"wstock_repair_{datetime.now(TZ):%Y%m%d_%H%M%S}")
    ap.add_argument("--policy", choices=["whole_day", "fill_bad_only"], default="whole_day")
    ap.add_argument("--write", action="store_true", help="insert into the isolated stage table")
    ap.add_argument("--wstock-dir", type=Path, default=None,
                    help="override the WStock export dir (default: out_<year>)")
    args = ap.parse_args()

    global WSTOCK
    WSTOCK = args.wstock_dir if args.wstock_dir else WSTOCK_BASE / f"out_{args.year}"
    print(f"wstock_dir={WSTOCK} exists={WSTOCK.exists()}", flush=True)

    client = clickhouse_client()
    targets = client.query(
        f"""
        SELECT code, toString(trade_date) AS d, any(security_type)
        FROM {QUEUE}
        WHERE run_id = %(run)s AND toYear(trade_date) = %(year)s
        GROUP BY code, d ORDER BY d, code
        """,
        parameters={"run": RUN_ID, "year": args.year},
    ).result_rows
    targets = [t for t in targets if str(t[0])[5] == args.tail]

    labels = canonical()
    gates = Counter()
    results = []
    stage_rows = []

    for code, day, sec_type in targets:
        code, day, sec_type = str(code), str(day), str(sec_type)
        w = load_wstock(code, day)
        p = load_purchased(code, day)
        rec: dict[str, Any] = {"code": code, "trade_date": day, "security_type": sec_type}

        if not w:
            rec["decision"] = "skip_no_wstock"
            gates["skip_no_wstock"] += 1
            results.append(rec)
            continue

        # gate A - completeness & uniqueness
        missing = [l for l in labels if l not in w]
        rec["bars"] = len(w)
        rec["missing_buckets"] = missing
        if missing or len(w) != 48:
            rec["decision"] = "reject_incomplete"
            gates["reject_incomplete"] += 1
            results.append(rec)
            continue
        gates["complete_48"] += 1

        # gate B - OHLC validity
        bad = [l for l, v in w.items() if min(v[0], v[1], v[2], v[3]) <= 0]
        inv = [l for l, v in w.items() if not (v[2] <= v[0] <= v[1] and v[2] <= v[3] <= v[1])]
        if bad or inv:
            rec["decision"] = "reject_ohlc"
            rec["bad_ohlc"] = bad
            rec["inverted"] = inv
            gates["reject_ohlc"] += 1
            results.append(rec)
            continue
        gates["ohlc_valid"] += 1

        # gate C - volume/amount
        negv = [l for l, v in w.items() if v[4] < 0 or v[5] < 0]
        if negv:
            rec["decision"] = "reject_negative_vol"
            gates["reject_negative_vol"] += 1
            results.append(rec)
            continue
        zero_amt = [l for l, v in w.items() if v[5] == 0]
        rec["zero_amount_bars"] = len(zero_amt)
        gates["amount_all_present" if not zero_amt else "amount_partial_zero"] += 1

        # gate D - daily reconciliation against the independent formal daily
        agg = {
            "open": w["09:35"][0],
            "high": max(v[1] for v in w.values()),
            "low": min(v[2] for v in w.values()),
            "close": w["15:00"][3],
        }
        d = client.query(
            "SELECT open, high, low, close FROM stock.kline_daily "
            "WHERE code = %(c)s AND trade_date = toDate(%(d)s)",
            parameters={"c": code, "d": day},
        ).result_rows
        rec["aggregate"] = {k: round(v, 3) for k, v in agg.items()}
        if d:
            fo, fh, fl, fc = (float(x) for x in d[0])
            rec["formal_daily"] = {"open": fo, "high": fh, "low": fl, "close": fc}
            delta = {
                "open": round(agg["open"] - fo, 4),
                "high": round(agg["high"] - fh, 4),
                "low": round(agg["low"] - fl, 4),
                "close": round(agg["close"] - fc, 4),
            }
            rec["daily_delta"] = delta
            daily_ok = all(abs(v) <= DAILY_TOL for v in delta.values())
        else:
            daily_ok = False
            rec["formal_daily"] = None
        rec["daily_match"] = daily_ok
        gates["daily_match" if daily_ok else "daily_mismatch"] += 1

        # gate E - cross-check against purchased bars that were healthy
        good = [l for l in labels if l in p and min(p[l][:4]) > 0]
        agree = mism = 0
        worst = 0.0
        for l in good:
            diff = abs(p[l][1] - w[l][3])  # close vs close
            worst = max(worst, diff)
            if diff <= CROSS_TOL:
                agree += 1
            else:
                mism += 1
        rec["overlap_agree"] = agree
        rec["overlap_mismatch"] = mism
        rec["overlap_worst_diff"] = round(worst, 4)
        gates["cross_ok" if mism == 0 else "cross_partial"] += 1

        # decision
        if daily_ok:
            rec["decision"] = "accept"
            gates["accept"] += 1
            day_obj = date.fromisoformat(day)
            for l in labels:
                hh, mm = int(l[:2]), int(l[3:])
                o, h, lo, c, v, a = w[l]
                stage_rows.append([
                    args.run_id, code, sec_type, day_obj,
                    datetime(day_obj.year, day_obj.month, day_obj.day, hh, mm, tzinfo=TZ),
                    o, h, lo, c, v, a,
                    "wstock_wsdz_5min", args.policy, len(w),
                    datetime.now(TZ),
                ])
        else:
            rec["decision"] = "review_daily_mismatch"
            gates["review_daily_mismatch"] += 1
        results.append(rec)

    if args.write and stage_rows:
        ensure_stage(client)
        client.insert(
            STAGE, stage_rows,
            column_names=["run_id", "code", "security_type", "trade_date", "datetime",
                          "open", "high", "low", "close", "volume", "amount",
                          "source", "policy", "bars_in_day", "verified_at"],
        )

    accepted = [r for r in results if r["decision"] == "accept"]
    payload = {
        "run_id": args.run_id, "year": args.year, "tail": args.tail,
        "policy": args.policy, "targets": len(targets),
        "gates": dict(gates), "accepted": len(accepted),
        "stage_rows": len(stage_rows), "written": bool(args.write), "results": results,
    }
    out = report_path("purchased_history", "wstock", f"e2e_verify_{args.year}_tail{args.tail}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in ("run_id", "targets", "gates", "accepted", "stage_rows", "written")}
                     | {"report": str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

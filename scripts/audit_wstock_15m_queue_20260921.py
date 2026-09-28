"""Audit unresolved 15m daily-diff keys against an isolated WStock 5m export.

The script never writes a formal kline table or the review queue.  It reads the
active 15m review keys, excludes code-days already repaired by the Harry 5m
reaggregation, aggregates complete WStock 5m days into 15m bars, and applies
the project's pre-2010 gates (OHLC 0.5%, volume 0.1%, amount 1%).  Passing
rows are written only to a gzipped candidate file and a JSON manifest.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

DEFAULT_RUN = "purchased_history_review_20260921_user_policy_frequency_daily_v2_harry_repair"
DEFAULT_WSTOCK = Path(r"E:\AStockHistory\wstock\输出\来源归档\out_历史混合\tail6_20260921")
DEFAULT_OUT = Path(r"F:\AiStock\data\data\runtime\quality_plan\wstock_15m_probe_20260921")
AUDIT_TABLE = "stock.purchased_native_15m_harry_repair_candidate_20260921_day_audit"
QUEUE_TABLE = "stock.purchased_history_data_review_items"
DAILY_TABLE = "stock.kline_daily"

FILENAME_RE = re.compile(r"^(SH|SZ)(\d{6})\.csv$", re.I)
PRICE_TOL = 0.005
VOLUME_TOL = 0.001
AMOUNT_TOL = 0.01


def parse_years(text: str) -> tuple[int, int]:
    if "-" in text:
        a, b = text.split("-", 1)
        return int(a), int(b)
    y = int(text)
    return y, y


def rel_diff(a: float, b: float) -> float:
    if b == 0:
        return 0.0 if a == 0 else float("inf")
    return abs(a - b) / abs(b)


def canonical_5m() -> list[str]:
    out = [f"{m // 60:02d}:{m % 60:02d}" for m in range(9 * 60 + 35, 11 * 60 + 30 + 1, 5)]
    out += [f"{m // 60:02d}:{m % 60:02d}" for m in range(13 * 60 + 5, 15 * 60 + 1, 5)]
    return out


def groups_15m() -> list[tuple[str, list[str]]]:
    labels = canonical_5m()
    return [(labels[i + 2], labels[i : i + 3]) for i in range(0, len(labels), 3)]


def load_wstock(root: Path) -> dict[str, dict[str, dict[str, tuple[float, ...]]]]:
    out: dict[str, dict[str, dict[str, tuple[float, ...]]]] = {}
    for p in sorted(root.rglob("*.csv")):
        m = FILENAME_RE.match(p.name)
        if not m:
            continue
        code = f"{m.group(2)}.{m.group(1).upper()}"
        by_day: dict[str, dict[str, tuple[float, ...]]] = defaultdict(dict)
        try:
            with p.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
                rows = csv.reader(fh)
                next(rows, None)
                for row in rows:
                    if len(row) < 7:
                        continue
                    try:
                        stamp = datetime.fromisoformat(row[0].strip())
                        vals = tuple(float(row[i]) for i in (1, 3, 2, 4, 5, 6))
                    except (ValueError, TypeError):
                        continue
                    by_day[stamp.date().isoformat()][stamp.strftime("%H:%M")] = vals
        except OSError:
            continue
        out[code] = dict(by_day)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wstock-dir", type=Path, default=DEFAULT_WSTOCK)
    ap.add_argument("--run-id", default=DEFAULT_RUN)
    ap.add_argument("--years", default="2000-2010")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    y0, y1 = parse_years(args.years)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    wstock = load_wstock(args.wstock_dir)
    codes = sorted(wstock)
    client = clickhouse_client()
    queue_rows = client.query(
        f"""SELECT code, toString(trade_date), issue_type
            FROM {QUEUE_TABLE}
            WHERE run_id = %(run)s AND period = '15m'
              AND toYear(trade_date) BETWEEN %(y0)s AND %(y1)s
              AND code IN %(codes)s""",
        parameters={"run": args.run_id, "y0": y0, "y1": y1, "codes": codes},
    ).result_rows
    targets = {(str(c), str(d)): str(issue) for c, d, issue in queue_rows}

    repaired_rows = client.query(
        f"SELECT code, toString(trade_date) FROM {AUDIT_TABLE} WHERE repair_status = 'pass_exact_5m_reaggregate'"
    ).result_rows
    already_repaired = {(str(c), str(d)) for c, d in repaired_rows}
    targets = {k: v for k, v in targets.items() if k not in already_repaired}

    target_codes = sorted({c for c, _ in targets})
    daily: dict[tuple[str, str], tuple[float, ...]] = {}
    if target_codes:
        daily_rows = client.query(
            f"""SELECT code, toString(trade_date), open, high, low, close, volume, amount
                FROM {DAILY_TABLE}
                WHERE code IN %(codes)s AND toYear(trade_date) BETWEEN %(y0)s AND %(y1)s""",
            parameters={"codes": target_codes, "y0": y0, "y1": y1},
        ).result_rows
        daily = {(str(c), str(d)): tuple(float(x) for x in vals) for c, d, *vals in daily_rows}

    counts = Counter()
    day_audit: list[dict[str, Any]] = []
    candidate_path = out_dir / "candidate_15m.jsonl.gz"
    with gzip.open(candidate_path, "wt", encoding="utf-8") as cand:
        for code, day in sorted(targets):
            counts["queue_targets"] += 1
            bars = wstock.get(code, {}).get(day, {})
            missing = [label for label in canonical_5m() if label not in bars]
            rec: dict[str, Any] = {"code": code, "trade_date": day, "source": "wstock_tail6_5m",
                                   "bars": len(bars), "missing": missing}
            if missing or len(bars) != 48:
                counts["review_incomplete"] += 1
                rec["status"] = "review_incomplete"
                day_audit.append(rec)
                continue
            invalid = [label for label, v in bars.items() if min(v[:4]) <= 0 or not (v[2] <= v[0] <= v[1] and v[2] <= v[3] <= v[1])]
            if invalid:
                counts["review_invalid_ohlc"] += 1
                rec["status"] = "review_invalid_ohlc"
                rec["invalid"] = invalid
                day_audit.append(rec)
                continue
            drow = daily.get((code, day))
            if not drow:
                counts["review_daily_missing"] += 1
                rec["status"] = "review_daily_missing"
                day_audit.append(rec)
                continue
            grouped: list[dict[str, Any]] = []
            for end_label, labels in groups_15m():
                vals = [bars[x] for x in labels]
                grouped.append({"datetime": f"{day} {end_label}:00", "open": vals[0][0],
                                "high": max(v[1] for v in vals), "low": min(v[2] for v in vals),
                                "close": vals[-1][3], "volume": sum(v[4] for v in vals),
                                "amount": sum(v[5] for v in vals)})
            agg = {"open": grouped[0]["open"], "high": max(x["high"] for x in grouped),
                   "low": min(x["low"] for x in grouped), "close": grouped[-1]["close"],
                   "volume": sum(x["volume"] for x in grouped), "amount": sum(x["amount"] for x in grouped)}
            rec["aggregate"] = agg
            rec["daily"] = dict(zip(("open", "high", "low", "close", "volume", "amount"), drow))
            diffs = {k: rel_diff(agg[k], rec["daily"][k]) for k in agg}
            rec["relative_diff"] = diffs
            gate = (all(diffs[k] <= PRICE_TOL for k in ("open", "high", "low", "close"))
                    and diffs["volume"] <= VOLUME_TOL and diffs["amount"] <= AMOUNT_TOL)
            if not gate:
                counts["review_daily_gate"] += 1
                rec["status"] = "review_daily_gate"
                day_audit.append(rec)
                continue
            counts["pass_wstock_5m_reaggregate"] += 1
            rec["status"] = "pass_wstock_5m_reaggregate"
            day_audit.append(rec)
            for row in grouped:
                cand.write(json.dumps({"code": code, "security_type": "stock", "trade_date": day,
                                       "source": "wstock_tail6_5m", "source_batch_id": "wstock_tail6_20260921",
                                       **row}, ensure_ascii=False) + "\n")

    manifest = {
        "schema": "wstock-15m-queue-probe-v1", "run_id": args.run_id,
        "source_root": str(args.wstock_dir), "years": [y0, y1],
        "queue_targets_before_exclusion": len(queue_rows),
        "excluded_harry_resolved": len(queue_rows) - len(targets),
        "wstock_files": len(codes), "counts": dict(counts),
        "candidate_path": str(candidate_path), "formal_tables_touched": False,
        "queue_touched": False,
    }
    (out_dir / "day_audit.json").write_text(json.dumps(day_audit, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

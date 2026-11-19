from __future__ import annotations

"""Read-only compare isolated derived files against formal ClickHouse tables.

The candidate files are split in-memory by month and code prefix, while each
ClickHouse query is bounded to one month/prefix.  No DDL, INSERT, FINAL or
mutation is performed.
"""

import argparse
import gzip
import json
import re
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client


TABLES = {"15": "stock.kline_minute_15", "30": "stock.kline_minute_30", "60": "stock.kline_minute_60", "1d": "stock.kline_daily"}
EXPECTED = {"15": 3, "30": 6, "60": 12, "1d": 48}
PRICE_TOL = 0.005
VOLUME_TOL = 0.001
AMOUNT_TOL = 0.01
SETTINGS = {
    "max_threads": 2,
    "max_memory_usage": 12 * 1024 * 1024 * 1024,
    "max_bytes_before_external_group_by": 8 * 1024 * 1024 * 1024,
    "max_bytes_before_external_sort": 4 * 1024 * 1024 * 1024,
    "max_execution_time": 240,
    "join_algorithm": "grace_hash",
}
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def q(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def shard(code: str) -> str:
    head = code.split(".", 1)[0]
    if len(head) and head[0].isdigit():
        return head[:3] if head.startswith("6") and len(head) >= 3 else head[0]
    return "other"


def shard_sql(value: str) -> str:
    if value == "other":
        return "(code IS NULL OR code = '' OR substring(code,1,1) NOT IN ('0','1','2','3','4','5','6','7','8','9'))"
    if len(value) == 1:
        return f"substring(code,1,1) = {q(value)}"
    return f"startsWith(code, {q(value)})"


def numeric(value: Any) -> float:
    return float(value or 0.0)


def differs(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) / max(abs(b), 1.0) > tolerance


def month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def load_groups(path: Path) -> dict[tuple[int, str], dict[tuple[str, str], tuple]]:
    groups: dict[tuple[int, str], dict[tuple[str, str], tuple]] = defaultdict(dict)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            dt = str(row.get("datetime") or "")
            if len(dt) < 10:
                continue
            code = str(row.get("code") or "")
            key = (code, dt[:19])
            month = int(dt[5:7])
            s = shard(code)
            groups[(month, s)][key] = (
                numeric(row.get("open")), numeric(row.get("high")), numeric(row.get("low")),
                numeric(row.get("close")), numeric(row.get("volume_lots")), numeric(row.get("amount_yuan")),
                int(row.get("source_bars") or 0),
            )
    return groups


def target_sql(table: str, start: date, end: date, s: str) -> str:
    time_col = "trade_date" if table == "stock.kline_daily" else "datetime"
    return f"""
    SELECT code, {time_col} AS datetime,
           argMax(open, tuple(created_at,id)) AS open,
           argMax(high, tuple(created_at,id)) AS high,
           argMax(low, tuple(created_at,id)) AS low,
           argMax(close, tuple(created_at,id)) AS close,
           argMax(volume, tuple(created_at,id)) AS volume,
           argMax(amount, tuple(created_at,id)) AS amount,
           count() AS raw_rows
    FROM {table}
    WHERE {time_col} >= toDate({q(start)}) AND {time_col} < toDate({q(end)})
      AND {shard_sql(s)} AND code IS NOT NULL AND code != ''
    GROUP BY code, datetime
    """


def compare(cand: dict[tuple[str, str], tuple], target_rows: list[tuple], period: str) -> dict:
    # Daily formal rows expose a Date (YYYY-MM-DD), while the isolated
    # candidate uses the canonical end-of-session timestamp (15:00:00).
    # Normalize both sides to the same logical key before calculating
    # missing/extra rows; otherwise every valid daily row is misclassified.
    if period == "1d":
        target = {(str(r[0]), str(r[1])[:10] + " 15:00:00"): r for r in target_rows}
    else:
        target = {(str(r[0]), str(r[1])[:19]): r for r in target_rows}
    missing = set(cand) - set(target)
    extra = set(target) - set(cand)
    duplicate = sum(1 for r in target_rows if int(r[8] or 0) > 1)
    incomplete = sum(1 for r in cand.values() if r[6] != EXPECTED[period])
    diff = 0
    for key, row in cand.items():
        tgt = target.get(key)
        if tgt is None:
            continue
        if (
            differs(row[0], numeric(tgt[2]), PRICE_TOL)
            or differs(row[1], numeric(tgt[3]), PRICE_TOL)
            or differs(row[2], numeric(tgt[4]), PRICE_TOL)
            or differs(row[3], numeric(tgt[5]), PRICE_TOL)
            or differs(row[4], numeric(tgt[6]), VOLUME_TOL)
            or differs(row[5], numeric(tgt[7]), AMOUNT_TOL)
        ):
            diff += 1
    return {
        "candidate_rows": len(cand),
        "formal_logical_rows": len(target_rows),
        "missing_formal_rows": len(missing),
        "formal_extra_rows": len(extra),
        "formal_duplicate_groups": duplicate,
        "candidate_incomplete_buckets": incomplete,
        "value_difference_rows": diff,
        "sample_missing": sorted(missing)[:20],
        "sample_extra": sorted(extra)[:20],
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--derived-root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--periods", default="15,30,60,1d")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    if not 2000 <= args.year <= 2009:
        raise SystemExit("year must be within 2000-2009")
    client = clickhouse_client()
    report = {
        "schema_version": "ocean-derived-formal-diff-v1",
        "year": args.year,
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "settings": SETTINGS,
        "rows": [],
    }
    for period in [x.strip() for x in args.periods.split(",") if x.strip()]:
        if period not in TABLES:
            raise SystemExit(f"unsupported_period={period}")
        path = args.derived_root / str(args.year) / f"candidate_{period}.jsonl.gz"
        if period == "1d" and not path.is_file():
            path = args.derived_root / str(args.year) / "candidate_1.jsonl.gz"
        groups = load_groups(path)
        for month in range(1, 13):
            start, end = month_bounds(args.year, month)
            for s in sorted({key[1] for key in groups if key[0] == month}):
                cand = groups.get((month, s), {})
                started = time.perf_counter()
                try:
                    rows = list(client.query(target_sql(TABLES[period], start, end, s), settings=SETTINGS).result_rows)
                    summary = compare(cand, rows, period)
                    status = "pass" if summary["missing_formal_rows"] == 0 and summary["candidate_incomplete_buckets"] == 0 and summary["value_difference_rows"] == 0 else "review"
                    report["rows"].append({"period": period, "month": f"{args.year:04d}-{month:02d}", "shard": s, "status": status, "elapsed_seconds": round(time.perf_counter() - started, 3), **summary})
                    if not args.quiet:
                        print(json.dumps(report["rows"][-1], ensure_ascii=False), flush=True)
                except Exception as exc:
                    report["rows"].append({"period": period, "month": f"{args.year:04d}-{month:02d}", "shard": s, "status": "not_checked", "error": repr(exc), "candidate_rows": len(cand)})
                    if not args.quiet:
                        print(json.dumps(report["rows"][-1], ensure_ascii=False), flush=True)
        del groups
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report["status"] = "pass" if report["rows"] and all(r["status"] == "pass" for r in report["rows"]) else "review"
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": report["status"], "rows": len(report["rows"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

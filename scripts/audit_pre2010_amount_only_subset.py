from __future__ import annotations

"""Bounded read-only field-level diff for the 2000--2002 5m candidates.

The existing aggregate reports count conflicts but do not distinguish
amount-only differences from price/volume differences.  This script repeats
the same month/code-prefix bounded SELECT and records that decomposition.  It
never creates tables or performs INSERT/DELETE/FINAL/mutation.
"""

import gzip
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client

SETTINGS = {
    "max_threads": 1,
    "max_memory_usage": 12 * 1024 * 1024 * 1024,
    "max_bytes_before_external_group_by": 8 * 1024 * 1024 * 1024,
    "max_bytes_before_external_sort": 4 * 1024 * 1024 * 1024,
    "max_execution_time": 300,
    "join_algorithm": "grace_hash",
}
SHARDS = tuple("0123456789") + ("other",)
PRICE_TOL = 0.005
VOLUME_TOL = 0.001
AMOUNT_TOL = 0.01


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def shard_sql(s: str) -> str:
    if s == "other":
        return "(code IS NULL OR code='' OR substring(code,1,1) NOT IN ('0','1','2','3','4','5','6','7','8','9'))"
    return f"substring(code,1,1)='{s}'"


def load(path: Path):
    groups = defaultdict(dict)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            code = str(r.get("code") or "")
            dt = str(r.get("datetime") or "")[:19]
            if len(dt) < 19 or not code:
                continue
            groups[(int(dt[5:7]), code[0] if code[0].isdigit() else "other")][(code, dt)] = (
                float(r.get("open") or 0), float(r.get("high") or 0),
                float(r.get("low") or 0), float(r.get("close") or 0),
                float(r.get("volume_lots") or 0), float(r.get("amount_yuan") or 0),
            )
    return groups


def month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start.isoformat(), end.isoformat()


def classify(cand, formal_rows):
    formal = {(str(r[0]), str(r[1])[:19]): r for r in formal_rows}
    missing = set(cand) - set(formal)
    extra = set(formal) - set(cand)
    counts = defaultdict(int)
    for key in set(cand) & set(formal):
        a = cand[key]
        b = formal[key]
        ds = [abs(a[i] - float(b[i + 2])) / max(abs(float(b[i + 2])), 1.0) for i in range(6)]
        p = any(x > PRICE_TOL for x in ds[:4])
        v = ds[4] > VOLUME_TOL
        amt = ds[5] > AMOUNT_TOL
        p_any = any(x > 0 for x in ds[:4])
        v_any = ds[4] > 0
        amt_any = ds[5] > 0
        any_small = any(ds)
        if not any_small:
            counts["exact"] += 1
        elif not p and not v and not amt:
            if amt_any and not p_any and not v_any:
                counts["amount_only_within_1pct"] += 1
            elif p_any and not v_any and not amt_any:
                counts["ohlc_only_within_0_5pct"] += 1
            elif v_any and not p_any and not amt_any:
                counts["volume_only_within_0_1pct"] += 1
            else:
                counts["within_policy_nonzero"] += 1
        elif not p and not v and amt:
            counts["amount_only_over_1pct"] += 1
        elif p and not v and not amt:
            counts["ohlc_only_over_0_5pct"] += 1
        elif v and not p and not amt:
            counts["volume_only_over_0_1pct"] += 1
        elif not p and v and amt:
            counts["volume_and_amount_over_policy"] += 1
        elif p and not v and amt:
            counts["ohlc_and_amount_over_policy"] += 1
        elif p and v and not amt:
            counts["ohlc_and_volume_over_policy"] += 1
        else:
            counts["mixed_ohlc_volume_amount_over_policy"] += 1
    counts["candidate_rows"] = len(cand)
    counts["formal_rows"] = len(formal_rows)
    counts["missing_exact"] = len(missing)
    counts["formal_extra_exact"] = len(extra)
    return dict(counts)


def main() -> int:
    root = Path(r"F:\AiStockData\data\runtime\ocean_detail_candidate_20260921")
    out = Path(r"F:\AiStockData\data\runtime\quality_plan\pre2010_amount_only_subset_20260921")
    out.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    report = {
        "schema_version": "pre2010-amount-only-subset-v1",
        "scope": [2000, 2002], "read_only": True,
        "formal_clickhouse_written": False, "formal_mutation": False,
        "policy": {"price_ohlc": PRICE_TOL, "volume": VOLUME_TOL, "amount": AMOUNT_TOL, "amount_unit": "yuan"},
        "rows": [], "sources": [],
    }
    for year in (2000, 2001, 2002):
        candidate = root / str(year) / "candidate_5m.jsonl.gz"
        report["sources"].append({"path": str(candidate), "sha256": sha(candidate)})
        groups = load(candidate)
        for month in range(1, 13):
            start, end = month_bounds(year, month)
            for s in SHARDS:
                cand = groups.get((month, s), {})
                if not cand:
                    continue
                sql = f"""
                SELECT code, datetime,
                       argMax(open,tuple(created_at,id)), argMax(high,tuple(created_at,id)),
                       argMax(low,tuple(created_at,id)), argMax(close,tuple(created_at,id)),
                       argMax(volume,tuple(created_at,id)), argMax(amount,tuple(created_at,id)), count()
                FROM stock.kline_minute_5
                WHERE datetime >= toDate(%(start)s) AND datetime < toDate(%(end)s)
                  AND {shard_sql(s)} AND code IS NOT NULL AND code!=''
                GROUP BY code,datetime
                """
                rows = list(client.query(sql, parameters={"start": start, "end": end}, settings=SETTINGS).result_rows)
                rec = {"year": year, "month": month, "code_shard": s, "status": "checked"}
                rec.update(classify(cand, rows))
                report["rows"].append(rec)
                print(json.dumps(rec, ensure_ascii=False), flush=True)
        del groups
    report["status"] = "pass"
    target = out / "manifest.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(target), "partitions": len(report["rows"]), "status": report["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

"""Read-only field-level comparison for 2006--2009 repair candidates.

The repair rows for these years live in the isolated ClickHouse candidate
table.  This audit deliberately reads one year/month/code-prefix at a time,
compares the candidate with the existing formal 5m table, and applies the
pre-2010 policy: OHLC <=0.5%, volume <=0.1%, amount <=1% (yuan).  It never
creates/inserts/mutates/finalizes a ClickHouse table.
"""

import hashlib
import json
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
    "max_execution_time": 600,
    "join_algorithm": "grace_hash",
}
SHARDS = tuple("0123456789") + ("other",)
PRICE_TOL, VOLUME_TOL, AMOUNT_TOL = 0.005, 0.001, 0.01


def shard_sql(s: str) -> str:
    if s == "other":
        return "(code IS NULL OR code='' OR substring(code,1,1) NOT IN ('0','1','2','3','4','5','6','7','8','9'))"
    return f"substring(code,1,1)='{s}'"


def norm_code(code: str) -> str:
    c = str(code or "").strip().upper()
    if "." in c:
        c = c.split(".", 1)[0]
    return "".join(ch for ch in c if ch.isdigit()) or c


def month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start.isoformat(), end.isoformat()


def rows_to_map(rows):
    out = {}
    dup = 0
    for r in rows:
        key = (str(r[0]), str(r[1])[:19])
        if key in out:
            dup += 1
        out[key] = tuple(float(r[i] or 0) for i in range(2, 8))
    return out, dup


def classify(cand_rows, formal_rows, formal_dup_groups):
    cand, cand_dup = rows_to_map(cand_rows)
    formal, _ = rows_to_map(formal_rows)
    missing = set(cand) - set(formal)
    extra = set(formal) - set(cand)
    counts = defaultdict(int)
    for key in set(cand) & set(formal):
        a, b = cand[key], formal[key]
        ds = [abs(a[i] - b[i]) / max(abs(b[i]), 1.0) for i in range(6)]
        p = any(x > PRICE_TOL for x in ds[:4]); v = ds[4] > VOLUME_TOL; amt = ds[5] > AMOUNT_TOL
        p_any = any(x > 0 for x in ds[:4]); v_any = ds[4] > 0; amt_any = ds[5] > 0
        if not any(ds): counts["exact"] += 1
        elif not p and not v and not amt:
            if amt_any and not p_any and not v_any: counts["amount_only_within_1pct"] += 1
            elif p_any and not v_any and not amt_any: counts["ohlc_only_within_0_5pct"] += 1
            elif v_any and not p_any and not amt_any: counts["volume_only_within_0_1pct"] += 1
            else: counts["within_policy_nonzero"] += 1
        elif not p and not v and amt: counts["amount_only_over_1pct"] += 1
        elif p and not v and not amt: counts["ohlc_only_over_0_5pct"] += 1
        elif v and not p and not amt: counts["volume_only_over_0_1pct"] += 1
        elif not p and v and amt: counts["volume_and_amount_over_policy"] += 1
        elif p and not v and amt: counts["ohlc_and_amount_over_policy"] += 1
        elif p and v and not amt: counts["ohlc_and_volume_over_policy"] += 1
        else: counts["mixed_ohlc_volume_amount_over_policy"] += 1
    formal_norm = defaultdict(set)
    for code, _ in formal:
        formal_norm[norm_code(code)].add(code)
    for code, _ in missing:
        if formal_norm.get(norm_code(code)):
            counts["missing_exact_with_normalized_code_present"] += 1
        else:
            counts["missing_exact_no_normalized_code_present"] += 1
    counts.update({"candidate_rows": len(cand), "formal_rows": len(formal),
                   "missing_exact": len(missing), "formal_extra_exact": len(extra),
                   "candidate_duplicate_rows": cand_dup,
                   "formal_duplicate_rows": formal_dup_groups,
                   "candidate_code_count": len({k[0] for k in cand}),
                   "formal_code_count": len({k[0] for k in formal})})
    return dict(counts)


def main() -> int:
    out = Path(r"F:\AiStock\data\data\runtime\quality_plan\pre2010_amount_only_subset_2006_2009_20260921")
    out.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    report = {"schema_version": "pre2010-amount-only-subset-v1", "scope": [2006, 2009],
              "read_only": True, "formal_clickhouse_written": False, "formal_mutation": False,
              "policy": {"price_ohlc": PRICE_TOL, "volume": VOLUME_TOL, "amount": AMOUNT_TOL,
                         "amount_unit": "yuan"}, "rows": [], "sources": []}
    for year in range(2006, 2010):
        for month in range(1, 13):
            start, end = month_bounds(year, month)
            for s in SHARDS:
                pred = shard_sql(s)
                cand_sql = f"""
                SELECT code, datetime, open, high, low, close, volume, amount
                FROM stock.pre2010_repair_5m_candidate_20260921
                WHERE datetime >= toDate(%(start)s) AND datetime < toDate(%(end)s)
                  AND {pred} AND code!=''
                ORDER BY code, datetime
                """
                formal_sql = f"""
                SELECT code, datetime, argMax(open,tuple(created_at,id)), argMax(high,tuple(created_at,id)),
                       argMax(low,tuple(created_at,id)), argMax(close,tuple(created_at,id)),
                       argMax(volume,tuple(created_at,id)), argMax(amount,tuple(created_at,id)), count()
                FROM stock.kline_minute_5
                WHERE datetime >= toDate(%(start)s) AND datetime < toDate(%(end)s)
                  AND {pred} AND code IS NOT NULL AND code!=''
                GROUP BY code,datetime
                """
                cr = list(client.query(cand_sql, parameters={"start": start, "end": end}, settings=SETTINGS).result_rows)
                fr0 = list(client.query(formal_sql, parameters={"start": start, "end": end}, settings=SETTINGS).result_rows)
                fr = [r[:-1] for r in fr0]
                dup = sum(max(int(r[-1]) - 1, 0) for r in fr0)
                rec = {"year": year, "month": month, "code_shard": s, "status": "checked"}
                rec.update(classify(cr, fr, dup))
                report["rows"].append(rec)
                print(json.dumps(rec, ensure_ascii=False), flush=True)
    report["status"] = "pass"
    target = out / "manifest.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(target), "partitions": len(report["rows"]), "status": report["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


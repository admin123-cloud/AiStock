from __future__ import annotations

"""Read-only diff of the Ocean 5m detail candidate against formal 5m.

The candidate is JSONL gzip and is grouped in bounded year/month/code-prefix
partitions before querying ClickHouse.  This is intentionally separate from
the older purchased candidate audit: review code-days are not present in the
candidate stream, and every missing/extra key is additionally classified by
six-digit code plus suffix so a lifecycle or suffix mismatch is not treated as
a data error.
"""

import argparse, gzip, json, re, sys, time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client

FORMAL = "stock.kline_minute_5"
SHARDS = tuple("0123456789") + ("other",)
SETTINGS = {"max_threads": 1, "max_memory_usage": 12*1024**3,
            "max_bytes_before_external_group_by": 8*1024**3,
            "max_bytes_before_external_sort": 4*1024**3,
            "max_execution_time": 300, "join_algorithm": "grace_hash"}

def base_code(code: str) -> str:
    m = re.match(r"(\d{6})", code or "")
    return m.group(1) if m else code

def shard(code: str) -> str:
    return code[0] if code and code[0].isdigit() else "other"

def shard_sql(s: str, col: str = "code") -> str:
    if s == "other":
        return f"({col} IS NULL OR {col}='' OR substring({col},1,1) NOT IN ('0','1','2','3','4','5','6','7','8','9'))"
    return f"substring({col},1,1)=%(shard)s"

def bounds(year: int, month: int) -> tuple[str, str]:
    a = date(year, month, 1)
    b = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return a.isoformat(), b.isoformat()

def load(path: Path) -> dict[tuple[int, str], dict[tuple[str, str], tuple]]:
    out: dict[tuple[int, str], dict[tuple[str, str], tuple]] = defaultdict(dict)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip(): continue
            r = json.loads(line); code = str(r.get("code") or "")
            dt = str(r.get("datetime") or "")[:19]
            if len(dt) < 19 or not code: continue
            out[(int(dt[5:7]), shard(code))][(code, dt)] = (
                float(r.get("open") or 0), float(r.get("high") or 0),
                float(r.get("low") or 0), float(r.get("close") or 0),
                float(r.get("volume_lots") or 0), float(r.get("amount_yuan") or 0))
    return out

def q(client: Any, sql: str, params: dict[str, Any]) -> list[tuple]:
    return list(client.query(sql, parameters=params, settings=SETTINGS).result_rows)

def formal_rows(client: Any, start: str, end: str, s: str) -> list[tuple]:
    wc = f"datetime >= toDate(%(start)s) AND datetime < toDate(%(end)s) AND {shard_sql(s)} AND code IS NOT NULL AND code!=''"
    return q(client, f"""
      SELECT code, datetime, argMax(open,tuple(created_at,id)),
             argMax(high,tuple(created_at,id)), argMax(low,tuple(created_at,id)),
             argMax(close,tuple(created_at,id)), argMax(volume,tuple(created_at,id)),
             argMax(amount,tuple(created_at,id)), count()
      FROM {FORMAL} WHERE {wc} GROUP BY code,datetime
    """, {"start": start, "end": end, "shard": s})

def summary(cand: dict[tuple[str,str], tuple], rows: list[tuple]) -> dict[str, Any]:
    tgt = {(str(r[0]), str(r[1])[:19]): r for r in rows}
    ckeys, tkeys = set(cand), set(tgt)
    missing, extra = ckeys - tkeys, tkeys - ckeys
    suffix_missing = lifecycle_missing = 0
    suffix_extra = lifecycle_extra = 0
    cbase = {(base_code(k[0]), k[1]) for k in ckeys}
    tbase = {(base_code(k[0]), k[1]) for k in tkeys}
    for k in missing:
        if (base_code(k[0]), k[1]) in tbase: suffix_missing += 1
        else: lifecycle_missing += 1
    for k in extra:
        if (base_code(k[0]), k[1]) in cbase: suffix_extra += 1
        else: lifecycle_extra += 1
    conflicts = ohlc = vol = amount = within = 0
    for k in ckeys & tkeys:
        a, b = cand[k], tgt[k]
        diffs = (abs(a[i]-float(b[i+2])) / max(abs(float(b[i+2])), 1.0) for i in range(6))
        ds = list(diffs)
        hit_ohlc = any(x > .005 for x in ds[:4])
        hit_vol = ds[4] > .001
        hit_amount = ds[5] > .01
        if hit_ohlc or hit_vol or hit_amount:
            conflicts += 1; ohlc += int(hit_ohlc); vol += int(hit_vol); amount += int(hit_amount)
        elif any(ds):
            within += 1
    return {"candidate_rows": len(cand), "formal_rows": len(rows),
            "candidate_duplicate_keys": len(cand)-len(ckeys), "formal_duplicate_groups": sum(int(r[8] or 0)>1 for r in rows),
            "missing_exact": len(missing), "formal_extra_exact": len(extra),
            "missing_suffix_variant": suffix_missing, "missing_lifecycle_or_coverage": lifecycle_missing,
            "extra_suffix_variant": suffix_extra, "extra_lifecycle_or_coverage": lifecycle_extra,
            "value_conflict_rows": conflicts, "ohlc_conflict_rows": ohlc,
            "volume_conflict_rows": vol, "amount_conflict_rows": amount,
            "within_tolerance_rows": within,
            "sample_missing": sorted(missing)[:20], "sample_extra": sorted(extra)[:20]}

def main() -> int:
    p=argparse.ArgumentParser(); p.add_argument("--year", type=int, required=True); p.add_argument("--candidate", type=Path, required=True); p.add_argument("--output", type=Path, required=True); p.add_argument("--quiet", action="store_true")
    a=p.parse_args()
    if not 2000 <= a.year <= 2002: raise SystemExit("scope is frozen to 2000-2002")
    groups=load(a.candidate); client=clickhouse_client(); report={"schema_version":"ocean-5m-formal-diff-v1","year":a.year,"formal_mutation":False,"formal_clickhouse_written":False,"settings":SETTINGS,"rows":[]}
    for month in range(1,13):
      for s in SHARDS:
        cand=groups.get((month,s),{})
        if not cand: continue
        start,end=bounds(a.year,month); row={"year":a.year,"month":month,"code_shard":s,"status":"checked","candidate_rows":len(cand)}; t=time.perf_counter()
        try: row.update(summary(cand,formal_rows(client,start,end,s)))
        except Exception as exc: row.update({"status":"not_checked","error":f"{type(exc).__name__}: {exc}"})
        row["elapsed_seconds"]=round(time.perf_counter()-t,3); report["rows"].append(row)
        if not a.quiet: print(json.dumps(row,ensure_ascii=False,default=str),flush=True)
        a.output.parent.mkdir(parents=True,exist_ok=True); (a.output.parent/"report.partial.json").write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
    report["status"]="pass" if all(x["status"]=="checked" for x in report["rows"]) else "review"; a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8"); print(json.dumps({"output":str(a.output),"status":report["status"],"partitions":len(report["rows"])},ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())

from __future__ import annotations

"""Create auditable samples for pre-2010 review categories.

Read-only: reads candidate gzip files and bounded ClickHouse SELECTs only.
Outputs 10 samples per category where raw evidence exists; categories that
cannot be represented at row level are explicitly marked as unavailable.
"""

import csv
import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client

OUT = Path(r"F:\AiDevelop\ai-workspace\projects\AiStock\changes\pre2010_review_samples_20260921")
RUNTIME = Path(r"F:\AiStockData\data\runtime")
PRICE_TOL = 0.005
VOL_TOL = 0.001
AMT_TOL = 0.01
SETTINGS = {"max_threads": 1, "max_memory_usage": 12 * 1024**3,
            "max_bytes_before_external_group_by": 8 * 1024**3,
            "max_bytes_before_external_sort": 4 * 1024**3,
            "max_execution_time": 180, "join_algorithm": "grace_hash"}


def rel(a: float, b: float) -> float:
    return abs(float(a or 0) - float(b or 0)) / max(abs(float(b or 0)), 1.0)


def read_structure_samples() -> list[dict]:
    out: list[dict] = []
    for y in (2000, 2001, 2002):
        p = RUNTIME / "ocean_detail_candidate_20260921" / str(y) / "review_code_days.jsonl.gz"
        if not p.exists():
            continue
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                if len(out) >= 10:
                    return out
                r = json.loads(line)
                r.update({"category": "structure_48slot_missing", "source": str(p),
                          "reason": "source candidate has no complete 48-slot day; actual_rows=0"})
                out.append(r)
    return out


def read_all_review() -> list[dict]:
    rows: list[dict] = []
    for y in (2000, 2001, 2002):
        p = RUNTIME / "ocean_detail_candidate_20260921" / str(y) / "review_code_days.jsonl.gz"
        if p.exists():
            with gzip.open(p, "rt", encoding="utf-8") as f:
                rows.extend(json.loads(x) for x in f if x.strip())
    return rows


def query_month(client, y: int, m: int) -> list[tuple]:
    start = date(y, m, 1).isoformat()
    end = date(y + 1, 1, 1).isoformat() if m == 12 else date(y, m + 1, 1).isoformat()
    q = f"""
    WITH f AS (
      SELECT code, datetime,
        argMax(open,tuple(created_at,id)) fo,
        argMax(high,tuple(created_at,id)) fh,
        argMax(low,tuple(created_at,id)) fl,
        argMax(close,tuple(created_at,id)) fc,
        argMax(volume,tuple(created_at,id)) fv,
        argMax(amount,tuple(created_at,id)) fa,
        count() fn
      FROM stock.kline_minute_5
      WHERE datetime>=toDate(%(s)s) AND datetime<toDate(%(e)s)
        AND code IS NOT NULL AND code!=''
      GROUP BY code,datetime
    )
    SELECT c.code,c.datetime,c.open,c.high,c.low,c.close,c.volume,c.amount,
           f.fo,f.fh,f.fl,f.fc,f.fv,f.fa,f.fn
    FROM stock.kline_minute_5_pre2010_v20260921 c
    INNER JOIN f USING(code,datetime)
    WHERE c.datetime>=toDate(%(s)s) AND c.datetime<toDate(%(e)s)
      AND (abs(c.open-f.fo)>0 OR abs(c.high-f.fh)>0 OR abs(c.low-f.fl)>0
           OR abs(c.close-f.fc)>0 OR abs(c.volume-f.fv)>0 OR abs(c.amount-f.fa)>0
           OR f.fn>1)
    LIMIT 20000
    """
    return list(client.query(q, parameters={"s": start, "e": end}, settings=SETTINGS).result_rows)


def classify(row: tuple) -> tuple[str, dict]:
    code, dt, *vals = row
    co, ch, cl, cc, cv, ca, fo, fh, fl, fc, fv, fa, fn = vals
    ds = [rel(co, fo), rel(ch, fh), rel(cl, fl), rel(cc, fc), rel(cv, fv), rel(ca, fa)]
    p, v, a = any(x > PRICE_TOL for x in ds[:4]), ds[4] > VOL_TOL, ds[5] > AMT_TOL
    if fn > 1:
        cat = "formal_duplicate_logical_key"
    elif p:
        cat = "ohlc_price_over_0_5pct"
    elif v:
        cat = "volume_over_0_1pct"
    elif a:
        cat = "amount_over_1pct"
    else:
        cat = "within_policy_nonzero"
    rec = {"code": str(code), "datetime": str(dt)[:19],
           "candidate_open": co, "candidate_high": ch, "candidate_low": cl, "candidate_close": cc,
           "candidate_volume_lots": cv, "candidate_amount_yuan": ca,
           "formal_open": fo, "formal_high": fh, "formal_low": fl, "formal_close": fc,
           "formal_volume_lots": fv, "formal_amount_yuan": fa, "formal_logical_rows": fn,
           "rel_open": ds[0], "rel_high": ds[1], "rel_low": ds[2], "rel_close": ds[3],
           "rel_volume": ds[4], "rel_amount": ds[5],
           "reason": {"ohlc_price_over_0_5pct": "OHLC至少一项相对差异>0.5%",
                      "volume_over_0_1pct": "成交量相对差异>0.1%",
                      "amount_over_1pct": "成交额相对差异>1%",
                      "formal_duplicate_logical_key": "formal同一(code,datetime)存在多行",
                      "within_policy_nonzero": "字段非零差异但均在政策容差内"}[cat]}
    return cat, rec


def duplicate_samples(client) -> list[dict]:
    q = """
    SELECT code,datetime,count() n,min(open),max(open),min(amount),max(amount)
    FROM stock.kline_minute_15
    WHERE datetime>=toDate('2002-06-01') AND datetime<toDate('2002-07-01')
    GROUP BY code,datetime HAVING n>1 LIMIT 10
    """
    out = []
    for code, dt, n, omin, omax, amin, amax in client.query(q, settings=SETTINGS).result_rows:
        out.append({"category": "formal_duplicate_logical_key", "code": str(code), "datetime": str(dt)[:19],
                    "formal_logical_rows": n, "formal_open_min": omin, "formal_open_max": omax,
                    "formal_amount_min": amin, "formal_amount_max": amax,
                    "reason": "formal 15m表同一(code,datetime)存在多行；需保留重复证据，不能直接覆盖"})
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    samples: dict[str, list[dict]] = defaultdict(list)
    structure = read_structure_samples()
    samples["structure_48slot_missing"].extend(structure)
    # Lifecycle/suffix evidence is available only as missing source code-days;
    # do not infer an exchange suffix without a matching formal row.
    for r in read_all_review()[:10]:
        x = dict(r)
        x.update({"category": "lifecycle_suffix_or_source_coverage", "reason": "candidate review日无48根明细；现有证据未证明可用后缀/生命周期替代", "evidence_level": "code-day review only"})
        samples["lifecycle_suffix_or_source_coverage"].append(x)
    client = clickhouse_client()
    samples["formal_duplicate_logical_key"].extend(duplicate_samples(client))
    # June is present in every active year and keeps bounded reads small enough
    # for an interactive audit; samples are illustrative, not population counts.
    for y in range(2000, 2010):
        for m in (6,):
            try:
                rows = query_month(client, y, m)
            except Exception:
                continue
            for row in rows:
                cat, rec = classify(row)
                if len(samples[cat]) < 10:
                    rec["category"] = cat
                    rec["year"] = y
                    rec["month"] = m
                    samples[cat].append(rec)
            if all(len(samples[c]) >= 10 for c in ("ohlc_price_over_0_5pct", "volume_over_0_1pct", "amount_over_1pct", "formal_duplicate_logical_key")):
                break
        if all(len(samples[c]) >= 10 for c in ("ohlc_price_over_0_5pct", "volume_over_0_1pct", "amount_over_1pct", "formal_duplicate_logical_key")):
            break
    # Add explicit unavailable entries rather than fabricating row-level samples.
    for cat in ("ohlc_price_over_0_5pct", "volume_over_0_1pct", "amount_over_1pct", "formal_duplicate_logical_key"):
        samples.setdefault(cat, [])
    all_rows = []
    for cat, rows in samples.items():
        for i, r in enumerate(rows[:10], 1):
            r = dict(r); r.setdefault("category", cat); r["sample_no"] = i; all_rows.append(r)
    (OUT / "samples.json").write_text(json.dumps({"schema_version": "pre2010-review-samples-v1", "read_only": True,
        "policy": {"price_ohlc_relative_tolerance": PRICE_TOL, "volume_relative_tolerance": VOL_TOL, "amount_relative_tolerance": AMT_TOL, "amount_unit": "yuan"},
        "categories": {k: {"count": len(v[:10]), "raw_evidence": bool(v), "samples": v[:10]} for k,v in samples.items()}}, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    if all_rows:
        with (OUT / "samples.csv").open("w", newline="", encoding="utf-8-sig") as f:
            fields = sorted({k for r in all_rows for k in r})
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(all_rows)
    summary = ["# 2000—2009 review样本（只读）", "", "口径：volume=手，amount=元；OHLC≤0.5%、volume≤0.1%、amount≤1%仅用于对账容差，不自动覆盖formal。", ""]
    for cat, rows in samples.items():
        summary.append(f"## {cat}（{len(rows[:10])}条样本）")
        summary.append("有原始逐条证据。" if rows else "当前产物没有该类逐条证据，未伪造样本；需按bounded逐键审计补采集。")
        for r in rows[:10]:
            summary.append(f"- {r.get('code','')} {r.get('datetime',r.get('trade_date',''))}：{r.get('reason','')}")
        summary.append("")
    (OUT / "README_中文摘要.md").write_text("\n".join(summary), encoding="utf-8")
    print(json.dumps({"output": str(OUT), "categories": {k: len(v[:10]) for k,v in samples.items()}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

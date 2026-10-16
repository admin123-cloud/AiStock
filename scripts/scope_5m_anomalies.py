"""Create a per-code/date evidence manifest for frozen 5m anomalies.

Read-only: queries stocks/status/daily tables and writes only an external
runtime evidence file. It never invokes repair or derived-table migration.
"""
from __future__ import annotations
import argparse, gzip, hashlib, json
import re
from collections import Counter
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path
from utils.market_status import load_status_index, partition_status_keys, find_status_trade_conflicts

BLOCKED = {"daily_no_trade_evidence", "quit", "stock_missing", "unregistered_source_gap",
           "status_conflict_requires_notice", "manual_status_not_registered"}

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", default="20260912_astra5")
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    root = runtime_path() / "minute_migration_v2"
    override_path = root / "manual_scope_overrides_20260913.json"
    overrides = {}
    if override_path.exists():
        overrides = {(x.get('code'), x.get('trade_date')): x for x in json.loads(override_path.read_text(encoding='utf-8')).get('overrides', [])}
    baseline_path = root / f"baseline_{args.run_id}.json"
    if not baseline_path.exists(): raise SystemExit(f"baseline not found: {baseline_path}")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    client = clickhouse_client()
    stocks = {r[0]: r[1:] for r in client.query(
        "SELECT code, market, type, listing_status, list_date, delist_date, quit FROM stocks FINAL"
    ).result_rows}
    status_index = load_status_index(client)
    anomaly_keys = set()
    for entry in baseline['months']:
        with gzip.open(Path(entry['daily_file']), 'rt', encoding='utf-8') as handle:
            for line in handle:
                day, code, count = line.rstrip('\n').split('\t')
                if int(count) != 48:
                    anomaly_keys.add((code, day))
    status_plan = partition_status_keys(anomaly_keys, status_index,
        find_status_trade_conflicts(client, anomaly_keys, status_index))
    rows = []
    for entry in baseline["months"]:
        detail = Path(entry["daily_file"])
        detail_hash = entry.get("daily_sha256") or sha256(detail)
        with gzip.open(detail, "rt", encoding="utf-8") as handle:
            for line in handle:
                trade_date, code, count = line.rstrip("\n").split("\t")
                count = int(count)
                if count == 48: continue
                stock = stocks.get(code)
                # QMT is authoritative when present; retain a conservative
                # legacy prefix fallback for historical snapshots without a
                # stocks row (SZ fund/ETF and SH ETF families).
                if re.fullmatch(r"1[5-9]\d{4}\.SZ|12\d{4}\.SZ|11\d{4}\.SH|200\d{3}\.SZ|900\d{3}\.SH", str(code).upper()) or re.fullmatch(r"(5|56|58)\d{4,5}\.SH", str(code).upper()):
                    continue
                category = "stock_missing" if not stock else None
                stock_evidence = None
                if stock:
                    market, kind, listing, listed, delisted, quit_flag = stock
                    stock_evidence = {"market": str(market), "type": str(kind), "listing_status": str(listing),
                                      "list_date": str(listed), "delist_date": str(delisted), "quit": bool(quit_flag)}
                    if kind != "stock": category = f"non_stock:{kind}"
                    elif listing != "active": category = f"listing:{listing}"
                    elif listed and str(listed) > trade_date: category = "not_listed"
                    elif delisted and str(delisted) < trade_date: category = "delisted"
                    elif quit_flag: category = "quit"
                status_evidence = []
                override = overrides.get((code, trade_date))
                if override:
                    classification = override.get('classification', category)
                    category = ('manual_status_not_registered' if classification in {'no_trade', 'confirm_suspension'}
                                else classification)
                status_evidence = status_index.matches(code, trade_date)
                # QMT is authoritative for instrument type. ETFs are not
                # stocks and must never become manual stock-gap reviews.
                if category == "non_stock:etf":
                    continue
                if (code, trade_date) in status_plan['conflict']:
                    category = 'status_conflict_requires_notice'
                elif (code, trade_date) in status_plan['exempt'] and category in {None, 'manual_status_not_registered'}:
                    category = 'status:verified_business_absence'
                if category is None: category = "unregistered_source_gap"
                blocked = category in BLOCKED
                rows.append({"code": code, "date": trade_date, "count": count, "category": category,
                    "missing_count": max(0, 48-count), "evidence": {
                        "baseline_path": str(baseline_path), "baseline_sha256": sha256(baseline_path),
                        "daily_detail_path": str(detail), "daily_detail_sha256": detail_hash,
                        "stocks_snapshot": stock_evidence, "status_audit": status_evidence,
                        "manual_observation": override,
                    }, "authority": "QMT frozen 5m baseline + ClickHouse stocks/status read-only snapshot",
                    "requires_notice_review": (code, trade_date) in status_plan['lookup'],
                    "validity": "blocked_evidence" if blocked else "scoped_observation"})
    payload = {"status": "blocked_evidence" if any(r["validity"] == "blocked_evidence" for r in rows) else "scoped",
        "run_id": args.run_id, "baseline_fingerprint": baseline.get("fingerprint"),
        "baseline_cutoff": baseline.get("cutoff"), "rows": len(rows),
        "categories": dict(Counter(r["category"] for r in rows)),
        "blocked_evidence_rows": sum(r["validity"] == "blocked_evidence" for r in rows),
        "read_only": True, "formal_table_mutation": False, "items": rows}
    output = args.output or root / f"scoped_anomalies_{args.run_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"); tmp.replace(output)
    print(json.dumps({"output": str(output), "rows": len(rows), "categories": payload["categories"], "read_only": True}, ensure_ascii=False))
    return 0
if __name__ == "__main__": raise SystemExit(main())

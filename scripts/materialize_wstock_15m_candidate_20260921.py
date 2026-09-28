"""Materialize the frozen WStock 15m probe into isolated ClickHouse tables."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client


def stable_id(code: str, dt: str, batch: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{code}|{dt}|{batch}".encode()).digest()[:8], "big")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--batch-id", default="purchased_15m_wstock_tail6_20260921")
    args = ap.parse_args()
    cand = args.run_dir / "candidate_15m.jsonl.gz"
    audit = args.run_dir / "day_audit.json"
    if not cand.exists() or not audit.exists():
        raise SystemExit("missing frozen probe files")
    c = clickhouse_client()
    table = "stock.purchased_native_15m_wstock_tail6_candidate_20260921"
    audit_table = table + "_day_audit"
    c.command(f"""CREATE TABLE IF NOT EXISTS {table}
    (code String, datetime DateTime('Asia/Shanghai'), open Float64, high Float64,
     low Float64, close Float64, volume Float64, amount Float64,
     volume_unit LowCardinality(String), amount_unit LowCardinality(String),
     source_kind LowCardinality(String), source_priority UInt8, provenance String,
     source_frequency LowCardinality(String), source_year UInt16, trade_date Date,
     input_bars UInt16, quality_status LowCardinality(String), repair_reason String,
     batch_id String, created_at DateTime('Asia/Shanghai'), id UInt64)
    ENGINE = MergeTree PARTITION BY toYYYYMM(datetime) ORDER BY (code, datetime)""")
    c.command(f"""CREATE TABLE IF NOT EXISTS {audit_table}
    (code String, trade_date Date, source LowCardinality(String), bars UInt16,
     missing String, status LowCardinality(String), aggregate_json String,
     daily_json String, relative_diff_json String, invalid String,
     batch_id String, created_at DateTime('Asia/Shanghai'))
    ENGINE = MergeTree ORDER BY (code, trade_date, batch_id)""")
    c.command("SET max_partitions_per_insert_block=1000")
    now = datetime.now().astimezone()
    existing = int(c.query(f"SELECT count() FROM {table} WHERE batch_id=%(b)s", parameters={"b": args.batch_id}).result_rows[0][0])
    inserted = existing
    if existing == 0:
        rows = []
        with gzip.open(cand, "rt", encoding="utf-8") as fh:
            for line in fh:
                x = json.loads(line)
                dt = str(x["datetime"])
                day = str(x["trade_date"])
                rows.append([x["code"], datetime.fromisoformat(dt), x["open"], x["high"], x["low"], x["close"],
                             x["volume"], x["amount"], "lots", "yuan", "wstock_5m_aggregate",
                             1, str(args.run_dir), "15m", int(day[:4]), datetime.fromisoformat(day).date(), 3,
                             "repair_pass", "wstock_daily_gate", args.batch_id, now,
                             stable_id(x["code"], dt, args.batch_id)])
                if len(rows) >= 50_000:
                    c.insert(table, rows, column_names=["code", "datetime", "open", "high", "low", "close", "volume", "amount", "volume_unit", "amount_unit", "source_kind", "source_priority", "provenance", "source_frequency", "source_year", "trade_date", "input_bars", "quality_status", "repair_reason", "batch_id", "created_at", "id"])
                    inserted += len(rows)
                    rows.clear()
        if rows:
            c.insert(table, rows, column_names=["code", "datetime", "open", "high", "low", "close", "volume", "amount", "volume_unit", "amount_unit", "source_kind", "source_priority", "provenance", "source_frequency", "source_year", "trade_date", "input_bars", "quality_status", "repair_reason", "batch_id", "created_at", "id"])
            inserted += len(rows)
    audit_rows = []
    for x in json.loads(audit.read_text(encoding="utf-8")):
        if x.get("status") != "pass_wstock_5m_reaggregate":
            continue
        audit_rows.append([x["code"], datetime.fromisoformat(x["trade_date"]).date(), x.get("source", "wstock_tail6_5m"), int(x.get("bars", 0)), ",".join(x.get("missing", [])), x["status"], json.dumps(x.get("aggregate", {}), ensure_ascii=False), json.dumps(x.get("daily", {}), ensure_ascii=False), json.dumps(x.get("relative_diff", {}), ensure_ascii=False), ",".join(x.get("invalid", [])), args.batch_id, now])
    existing_audit = int(c.query(f"SELECT count() FROM {audit_table} WHERE batch_id=%(b)s", parameters={"b": args.batch_id}).result_rows[0][0])
    if audit_rows and existing_audit == 0:
        c.insert(audit_table, audit_rows, column_names=["code", "trade_date", "source", "bars", "missing", "status", "aggregate_json", "daily_json", "relative_diff_json", "invalid", "batch_id", "created_at"])
    payload = {"batch_id": args.batch_id, "candidate_table": table, "audit_table": audit_table, "inserted_rows": inserted, "pass_code_days": len(audit_rows), "formal_tables_touched": False, "review_queue_touched": False}
    (args.run_dir / "materialization.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

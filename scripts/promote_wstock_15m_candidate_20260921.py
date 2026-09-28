"""Promote the frozen WStock 15m candidate with an exact, recoverable backup."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client


FORMAL = "stock.kline_minute_15"
CANDIDATE = "stock.purchased_native_15m_wstock_tail6_candidate_20260921"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", default="purchased_15m_wstock_tail6_20260921")
    ap.add_argument("--backup", default="stock.kline_minute_15_repair_backup_wstock_tail6_20260921")
    ap.add_argument("--report", type=Path, default=Path(r"F:\AiStock\data\data\runtime\quality_plan\wstock_15m_probe_20260921_frozen\promotion.json"))
    args = ap.parse_args()
    c = clickhouse_client()
    candidate_count = int(c.query(f"SELECT count() FROM {CANDIDATE} WHERE batch_id=%(b)s", parameters={"b": args.batch_id}).result_rows[0][0])
    candidate_unique = int(c.query(f"SELECT uniqExact(tuple(code,datetime)) FROM {CANDIDATE} WHERE batch_id=%(b)s", parameters={"b": args.batch_id}).result_rows[0][0])
    if candidate_count == 0 or candidate_count != candidate_unique:
        raise SystemExit(f"candidate uniqueness gate failed: {candidate_count=} {candidate_unique=}")
    c.command(f"CREATE TABLE IF NOT EXISTS {args.backup} AS {FORMAL}")
    backup_existing = int(c.query(f"SELECT count() FROM {args.backup}").result_rows[0][0])
    if backup_existing:
        raise SystemExit(f"refusing to reuse non-empty backup table: {args.backup} rows={backup_existing}")
    keys = c.query(f"SELECT code, groupUniqArray(toString(trade_date)) FROM {CANDIDATE} WHERE batch_id=%(b)s GROUP BY code ORDER BY code", parameters={"b": args.batch_id}).result_rows
    backup_rows = 0
    deleted_groups = 0
    for code, days in keys:
        params = {"code": str(code), "days": [str(x) for x in days]}
        c.command(f"INSERT INTO {args.backup} SELECT * FROM {FORMAL} WHERE code=%(code)s AND toDate(datetime) IN %(days)s", parameters=params)
        c.command(f"ALTER TABLE {FORMAL} DELETE WHERE code=%(code)s AND toDate(datetime) IN %(days)s SETTINGS mutations_sync=1", parameters=params)
        deleted_groups += 1
    backup_rows = int(c.query(f"SELECT count() FROM {args.backup}").result_rows[0][0])
    c.command("SET max_partitions_per_insert_block=1000")
    c.command(f"""INSERT INTO {FORMAL} (code,datetime,open,high,low,close,volume,amount,created_at,id)
        SELECT code,datetime,open,high,low,close,volume,amount,created_at,id
        FROM {CANDIDATE} WHERE batch_id=%(b)s""", parameters={"b": args.batch_id})
    post = c.query(f"""SELECT count(), uniqExact(tuple(f.code,f.datetime))
        FROM {FORMAL} f INNER JOIN
        (SELECT DISTINCT code,datetime FROM {CANDIDATE} WHERE batch_id=%(b)s) k
        ON f.code=k.code AND f.datetime=k.datetime""", parameters={"b": args.batch_id}).result_rows[0]
    payload = {"batch_id": args.batch_id, "candidate_rows": candidate_count,
               "candidate_unique": candidate_unique, "backup_table": args.backup,
               "backup_rows": backup_rows, "deleted_code_groups": deleted_groups,
               "post_formal_rows_for_candidate_keys": int(post[0]),
               "post_formal_unique_for_candidate_keys": int(post[1]),
               "formal_tables_touched": True, "review_queue_touched": False,
               "completed_at": datetime.now().astimezone().isoformat()}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

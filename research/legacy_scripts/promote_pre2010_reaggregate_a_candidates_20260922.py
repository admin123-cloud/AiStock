"""Promote only A-grade rows produced by the B reaggregation pass."""

from __future__ import annotations

import json
import sys
from datetime import datetime

ROOT = __file__
from pathlib import Path
ROOT = Path(ROOT).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


SPECS = {
    "30": ("stock.pre2010_a_grade_30m_reaggregate_candidate_20260922", "stock.kline_minute_30", "stock.kline_minute_30_repair_backup_b_reaggregate_a_grade_20260922_v2"),
    "60": ("stock.pre2010_a_grade_60m_reaggregate_candidate_20260922", "stock.kline_minute_60", "stock.kline_minute_60_repair_backup_b_reaggregate_a_grade_20260922_v2"),
}


def main() -> int:
    c = clickhouse_client()
    c.command("SET max_partitions_per_insert_block=1000")
    result = {"policy": "only_A", "formal_tables_touched": True, "periods": {}}
    for freq, (stage, formal, backup) in SPECS.items():
        candidate_rows, candidate_unique = c.query(f"SELECT count(),uniqExact(tuple(code,datetime)) FROM {stage}").result_rows[0]
        if not candidate_rows or candidate_rows != candidate_unique:
            raise SystemExit(f"candidate gate failed {stage}: {candidate_rows} {candidate_unique}")
        c.command(f"CREATE TABLE {backup} AS {formal}")
        c.command(f"""INSERT INTO {backup}
            SELECT f.* FROM {formal} f
            WHERE (f.code,toDate(f.datetime)) IN
              (SELECT code,toDate(datetime) FROM {stage})""")
        c.command(f"""ALTER TABLE {formal} DELETE
            WHERE (code,toDate(datetime)) IN
              (SELECT code,toDate(datetime) FROM {stage})
            SETTINGS mutations_sync=1""")
        backup_rows = int(c.query(f"SELECT count() FROM {backup}").result_rows[0][0])
        c.command(f"""INSERT INTO {formal} (code,datetime,open,high,low,close,volume,amount,created_at,id)
            SELECT code,datetime,open,high,low,close,volume,amount,created_at,id FROM {stage}""")
        post = c.query(f"SELECT count(),uniqExact(tuple(code,datetime)) FROM {formal} WHERE (code,datetime) IN (SELECT code,datetime FROM {stage})").result_rows[0]
        if int(post[0]) != int(candidate_rows) or int(post[1]) != int(candidate_unique):
            raise SystemExit(f"post gate failed {formal}: {post}")
        result["periods"][freq] = {"candidate_rows": int(candidate_rows), "candidate_unique": int(candidate_unique), "backup": backup, "backup_rows": backup_rows, "post_formal_rows": int(post[0]), "post_formal_unique": int(post[1])}
    result["completed_at"] = datetime.now().astimezone().isoformat()
    out = runtime_path("quality_plan", "pre2010_bc_reaggregate_20260922", "promotion.json")
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

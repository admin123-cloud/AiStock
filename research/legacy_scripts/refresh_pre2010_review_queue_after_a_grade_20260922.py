"""Keep B/C review items while removing only newly auto-promoted A keys."""

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
from utils.paths import runtime_path


TABLE = "stock.purchased_history_data_review_items"
OLD_RUN = "purchased_history_review_20260921_user_policy_frequency_daily_v3_harry_wstock_tail6"
NEW_RUN = "purchased_history_review_20260922_a_grade_auto_b_c_manual"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", type=Path, default=runtime_path("quality_plan", "pre2010_a_grade_auto_20260921", "queue_refresh.json"))
    args = ap.parse_args()
    c = clickhouse_client()
    old_count = int(c.query(f"SELECT count() FROM {TABLE} WHERE run_id=%(run)s", parameters={"run": OLD_RUN}).result_rows[0][0])
    if not args.apply:
        print(json.dumps({"old_run": OLD_RUN, "new_run": NEW_RUN, "old_rows": old_count, "applied": False}, ensure_ascii=False, indent=2))
        return 0
    existing = int(c.query(f"SELECT count() FROM {TABLE} WHERE run_id=%(run)s", parameters={"run": NEW_RUN}).result_rows[0][0])
    if existing:
        raise SystemExit(f"refusing to append to existing run {NEW_RUN}: {existing} rows")
    c.command("SET max_partitions_per_insert_block=1000")
    cols = "run_id,review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason"
    sql = f"""
        INSERT INTO {TABLE} ({cols})
        SELECT
            '{NEW_RUN}', review_key, issue_type, source, code, trade_date, period,
            security_type, expected_count, actual_count, missing_count, source_batch_id,
            source_archive, source_entry, evidence_ref, evidence_sha256, observed_at, reason
        FROM {TABLE}
        WHERE run_id = '{OLD_RUN}'
          AND NOT (
            (period='15m' AND (code,trade_date) IN
                (SELECT code,toDate(datetime) FROM stock.pre2010_a_grade_15m_candidate_20260921))
            OR (period='30m' AND (code,trade_date) IN
                (SELECT code,toDate(datetime) FROM stock.pre2010_a_grade_30m_candidate_20260921))
            OR (period='60m' AND (code,trade_date) IN
                (SELECT code,toDate(datetime) FROM stock.pre2010_a_grade_60m_candidate_20260921))
          )
    """
    c.command(sql)
    counts = c.query(f"SELECT period,issue_type,count() FROM {TABLE} WHERE run_id=%(run)s GROUP BY period,issue_type ORDER BY period,issue_type", parameters={"run": NEW_RUN}).result_rows
    result = {"old_run": OLD_RUN, "new_run": NEW_RUN, "old_rows": old_count,
              "new_rows": sum(int(r[2]) for r in counts),
              "issue_type_counts": {f"{r[0]}:{r[1]}": int(r[2]) for r in counts},
              "a_grade_auto_promoted": True, "b_c_manual_review": True,
              "completed_at": datetime.now().astimezone().isoformat()}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

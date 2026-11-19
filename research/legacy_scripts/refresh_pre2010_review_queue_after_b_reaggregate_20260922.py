"""Remove only the newly A-approved B reaggregation keys from the review projection."""

from __future__ import annotations

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
OLD_RUN = "purchased_history_review_20260922_a_grade_auto_b_c_manual"
NEW_RUN = "purchased_history_review_20260922_b_reaggregate_a_auto_b_c_manual"


def main() -> int:
    c = clickhouse_client()
    old_rows = int(c.query(f"SELECT count() FROM {TABLE} WHERE run_id=%(r)s", parameters={"r": OLD_RUN}).result_rows[0][0])
    if int(c.query(f"SELECT count() FROM {TABLE} WHERE run_id=%(r)s", parameters={"r": NEW_RUN}).result_rows[0][0]):
        raise SystemExit(f"run already exists: {NEW_RUN}")
    cols = "run_id,review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason"
    c.command(f"""INSERT INTO {TABLE} ({cols})
        SELECT '{NEW_RUN}',review_key,issue_type,source,code,trade_date,period,security_type,
            expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,
            evidence_ref,evidence_sha256,observed_at,reason
        FROM {TABLE}
        WHERE run_id='{OLD_RUN}'
          AND NOT (period='30m' AND (code,trade_date) IN
              (SELECT code,toDate(datetime) FROM stock.pre2010_a_grade_30m_reaggregate_candidate_20260922))
          AND NOT (period='60m' AND (code,trade_date) IN
              (SELECT code,toDate(datetime) FROM stock.pre2010_a_grade_60m_reaggregate_candidate_20260922))""")
    counts = c.query(f"SELECT period,issue_type,count() FROM {TABLE} WHERE run_id=%(r)s GROUP BY period,issue_type ORDER BY period,issue_type", parameters={"r": NEW_RUN}).result_rows
    result = {"old_run": OLD_RUN, "new_run": NEW_RUN, "old_rows": old_rows,
              "new_rows": sum(int(x[2]) for x in counts),
              "counts": {f"{x[0]}:{x[1]}": int(x[2]) for x in counts},
              "removed_only_a_grade": True, "completed_at": datetime.now().astimezone().isoformat()}
    out = runtime_path("quality_plan", "pre2010_bc_reaggregate_20260922", "queue_refresh.json")
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

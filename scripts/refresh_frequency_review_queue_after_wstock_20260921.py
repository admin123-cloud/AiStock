"""Project the active review queue after WStock 15m repairs (queue only)."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path

TABLE = "stock.purchased_history_data_review_items"
OLD_RUN = "purchased_history_review_20260921_user_policy_frequency_daily_v2_harry_repair"
RUN_ID = "purchased_history_review_20260921_user_policy_frequency_daily_v3_harry_wstock_tail6"
TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    c = clickhouse_client()
    cols = [x[0] for x in c.query(f"DESCRIBE TABLE {TABLE}").result_rows]
    rows = c.query(f"SELECT * FROM {TABLE} WHERE run_id=%(run)s ORDER BY issue_type,trade_date,code", parameters={"run": OLD_RUN}).result_rows
    resolved = {(str(code), day) for code, day in c.query("""SELECT code,trade_date FROM stock.purchased_native_15m_wstock_tail6_candidate_20260921_day_audit WHERE batch_id='purchased_15m_wstock_tail6_20260921' AND status='pass_wstock_5m_reaggregate'""").result_rows}
    resolved |= {(str(code), day) for code, day in c.query("""SELECT code,trade_date FROM stock.purchased_native_15m_harry_repair_candidate_20260921_day_audit WHERE repair_status='pass_exact_5m_reaggregate'""").result_rows}
    kept = []
    skipped = 0
    for row in rows:
        item = list(row)
        if str(item[2]) == "purchased_15m_daily_snapshot_reconciliation" and (str(item[4]), item[5]) in resolved:
            skipped += 1
            continue
        item[0] = RUN_ID
        kept.append(item)
        if len(kept) >= 5000:
            c.insert(TABLE, kept, column_names=cols)
            kept.clear()
    if kept:
        c.insert(TABLE, kept, column_names=cols)
    counts = c.query(f"SELECT issue_type,count() FROM {TABLE} WHERE run_id=%(run)s GROUP BY issue_type ORDER BY issue_type", parameters={"run": RUN_ID}).result_rows
    total = sum(int(x[1]) for x in counts)
    manifest = {"schema_version": 1, "run_id": RUN_ID, "old_run": OLD_RUN,
                "generated_at": datetime.now(TZ).isoformat(), "old_rows": len(rows),
                "resolved_15m_code_days": len(resolved), "skipped_15m_rows": skipped,
                "rows": total, "issue_type_counts": {str(k): int(v) for k, v in counts},
                "formal_table_mutation": False, "source_runs": [OLD_RUN, "purchased_15m_wstock_tail6_20260921"]}
    out = runtime_path("purchased_history", "data_review_manifest.json")
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = runtime_path("quality_plan", "wstock_15m_probe_20260921_frozen", "queue_refresh.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

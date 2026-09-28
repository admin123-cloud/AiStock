"""Materialize 15m/30m/60m and 2010 daily review items into the manual queue."""
from __future__ import annotations

import hashlib
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
MANIFEST = runtime_path("purchased_history", "data_review_manifest.json")
RUN_ID = "purchased_history_review_20260921_user_policy_frequency_daily_v2_harry_repair"
TZ = ZoneInfo("Asia/Shanghai")


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_row(*, issue_type: str, source: str, code: str, day, period: str,
             expected: int, actual: int, source_batch: str, evidence_ref: str,
             reason: dict, source_archive: str = "", source_entry: str = "") -> list[object]:
    evidence = {"issue_type": issue_type, "source": source, "code": code,
                "trade_date": str(day), "period": period, "expected_count": expected,
                "actual_count": actual, "reason": reason, "evidence_ref": evidence_ref}
    return [
        RUN_ID,
        f"purchased_history|{issue_type}|{period}|{code}|{day}",
        issue_type, source, code, day, period, "stock", expected, actual,
        max(expected - actual, 0), source_batch, source_archive, source_entry,
        evidence_ref, digest(evidence), datetime.now(TZ).replace(tzinfo=None),
        json.dumps(reason, ensure_ascii=False, sort_keys=True),
    ]


def insert_rows(client, rows: list[list[object]], total: int) -> int:
    if not rows:
        return total
    client.insert(TABLE, rows, column_names=[
        "run_id", "review_key", "issue_type", "source", "code", "trade_date", "period",
        "security_type", "expected_count", "actual_count", "missing_count", "source_batch_id",
        "source_archive", "source_entry", "evidence_ref", "evidence_sha256", "observed_at", "reason",
    ])
    return total + len(rows)


def main() -> int:
    client = clickhouse_client()
    client.command(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS reason String DEFAULT ''")
    old_run = "purchased_history_review_20260918_5m_source_closeout_v14"
    old = client.query(
        f"SELECT * FROM {TABLE} WHERE run_id=%(run_id)s",
        parameters={"run_id": old_run},
    ).result_rows
    columns = [x[0] for x in client.query(f"DESCRIBE TABLE {TABLE}").result_rows]
    rows = []
    total = 0
    resolved_15m = {
        (str(code), day)
        for code, day in client.query(
            """SELECT code, trade_date
               FROM stock.purchased_native_15m_harry_repair_candidate_20260921_day_audit
               WHERE repair_status='pass_exact_5m_reaggregate'"""
        ).result_rows
    }
    # Carry forward the current active queue into the new projection.
    for source_row in old:
        item = list(source_row)
        if str(item[2]) == "purchased_15m_daily_snapshot_reconciliation" and (str(item[4]), item[5]) in resolved_15m:
            continue
        item[0] = RUN_ID
        rows.append(item)
    total = insert_rows(client, rows, total)
    print(json.dumps({"carried_forward": total}, ensure_ascii=False), flush=True)

    sources = [
        ("15m", 16, "stock.purchased_native_15m_candidate_user_policy_20260921_day_audit",
         "purchased_native_15m_candidate_user_policy_20260921", "purchased_15m_daily_snapshot_reconciliation"),
        ("30m", 8, "stock.purchased_native_30m_candidate_user_policy_20260921_day_audit",
         "purchased_native_30m_candidate_user_policy_20260921", "purchased_30m_daily_snapshot_reconciliation"),
        ("60m", 4, "stock.purchased_native_60m_candidate_user_policy_20260921_day_audit",
         "purchased_native_60m_candidate_user_policy_20260921", "purchased_60m_daily_snapshot_reconciliation"),
    ]
    for period, expected, audit_table, batch, issue_type in sources:
        sql = f"""
        SELECT source_year, code, trade_date, candidate_bars, audit_status,
               price_relative_diff, volume_relative_diff, amount_relative_diff
        FROM {audit_table}
        WHERE audit_status IN ('review','review_daily_diff','review_no_daily')
        ORDER BY source_year, trade_date, code
        """
        pending = []
        inserted = 0
        for year, code, day, actual, status, price_diff, volume_diff, amount_diff in client.query(sql).result_rows:
            if period == "15m" and (str(code), day) in resolved_15m:
                continue
            reason = {"audit_status": str(status), "price_relative_diff": price_diff,
                      "volume_relative_diff": volume_diff, "amount_relative_diff": amount_diff,
                      "thresholds": {"price": 0.005, "volume": 0.001, "amount": 0.01}}
            pending.append(make_row(issue_type=issue_type, source="purchased_native_frequency_candidate",
                                    code=str(code), day=day, period=period, expected=expected,
                                    actual=int(actual), source_batch=batch, evidence_ref=audit_table,
                                    reason=reason, source_archive=audit_table,
                                    source_entry=f"{year}|{code}|{day}"))
            if len(pending) >= 5000:
                inserted = insert_rows(client, pending, inserted); pending = []
        inserted = insert_rows(client, pending, inserted)
        total += inserted
        print(json.dumps({"period": period, "inserted": inserted}, ensure_ascii=False), flush=True)

    # 2010 purchased daily rows with missing OHLC are not safe to promote.
    pending = []
    inserted = 0
    for code, day in client.query("""
        SELECT code, trade_date
        FROM stock.purchased_history_daily_snapshot_stage
        WHERE toYear(trade_date)=2010 AND quality_status='missing_ohlc'
        GROUP BY code, trade_date ORDER BY trade_date, code
    """).result_rows:
        pending.append(make_row(issue_type="purchased_daily_missing_ohlc_2010", source="purchased_daily_snapshot",
                                code=str(code), day=day, period="1d", expected=1, actual=0,
                                source_batch="purchased_daily_snapshot_20260921_2010",
                                evidence_ref="stock.purchased_history_daily_snapshot_stage",
                                reason={"quality_status": "missing_ohlc", "next_action": "manual_source_confirmation"},
                                source_archive="stock.purchased_history_daily_snapshot_stage",
                                source_entry=f"2010|{code}|{day}"))
        if len(pending) >= 5000:
            inserted = insert_rows(client, pending, inserted); pending = []
    inserted = insert_rows(client, pending, inserted)
    total += inserted
    print(json.dumps({"daily_missing_ohlc_inserted": inserted}, ensure_ascii=False), flush=True)

    # 2010 valid purchased daily rows whose formal volume differs materially.
    pending = []
    inserted = 0
    for code, day, diff in client.query("""
        SELECT s.code, s.trade_date,
               abs(s.volume_lots-f.volume)/greatest(abs(f.volume),1.) AS volume_diff
        FROM (SELECT code,trade_date,any(volume_lots) volume_lots
              FROM stock.purchased_history_daily_snapshot_stage
              WHERE toYear(trade_date)=2010 AND quality_status='valid'
              GROUP BY code,trade_date) s
        INNER JOIN (SELECT code,trade_date,any(volume) volume
                    FROM stock.kline_daily WHERE toYear(trade_date)=2010
                    GROUP BY code,trade_date) f
          ON s.code=f.code AND s.trade_date=f.trade_date
        WHERE abs(s.volume_lots-f.volume)/greatest(abs(f.volume),1.) > 0.001
        ORDER BY s.trade_date,s.code
    """).result_rows:
        pending.append(make_row(issue_type="purchased_daily_volume_diff_2010", source="purchased_daily_snapshot",
                                code=str(code), day=day, period="1d", expected=1, actual=1,
                                source_batch="purchased_daily_snapshot_20260921_2010",
                                evidence_ref="stock.purchased_history_daily_snapshot_stage_vs_stock.kline_daily",
                                reason={"volume_relative_diff": float(diff), "threshold": 0.001,
                                        "next_action": "manual_volume_unit_confirmation"},
                                source_archive="stock.purchased_history_daily_snapshot_stage",
                                source_entry=f"2010|{code}|{day}"))
    inserted = insert_rows(client, pending, inserted)
    total += inserted
    print(json.dumps({"daily_volume_diff_inserted": inserted}, ensure_ascii=False), flush=True)

    manifest = {
        "schema_version": 1, "run_id": RUN_ID, "table": TABLE,
        "generated_at": datetime.now(TZ).isoformat(), "rows": total,
        "unique_review_keys": client.query(f"SELECT uniqExact(review_key) FROM {TABLE} WHERE run_id=%(run_id)s", parameters={"run_id": RUN_ID}).result_rows[0][0],
        "formal_table_mutation": False,
        "source_runs": [old_run, "purchased_15m_harry_repair_20260921",
                        "purchased_15m_harry_repair_20260921_full_v1",
                        "purchased_native_15m_candidate_user_policy_20260921",
                        "purchased_native_30m_candidate_user_policy_20260921",
                        "purchased_native_60m_candidate_user_policy_20260921"],
    }
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

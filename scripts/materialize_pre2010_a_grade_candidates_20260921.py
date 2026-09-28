"""Build the A-grade-only pre-2010 candidate layers.

This stage is intentionally non-destructive: it creates isolated candidate
tables from the existing purchased native candidate layers.  B/C rows remain
in the review queue and are never copied here.
"""

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


RUN = "pre2010_a_grade_auto_20260921"
SPECS = {
    "15": {
        "candidate": "stock.purchased_native_15m_candidate_user_policy_20260921",
        "audit": "stock.purchased_native_15m_candidate_user_policy_20260921_day_audit",
        "stage": "stock.pre2010_a_grade_15m_candidate_20260921",
        "bars": 16,
        "extra": "(native_complete=1 OR five_complete=1)",
        "id_expr": "id",
        "day_expr": "trade_date",
    },
    "30": {
        "candidate": "stock.purchased_native_30m_candidate_user_policy_20260921",
        "audit": "stock.purchased_native_30m_candidate_user_policy_20260921_day_audit",
        "stage": "stock.pre2010_a_grade_30m_candidate_20260921",
        "bars": 8,
        "extra": "1=1",
        "id_expr": "cityHash64(concat(code, '|', toString(datetime)))",
        "day_expr": "toDate(datetime)",
    },
    "60": {
        "candidate": "stock.purchased_native_60m_candidate_user_policy_20260921",
        "audit": "stock.purchased_native_60m_candidate_user_policy_20260921_day_audit",
        "stage": "stock.pre2010_a_grade_60m_candidate_20260921",
        "bars": 4,
        "extra": "1=1",
        "id_expr": "cityHash64(concat(code, '|', toString(datetime)))",
        "day_expr": "toDate(datetime)",
    },
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="drop only the exact stage tables before rebuilding")
    ap.add_argument("--report", type=Path, default=runtime_path("quality_plan", "pre2010_a_grade_auto_20260921", "candidate_report.json"))
    args = ap.parse_args()
    c = clickhouse_client()
    result = {"run_id": RUN, "policy": {"price_max": 0.01, "volume_max": 0.05, "amount_max": 0.10}, "periods": {}}

    for freq, spec in SPECS.items():
        stage = spec["stage"]
        if args.rebuild:
            c.command(f"DROP TABLE IF EXISTS {stage}")
        c.command(f"CREATE TABLE IF NOT EXISTS {stage} AS stock.kline_minute_{freq}")
        existing = int(c.query(f"SELECT count() FROM {stage}").result_rows[0][0])
        if existing:
            raise SystemExit(f"refusing to append to non-empty stage {stage}: {existing} rows; use --rebuild")

        key_pred = f"""
            source_year BETWEEN 2000 AND 2010
            AND audit_status != 'pass'
            AND daily_exists = 1
            AND candidate_bars = {spec['bars']}
            AND {spec['extra']}
            AND abs(price_relative_diff) <= 0.01
            AND abs(volume_relative_diff) <= 0.05
            AND abs(amount_relative_diff) <= 0.10
        """
        audit_stats = c.query(f"""SELECT count(), sum(candidate_bars)
            FROM {spec['audit']} WHERE {key_pred}""").result_rows[0]
        insert_sql = f"""
            INSERT INTO {stage}
                (code, datetime, open, high, low, close, volume, amount, created_at, id)
            SELECT code, datetime, open, high, low, close, volume, amount, created_at, {spec['id_expr']}
            FROM {spec['candidate']}
            WHERE source_year BETWEEN 2000 AND 2010
              AND (code, {spec['day_expr']}) IN
                (SELECT code, trade_date FROM {spec['audit']} WHERE {key_pred})
              AND (code, {spec['day_expr']}) NOT IN
                (SELECT code, {spec['day_expr']} FROM {spec['candidate']}
                 WHERE source_year BETWEEN 2000 AND 2010
                   AND (open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
                        OR high < greatest(open, close) OR low > least(open, close)
                        OR volume < 0 OR amount < 0))
        """
        c.command("SET max_partitions_per_insert_block=1000")
        c.command(insert_sql)
        counts = c.query(f"""SELECT count(), uniqExact(tuple(code, datetime)),
            countIf(isNull(code) OR isNull(datetime) OR isNull(open) OR isNull(high)
                OR isNull(low) OR isNull(close) OR isNull(volume) OR isNull(amount)
                OR open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
                OR high < greatest(open, close) OR low > least(open, close)
                OR volume < 0 OR amount < 0)
            FROM {stage}""").result_rows[0]
        if int(counts[0]) != int(counts[1]) or int(counts[2]):
            raise SystemExit(f"A-stage quality gate failed for {stage}: {counts}")
        result["periods"][freq] = {
            "audit_code_days": int(audit_stats[0]),
            "audit_candidate_bars": int(audit_stats[1]),
            "stage_rows": int(counts[0]),
            "stage_unique_keys": int(counts[1]),
            "invalid_rows": int(counts[2]),
            "stage_table": stage,
        }

    result["completed_at"] = datetime.now().astimezone().isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Promote only the validated A-grade candidate layers into formal tables."""

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


SPECS = {
    "15": ("stock.pre2010_a_grade_15m_candidate_20260921", "stock.kline_minute_15", "stock.kline_minute_15_repair_backup_a_grade_20260921"),
    "30": ("stock.pre2010_a_grade_30m_candidate_20260921", "stock.kline_minute_30", "stock.kline_minute_30_repair_backup_a_grade_20260921"),
    "60": ("stock.pre2010_a_grade_60m_candidate_20260921", "stock.kline_minute_60", "stock.kline_minute_60_repair_backup_a_grade_20260921"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="perform formal-table mutations; without this only print the plan")
    ap.add_argument("--report", type=Path, default=runtime_path("quality_plan", "pre2010_a_grade_auto_20260921", "promotion.json"))
    args = ap.parse_args()
    c = clickhouse_client()
    result = {"policy": {"auto_grade": "A", "B_C": "manual_review"}, "periods": {}, "applied": args.apply}
    if not args.apply:
        for freq, (stage, formal, backup) in SPECS.items():
            result["periods"][freq] = {"stage": stage, "formal": formal,
                                        "stage_rows": int(c.query(f"SELECT count() FROM {stage}").result_rows[0][0]),
                                        "backup": backup}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    c.command("SET max_partitions_per_insert_block=1000")
    for freq, (stage, formal, backup) in SPECS.items():
        candidate_rows, candidate_unique = c.query(
            f"SELECT count(), uniqExact(tuple(code,datetime)) FROM {stage}"
        ).result_rows[0]
        if int(candidate_rows) == 0 or int(candidate_rows) != int(candidate_unique):
            raise SystemExit(f"candidate uniqueness gate failed: {stage} {candidate_rows=} {candidate_unique=}")
        c.command(f"CREATE TABLE IF NOT EXISTS {backup} AS {formal}")
        backup_existing = int(c.query(f"SELECT count() FROM {backup}").result_rows[0][0])
        if backup_existing:
            raise SystemExit(f"refusing to reuse non-empty backup: {backup} rows={backup_existing}")

        backup_rows = 0
        for year in range(2000, 2011):
            params = {"year": year}
            c.command(f"""INSERT INTO {backup}
                SELECT f.* FROM {formal} f
                WHERE toYear(f.datetime)=%(year)s
                  AND (f.code,toDate(f.datetime)) IN
                    (SELECT code,toDate(datetime) FROM {stage} WHERE toYear(datetime)=%(year)s)""", parameters=params)
            c.command(f"""ALTER TABLE {formal} DELETE
                WHERE toYear(datetime)=%(year)s
                  AND (code,toDate(datetime)) IN
                    (SELECT code,toDate(datetime) FROM {stage} WHERE toYear(datetime)=%(year)s)
                SETTINGS mutations_sync=1""", parameters=params)
        backup_rows = int(c.query(f"SELECT count() FROM {backup}").result_rows[0][0])
        c.command(f"""INSERT INTO {formal} (code,datetime,open,high,low,close,volume,amount,created_at,id)
            SELECT code,datetime,open,high,low,close,volume,amount,created_at,id FROM {stage}""")
        post = c.query(f"""SELECT count(), uniqExact(tuple(code,datetime)) FROM {formal}
            WHERE (code,datetime) IN (SELECT code,datetime FROM {stage})""").result_rows[0]
        if int(post[0]) != int(candidate_rows) or int(post[1]) != int(candidate_unique):
            raise SystemExit(f"formal post gate failed: {formal} post={post} candidate={(candidate_rows,candidate_unique)}")
        result["periods"][freq] = {"stage": stage, "formal": formal,
                                    "candidate_rows": int(candidate_rows), "candidate_unique": int(candidate_unique),
                                    "backup": backup, "backup_rows": backup_rows,
                                    "post_formal_rows": int(post[0]), "post_formal_unique": int(post[1])}
    result["completed_at"] = datetime.now().astimezone().isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

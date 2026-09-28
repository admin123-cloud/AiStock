"""Reaggregate B-grade 30m/60m keys from complete isolated 5m detail.

This script only creates isolated raw/audit/A-grade candidate tables. It never
mutates formal tables or removes review items.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


RUN = os.getenv("AISTOCK_B_REAGGREGATE_RUN", "pre2010_refit_b_20260922")
FIVE = os.getenv("AISTOCK_B_REAGGREGATE_SOURCE", "stock.pre2010_repair_5m_candidate_20260921")
SUFFIX = os.getenv("AISTOCK_B_REAGGREGATE_SUFFIX", "")
QUEUE_RUN = os.getenv("AISTOCK_B_REAGGREGATE_QUEUE_RUN", "purchased_history_review_20260922_formal5m_reaggregate_a_auto_b_c_manual")
SELECTED_PERIODS = set(os.getenv("AISTOCK_B_REAGGREGATE_PERIODS", "15,30,60").split(","))
QUEUE = "stock.purchased_history_data_review_items"
PERIODS = {
    "15": {"bars": 16, "audit": "stock.purchased_native_15m_candidate_user_policy_20260921_day_audit"},
    "30": {"bars": 8, "audit": "stock.purchased_native_30m_candidate_user_policy_20260921_day_audit"},
    "60": {"bars": 4, "audit": "stock.purchased_native_60m_candidate_user_policy_20260921_day_audit"},
}


def main() -> int:
    c = clickhouse_client()
    c.command("SET max_partitions_per_insert_block=1000")
    result = {"run_id": RUN, "source": FIVE, "periods": {}, "formal_tables_touched": False}
    for freq, spec in PERIODS.items():
        if freq not in SELECTED_PERIODS:
            continue
        key = f"stock.pre2010_bc_b_keys_{freq}m_20260922{SUFFIX}"
        raw = f"stock.pre2010_bc_reaggregate_{freq}m_raw_20260922{SUFFIX}"
        audit = f"stock.pre2010_bc_reaggregate_{freq}m_audit_20260922{SUFFIX}"
        astage = f"stock.pre2010_a_grade_{freq}m_reaggregate_candidate_20260922{SUFFIX}"
        for t in [key, raw, audit, astage]:
            if c.query("SELECT count() FROM system.tables WHERE database='stock' AND name=%(name)s", parameters={"name": t.split('.', 1)[1]}).result_rows[0][0]:
                raise SystemExit(f"refusing existing table {t}; use a new run id")

        key_ddl = f"""CREATE TABLE {key} (
            code String, trade_date Date, source_year UInt16,
            daily_close Float64, daily_volume Float64, daily_amount Float64
        ) ENGINE=MergeTree ORDER BY (source_year,code,trade_date)"""
        c.command(key_ddl)
        if freq == "15":
            key_sql = f"""INSERT INTO {key}
                SELECT a.code,a.trade_date,a.source_year,d.close,d.volume,d.amount
                FROM {spec['audit']} a INNER JOIN
                  (SELECT code,trade_date FROM {QUEUE}
                   WHERE run_id='{QUEUE_RUN}' AND period='{freq}m') q
                ON a.code=q.code AND a.trade_date=q.trade_date
                INNER JOIN stock.kline_daily d ON d.code=a.code AND d.trade_date=a.trade_date
                WHERE a.daily_exists=1 AND a.candidate_bars={spec['bars']}"""
        else:
            key_sql = f"""INSERT INTO {key}
                SELECT a.code,a.trade_date,a.source_year,d.close,d.volume,d.amount
                FROM {spec['audit']} a INNER JOIN
                  (SELECT code,trade_date FROM {QUEUE}
                   WHERE run_id='{QUEUE_RUN}' AND period='{freq}m') q
                ON a.code=q.code AND a.trade_date=q.trade_date
                INNER JOIN stock.kline_daily d ON d.code=a.code AND d.trade_date=a.trade_date
                WHERE a.candidate_bars={spec['bars']}
                  AND d.open>0 AND d.high>0 AND d.low>0 AND d.close>0
                  AND d.high>=greatest(d.open,d.close) AND d.low<=least(d.open,d.close)
                  AND d.volume>=0 AND d.amount>=0"""
        c.command(key_sql)
        key_count = int(c.query(f"SELECT count() FROM {key}").result_rows[0][0])

        c.command(f"CREATE TABLE {raw} AS stock.kline_minute_{freq}")
        slot_width = 3 if freq == "15" else (6 if freq == "30" else 12)
        raw_sql = f"""INSERT INTO {raw} (code,datetime,open,high,low,close,volume,amount,created_at,id)
            WITH src AS (
                SELECT p.code,p.datetime,p.open,p.high,p.low,p.close,p.volume,p.amount,
                    if(toHour(p.datetime)<12,
                       intDiv(dateDiff('minute',toDateTime(concat(toString(toDate(p.datetime)),' 09:30:00'),'Asia/Shanghai'),p.datetime),5),
                       24+intDiv(dateDiff('minute',toDateTime(concat(toString(toDate(p.datetime)),' 13:00:00'),'Asia/Shanghai'),p.datetime),5)) AS idx
                FROM {FIVE} p
                WHERE (p.code,toDate(p.datetime)) IN (SELECT code,trade_date FROM {key})
            ), bars AS (
                SELECT code,toDate(datetime) AS d,
                    intDiv(idx,{slot_width}) AS slot,
                    argMin(open,datetime) AS open,argMax(close,datetime) AS close,
                    max(high) AS high,min(low) AS low,sum(volume) AS volume,sum(amount) AS amount,
                    min(datetime) AS dt
                FROM src WHERE idx BETWEEN 0 AND 47 GROUP BY code,d,slot
            )
            SELECT code,dt,open,high,low,close,volume,amount,now(),
                cityHash64(concat(code,'|',toString(dt),'|','{freq}m|','{RUN}')) FROM bars"""
        c.command(raw_sql)
        raw_count = int(c.query(f"SELECT count() FROM {raw}").result_rows[0][0])

        c.command(f"""CREATE TABLE {audit} (
            code String, trade_date Date, source_rows UInt32, candidate_bars UInt16,
            candidate_close Float64, candidate_volume Float64, candidate_amount Float64,
            daily_close Float64, daily_volume Float64, daily_amount Float64,
            price_relative_diff Float64, volume_relative_diff Float64, amount_relative_diff Float64,
            grade LowCardinality(String), reason LowCardinality(String)
        ) ENGINE=MergeTree ORDER BY (code,trade_date)""")
        audit_sql = f"""INSERT INTO {audit}
            WITH source_counts AS (
                SELECT p.code,toDate(p.datetime) d,count() source_rows
                FROM {FIVE} p INNER JOIN (SELECT code,trade_date FROM {key}) k
                  ON p.code=k.code AND toDate(p.datetime)=k.trade_date
                GROUP BY p.code,d
            ), agg AS (
                SELECT r.code,toDate(r.datetime) d,count() candidate_bars,
                    argMax(r.close,r.datetime) candidate_close,sum(r.volume) candidate_volume,sum(r.amount) candidate_amount
                FROM {raw} r GROUP BY r.code,d
            )
            SELECT a.code,a.d,sc.source_rows,a.candidate_bars,a.candidate_close,a.candidate_volume,a.candidate_amount,
                k.daily_close,k.daily_volume,k.daily_amount,
                abs(a.candidate_close-k.daily_close)/greatest(abs(k.daily_close),1.0),
                abs(a.candidate_volume-k.daily_volume)/greatest(abs(k.daily_volume),1.0),
                abs(a.candidate_amount-k.daily_amount)/greatest(abs(k.daily_amount),1.0),
                if(sc.source_rows=48 AND a.candidate_bars={spec['bars']}
                    AND abs(a.candidate_close-k.daily_close)/greatest(abs(k.daily_close),1.0)<=0.01
                    AND abs(a.candidate_volume-k.daily_volume)/greatest(abs(k.daily_volume),1.0)<=0.05
                    AND abs(a.candidate_amount-k.daily_amount)/greatest(abs(k.daily_amount),1.0)<=0.10,'A','B'),
                if(sc.source_rows=48 AND a.candidate_bars={spec['bars']},'reaggregate_complete','reaggregate_incomplete')
            FROM agg a INNER JOIN source_counts sc ON a.code=sc.code AND a.d=sc.d
            INNER JOIN {key} k ON a.code=k.code AND a.d=k.trade_date"""
        c.command(audit_sql)
        c.command(f"""CREATE TABLE {astage} AS stock.kline_minute_{freq}""")
        c.command(f"""INSERT INTO {astage} (code,datetime,open,high,low,close,volume,amount,created_at,id)
            SELECT r.code,r.datetime,r.open,r.high,r.low,r.close,r.volume,r.amount,r.created_at,r.id
            FROM {raw} r INNER JOIN {audit} a ON r.code=a.code AND toDate(r.datetime)=a.trade_date
            WHERE a.grade='A'""")
        stats = c.query(f"SELECT count(),uniqExact(tuple(code,datetime)) FROM {astage}").result_rows[0]
        invalid = int(c.query(f"""SELECT countIf(open<=0 OR high<=0 OR low<=0 OR close<=0
            OR high<greatest(open,close) OR low>least(open,close) OR volume<0 OR amount<0) FROM {astage}""").result_rows[0][0])
        final_stage = astage
        if invalid:
            clean = astage + "_clean"
            c.command(f"CREATE TABLE {clean} AS stock.kline_minute_{freq}")
            c.command(f"""INSERT INTO {clean}
                SELECT * FROM {astage}
                WHERE (code,toDate(datetime)) NOT IN
                  (SELECT code,toDate(datetime) FROM {astage}
                   WHERE open<=0 OR high<=0 OR low<=0 OR close<=0
                     OR high<greatest(open,close) OR low>least(open,close)
                     OR volume<0 OR amount<0)""")
            clean_stats = c.query(f"SELECT count(),uniqExact(tuple(code,datetime)),countIf(open<=0 OR high<=0 OR low<=0 OR close<=0 OR high<greatest(open,close) OR low>least(open,close) OR volume<0 OR amount<0) FROM {clean}").result_rows[0]
            if int(clean_stats[0]) != int(clean_stats[1]) or int(clean_stats[2]):
                raise SystemExit(f"clean A-stage gate failed {clean}: {clean_stats}")
            final_stage = clean
        audit_counts = c.query(f"SELECT grade,count() FROM {audit} GROUP BY grade ORDER BY grade").result_rows
        result["periods"][freq] = {"key_count": key_count, "raw_rows": raw_count,
            "audit_grade_counts": {str(x[0]): int(x[1]) for x in audit_counts},
            "a_stage_rows": int(stats[0]), "a_stage_unique": int(stats[1]), "invalid_rows": invalid,
            "key_table": key, "raw_table": raw, "audit_table": audit, "a_stage": final_stage}
    out = runtime_path("quality_plan", "pre2010_bc_reaggregate_20260922", "report.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    result["completed_at"] = datetime.now().astimezone().isoformat()
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Build a read-only unified code-day ledger for the 2000-2010 review run."""
from __future__ import annotations
import json
import sys
import os
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path

RUN = os.getenv("AISTOCK_LEDGER_RUN", "purchased_history_review_20260922_refit_a_b_confirmed_20260922")
TABLE = "stock.pre2010_quality_ledger_20260922"

def main() -> int:
    c = clickhouse_client()
    c.command("SET max_threads=2")
    c.command(f"DROP TABLE IF EXISTS {TABLE}")
    c.command(f"""
    CREATE TABLE {TABLE} (
      code String, trade_date Date, list_date Nullable(Date), security_type LowCardinality(String),
      has_5m UInt8, has_15m UInt8, has_30m UInt8, has_60m UInt8,
      review_5m UInt8, review_15m UInt8, review_30m UInt8, review_60m UInt8,
      daily_exists UInt8, daily_valid UInt8, issue_count UInt16,
      lifecycle_valid UInt8, ledger_status LowCardinality(String),
      built_at DateTime('Asia/Shanghai')
    ) ENGINE=MergeTree ORDER BY (trade_date, code)
    """)
    c.command(f"""
    INSERT INTO {TABLE}
    WITH q AS (
      SELECT code,trade_date,
        countIf(period='5m') review_5m,countIf(period='15m') review_15m,
        countIf(period='30m') review_30m,countIf(period='60m') review_60m,
        count() issue_count
      FROM stock.purchased_history_data_review_items
      WHERE run_id='{RUN}' GROUP BY code,trade_date
    ), d AS (
      SELECT code,trade_date,1 daily_exists,
        if(open>0 AND high>0 AND low>0 AND close>0 AND high>=greatest(open,close)
           AND low<=least(open,close) AND volume>=0 AND amount>=0,1,0) daily_valid
      FROM stock.kline_daily
      WHERE trade_date BETWEEN toDate('2000-01-01') AND toDate('2010-12-31')
    ), s AS (SELECT code,list_date,type FROM stocks)
    SELECT q.code,q.trade_date,s.list_date,s.type,
      if(q.review_5m=0,1,0),if(q.review_15m=0,1,0),if(q.review_30m=0,1,0),if(q.review_60m=0,1,0),
      q.review_5m>0,q.review_15m>0,q.review_30m>0,q.review_60m>0,
      ifNull(d.daily_exists,0),ifNull(d.daily_valid,0),q.issue_count,
      if(s.type!='stock' OR s.list_date IS NULL OR q.trade_date>=s.list_date,1,0),
      multiIf(s.type='stock' AND s.list_date IS NOT NULL AND q.trade_date<s.list_date,'lifecycle_invalid',
              q.review_5m>0,'source_5m_review',q.review_15m>0 OR q.review_30m>0 OR q.review_60m>0,'frequency_review',
              q.issue_count>0,'daily_review','closed'),
      now()
    FROM q
    LEFT JOIN s ON q.code=s.code
    LEFT JOIN d ON q.code=d.code AND q.trade_date=d.trade_date
    /* The review run itself is the authoritative sparse key set; avoid a full
       five-table fanout here and derive presence from whether a period remains
       in the queue. Formal-table validation is performed separately. */
    """)
    counts = c.query(f"SELECT ledger_status,count() FROM {TABLE} GROUP BY ledger_status ORDER BY ledger_status").result_rows
    periods = c.query(f"SELECT review_5m,review_15m,review_30m,review_60m,count() FROM {TABLE} GROUP BY review_5m,review_15m,review_30m,review_60m ORDER BY count() DESC LIMIT 20").result_rows
    out = runtime_path('quality_plan','pre2010_unified_quality_ledger_20260922','summary.json')
    out.parent.mkdir(parents=True,exist_ok=True)
    payload={'run_id':RUN,'table':TABLE,'rows':int(c.query(f'SELECT count() FROM {TABLE}').result_rows[0][0]),'status_counts':counts,'period_matrix':periods,'built_at':datetime.now().astimezone().isoformat()}
    out.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(payload,ensure_ascii=False))
    return 0
if __name__=='__main__': raise SystemExit(main())

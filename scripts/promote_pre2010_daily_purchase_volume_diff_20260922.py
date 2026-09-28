"""Promote tiny 2010 daily volume differences using the purchased snapshot."""
from pathlib import Path
import json
import clickhouse_connect

BASE='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v3_no_trade_resolved'
NEW='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v4_daily_purchase_volume'
KEYS=[('000603.SZ','2010-05-07'),('000603.SZ','2010-05-14'),('002214.SZ','2010-07-05'),('600892.SH','2010-07-05'),('002004.SZ','2010-07-07'),('000922.SZ','2010-11-04'),('000922.SZ','2010-11-05'),('000922.SZ','2010-11-09'),('600706.SH','2010-11-09'),('000895.SZ','2010-12-01'),('000895.SZ','2010-12-02'),('000895.SZ','2010-12-06'),('300055.SZ','2010-12-20'),('300087.SZ','2010-12-29')]
COND=','.join(f"('{c}','{d}')" for c,d in KEYS)
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
c.command('SET mutations_sync=2')
candidate='stock.pre2010_daily_purchase_volume_diff_candidate_20260922'
backup='stock.kline_daily_repair_backup_purchase_volume_diff_20260922'
c.command(f'DROP TABLE IF EXISTS {candidate}')
c.command(f'DROP TABLE IF EXISTS {backup}')
c.command(f'''CREATE TABLE {backup} ENGINE=MergeTree ORDER BY (code,trade_date) AS
SELECT * FROM stock.kline_daily WHERE (code,trade_date) IN ({COND})''')
c.command(f'''CREATE TABLE {candidate} ENGINE=MergeTree ORDER BY (code,trade_date) AS
SELECT d.code,d.trade_date,d.open,d.high,d.low,d.close,
       s.volume_lots AS volume,s.amount_yuan AS amount,
       d.amplitude,d.change_pct,d.change_amount,d.turnover_rate,d.created_at,d.id
FROM stock.kline_daily d
INNER JOIN (SELECT code,trade_date,argMax(volume_lots,ingested_at) volume_lots,argMax(amount_yuan,ingested_at) amount_yuan
            FROM stock.purchased_history_daily_snapshot_stage
            WHERE (code,trade_date) IN ({COND}) GROUP BY code,trade_date) s
 ON d.code=s.code AND d.trade_date=s.trade_date
WHERE (d.code,d.trade_date) IN ({COND})''')
rows=int(c.query(f'SELECT count() FROM {candidate}').result_rows[0][0])
if rows != len(KEYS): raise RuntimeError(f'candidate rows {rows} != expected {len(KEYS)}')
c.command(f"ALTER TABLE stock.kline_daily DELETE WHERE (code,trade_date) IN ({COND})")
c.command(f'''INSERT INTO stock.kline_daily
SELECT code,trade_date,open,high,low,close,volume,amount,amplitude,change_pct,change_amount,turnover_rate,now(),id
FROM {candidate}''')
cols='run_id,review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason'
c.command(f'''INSERT INTO stock.purchased_history_data_review_items ({cols})
SELECT '{NEW}',review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason
FROM stock.purchased_history_data_review_items
WHERE run_id='{BASE}' AND NOT (period='1d' AND issue_type='purchased_daily_volume_diff_2010')''')
count=int(c.query(f"SELECT count() FROM stock.purchased_history_data_review_items WHERE run_id='{NEW}'").result_rows[0][0])
result={'base_run':BASE,'run_id':NEW,'policy':'purchased_daily_snapshot_authoritative_for_2010_volume_diff','candidate_rows':rows,'backup':backup,'formal_rows_written':rows,'new_review_rows':count}
Path(r'F:\AiStock\data\data\runtime\purchased_history\daily_purchase_volume_diff_promotion_20260922.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))

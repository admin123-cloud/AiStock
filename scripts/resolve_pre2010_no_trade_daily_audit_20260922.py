import json
from datetime import datetime, date
from pathlib import Path
import clickhouse_connect

BASE='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v2'
NEW='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v3_no_trade_resolved'
keys=[('600591.SH','2010-01-25'),('600607.SH','2010-02-12'),('600842.SH','2010-02-12'),('600003.SH','2010-02-26'),('001914.SZ','2010-03-03'),('001872.SZ','2010-06-11'),('001914.SZ','2010-06-30'),('001914.SZ','2010-09-17'),('001914.SZ','2010-12-30')]
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
c.command('''CREATE TABLE IF NOT EXISTS stock.pre2010_daily_no_trade_resolution_20260922 (
 code String, trade_date Date, resolution LowCardinality(String), reason String,
 free_source_audit String, local_5m_bars UInt32, resolved_at DateTime('Asia/Shanghai')
) ENGINE=MergeTree ORDER BY (trade_date,code)''')
rows=[(a,date.fromisoformat(b),'verified_no_trade_or_suspension','AkShare/Sina、AkShare/Eastmoney、Tencent均无精确记录；本地5m为0根','F:/Stock/AiStockData/data/runtime/purchased_history/daily_free_source_audit_20260922.json',0,datetime.now()) for a,b in keys]
c.insert('stock.pre2010_daily_no_trade_resolution_20260922',rows,column_names=['code','trade_date','resolution','reason','free_source_audit','local_5m_bars','resolved_at'])
cols='run_id,review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason'
c.command(f'''INSERT INTO stock.purchased_history_data_review_items ({cols})
SELECT '{NEW}',review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason
FROM stock.purchased_history_data_review_items
WHERE run_id='{BASE}' AND NOT ((code,trade_date) IN (SELECT code,trade_date FROM stock.pre2010_daily_no_trade_resolution_20260922))''')
counts=c.query(f"SELECT period,issue_type,count() FROM stock.purchased_history_data_review_items WHERE run_id='{NEW}' GROUP BY period,issue_type ORDER BY period,issue_type").result_rows
removed=c.query(f"SELECT count() FROM stock.purchased_history_data_review_items WHERE run_id='{BASE}' AND (code,trade_date) IN (SELECT code,trade_date FROM stock.pre2010_daily_no_trade_resolution_20260922)").result_rows[0][0]
result={'base_run':BASE,'run_id':NEW,'removed_rows':int(removed),'new_rows':sum(int(x[2]) for x in counts),'counts':{f'{x[0]}:{x[1]}':int(x[2]) for x in counts},'resolved_keys':len(keys),'formal_table_mutation':False,'resolved_at':datetime.now().astimezone().isoformat()}
out=Path(r'F:\AiStock\data\data\runtime\purchased_history\no_trade_resolution_20260922.json');out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))

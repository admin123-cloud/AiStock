import json
from datetime import datetime
from pathlib import Path
import clickhouse_connect

BASE='purchased_history_review_20260922_formal5m_reaggregate_a_auto_b_c_manual'
NEW='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v2'
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
c.command(f'DROP TABLE IF EXISTS stock._tmp_final_queue_keys')
c.command(f"ALTER TABLE stock.purchased_history_data_review_items DELETE WHERE run_id='{NEW}'")
cols='run_id,review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason'
c.command(f'''INSERT INTO stock.purchased_history_data_review_items ({cols})
SELECT '{NEW}',review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason
FROM stock.purchased_history_data_review_items
WHERE run_id='{BASE}'
AND NOT (period='15m' AND (code,trade_date) IN (SELECT code,toDate(datetime) FROM stock.pre2010_refit_a_confirmed_15m_20260922))
AND NOT (period='30m' AND (code,trade_date) IN (SELECT code,toDate(datetime) FROM stock.pre2010_refit_b_confirmed_30m_20260922))
AND NOT (period='60m' AND (code,trade_date) IN (SELECT code,toDate(datetime) FROM stock.pre2010_refit_b_confirmed_60m_20260922))''')
rows=c.query(f"SELECT period,issue_type,count() FROM stock.purchased_history_data_review_items WHERE run_id='{NEW}' GROUP BY period,issue_type ORDER BY period,issue_type").result_rows
res={'base_run':BASE,'run_id':NEW,'rows':sum(int(x[2]) for x in rows),'counts':{f'{x[0]}:{x[1]}':int(x[2]) for x in rows},'created_at':datetime.now().astimezone().isoformat()}
print(json.dumps(res,ensure_ascii=False,indent=2))
Path(r'F:/Stock/AiStockData/data/runtime/purchased_history/queue_refresh_final_20260922.json').write_text(json.dumps(res,ensure_ascii=False,indent=2),encoding='utf-8')

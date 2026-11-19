import json
from datetime import datetime
from pathlib import Path
import clickhouse_connect
BASE='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v6_30m_strict_a'
NEW='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v7_30m_b_confirmed'
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
cols='run_id,review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason'
c.command(f'''INSERT INTO stock.purchased_history_data_review_items ({cols})
SELECT '{NEW}',review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason
FROM stock.purchased_history_data_review_items
WHERE run_id='{BASE}' AND NOT (period='30m' AND (code,trade_date) IN (SELECT code,trade_date FROM stock.pre2010_30m_b_selected_clean_keys_20260922))''')
rows=c.query(f"SELECT period,issue_type,count() FROM stock.purchased_history_data_review_items WHERE run_id='{NEW}' GROUP BY period,issue_type ORDER BY period,issue_type").result_rows
result={'base_run':BASE,'run_id':NEW,'rows':sum(int(x[2]) for x in rows),'removed_30m_rows':1942,'counts':{f'{x[0]}:{x[1]}':int(x[2]) for x in rows},'created_at':datetime.now().astimezone().isoformat()}
Path(r'F:\AiStock\data\data\runtime\purchased_history\30m_queue_refresh_v7_20260922.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))

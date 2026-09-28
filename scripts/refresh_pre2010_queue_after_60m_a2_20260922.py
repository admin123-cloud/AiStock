import json
import clickhouse_connect

base='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v4_daily_purchase_volume'
new='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v5_60m_a2_daily_volume_resolved'
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
c.command(f"ALTER TABLE stock.purchased_history_data_review_items DELETE WHERE run_id='{new}'")
cols='run_id,review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason'
c.command(f'''INSERT INTO stock.purchased_history_data_review_items ({cols})
SELECT '{new}',review_key,issue_type,source,code,trade_date,period,security_type,expected_count,actual_count,missing_count,source_batch_id,source_archive,source_entry,evidence_ref,evidence_sha256,observed_at,reason
FROM stock.purchased_history_data_review_items WHERE run_id='{base}'
AND NOT (period='60m' AND (code,trade_date) IN (SELECT code,trade_date FROM stock.pre2010_60m_clear_better_keys_20260922))''')
rows=c.query(f"SELECT period,issue_type,count() FROM stock.purchased_history_data_review_items WHERE run_id='{new}' GROUP BY period,issue_type ORDER BY period,issue_type").result_rows
result={'run_id':new,'rows':sum(int(x[2]) for x in rows),'counts':{f'{x[0]}:{x[1]}':int(x[2]) for x in rows}}
print(json.dumps(result,ensure_ascii=False,indent=2))

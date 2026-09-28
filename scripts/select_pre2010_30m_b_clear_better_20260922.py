import json
from datetime import datetime
from pathlib import Path
import clickhouse_connect

c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
c.command('SET max_partitions_per_insert_block=1000')
a='stock.pre2010_bc_reaggregate_30m_audit_20260922_v4'
cmp='stock.pre2010_30m_b_confirmed_keys_20260922'; raw='stock.pre2010_bc_reaggregate_30m_raw_20260922_v4'
fa='stock.pre2010_30m_formal_agg_for_b_20260922'; sel='stock.pre2010_30m_b_selected_20260922'; out='stock.pre2010_30m_b_selected_candidate_20260922'
for t in [fa,sel,out]: c.command(f'DROP TABLE IF EXISTS {t}')
c.command(f'''CREATE TABLE {fa} ENGINE=MergeTree ORDER BY (code,trade_date) SETTINGS allow_nullable_key=1 AS
SELECT f.code,toDate(f.datetime) trade_date,argMax(f.close,f.datetime) close,sum(f.volume) volume,sum(f.amount) amount,count() bars
FROM stock.kline_minute_30 f INNER JOIN {cmp} k ON f.code=k.code AND toDate(f.datetime)=k.trade_date
GROUP BY f.code,toDate(f.datetime)''')
c.command(f'CREATE TABLE {sel} (code String, trade_date Date) ENGINE=MergeTree ORDER BY (code,trade_date)')
c.command(f'''INSERT INTO {sel}
SELECT k.code,k.trade_date FROM {cmp} k INNER JOIN {a} a ON k.code=a.code AND k.trade_date=a.trade_date
LEFT JOIN {fa} f ON k.code=f.code AND k.trade_date=f.trade_date
WHERE (f.bars=0 OR (a.price_relative_diff+a.volume_relative_diff+a.amount_relative_diff) < (abs(a.candidate_close-f.close)/greatest(abs(f.close),1)+abs(a.candidate_volume-f.volume)/greatest(abs(f.volume),1)+abs(a.candidate_amount-f.amount)/greatest(abs(f.amount),1)))''')
c.command(f'CREATE TABLE {out} AS stock.kline_minute_30')
c.command(f'INSERT INTO {out} SELECT r.* FROM {raw} r INNER JOIN {sel} k ON r.code=k.code AND toDate(r.datetime)=k.trade_date')
result={'confirmed_keys':int(c.query(f'SELECT count() FROM {cmp}').result_rows[0][0]),'selected_better_keys':int(c.query(f'SELECT count() FROM {sel}').result_rows[0][0]),'candidate_rows':int(c.query(f'SELECT count() FROM {out}').result_rows[0][0]),'created_at':datetime.now().astimezone().isoformat()}
Path(r'F:\AiStock\data\data\runtime\purchased_history\30m_b_clear_better_selection_20260922.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))

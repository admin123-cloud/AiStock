import json
from datetime import datetime
from pathlib import Path
import clickhouse_connect

c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
c.command('SET max_partitions_per_insert_block=1000')
a='stock.pre2010_bc_reaggregate_30m_audit_20260922_v4'; native='stock.purchased_native_30m_candidate_user_policy_20260921'; raw='stock.pre2010_bc_reaggregate_30m_raw_20260922_v4'
agg='stock.pre2010_30m_native_agg_20260922'; cmp='stock.pre2010_30m_b_confirmed_keys_20260922'; out='stock.pre2010_30m_b_confirmed_candidate_20260922'
for t in [agg,cmp,out]: c.command(f'DROP TABLE IF EXISTS {t}')
c.command(f'''CREATE TABLE {agg} ENGINE=MergeTree ORDER BY (code,trade_date) AS
SELECT n.code,toDate(n.datetime) trade_date,argMin(n.open,n.datetime) open,argMax(n.close,n.datetime) close,max(n.high) high,min(n.low) low,sum(n.volume) volume,sum(n.amount) amount,count() bars
FROM {native} n INNER JOIN (SELECT code,trade_date FROM {a} WHERE grade='B') k ON n.code=k.code AND toDate(n.datetime)=k.trade_date
GROUP BY n.code,toDate(n.datetime)''')
c.command(f'''CREATE TABLE {cmp} ENGINE=MergeTree ORDER BY (code,trade_date) AS
SELECT a.code,a.trade_date FROM {a} a INNER JOIN {agg} n ON a.code=n.code AND a.trade_date=n.trade_date
WHERE a.grade='B' AND n.bars=8
  AND abs(a.candidate_close-n.close)/greatest(abs(n.close),1)<=0.02
  AND abs(a.candidate_volume-n.volume)/greatest(abs(n.volume),1)<=0.15
  AND abs(a.candidate_amount-n.amount)/greatest(abs(n.amount),1)<=0.25''')
c.command(f'CREATE TABLE {out} AS stock.kline_minute_30')
c.command(f'INSERT INTO {out} SELECT r.* FROM {raw} r INNER JOIN {cmp} k ON r.code=k.code AND toDate(r.datetime)=k.trade_date')
result={'b_audit_keys':int(c.query(f"SELECT count() FROM {a} WHERE grade='B'").result_rows[0][0]),'native_agg_keys':int(c.query(f'SELECT count() FROM {agg}').result_rows[0][0]),'confirmed_keys':int(c.query(f'SELECT count() FROM {cmp}').result_rows[0][0]),'candidate_rows':int(c.query(f'SELECT count() FROM {out}').result_rows[0][0]),'created_at':datetime.now().astimezone().isoformat()}
Path(r'F:\AiStock\data\data\runtime\purchased_history\30m_b_native_confirmation_20260922.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False,indent=2))

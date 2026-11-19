import clickhouse_connect
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
c.command('SET max_partitions_per_insert_block=1000')
src='stock.pre2010_30m_b_selected_candidate_20260922'; bad='stock.pre2010_30m_b_bad_keys_20260922'; out='stock.pre2010_30m_b_selected_candidate_20260922_clean'
for t in [bad,out]: c.command(f'DROP TABLE IF EXISTS {t}')
c.command(f'''CREATE TABLE {bad} (code String,trade_date Date) ENGINE=MergeTree ORDER BY (code,trade_date) AS
SELECT code,toDate(datetime) trade_date FROM {src} WHERE open<=0 OR high<=0 OR low<=0 OR close<=0 OR high<greatest(open,close) OR low>least(open,close) OR volume<0 OR amount<0 GROUP BY code,toDate(datetime)''')
c.command(f'CREATE TABLE {out} AS stock.kline_minute_30')
c.command(f'INSERT INTO {out} SELECT s.* FROM {src} s WHERE (s.code,toDate(s.datetime)) NOT IN (SELECT code,trade_date FROM {bad})')
print({'bad_keys':int(c.query(f'SELECT count() FROM {bad}').result_rows[0][0]),'clean_rows':int(c.query(f'SELECT count() FROM {out}').result_rows[0][0]),'clean_unique':int(c.query(f'SELECT uniqExact((code,datetime)) FROM {out}').result_rows[0][0])})

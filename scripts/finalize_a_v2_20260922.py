import clickhouse_connect
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
for f in ['30','60']:
 k=f'stock.pre2010_refit_a_keys_{f}m_20260922_v2'; raw=f'stock.pre2010_bc_reaggregate_{f}m_raw_20260922_formal5m_v2'; out=f'stock.pre2010_refit_confirmed_{f}m_20260922_v2'; sel=f'stock.pre2010_refit_a_selected_{f}m_20260922_v2'
 c.command(f'DROP TABLE IF EXISTS {sel}')
 c.command(f'CREATE TABLE {sel} ENGINE=MergeTree ORDER BY (code,trade_date) AS SELECT k.code,k.trade_date FROM {k} k LEFT JOIN (SELECT DISTINCT code,toDate(datetime) d FROM stock.kline_minute_{f}) x ON k.code=x.code AND k.trade_date=x.d WHERE x.code=\'\'')
 c.command(f'DROP TABLE IF EXISTS {out}')
 c.command(f'CREATE TABLE {out} ENGINE=MergeTree ORDER BY (code,datetime) SETTINGS allow_nullable_key=1 AS SELECT * FROM stock.kline_minute_{f} LIMIT 0')
 c.command(f'INSERT INTO {out} SELECT r.* FROM {raw} r INNER JOIN {sel} x ON r.code=x.code AND toDate(r.datetime)=x.trade_date WHERE r.code IS NOT NULL AND r.datetime IS NOT NULL')
 print(f,c.query(f'SELECT count() FROM {sel}').result_rows[0][0],c.query(f'SELECT count() FROM {out}').result_rows[0][0],flush=True)

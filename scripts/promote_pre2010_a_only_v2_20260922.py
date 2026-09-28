import clickhouse_connect
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
for freq,bars in [('30',8),('60',4)]:
 audit=f'stock.pre2010_bc_reaggregate_{freq}m_audit_20260922_formal5m_v2'; raw=f'stock.pre2010_bc_reaggregate_{freq}m_raw_20260922_formal5m_v2'; k=f'stock.pre2010_refit_a_keys_{freq}m_20260922_v2'; out=f'stock.pre2010_refit_confirmed_{freq}m_20260922_v2'
 c.command(f'DROP TABLE IF EXISTS {k}'); c.command(f'DROP TABLE IF EXISTS {out}')
 c.command(f'''CREATE TABLE {k} ENGINE=MergeTree ORDER BY (code,trade_date) AS SELECT code,trade_date FROM {audit} LIMIT 0''')
 c.command(f'''INSERT INTO {k} SELECT code,trade_date FROM {audit} WHERE grade='A' AND candidate_bars={bars} AND price_relative_diff<=0.005 AND volume_relative_diff<=0.001 AND amount_relative_diff<=0.01''')
 c.command(f'''CREATE TABLE {out} ENGINE=MergeTree ORDER BY (code,datetime) SETTINGS allow_nullable_key=1 AS SELECT * FROM stock.kline_minute_{freq} LIMIT 0''')
 c.command(f'''INSERT INTO {out} SELECT r.* FROM {raw} r INNER JOIN {k} x ON r.code=x.code AND toDate(r.datetime)=x.trade_date WHERE r.code IS NOT NULL AND r.datetime IS NOT NULL''')
 print(freq,c.query(f'SELECT count() FROM {k}').result_rows[0][0],c.query(f'SELECT count() FROM {out}').result_rows[0][0],flush=True)

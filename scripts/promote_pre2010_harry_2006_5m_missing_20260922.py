import clickhouse_connect
c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
out='stock.pre2010_harry_2006_5m_missing_only_20260922'
c.command(f'DROP TABLE IF EXISTS {out}')
c.command(f'''CREATE TABLE {out} ENGINE=MergeTree ORDER BY (code,datetime) SETTINGS allow_nullable_key=1 AS
SELECT r.* FROM stock.pre2010_harry_2006_5m_accepted_20260922 r
LEFT JOIN (SELECT code,datetime FROM stock.kline_minute_5
           WHERE toDate(datetime) IN ('2006-01-09','2006-12-14','2006-12-15')
             AND code IN (SELECT DISTINCT code FROM stock.pre2010_harry_2006_5m_accepted_20260922)) f
ON r.code=f.code AND r.datetime=f.datetime
WHERE f.code='' ''')
print(c.query(f'SELECT count(),uniqExact((code,datetime)) FROM {out}').result_rows[0])

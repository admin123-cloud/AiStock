import sys
sys.path.insert(0, r'F:\AiStock\app')
from utils.market_warehouse import clickhouse_client

c = clickhouse_client()
print('db:', c.database if hasattr(c, 'database') else '?')
print('000056.SZ 2003:', c.query("SELECT toYYYYMM(datetime), count() FROM kline_minute_5 WHERE code='000056.SZ' AND toYYYYMM(datetime) BETWEEN 200301 AND 200307 GROUP BY 1 ORDER BY 1").result_rows)
print('000056.SZ all count:', c.query("SELECT count() FROM kline_minute_5 WHERE code='000056.SZ'").result_rows)
print('2003-07-03 count by code sample (like 00005x):', c.query("SELECT code, count() FROM kline_minute_5 WHERE toDate(datetime)='2003-07-03' AND code LIKE '00005%.SZ' GROUP BY code").result_rows)
print('day rows for 000006.SH (target full 48):', c.query("SELECT count() FROM kline_minute_5 WHERE code='000006.SH' AND toDate(datetime)='2002-01-31'").result_rows)

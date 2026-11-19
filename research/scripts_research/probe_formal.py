import sys
sys.path.insert(0, r'F:\AiStock\app')
from utils.market_warehouse import clickhouse_client

c = clickhouse_client()
print(c.query("SELECT count(), min(datetime), max(datetime) FROM kline_minute_5 WHERE toDate(datetime)='2003-07-03'").result_rows)
print(c.query("SELECT count() FROM kline_minute_5 WHERE code='000056.SZ' AND toDate(datetime)='2003-07-03'").result_rows)
print(c.query("SELECT DISTINCT code FROM kline_minute_5 WHERE toDate(datetime)='2003-07-03' LIMIT 8").result_rows)
print(c.query("SELECT name FROM system.tables WHERE database='stock' AND name LIKE '%minute%'").result_rows)

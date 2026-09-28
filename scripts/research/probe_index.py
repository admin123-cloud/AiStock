import sys
sys.path.insert(0, r'F:\AiStock\app')
from utils.market_warehouse import clickhouse_client

c = clickhouse_client()
print('000006.SH total in kline_minute_5:', c.query("SELECT count(), min(datetime), max(datetime) FROM kline_minute_5 WHERE code='000006.SH'").result_rows)
print('000006.SH around 2002-01:', c.query("SELECT count() FROM kline_minute_5 WHERE code='000006.SH' AND toYYYYMM(datetime)=200201").result_rows)
print('stocks table type:', c.query("SELECT code, market, type, listing_status FROM stocks FINAL WHERE code IN ('000006.SH','000056.SZ','399106.SZ')").result_rows)
print('399106.SZ total:', c.query("SELECT count(), min(datetime) FROM kline_minute_5 WHERE code='399106.SZ'").result_rows)

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

client = clickhouse_client()
source = client.query("""
SELECT code, datetime, open, high, low, close, volume, amount
FROM stock.pre2010_repair_5m_candidate_20260921
WHERE code='000001.SZ'
  AND datetime >= toDateTime('2006-01-09 09:35:00','Asia/Shanghai')
  AND datetime < toDateTime('2006-01-09 15:05:00','Asia/Shanghai')
ORDER BY datetime
""").result_rows
target = client.query("""
SELECT code, datetime, open, high, low, close, volume, amount
FROM stock.kline_minute_15
WHERE datetime >= toDateTime('2006-01-09 00:00:00','Asia/Shanghai')
  AND datetime < toDateTime('2006-01-10 00:00:00','Asia/Shanghai')
ORDER BY datetime
""").result_rows
print('source_5m_count', len(source), 'first', source[:3])
print('target_15m_count', len(target), 'first', target[:10])

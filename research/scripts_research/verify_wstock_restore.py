import json, sys
sys.path.insert(0, r'F:\AiStock\app')
from utils.market_warehouse import clickhouse_client

c = clickhouse_client()
staged = json.load(open(r'F:\AiStock\app\scripts\research\wstock_staged.json', encoding='utf-8'))
bad = []
pairs = sorted({(r['code'], r['trade_date']) for r in staged['accepted']})
for code, day in pairs:
    n = c.query('SELECT count() FROM kline_minute_5 WHERE toDate(datetime)={d:Date} AND code={c:String}', parameters={'d': day, 'c': code}, settings={'max_threads': 1}).result_rows[0][0]
    if n != 48:
        bad.append((code, day, n))
print('code_days checked:', len(pairs), 'not-48:', bad if bad else 'none — all 48/48 ✓')

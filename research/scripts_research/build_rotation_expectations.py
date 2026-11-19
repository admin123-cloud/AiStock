"""为轮换日生成精确预期:每个尾号在正式表中整日缺失(或部分缺失)的代码日清单与根数。"""
import json, sys
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client

queue = json.load(open(r'F:\AiStock\app\scripts\research\queue_items_20260918.json', encoding='utf-8'))
man = {r['key']: r for r in json.load(open(r'F:\AiStock\app\scripts\research\wstock_restore_manifest.json', encoding='utf-8'))}
done = json.load(open(r'F:\AiStock\app\scripts\research\wstock_applied.json', encoding='utf-8'))
# tail6 已恢复的 60 个代码日不再计入
restored6 = set()
staged = json.load(open(r'F:\AiStock\app\scripts\research\wstock_staged.json', encoding='utf-8'))
for r in staged['staged_rows']:
    restored6.add((r[0], r[1][:10]))

c = clickhouse_client()
by_tail = defaultdict(list)
for it in queue:
    code, day = it['code'], it['trade_date'][:10]
    tail = code.split('.')[0][-1]
    if tail == '6' or (code, day) in restored6:
        continue
    by_tail[tail].append((code, day))

out = {}
for tail in '3457':
    rows = []
    for code, day in sorted(by_tail[tail]):
        n = c.query('SELECT count() FROM kline_minute_5 WHERE toDate(datetime)={d:Date} AND code={c:String}',
                    parameters={'d': day, 'c': code}, settings={'max_threads': 1}).result_rows[0][0]
        rows.append({'code': code, 'date': day, 'formal_rows': n, 'to_add': 48 - n})
    out[tail] = rows
    print(f'tail {tail}: {len(rows)} code_days, bars to add: {sum(max(0,r["to_add"]) for r in rows)}')

json.dump(out, open(r'F:\AiStock\data\data\warehouse\wstock_restore_20260919\rotation_expectations.json', 'w', encoding='utf-8'),
          ensure_ascii=False, indent=1)
print('saved rotation_expectations.json')

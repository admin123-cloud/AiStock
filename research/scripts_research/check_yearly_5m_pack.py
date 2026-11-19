# 用 2000_2005_5min.zip(独立年包)交叉验证审核队列中 2010 前的 5m 缺失项
import json, zipfile, io
from collections import Counter

ZP = r'E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按年汇总\2000_2005_5min.zip'
items = json.load(open(r'F:\AiStock\app\scripts\research\queue_items_20260918.json', encoding='utf-8'))
targets = [i for i in items if i['trade_date'][:4] <= '2005']
print('queue items <=2005:', len(targets))

z = zipfile.ZipFile(ZP)
names = set(z.namelist())

BUCKETS = ['09:35','09:40','09:45','09:50','09:55','10:00','10:05','10:10','10:15','10:20','10:25','10:30',
           '10:35','10:40','10:45','10:50','10:55','11:00','11:05','11:10','11:15','11:20','11:25','11:30',
           '13:05','13:10','13:15','13:20','13:25','13:30','13:35','13:40','13:45','13:50','13:55','14:00',
           '14:05','14:10','14:15','14:20','14:25','14:30','14:35','14:40','14:45','14:50','14:55','15:00']

cache = {}
def rows_for(code):
    if code not in cache:
        try:
            text = z.read(code + '.csv').decode('utf-8-sig', errors='replace')
        except KeyError:
            cache[code] = None
        else:
            by_date = {}
            for line in io.StringIO(text):
                p = line.rstrip('\n').split(',')
                if len(p) < 9 or not p[0][:10].startswith('20'):
                    continue
                by_date.setdefault(p[0][:10], []).append(p)
            cache[code] = by_date
    return cache[code]

result = {}
stat = Counter()
for it in targets:
    d = it['trade_date'][:10]
    code = it['code']  # e.g. 000010.SH
    num, mkt = code.split('.')
    key = (mkt + num).lower()
    rs = rows_for(key)
    if rs is None:
        stat['csv_missing'] += 1; result[it['key']] = 'csv_missing'; continue
    rows = rs.get(d, [])
    times = [r[0][11:16] for r in rows]
    if len(rows) == 48 and set(times) == set(BUCKETS):
        ok = all(float(r[3]) > 0 and float(r[4]) > 0 and float(r[5]) > 0 and float(r[6]) > 0 for r in rows)
        amt_ok = all(float(r[8]) >= 0 for r in rows)
        stat['complete_48_valid' if (ok and amt_ok) else 'complete_48_price_fail'] += 1
        result[it['key']] = 'complete_48_valid' if (ok and amt_ok) else 'complete_48_price_fail'
    else:
        have = set(times) & set(BUCKETS)
        stat[f'incomplete_{len(rows)}'] += 1
        result[it['key']] = f'incomplete_{len(rows)}'

print(stat)
json.dump(result, open(r'F:\AiStock\app\scripts\research\yearly5m_check_2005.json', 'w'), indent=0)

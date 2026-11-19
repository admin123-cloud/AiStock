# 统计年度包(2000_2005_5min.zip)中每个队列代码日的"真实有效"5m根数与占位行
import json, zipfile, io
from collections import Counter

ZP = r'E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按年汇总\2000_2005_5min.zip'
items = json.load(open(r'F:\AiStock\app\scripts\research\queue_items_20260918.json', encoding='utf-8'))
targets = [i for i in items if i['trade_date'][:4] <= '2005']
z = zipfile.ZipFile(ZP)
names = set(z.namelist())
cache = {}

def rows_for(key):
    if key not in cache:
        try:
            text = z.read(key + '.csv').decode('utf-8-sig', errors='replace')
        except KeyError:
            cache[key] = None
        else:
            by = {}
            for line in io.StringIO(text):
                p = line.rstrip('\n').split(',')
                if len(p) >= 9 and p[0][:2] == '20':
                    by.setdefault(p[0][:10], []).append(p)
            cache[key] = by
    return cache[key]

def is_real(p):
    # 真实K线:OHLC全为正(厂商占位行 close/low=0)
    try:
        o, c, h, l = float(p[3]), float(p[4]), float(p[5]), float(p[6])
        v = float(p[7])
    except ValueError:
        return False
    return o > 0 and c > 0 and h > 0 and l > 0 and (v >= 0)

out = {}
stat = Counter()
for it in targets:
    d = it['trade_date'][:10]
    num, mkt = it['code'].split('.')
    key = (mkt + num).lower()
    rs = rows_for(key)
    if rs is None:
        stat['csv_missing'] += 1; out[it['key']] = {'csv': False}; continue
    rows = rs.get(d, [])
    real = [p for p in rows if is_real(p)]
    pad = len(rows) - len(real)
    stat[f'real_{len(real)}'] += 1
    out[it['key']] = {'csv': True, 'rows': len(rows), 'real': len(real), 'pad': pad,
                      'queue_missing': it['count'], 'issue': it['issue_type']}
json.dump(out, open(r'F:\AiStock\app\scripts\research\yearly5m_real_counts.json', 'w'), indent=1)
print(stat.most_common(30))
# 重点:no_datetime 的3条 + 完全没csv的
for k, v in out.items():
    if not v.get('csv') or v.get('issue') == 'source_5m_file_invalid_no_datetime':
        code = next(i['code'] + ' ' + i['trade_date'][:10] for i in targets if i['key'] == k)
        print(code, v)

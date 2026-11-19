# wstock CSV vs 购买包5m月度包 交叉验证,生成 636 队列项的可恢复清单
# 前提: E:\AStockHistory\wstock\输出\来源归档\out_历史混合\wstock\{SH,SZ}\*.csv 已导出完成
import json, os, glob, zipfile, io
from collections import Counter

OUT = r'E:\AStockHistory\wstock\输出\来源归档\out_历史混合\wstock'
BUCKETS = ['09:35','09:40','09:45','09:50','09:55','10:00','10:05','10:10','10:15','10:20','10:25','10:30',
           '10:35','10:40','10:45','10:50','10:55','11:00','11:05','11:10','11:15','11:20','11:25','11:30',
           '13:05','13:10','13:15','13:20','13:25','13:30','13:35','13:40','13:45','13:50','13:55','14:00',
           '14:05','14:10','14:15','14:20','14:25','14:30','14:35','14:40','14:45','14:50','14:55','15:00']

items = json.load(open(r'F:\AiStock\app\scripts\research\queue_items_20260918.json', encoding='utf-8'))

def wstock_path(code):
    num, mkt = code.split('.')
    p = os.path.join(OUT, mkt, mkt + num + '.csv')
    return p if os.path.exists(p) else None

def load_wstock_date(fp, d):
    by = {}
    for line in open(fp, encoding='gbk', errors='replace'):
        if line.startswith(d):
            p = line.rstrip('\n').split(',')
            if len(p) >= 7:
                by[p[0][11:16]] = p  # time -> row
    return by

def is_real(p):
    # Date,Open,Low,High,Close,Volume,Amount
    try:
        o, l, h, c = float(p[1]), float(p[2]), float(p[3]), float(p[4])
        v = float(p[5])
        return o > 0 and l > 0 and h > 0 and c > 0
    except (ValueError, IndexError):
        return False

manifest = []
stat = Counter()
for it in items:
    code = it['code']
    d = it['trade_date'][:10]
    fp = wstock_path(code)
    rec = {'key': it['key'], 'code': code, 'date': d, 'queue_missing': it['count']}
    if not fp:
        stat['no_wstock_csv'] += 1
        rec['verdict'] = 'no_wstock_csv'
        manifest.append(rec)
        continue
    rows = load_wstock_date(fp, d)
    real_times = [t for t in BUCKETS if t in rows and is_real(rows[t])]
    bad_times = [t for t in rows if t in BUCKETS and not is_real(rows[t])]
    rec['wstock_rows'] = len(rows)
    rec['wstock_real'] = len(real_times)
    rec['wstock_bad'] = len(bad_times)
    # 与购买包年包对比的占位桶(来自之前核查)判断 wstock 是否补上
    if len(real_times) == 48 and not bad_times:
        stat['full_48_real'] += 1
        rec['verdict'] = 'full_48_real'
    elif real_times:
        stat['partial_real'] += 1
        rec['verdict'] = 'partial_real'
    else:
        stat['no_real_bars'] += 1
        rec['verdict'] = 'no_real_bars'
    manifest.append(rec)

json.dump(manifest, open(r'F:\AiStock\app\scripts\research\wstock_restore_manifest.json', 'w'), indent=1)
print(stat)
print('saved wstock_restore_manifest.json')

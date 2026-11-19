# 质量门禁:wstock vs 购买包月度zip 在重叠桶上的价格一致性 + 缺失桶互补性
import json, os, zipfile, io, glob
from collections import Counter

items = json.load(open(r'F:\AiStock\app\scripts\research\queue_items_20260918.json', encoding='utf-8'))
man = {r['key']: r for r in json.load(open(r'F:\AiStock\app\scripts\research\wstock_restore_manifest.json', encoding='utf-8'))}
targets = [i for i in items if man[i['key']]['verdict'] == 'full_48_real']
print('targets:', len(targets))

MONTH_DIR = r'E:\AStockHistory\A数据1\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按月归档'
MONTH_DIR = r'E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按月归档'

def month_zip(code, d):
    ym = d[:7]
    zpath = os.path.join(MONTH_DIR, ym, d[:4] + d[5:7] + d[8:10] + '_5min.zip')
    if not os.path.exists(zpath):
        return None
    z = zipfile.ZipFile(zpath)
    num, mkt = code.split('.')
    key = (mkt + num).lower() + '.csv'
    if key not in z.namelist():
        # 尝试其它命名
        hits = [n for n in z.namelist() if num in n.lower()]
        if not hits:
            return None
        key = hits[0]
    return z.read(key).decode('utf-8-sig', errors='replace')

def parse_purchased(text, d):
    rows = {}
    for line in io.StringIO(text):
        p = line.rstrip('\n').split(',')
        if len(p) >= 9:
            key = p[0].replace('/', '-')
            if key[:10] == d:
                # O,C,H,L,V,Amt
                rows[key[11:16]] = [float(x) for x in p[3:9]]
    return rows

report = []
stat = Counter()
for it in targets:
    code, d = it['code'], it['trade_date'][:10]
    num, mkt = code.split('.')
    wfp = os.path.join(r'E:\AStockHistory\wstock\输出\来源归档\out_历史混合\wstock', mkt, mkt + num + '.csv')
    wrows = {}
    for line in open(wfp, encoding='gbk', errors='replace'):
        if line.startswith(d):
            p = line.rstrip('\n').split(',')
            wrows[p[0][11:16]] = [float(x) for x in p[1:7]]  # O,L,H,C,V,Amt
    ptext = month_zip(code, d)
    rec = {'code': code, 'date': d, 'queue_missing': it['count']}
    if not ptext:
        stat['no_purchased_zip'] += 1
        rec['overlap_check'] = 'no_purchased_zip'
        report.append(rec)
        continue
    prows = parse_purchased(ptext, d)
    diffs = []
    overlap = 0
    for t, wr in wrows.items():
        pr = prows.get(t)
        if not pr or pr[3] == 0 or pr[1] == 0:  # 购买包该桶缺失/占位
            continue
        overlap += 1
        # purchased O,C,H,L vs wstock O,L,H,C
        diffs.append(max(abs(wr[0] - pr[0]), abs(wr[1] - pr[3]), abs(wr[2] - pr[2]), abs(wr[3] - pr[1])))
    mx = max(diffs) if diffs else None
    avg = sum(diffs) / len(diffs) if diffs else None
    rec['overlap'] = overlap
    rec['max_diff'] = round(mx, 4) if mx is not None else None
    rec['avg_diff'] = round(avg, 4) if avg is not None else None
    if mx is None:
        stat['no_overlap'] += 1
    elif mx <= 0.011:
        stat['match'] += 1
        rec['verdict'] = 'match'
    else:
        stat['mismatch'] += 1
        rec['verdict'] = f'mismatch_max_{mx:.3f}'
    report.append(rec)

json.dump(report, open(r'F:\AiStock\app\scripts\research\wstock_quality_report.json', 'w'), indent=1)
print(stat)

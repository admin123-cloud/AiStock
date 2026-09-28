# 批量 mark_reviewed:636 条 2010 年前 5m 不可恢复缺失(用户 2026-09-18 批准)
# 只登记审核结论,不写行情表、不删除队列记录。
import json, hashlib, urllib.request, time, sys

BASE = 'http://127.0.0.1:3000/api/operations'
REASON = ('确认不可恢复,保留缺失:购买5m月度/年度包为同源批次,缺失桶在2000_2005年包中为厂商0价零量占位行'
          '(如sz000001 2003-07-03恰好18根占位=队列缺失18根);1m同桶缺失(20260918 v13交叉审计);'
          '15/30/60m不可反推5m;复盘大师1m仅2000年;免费外部源(baostock/tushare/akshare/腾讯/通达信)均无2010年前分钟线。'
          '不补造任何行情数据,回测侧按data-wall计划排除。')

items = json.load(open(r'F:\AiStock\app\scripts\research\queue_items_20260918.json', encoding='utf-8'))
print('items:', len(items))

def evid_hash(reason, refs):
    canonical = json.dumps({'reason': reason, 'evidence_refs': refs}, ensure_ascii=False,
                           sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()

ok, fail, replay = 0, 0, 0
fails = []
for i, it in enumerate(items, 1):
    refs = [e for e in it.get('evidence', []) if e] + [
        'research:scripts/research/check_yearly_5m_pack.py',
        'research:scripts/research/count_yearly_real_bars.py',
        'research:scripts/research/yearly5m_real_counts.json',
        'research:F:/AiDevelop/ai-workspace/projects/AiStock/changes/2026-09-18-free-5m-history-source-research.md',
    ]
    body = {
        'action': 'mark_reviewed',
        'review_key': it['key'],
        'idempotency_key': 'bulk-closeout-unrecoverable-5m-20260918-' + hashlib.sha256(it['key'].encode()).hexdigest()[:16],
        'reason': REASON,
        'evidence_refs': refs,
        'evidence_hash': evid_hash(REASON, refs),
        'confirm_token': 'auto',   # 服务端路由会对 mark_reviewed 自动重绑指纹/token
        'payload': {'batch': 'bulk-closeout-unrecoverable-5m-20260918'},
    }
    req = urllib.request.Request(BASE + '/data-review-actions',
                                 data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        r = json.load(urllib.request.urlopen(req, timeout=30))
        if r.get('idempotent_replay'):
            replay += 1
        else:
            ok += 1
    except Exception as e:
        fail += 1
        fails.append({'key': it['key'], 'err': str(e)[:200]})
    if i % 50 == 0:
        print(f'{i}/{len(items)} ok={ok} replay={replay} fail={fail}', flush=True)

print('DONE ok=', ok, 'replay=', replay, 'fail=', fail)
if fails:
    json.dump(fails, open(r'F:\AiStock\app\scripts\research\closeout_fails.json', 'w'), ensure_ascii=False, indent=1)
    for f in fails[:5]:
        print(f)

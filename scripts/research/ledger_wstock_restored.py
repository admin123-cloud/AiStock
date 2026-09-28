# 对 60 个已恢复代码日,在审核账本补记 mark_reviewed(已恢复) 事件
import json, hashlib, urllib.request

BASE = 'http://127.0.0.1:3000/api/operations'
REASON = ('结论更新:该代码日已通过 wstock.net wdz 独立源补全并写入正式表。'
          '数据经三方验证(与日线源首开末收/日量/日额吻合,不复权口径),'
          '与购买包重叠桶价格一致性通过(容忍 max(0.02,1.5%)),'
          '写入后逐键读回校验通过。来源类别 wstock_wdz_csv_sample,'
          'manifest 见 warehouse/wstock_restore_20260919/。')

staged = json.load(open(r'F:\AiStock\app\scripts\research\wstock_staged.json', encoding='utf-8'))
accepted = {(a['code'], a['trade_date']) for a in staged['accepted']}
items = json.load(open(r'F:\AiStock\app\scripts\research\queue_items_20260918.json', encoding='utf-8'))
targets = [i for i in items if (i['code'], i['trade_date'][:10]) in accepted]
print('targets:', len(targets))

def evid_hash(reason, refs):
    canonical = json.dumps({'reason': reason, 'evidence_refs': refs}, ensure_ascii=False,
                           sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(canonical).hexdigest()

ok = fail = 0
for it in targets:
    refs = ['research:F:/Stock/AiStockData/data/warehouse/wstock_restore_20260919/wstock_staged.json',
            'research:F:/Stock/AiStockData/data/warehouse/wstock_restore_20260919/wstock_applied.json',
            'research:E:/AStockHistory/wstock/输出/来源归档/out_历史混合/wstock/' + it['code'][-2:].lower() + it['code'][:6] + '.csv']
    body = {
        'action': 'mark_reviewed',
        'review_key': it['key'],
        'idempotency_key': 'wstock-restored-20260919-' + hashlib.sha256(it['key'].encode()).hexdigest()[:16],
        'reason': REASON,
        'evidence_refs': refs,
        'evidence_hash': evid_hash(REASON, refs),
        'confirm_token': 'auto',
        'payload': {'batch': 'wstock-restore-20260919'},
    }
    req = urllib.request.Request(BASE + '/data-review-actions', data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        urllib.request.urlopen(req, timeout=30)
        ok += 1
    except Exception as e:
        fail += 1
        print('FAIL', it['key'], str(e)[:120])
print('ok=', ok, 'fail=', fail)

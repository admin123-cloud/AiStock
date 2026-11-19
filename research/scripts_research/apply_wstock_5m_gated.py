"""Apply staged wstock 5m full-day restorations into stock.kline_minute_5 with readback verification.

前置: wstock_staged.json (state=staged, 60 code_days / 2880 rows, 全部门禁通过)
安全: 每键写入前再查一次 formal;写入后逐键读回校验;manifest 落盘 warehouse。
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client

FIELDS = ['code', 'datetime', 'open', 'high', 'low', 'close', 'volume', 'amount', 'created_at', 'id']

def sid(code, dt):
    return int.from_bytes(hashlib.blake2b(f'5m|{code}|{dt:%Y-%m-%d %H:%M:%S}'.encode(), digest_size=8).digest(), 'big')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--staged', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--yes', action='store_true')
    a = ap.parse_args()
    x = json.loads(a.staged.read_text(encoding='utf-8'))
    assert x.get('state') == 'staged', 'only staged payloads can be applied'
    c = clickhouse_client()
    written, skipped, readback_fail = 0, 0, []
    for raw in x.get('staged_rows', []):
        code, dt_s, o, h, l, cl, v, amt, created, sid_val = raw
        dt = datetime.fromisoformat(dt_s)
        exists = c.query('SELECT count() FROM kline_minute_5 WHERE code={code:String} AND datetime={dt:DateTime}', parameters={'code': code, 'dt': dt}, settings={'max_threads': 1}).result_rows[0][0]
        if exists:
            skipped += 1
            continue
        if not a.yes:
            written += 1  # dry-run counting only
            continue
        c.insert('stock.kline_minute_5', [(code, dt, o, h, l, cl, v, amt, datetime.now(), sid_val)], column_names=FIELDS)
        check = c.query('SELECT code,datetime,open,high,low,close,volume,amount FROM stock.kline_minute_5 FINAL WHERE code={code:String} AND datetime={dt:DateTime}', parameters={'code': code, 'dt': dt}, settings={'max_threads': 1}).result_rows
        if len(check) != 1:
            readback_fail.append({'code': code, 'datetime': dt_s})
        else:
            written += 1
    out = {'state': 'applied' if a.yes else 'dry_run', 'written': written, 'skipped_existing': skipped,
           'readback_fail': readback_fail, 'source': x.get('source'), 'code_days': x.get('accepted_code_days'),
           'applied_at': datetime.now().isoformat()}
    a.output.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(out, ensure_ascii=False))

if __name__ == '__main__':
    main()

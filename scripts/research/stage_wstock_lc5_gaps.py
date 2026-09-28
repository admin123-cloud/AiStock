"""Stage wstock TDX-export lc5 restorations for a given tail digit (read-only)."""
from __future__ import annotations
import argparse, hashlib, json, sys, zipfile, io
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from scripts.build_tdx_minute_periods import _find_files, _iter_lc5

VALID = {(9,m) for m in range(35,60,5)} | {(10,m) for m in range(0,60,5)} | {(11,m) for m in range(0,35,5)} | {(13,m) for m in range(5,60,5)} | {(14,m) for m in range(0,60,5)} | {(15,0)}
MONTH_DIR = Path(r'E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按月归档')

def sid(code, dt):
    return int.from_bytes(hashlib.blake2b(f'5m|{code}|{dt:%Y-%m-%d %H:%M:%S}'.encode(), digest_size=8).digest(), 'big')

def load_purchased(code, d):
    ym = d[:7]
    zpath = MONTH_DIR / ym / (d[:4] + d[5:7] + d[8:10] + '_5min.zip')
    if not zpath.exists():
        return None
    z = zipfile.ZipFile(zpath)
    num, mkt = code.split('.')
    key = (mkt + num).lower() + '.csv'
    if key not in z.namelist():
        return None
    rows = {}
    for line in io.StringIO(z.read(key).decode('utf-8-sig', errors='replace')):
        p = line.rstrip('\n').split(',')
        if len(p) >= 9:
            k = p[0].replace('/', '-')
            if k[:10] == d and k[11:16] in {f'{h:02d}:{m:02d}' for h, m in VALID}:
                try:
                    rows[k[11:16]] = [float(x) for x in p[3:9]]
                except ValueError:
                    pass
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tail', type=int, required=True)
    ap.add_argument('--queue', type=Path, required=True)
    ap.add_argument('--tdx-root', type=Path, default=Path(r'E:\AStockHistory\wstock\TDX旁证\tdx_out\vipdoc'))
    ap.add_argument('--output', type=Path, required=True)
    a = ap.parse_args()
    queue = json.loads(a.queue.read_text(encoding='utf-8'))
    tail = str(a.tail)
    items = [i for i in queue if i['code'].split('.')[0].endswith(tail)]
    print(f'tail {tail} targets:', len(items), flush=True)
    c = clickhouse_client()
    codes = {i['code'] for i in items}
    files = _find_files(a.tdx_root, {'sh', 'sz'}, codes, 0)
    file_by_code = {p.stem[2:] + '.' + ({'sh': 'SH', 'sz': 'SZ'}[p.name[:2].lower()]): p for p in files}
    print('lc5 files matched:', len(file_by_code), flush=True)
    accepted, blocked = [], []
    for it in items:
        code, day = it['code'], it['trade_date'][:10]
        path = file_by_code.get(code)
        if not path:
            blocked.append({'code': code, 'trade_date': day, 'reason': 'no_lc5'}); continue
        lo = datetime.fromisoformat(day); hi = lo.replace(hour=23, minute=59, second=59)
        src = {r.dt: (r.open, r.high, r.low, r.close, r.volume, r.amount) for r in _iter_lc5(path, lo, hi) if (r.dt.hour, r.dt.minute) in VALID and r.dt.date().isoformat() == day}
        if len(src) != 48:
            blocked.append({'code': code, 'trade_date': day, 'reason': f'wstock_not_48:{len(src)}'}); continue
        if any(min(r[0], r[1], r[2], r[3]) <= 0 or r[4] < 0 or r[5] < 0 for r in src.values()):
            blocked.append({'code': code, 'trade_date': day, 'reason': 'invalid_or_zero_wstock_values'}); continue
        rows = c.query('SELECT code,datetime,open,high,low,close,volume,amount FROM kline_minute_5 WHERE toDate(datetime)={day:Date} AND code={code:String} ORDER BY datetime', parameters={'day': day, 'code': code}, settings={'max_threads': 1}).result_rows
        formal_n = len(rows)
        prows = load_purchased(code, day) or {}
        diffs = []
        for t, wr in src.items():
            pr = prows.get(t)
            if not pr or pr[1] == 0 or pr[3] == 0:
                continue
            pc = abs(pr[1])
            df = max(abs(wr[0] - pr[0]), abs(wr[1] - pr[3]), abs(wr[2] - pr[2]), abs(wr[3] - pr[1]))
            diffs.append((df, pc))
        ok = sum(1 for df, pc in diffs if df <= max(.02, .015 * pc))
        ratio = (ok / len(diffs)) if diffs else None
        if ratio is not None and ratio < .7:
            blocked.append({'code': code, 'trade_date': day, 'reason': f'overlap_price_noise_high:{ratio:.3f}'}); continue
        missing = sorted(set(src) - {r[1].replace(tzinfo=None) for r in rows})
        accepted.append({'code': code, 'trade_date': day, 'formal_rows': formal_n, 'overlap': len(diffs),
                         'overlap_price_match_ratio': round(ratio, 4) if ratio is not None else None,
                         'missing': len(missing), 'rows': [(code, d, src[d]) for d in missing]})
    out_rows = []
    now = datetime.now()
    for item in accepted:
        for code, dt, r in item['rows']:
            out_rows.append((code, dt, r[0], r[1], r[2], r[3], r[4], r[5], now, sid(code, dt)))
    payload = {'source': 'wstock_wdz_tdx_lc5', 'state': 'staged', 'tail': tail,
               'code_days': len(items), 'accepted_code_days': len(accepted),
               'blocked_code_days': len(blocked), 'rows': len(out_rows),
               'staged_rows': [[r[0], r[1].isoformat(), r[2], r[3], r[4], r[5], r[6], r[7], r[8].isoformat(), r[9]] for r in out_rows],
               'accepted': [{k: v for k, v in i.items() if k != 'rows'} for i in accepted],
               'blocked': blocked}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'output': str(a.output), 'accepted_code_days': len(accepted), 'blocked_code_days': len(blocked), 'rows': len(out_rows)}, ensure_ascii=False))

if __name__ == '__main__':
    main()

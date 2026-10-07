"""Repair a frozen Big QMT export by stable keys, with backups and readback.

Requires native unadjusted daily truth and closed 48-slot five-minute days.
Never deletes rows, infers suspension, rescales units, or fills source gaps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.qmtmini_daily_backfill_validate import ch_client

TZ = ZoneInfo('Asia/Shanghai')
FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount']
MINUTE_COLUMNS = ['code', 'datetime', *FIELDS, 'created_at', 'id']
DAILY_COLUMNS = ['code', 'trade_date', *FIELDS, 'amplitude', 'change_pct',
                 'change_amount', 'turnover_rate', 'created_at', 'id']
SETTINGS = {'max_threads': 2, 'max_memory_usage': 2_000_000_000}
SLOTS = [f'{h:02d}{m:02d}00' for start in (570, 780)
         for minute in range(start + 5, start + 121, 5) for h, m in [divmod(minute, 60)]]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, body):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def same(left, right):
    return all(a is not None and b is not None and
               math.isclose(float(a), float(b), rel_tol=1e-7, abs_tol=2e-5 if i < 4 else 1e-5)
               for i, (a, b) in enumerate(zip(left, right)))


def stable_id(period, code, key):
    return int.from_bytes(hashlib.blake2b(f'{period}|{code}|{key}'.encode(), digest_size=8).digest(), 'big')


def epoch(text):
    return int(datetime.strptime(str(text), '%Y%m%d%H%M%S').replace(tzinfo=TZ).timestamp())


def valid_values(values):
    o, h, l, c, v, a = map(float, values)
    return (all(math.isfinite(x) for x in (o, h, l, c, v, a)) and min(o, h, l, c) > 0
            and v >= 0 and a >= 0 and h + 1e-8 >= max(o, c, l) and l - 1e-8 <= min(o, c, h))


def validate_day(day, frame, daily, instrument_type):
    """No approximate slot closure, unit guessing, or daily synthesis."""
    if list(map(str, frame.index)) != [day + slot for slot in SLOTS]:
        return 'minute_slots_not_closed'
    values = frame[FIELDS].to_numpy(dtype=float)
    if not all(valid_values(row) for row in values) or not valid_values(daily[FIELDS]):
        return 'invalid_ohlc_or_quantity'
    if not all(math.isclose(float(v), round(float(v)), abs_tol=1e-5) for v in values[:, 4]):
        return 'native_minute_volume_not_integer_lots'
    if float(frame.low.min()) < float(daily.low) - 0.011 or float(frame.high.max()) > float(daily.high) + 0.011:
        return 'minute_outside_native_daily_range'
    if not math.isclose(float(frame.volume.sum()), float(daily.volume), rel_tol=0.005, abs_tol=1):
        return 'native_minute_daily_volume_mismatch'
    if not math.isclose(float(frame.amount.sum()), float(daily.amount), rel_tol=0.005, abs_tol=1):
        return 'native_minute_daily_amount_mismatch'
    return None


def validate_daily(daily, instrument_type):
    if int(daily.get('suspendFlag', 0)) == 1:
        return 'native_suspended'
    if not valid_values(daily[FIELDS]):
        return 'invalid_native_daily'
    if instrument_type == 'stock' and float(daily.volume) > 0:
        vwap = float(daily.amount) / (float(daily.volume) * 100)
        if not float(daily.low) - 0.011 <= vwap <= float(daily.high) + 0.011:
            return 'native_daily_lots_yuan_contract_mismatch'
    return None


def aggregate(frame, minutes):
    size = minutes // 5
    values = frame[FIELDS].to_numpy(dtype=float).reshape(48 // size, size, 6)
    result = np.column_stack((values[:, 0, 0], values[:, :, 1].max(axis=1),
                              values[:, :, 2].min(axis=1), values[:, -1, 3],
                              values[:, :, 4].sum(axis=1), values[:, :, 5].sum(axis=1)))
    return list(zip(frame.index[size-1::size].map(str), result.tolist()))


def load_raw(root, code, period, manifest):
    key = f'{code}_{period}'
    info = manifest['files'][key]
    path = Path(info.get('file', root / 'native_raw' / f'{key}.parquet'))
    if not path.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('Source file outside frozen artifact root: ' + key)
    if sha(path) != info['sha256']:
        raise RuntimeError('Frozen source hash mismatch: ' + key)
    frame = pd.read_parquet(path)
    if len(frame) != info['rows'] or frame.index.duplicated().any():
        raise RuntimeError('Frozen source row identity mismatch: ' + key)
    frame.index = frame.index.map(str)
    return frame.sort_index()


def fetch_existing(ch, table, codes, daily=False):
    key = 'trade_date' if daily else 'datetime'
    columns = DAILY_COLUMNS if daily else MINUTE_COLUMNS
    select = ','.join(columns)
    if not daily:
        # Never shadow the storage column: expanding an alias in WHERE would
        # hide datetime from monthly partition pruning and scan all history.
        select = select.replace('datetime', 'toUnixTimestamp(datetime) AS key_epoch')
    sql = f'SELECT {select} FROM {table} FINAL WHERE code IN %(codes)s AND '
    sql += ("trade_date BETWEEN '2026-08-01' AND '2026-09-30'" if daily else
            "datetime >= toDateTime('2026-07-31 16:00:00','UTC') AND datetime < toDateTime('2026-09-30 16:00:00','UTC')")
    rows = ch.query(sql, parameters={'codes': codes}, settings=SETTINGS).result_rows
    result = {(row[0], row[1]): row for row in rows}
    if len(result) != len(rows):
        raise RuntimeError('Formal FINAL duplicate keys: ' + table)
    return result


def repair_table(ch, table, candidates, codes, output, label, apply, daily=False):
    columns = DAILY_COLUMNS if daily else MINUTE_COLUMNS
    existing = fetch_existing(ch, table, codes, daily)
    missing, corrected, unchanged = [], [], 0
    for row in candidates:
        key = (row[0], row[1] if daily else int(row[1].timestamp()))
        old = existing.get(key)
        if old is None:
            missing.append(row)
        elif not same(row[2:8], old[2:8]):
            if daily:
                row = list(row)
                # Historical float-share counts are unavailable. Preserve existing
                # turnover instead of estimating from today's instrument details.
                row[11] = old[11]
                row = tuple(row)
            corrected.append((key, old, row))
        else:
            unchanged += 1
    additions = missing + [item[2] for item in corrected]
    report = {'table': table, 'missing': len(missing), 'corrections': len(corrected),
              'unchanged': unchanged, 'applied': False, 'readback_verified': False}
    if not apply or not additions:
        return report
    if shutil.disk_usage(output).free < 2_000_000_000:
        raise RuntimeError('Disk reserve below 2 GB; stop before writes')
    backup = output / f'{label}_{table}_before.parquet'
    payload = output / f'{label}_{table}_append.parquet'
    pd.DataFrame([item[1] for item in corrected], columns=columns).to_parquet(backup)
    pd.DataFrame(additions, columns=columns).to_parquet(payload)
    report.update({'backup': str(backup), 'backup_sha256': sha(backup),
                   'payload': str(payload), 'payload_sha256': sha(payload)})
    # Persist recovery information before INSERT; a client timeout is ambiguous.
    journal = output / f'{label}_{table}_journal.json'
    dump(journal, report)
    ch.insert(table, additions, column_names=columns)
    report['applied'] = True
    readback = fetch_existing(ch, table, codes, daily)
    for row in additions:
        key = (row[0], row[1] if daily else int(row[1].timestamp()))
        got = readback.get(key)
        if got is None or not same(row[2:8], got[2:8]) or int(got[-1]) != int(row[-1]):
            dump(journal, report)
            raise RuntimeError('Stable key readback failed: ' + str(key))
    report['readback_verified'] = True
    dump(journal, report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--codes', default='')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--manifest', default='native_cache_manifest.json')
    parser.add_argument('--batch-size', type=int, default=64)
    args = parser.parse_args()
    root = args.root
    manifest = json.loads((root / args.manifest).read_text(encoding='utf-8'))
    scope = json.loads((root / 'scope_audit.json').read_text(encoding='utf-8'))
    if manifest['scope_sha256'] != sha(root / 'scope_audit.json') or not manifest['complete']:
        raise RuntimeError('Source manifest incomplete or scope changed')
    if manifest['source'] != 'qmt_builtin_contextinfo' or manifest['dividend_type'] != 'none' or manifest['fill_data']:
        raise RuntimeError('Unexpected source or price contract')
    metadata = {r[0]: r for r in scope['stock_metadata']}
    native_meta = json.loads((root / 'native_scope_metadata.json').read_text(encoding='utf-8'))
    dates = native_meta['calendar']
    if dates != sorted(set(dates)) or not dates or any(not '20260801' <= day <= '20260930' for day in dates):
        raise RuntimeError('Calendar outside the fixed August/September 2026 repair window')
    codes = args.codes.split(',') if args.codes else scope['codes']
    if not set(codes) <= set(scope['codes']):
        raise RuntimeError('Code outside frozen scope')
    if manifest.get('batch_codes') is not None and set(codes) != set(manifest['batch_codes']):
        raise RuntimeError('Batch manifest scope differs from requested codes')
    if not 1 <= args.batch_size <= 128:
        raise ValueError('Batch size must be between 1 and 128')
    out = root / ('apply_' if args.apply else 'audit_') / datetime.now(TZ).strftime('%Y%m%d_%H%M%S')
    out.mkdir(parents=True)
    summary = {'source_manifest': str(root / args.manifest), 'manifest_sha256': sha(root / args.manifest),
               'script_sha256': sha(Path(__file__)), 'apply_requested': args.apply,
               'codes': len(codes), 'complete': False, 'tables': {}, 'issues': {}, 'batches': []}
    ch = ch_client()
    issues = Counter()
    now = datetime.now(TZ).replace(microsecond=0)
    with (out / 'code_day_issues.jsonl').open('w', encoding='utf-8') as ledger:
        for offset in range(0, len(codes), args.batch_size):
            batch = codes[offset:offset+args.batch_size]
            targets = {period: [] for period in ('1d', 5, 15, 30, 60)}
            for code in batch:
                daily = load_raw(root, code, '1d', manifest)
                minute = load_raw(root, code, '5m', manifest)
                minute_days = {str(day):frame for day,frame in minute.groupby(minute.index.str[:8], sort=False)}
                foreign_days = {str(i)[:8] for i in minute.index} - set(dates)
                if foreign_days:
                    issues['native_minute_outside_calendar'] += len(foreign_days)
                    ledger.write(json.dumps({'code':code, 'days':sorted(foreign_days),
                                            'issue':'native_minute_outside_calendar'})+'\n')
                kind = 'index' if code in scope['registered_equity_indices'] else 'stock'
                record = metadata.get(code)
                list_date = str(record[3]).replace('-', '') if record and record[3] else None
                detail = native_meta['details'].get(code, {})
                if detail.get('OpenDate') and str(detail['OpenDate']) > '19900101':
                    list_date = str(detail['OpenDate'])
                delist_date = str(record[4]).replace('-', '') if record and record[4] else None
                for day in dates:
                    if list_date and day < list_date or delist_date and day > delist_date:
                        continue
                    d = daily.loc[day] if day in daily.index else None
                    m = minute_days.get(day, minute.iloc[:0])
                    issue = 'native_daily_missing' if d is None else validate_daily(d, kind)
                    if issue:
                        issues[issue] += 1
                        ledger.write(json.dumps({'code': code, 'day': day, 'issue': issue,
                                                'native_minute_rows': len(m)}, ensure_ascii=False) + '\n')
                        continue
                    key_date = datetime.strptime(day, '%Y%m%d').date()
                    pre = float(d.get('preClose', 0))
                    previous = daily.loc[daily.index < day]
                    if pre <= 0 and len(previous):
                        pre = float(previous.close.iloc[-1])
                    amp = (float(d.high)-float(d.low))/pre*100 if pre > 0 else 0.0
                    change = float(d.close)-pre if pre > 0 else 0.0
                    pct = change/pre*100 if pre > 0 else 0.0
                    targets['1d'].append((code, key_date, *map(float, d[FIELDS]), amp, pct,
                                          change, 0.0, now, stable_id('1d', code, day)))
                    issue = validate_day(day, m, d, kind)
                    if issue:
                        issues[issue] += 1
                        ledger.write(json.dumps({'code': code, 'day': day, 'issue': issue,
                                                'native_minute_rows': len(m)}, ensure_ascii=False) + '\n')
                        continue
                    for period in (5, 15, 30, 60):
                        for timestamp, values in aggregate(m, period):
                            dt = datetime.fromtimestamp(epoch(timestamp), TZ)
                            values[4] = int(round(values[4])) if period == 5 else values[4]
                            targets[period].append((code, dt, *values, now, stable_id(period, code, timestamp)))
            results = []
            for period, rows in targets.items():
                table = 'kline_daily' if period == '1d' else f'kline_minute_{period}'
                result = repair_table(ch, table, rows, batch, out, f'{offset:05d}', args.apply, period == '1d')
                results.append(result)
                totals = summary['tables'].setdefault(table, Counter())
                for name in ('missing', 'corrections', 'unchanged'):
                    totals[name] += result[name]
            summary['batches'].append({'offset': offset, 'codes': batch, 'results': results})
            summary['issues'] = dict(issues)
            dump(out / 'summary.json', summary)
            print(json.dumps({'codes_done': min(offset+len(batch),len(codes)), 'total':len(codes),
                              'tables':summary['tables'], 'issues':dict(issues)}, ensure_ascii=False), flush=True)
    summary['complete'] = True
    dump(out / 'summary.json', summary)
    print('REPORT=' + str(out / 'summary.json'), flush=True)


if __name__ == '__main__':
    main()

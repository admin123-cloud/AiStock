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
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.qmtmini_daily_backfill_validate import ch_client
from utils.qmt_universe import (is_market_statistics_code, MARKET_STATISTICS_EVIDENCE_URL,
                                is_bond_index, BOND_INDEX_EVIDENCE_URL, CROSS_BORDER_EQUITY_INDEX_CODES)
from utils.qmt_daily_prices import daily_price_metrics

TZ = ZoneInfo('Asia/Shanghai')
FIELDS = ['open', 'high', 'low', 'close', 'volume', 'amount']
MINUTE_COLUMNS = ['code', 'datetime', *FIELDS, 'created_at', 'id']
DAILY_COLUMNS = ['code', 'trade_date', *FIELDS, 'amplitude', 'change_pct',
                 'change_amount', 'turnover_rate', 'created_at', 'id']
SETTINGS = {'max_threads': 2, 'max_memory_usage': 2_000_000_000}
SLOTS = [f'{h:02d}{m:02d}00' for start in (570, 780)
         for minute in range(start + 5, start + 121, 5) for h, m in [divmod(minute, 60)]]
CROSS_BORDER_CODES = CROSS_BORDER_EQUITY_INDEX_CODES
CROSS_BORDER_SLOTS = [f'{h:02d}{m:02d}00' for start, end in ((570, 720), (780, 970))
                      for minute in range(start + 5, end + 1, 5) for h, m in [divmod(minute, 60)]]
# Observed native signed-int32 overflow; do not generalize to other securities.
UINT32_VOLUME_CODES = frozenset({'399379.SZ', '399380.SZ'})
BSE_TRADING_RULES_URL = 'https://www.bse.cn/jygl_list/200028217.html'


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


@lru_cache(maxsize=4096)
def epoch(text):
    return int(datetime.strptime(str(text), '%Y%m%d%H%M%S').replace(tzinfo=TZ).timestamp())


@lru_cache(maxsize=4096)
def market_datetime(text):
    return datetime.fromtimestamp(epoch(text), TZ)


def field_values(frame):
    return (frame if list(frame.columns) == FIELDS else frame[FIELDS]).to_numpy(dtype=float, copy=False)


def valid_values(values):
    o, h, l, c, v, a = map(float, values)
    return (all(math.isfinite(x) for x in (o, h, l, c, v, a)) and min(o, h, l, c) > 0
            and v >= 0 and a >= 0 and h + 1e-8 >= max(o, c, l) and l - 1e-8 <= min(o, c, h))


def validate_day(day, frame, daily, instrument_type, code=None):
    """No approximate slot closure, unit guessing, or daily synthesis."""
    slots = CROSS_BORDER_SLOTS if instrument_type == 'index' and code in CROSS_BORDER_CODES else SLOTS
    if list(map(str, frame.index)) != [day + slot for slot in slots]:
        return 'minute_slots_not_closed'
    values = field_values(frame)
    if (not np.isfinite(values).all() or values[:, :4].min() <= 0 or values[:, 4:].min() < 0
            or (values[:, 1]+1e-8 < values[:, [0, 2, 3]].max(axis=1)).any()
            or (values[:, 2]-1e-8 > values[:, [0, 1, 3]].min(axis=1)).any()
            or not valid_values(daily[FIELDS])):
        return 'invalid_ohlc_or_quantity'
    if not np.isclose(values[:, 4], np.rint(values[:, 4]), rtol=0, atol=1e-5).all():
        return 'native_minute_volume_not_integer_lots'
    prices = values
    if instrument_type == 'stock':
        active = (values[:, 4:] > 0).any(axis=1)
        inactive = values[~active, :4]
        # Native OHLC can move even when the supplied quantity fields are zero.
        # Preserve these quotes; never infer their missing intrabar trades.
        # Only a flat carry quote may lie outside today's transaction range.
        nonflat = inactive[~np.isclose(inactive, inactive[:, -1:], rtol=0, atol=1e-8).all(axis=1)]
        if len(nonflat) and (nonflat.min() < float(daily.low)-0.011 or nonflat.max() > float(daily.high)+0.011):
            return 'native_zero_quantity_quote_outside_daily_range'
        if active.any():
            prices = values[active]
    if prices[:, 2].min() < float(daily.low) - 0.011 or prices[:, 1].max() > float(daily.high) + 0.011:
        return 'minute_outside_native_daily_range'
    if not math.isclose(float(values[:, 4].sum()), float(daily.volume), rel_tol=0.005, abs_tol=1):
        return 'native_minute_daily_volume_mismatch'
    if not math.isclose(float(values[:, 5].sum()), float(daily.amount), rel_tol=0.005, abs_tol=1):
        return 'native_minute_daily_amount_mismatch'
    if instrument_type == 'stock' and len(nonflat) and float(values[:, 4].sum()) != float(daily.volume):
        return 'native_zero_quantity_quote_volume_not_exact'
    return None


def normalize_native_daily_volume(code, day, daily, minute, instrument_type):
    """Correct only a proved int32 wrap, preserving the original source file."""
    if daily is None or instrument_type != 'index' or code not in UINT32_VOLUME_CODES:
        return daily, None
    raw = float(daily.volume)
    if not math.isfinite(raw) or not -(2**31) <= raw < 0 or raw != round(raw):
        return daily, None
    candidate = daily.copy()
    candidate.volume = raw + 2**32
    if validate_daily(candidate, instrument_type) or validate_day(day, minute, candidate, instrument_type, code):
        return daily, None
    values = field_values(minute)
    if float(values[:, 4].sum()) != candidate.volume:
        return daily, None
    if not math.isclose(float(values[:, 5].sum()), float(candidate.amount), rel_tol=1e-7, abs_tol=1):
        return daily, None
    if abs(float(values[0, 0]) - float(candidate.open)) > 0.011 or abs(float(values[-1, 3]) - float(candidate.close)) > 0.011:
        return daily, None
    return candidate, {'code': code, 'day': day, 'rule': 'proved_signed_int32_volume_wrap',
                       'raw_volume': raw, 'normalized_volume': float(candidate.volume),
                       'minute_volume_sum': float(values[:, 4].sum()),
                       'minute_amount_sum': float(values[:, 5].sum()), 'daily_amount': float(candidate.amount)}


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


def validate_bse_total_quantity(day, minute, daily, code, list_date):
    """BSE total quantity includes block trades outside the auction price band.

    This changes no numeric field. Require a listed stock, legal 30% price
    limits, and complete native minute agreement; IPO days are excluded.
    """
    if (not code.endswith('.BJ') or not list_date or not str(list_date).isdigit()
            or len(str(list_date)) != 8 or day <= str(list_date)):
        return None
    pre = float(daily.get('preClose', 0) or 0)
    if not math.isfinite(pre) or pre <= 0 or validate_day(day, minute, daily, 'stock', code):
        return None
    lower, upper = pre * 0.7, pre * 1.3
    vwap = float(daily.amount)/(float(daily.volume)*100)
    if not (lower-.011 <= float(daily.low) <= float(daily.high) <= upper+.011
            and lower-.011 <= vwap <= upper+.011):
        return None
    return {'code':code,'day':day,'rule':'bse_block_trade_inclusive_total_quantity',
            'vwap':vwap,'auction_low':float(daily.low),'auction_high':float(daily.high),
            'price_limit_low':lower,'price_limit_high':upper,'evidence_url':BSE_TRADING_RULES_URL}


def verified_absence(metadata, code, day):
    for event in metadata.get('verified_events', []):
        if event['status'] not in {'suspended','pending_listing','not_trading','delisted'} or not event.get('evidence_url'):
            raise RuntimeError('Invalid business absence evidence')
        if event['code'] == code and event['start_date'].replace('-','') <= day <= event['end_date'].replace('-',''):
            return event
    return None


def validate_absence_source(daily, minute):
    """A notice never overrides actual daily or minute trading evidence."""
    quantities = []
    if daily is not None:
        quantities.extend([daily.volume, daily.amount])
    if len(minute):
        quantities.extend(minute[['volume', 'amount']].to_numpy().reshape(-1))
    values = np.asarray(quantities, dtype=float)
    if not np.isfinite(values).all() or (values < 0).any():
        return 'invalid_native_absence_source'
    if (values > 0).any():
        return 'source_conflicts_with_verified_absence'
    if daily is not None and int(daily.get('suspendFlag', 0)) != 1:
        return 'native_zero_row_without_suspension'
    return None


def aggregate(frame, minutes, instrument_type='stock'):
    if minutes not in (5, 15, 30, 60):
        raise ValueError('Unsupported aggregation period')
    size = minutes // 5
    keys = list(map(str, frame.index))
    slots = [key[8:] for key in keys]
    if not keys or len({key[:8] for key in keys}) != 1 or slots not in (SLOTS, CROSS_BORDER_SLOTS):
        raise ValueError('Aggregation requires an independently validated closed day')
    if minutes == 5:
        # Base bars are the exact native record, including zero-quantity OHLC.
        return list(zip(keys, field_values(frame).tolist()))
    if slots == CROSS_BORDER_SLOTS:
        # Session-local chunks retain genuine partial bars at 12:00 and 16:10.
        values = field_values(frame)
        rows = []
        for start, end in ((0, 30), (30, 68)):
            for offset in range(start, end, size):
                stop = min(offset + size, end)
                part = values[offset:stop]
                rows.append((keys[stop-1], aggregate_values(part[None, :, :], instrument_type)[0].tolist()))
        return rows
    values = field_values(frame).reshape(48 // size, size, 6)
    result = aggregate_values(values, instrument_type)
    return list(zip(frame.index[size-1::size].map(str), result.tolist()))


def aggregate_values(values, instrument_type):
    active = (values[:, :, 4:] > 0).any(axis=2) if instrument_type == 'stock' else np.ones(values.shape[:2], dtype=bool)
    empty = ~active.any(axis=1)
    first = active.argmax(axis=1)
    last = values.shape[1]-1-active[:, ::-1].argmax(axis=1)
    row = np.arange(len(values))
    result = np.column_stack((values[row, first, 0], np.where(active, values[:, :, 1], -np.inf).max(axis=1),
                              np.where(active, values[:, :, 2], np.inf).min(axis=1), values[row, last, 3],
                              values[:, :, 4].sum(axis=1), values[:, :, 5].sum(axis=1)))
    result[empty, :4] = values[empty, -1, 3, None]
    return result


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
    if not codes:
        return {}
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
    candidate_keys = {(row[0], row[1] if daily else int(row[1].timestamp())) for row in candidates}
    if len(candidate_keys) != len(candidates):
        raise RuntimeError('Duplicate native target keys: ' + table)
    canonical_only = sorted(set(existing) - candidate_keys)
    missing, corrected, unchanged = [], [], 0
    for row in candidates:
        key = (row[0], row[1] if daily else int(row[1].timestamp()))
        old = existing.get(key)
        if old is None:
            missing.append(row)
        elif not same(row[2:11] if daily else row[2:8], old[2:11] if daily else old[2:8]):
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
              'unchanged': unchanged, 'canonical_only': len(canonical_only),
              'native_target_rows': len(candidates), 'formal_rows': len(existing),
              'applied': False, 'readback_verified': False}
    report['compared_fields'] = FIELDS + (['amplitude', 'change_pct', 'change_amount'] if daily else [])
    if canonical_only:
        # Preserve both directions of the reconciliation. These keys may be
        # outside lifecycle, suspended, or blocked by source validation; their
        # presence never authorizes removal or fabricated source coverage.
        extra = output / f'{label}_{table}_canonical_only.parquet'
        pd.DataFrame(canonical_only, columns=['code', 'trade_date' if daily else 'key_epoch']).to_parquet(extra)
        report.update(canonical_only_file=str(extra), canonical_only_sha256=sha(extra))
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
        if got is None or not same(row[2:11] if daily else row[2:8], got[2:11] if daily else got[2:8]) or int(got[-1]) != int(row[-1]):
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
    manifest_digest = sha(root / args.manifest)
    scope = json.loads((root / 'scope_audit.json').read_text(encoding='utf-8'))
    if manifest['scope_sha256'] != sha(root / 'scope_audit.json') or not manifest['complete']:
        raise RuntimeError('Source manifest incomplete or scope changed')
    if manifest['source'] != 'qmt_builtin_contextinfo' or manifest['dividend_type'] != 'none' or manifest['fill_data']:
        raise RuntimeError('Unexpected source or price contract')
    # An independently downloaded, immutable minute export may supersede the
    # initial cache. Its completeness describes export closure, not coverage.
    overlay_path = root / 'native_minute_refreshed_manifest.json'
    overlay_digest = None
    if overlay_path.exists():
        overlay_bytes = overlay_path.read_bytes()
        overlay = json.loads(overlay_bytes.decode('utf-8'))
        if overlay.get('complete'):
            if (overlay['scope_sha256'] != manifest['scope_sha256']
                    or overlay['source'] != 'qmt_builtin_contextinfo'
                    or overlay['dividend_type'] != 'none' or overlay['fill_data']
                    or any(not key.endswith('_5m') for key in overlay['files'])):
                raise RuntimeError('Minute overlay source/scope contract mismatch')
            manifest['files'].update({key:value for key,value in overlay['files'].items() if key in manifest['files']})
            overlay_digest = hashlib.sha256(overlay_bytes).hexdigest()
    metadata = {r[0]: r for r in scope['stock_metadata']}
    metadata_path = root / 'native_scope_metadata.json'
    metadata_bytes = metadata_path.read_bytes()
    native_meta = json.loads(metadata_bytes.decode('utf-8'))
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
    summary = {'source_manifest': str(root / args.manifest), 'manifest_sha256': manifest_digest,
               'script_sha256': sha(Path(__file__)), 'apply_requested': args.apply,
               'dependency_sha256': {name:sha(Path(__file__).resolve().parents[1]/name)
                                      for name in ('utils/qmt_daily_prices.py','utils/qmt_universe.py')},
               'metadata_file': str(metadata_path), 'metadata_sha256': hashlib.sha256(metadata_bytes).hexdigest(),
               'minute_overlay': str(overlay_path) if overlay_digest else None, 'minute_overlay_sha256': overlay_digest,
               'codes': len(codes), 'complete': False, 'tables': {}, 'issues': {}, 'batches': []}
    ch = ch_client()
    issues = Counter()
    business_exclusions = Counter()
    non_price_codes = []
    bond_index_codes = []
    normalizations = []
    quantity_contracts = []
    zero_quantity_quotes = []
    now = datetime.now(TZ).replace(microsecond=0)
    with (out / 'code_day_issues.jsonl').open('w', encoding='utf-8') as ledger:
        for offset in range(0, len(codes), args.batch_size):
            batch = codes[offset:offset+args.batch_size]
            targets = {period: [] for period in ('1d', 5, 15, 30, 60)}
            for code in batch:
                if is_market_statistics_code(code):
                    non_price_codes.append(code)
                    ledger.write(json.dumps({'code':code,'issue':'verified_non_price_scope',
                                             'evidence_url':MARKET_STATISTICS_EVIDENCE_URL})+'\n')
                    continue
                record = metadata.get(code)
                if code in scope['registered_equity_indices'] and is_bond_index(code, record[1] if record else ''):
                    bond_index_codes.append(code)
                    ledger.write(json.dumps({'code':code,'issue':'verified_bond_index_scope',
                                             'evidence_url':BOND_INDEX_EVIDENCE_URL})+'\n')
                    continue
                daily = load_raw(root, code, '1d', manifest)
                metrics = daily_price_metrics(daily)
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
                override = native_meta.get('lifecycle_overrides', {}).get(code, {})
                if 'delist_date' in override:
                    delist_date = str(override['delist_date']).replace('-','') if override['delist_date'] else None
                for day in dates:
                    if list_date and day < list_date or delist_date and day > delist_date:
                        continue
                    d = daily.loc[day] if day in daily.index else None
                    m = minute_days.get(day, minute.iloc[:0])
                    event = verified_absence(native_meta, code, day)
                    if event:
                        absence_issue = validate_absence_source(d, m)
                        if absence_issue:
                            issues[absence_issue] += 1
                            kind_issue=absence_issue
                        else:
                            business_exclusions[event['status']] += 1
                            kind_issue='verified_business_absence'
                        ledger.write(json.dumps({'code':code,'day':day,'issue':kind_issue,'evidence':event},ensure_ascii=False)+'\n')
                        continue
                    d, normalization = normalize_native_daily_volume(code, day, d, m, kind)
                    if normalization:
                        normalizations.append(normalization)
                    issue = 'native_daily_missing' if d is None else validate_daily(d, kind)
                    if issue == 'native_daily_lots_yuan_contract_mismatch' and kind == 'stock':
                        contract = validate_bse_total_quantity(day, m, d, code, list_date)
                        if contract:
                            quantity_contracts.append(contract)
                            issue = None
                    if issue:
                        issues[issue] += 1
                        ledger.write(json.dumps({'code': code, 'day': day, 'issue': issue,
                                                'native_minute_rows': len(m)}, ensure_ascii=False) + '\n')
                        continue
                    key_date = datetime.strptime(day, '%Y%m%d').date()
                    amp, pct, change = map(float, metrics.loc[day, ['amplitude','change_pct','change_amount']])
                    targets['1d'].append((code, key_date, *map(float, d[FIELDS]), amp, pct,
                                          change, 0.0, now, stable_id('1d', code, day)))
                    issue = validate_day(day, m, d, kind, code)
                    if issue:
                        issues[issue] += 1
                        ledger.write(json.dumps({'code': code, 'day': day, 'issue': issue,
                                                'native_minute_rows': len(m)}, ensure_ascii=False) + '\n')
                        continue
                    if kind == 'stock':
                        values = field_values(m)
                        zero = (values[:, 4:] == 0).all(axis=1)
                        nonflat = zero & ~np.isclose(values[:, :4], values[:, 3, None], rtol=0, atol=1e-8).all(axis=1)
                        if nonflat.any():
                            zero_quantity_quotes.append({'code':code,'day':day,
                                'rule':'native_zero_quantity_ohlc_preserved_with_daily_range_and_quantity_checks',
                                'quotes':m.loc[nonflat,FIELDS].reset_index().to_dict('records'),
                                'minute_source_sha256':manifest['files'][f'{code}_5m']['sha256'],
                                'daily_source_sha256':manifest['files'][f'{code}_1d']['sha256'],
                                'minute_volume':float(values[:,4].sum()),'daily_volume':float(d.volume),
                                'intrabar_transaction_quantity_verified':False})
                    for period in (5, 15, 30, 60):
                        for timestamp, values in aggregate(m, period, kind):
                            dt = market_datetime(timestamp)
                            values[4] = int(round(values[4])) if period == 5 else values[4]
                            targets[period].append((code, dt, *values, now, stable_id(period, code, timestamp)))
            results = []
            # Scope exclusions apply to both sides of the reconciliation.
            # Their preserved historical rows are outside this price contract.
            price_batch = [code for code in batch if code not in non_price_codes and code not in bond_index_codes]
            for period, rows in targets.items():
                table = 'kline_daily' if period == '1d' else f'kline_minute_{period}'
                result = repair_table(ch, table, rows, price_batch, out, f'{offset:05d}', args.apply, period == '1d')
                results.append(result)
                totals = summary['tables'].setdefault(table, Counter())
                for name in ('missing', 'corrections', 'unchanged', 'canonical_only', 'native_target_rows', 'formal_rows'):
                    totals[name] += result[name]
            summary['batches'].append({'offset': offset, 'codes': batch, 'results': results})
            summary['issues'] = dict(issues)
            summary['business_exclusions'] = dict(business_exclusions)
            summary['non_price_scope_exclusions'] = non_price_codes
            summary['bond_index_scope_exclusions'] = bond_index_codes
            summary['price_scope_codes'] = len(codes) - len(non_price_codes) - len(bond_index_codes)
            summary['native_daily_volume_normalizations'] = normalizations
            summary['bse_quantity_contracts'] = quantity_contracts
            summary['native_zero_quantity_quotes'] = zero_quantity_quotes
            dump(out / 'summary.json', summary)
            print(json.dumps({'codes_done': min(offset+len(batch),len(codes)), 'total':len(codes),
                              'tables':summary['tables'], 'issues':dict(issues)}, ensure_ascii=False), flush=True)
    summary['complete'] = True
    dump(out / 'summary.json', summary)
    print('REPORT=' + str(out / 'summary.json'), flush=True)


if __name__ == '__main__':
    main()

from __future__ import annotations

"""Build a bounded read-only Ocean scope baseline from existing evidence."""

import argparse
import gzip
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--ocean-root', type=Path, required=True)
    ap.add_argument('--daily-root', type=Path, required=True)
    ap.add_argument('--output-root', type=Path, required=True)
    ap.add_argument('--start-year', type=int, default=2021)
    ap.add_argument('--end-year', type=int, default=2026)
    args = ap.parse_args()
    started = time.monotonic()
    args.output_root.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    stocks = {}
    for row in client.query('SELECT code,type,market,list_date,delist_date,listing_status,quit FROM stocks').result_rows:
        code, typ, market, list_date, delist_date, listing_status, quit_flag = row
        if str(code) not in stocks:
            stocks[str(code)] = {'type': str(typ), 'market': str(market), 'list_date': list_date, 'delist_date': delist_date, 'listing_status': str(listing_status), 'quit': int(quit_flag or 0)}
    calendar = defaultdict(set)
    for market, day in client.query(
        'SELECT market,trade_date FROM trade_calendar WHERE is_trading=1 AND trade_date>=toDate(%(s)s) AND trade_date<=toDate(%(e)s)',
        parameters={'s': f'{args.start_year}-01-01', 'e': f'{args.end_year}-12-31'},
    ).result_rows:
        calendar[str(market)].add(day)

    daily_valid = set()
    daily_activity = set()
    daily_counts = Counter()
    daily_manifest = args.daily_root / '_manifest.json'
    for year in range(args.start_year, args.end_year + 1):
        path = args.daily_root / f'trade_year={year}' / 'part-00000.parquet'
        if not path.is_file():
            continue
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=100_000, columns=['code','trade_date','security_type','quality_status','open','high','low','close','volume_lots']):
            data = batch.to_pydict()
            for i, raw_code in enumerate(data['code']):
                code = str(raw_code)
                day = data['trade_date'][i]
                key = (code, day)
                positive = all(float(data[name][i] or 0) > 0 for name in ('open','high','low','close'))
                valid = str(data['security_type'][i]) == 'stock' and str(data['quality_status'][i]) == 'valid' and positive
                if valid:
                    daily_valid.add(key)
                    daily_counts[year] += 1
                    if float(data['volume_lots'][i] or 0) > 0:
                        daily_activity.add(key)

    source_keys = set()
    source_status = {}
    source_counts = Counter()
    source_years = Counter()
    for year in range(args.start_year, args.end_year + 1):
        stage_root = args.ocean_root / f'stage_{year}'
        for path in stage_root.rglob('day_audit.jsonl.gz'):
            with gzip.open(path, 'rt', encoding='utf-8') as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    key = (str(row.get('code') or ''), date.fromisoformat(str(row['trade_date'])))
                    source_keys.add(key)
                    status = str(row.get('status') or 'unknown')
                    source_status[key] = status
                    source_counts[status] += 1
                    source_years[year] += 1

    exclusion = Counter()
    eligible = set()
    identity = Counter()
    for key in daily_valid:
        code, day = key
        item = stocks.get(code)
        market = code[-2:]
        if item is None or item['type'] != 'stock':
            identity['historical_source_only_or_unknown'] += 1
            exclusion['identity_unknown'] += 1
            continue
        identity['master_confirmed'] += 1
        if day not in calendar.get(market, set()):
            exclusion['non_trading_day_or_calendar_mismatch'] += 1
            continue
        ld = item['list_date']
        dd = item['delist_date']
        if ld is not None and day < ld:
            exclusion['before_listing'] += 1
            continue
        if dd is not None and day > dd:
            exclusion['after_delisting'] += 1
            continue
        if item['listing_status'] == 'pending_listing':
            exclusion['pending_listing'] += 1
            continue
        eligible.add(key)

    source_pass = {key for key, status in source_status.items() if status == 'pass'}
    source_partial = {key for key, status in source_status.items() if status != 'pass'}
    result = {
        'schema': 'scope-baseline-v1',
        'run_id': f'scope_baseline_{time.strftime("%Y%m%dT%H%M%S")}',
        'scope_version': '2021-current-ocean-daily-activity-v1',
        'scope_start': f'{args.start_year}-01-01',
        'scope_end': f'{args.end_year}-12-31',
        'business_timezone': 'Asia/Shanghai',
        'formal_authorized': False,
        'write': False,
        'final_used': False,
        'formal_diff': {'status': 'unknown', 'reason': 'bounded formal key query not run in this baseline pass; full-table aggregate exceeded time budget', 'formal_missing_keys': None, 'formal_conflicts': None},
        'stocks': {'rows_loaded': len(stocks), 'identity_counts': dict(identity), 'master_query': 'SELECT code,type,market,list_date,delist_date,listing_status,quit FROM stocks'},
        'calendar': {'trading_day_counts': {market: len(days) for market, days in calendar.items()}, 'query_market_specific': True},
        'daily_snapshot': {'manifest': str(daily_manifest), 'manifest_sha256': sha256(daily_manifest) if daily_manifest.is_file() else None, 'valid_positive_code_days': len(daily_valid), 'activity_positive_volume_code_days': len(daily_activity), 'valid_by_year': dict(daily_counts)},
        'eligible_code_days': len(eligible),
        'eligible_with_source_pass': len(eligible & source_pass),
        'eligible_with_source_partial_or_isolated': len(eligible & source_partial),
        'missing_code_days_against_ocean_pass': len(eligible - source_pass),
        'exclusion_counts': dict(exclusion),
        'ocean': {'observed_code_days': len(source_keys), 'pass_code_days': len(source_pass), 'partial_or_isolated_code_days': len(source_partial), 'status_counts': dict(source_counts), 'audit_rows_by_year': dict(source_years), 'evidence_root': str(args.ocean_root)},
        'wstock': {'status': 'not_in_scope_for_2021_current', 'reason': 'existing WStock manifest evidence is concentrated in earlier years and partial accessible members only; no full-coverage claim'},
        'elapsed_seconds': round(time.monotonic() - started, 3),
        'notes': ['Eligible is based on positive valid daily snapshot, market calendar, current master identity and known lifecycle; daily activity/suspension is retained as a separate field.', 'Current master omission is not counted as a clean historical exclusion.', 'Formal missing keys remain unknown because no complete bounded formal snapshot was available.'],
    }
    payload = json.dumps(result, ensure_ascii=False, indent=2, default=str) + '\n'
    out = args.output_root / 'scope_baseline.json'
    out.write_text(payload, encoding='utf-8')
    result['evidence_sha256'] = {'scope_baseline.json': sha256(out)}
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

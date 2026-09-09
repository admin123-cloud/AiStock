"""Build verified monthly candidates; never rename or delete production tables."""
import argparse
import calendar
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.govern_kline_history import _derived_aggregate_sql
from services.operations.health import BUSINESS_TZ, write_snapshot
from services.operations.lifecycle import InstanceLock
from services.operations.qmt_download_queue import protected_session
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path

FIELDS = 'code, datetime, open, high, low, close, volume, amount'
SETTINGS = {'max_execution_time': 600, 'max_threads': 2,
            'max_memory_usage': 4000000000, 'max_bytes_before_external_group_by': 1000000000}


def bounds(month, cutoff):
    first = datetime.strptime(str(month), '%Y%m').date().replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, min(last, date.fromisoformat(cutoff))


def aggregate(period, month, cutoff):
    first, last = bounds(month, cutoff)
    sql = _derived_aggregate_sql(f'{period}m', first, last, True)
    # Stable chronological summation avoids nondeterministic Float64 merge order.
    sql = sql.replace('sum(amount) AS amount',
                      'if(count(amount) = 0, NULL, arraySum(arrayMap(x -> ifNull(x.2, 0), '
                      'arraySort(x -> x.1, groupArray(tuple(source_datetime, amount)))))) AS amount')
    # Explicit partition predicate is necessary with Nullable(DateTime) on this deployment.
    return sql.replace('WHERE ', f'WHERE toYYYYMM(datetime) = {int(month)} AND ', 1)


def source_sql(period, month, cutoff):
    return (f'SELECT {FIELDS} FROM ({aggregate(period, month, cutoff)}) '
            f'WHERE source_bars = {period // 5}')


def digest(client, sql):
    # Count and two independent reductions detect content changes, not just row presence.
    row = client.query(f'SELECT count(), uniqExact(tuple(code, datetime)), '
                       f'toString(sumWithOverflow(cityHash64(tuple({FIELDS})))), '
                       f'toString(groupBitXor(cityHash64(tuple({FIELDS})))) '
                       f'FROM ({sql})', settings=SETTINGS).result_rows[0]
    return list(row)


def table_name(period, run_id):
    if not re.fullmatch(r'[a-z0-9_]{1,48}', run_id):
        raise ValueError('run-id must contain only lowercase ASCII letters, digits, underscores')
    return f'kline_minute_{period}_recovery_{run_id}'


def build_month(client, state, period, month, save):
    key = f'{period}:{month}'
    job = state['jobs'].get(key, {})
    table = table_name(period, state['run_id'])
    target = f'SELECT {FIELDS} FROM {table} WHERE toYYYYMM(datetime) = {int(month)}'
    if job.get('state') == 'verified':
        if digest(client, target) != job['actual']:
            raise RuntimeError(f'{key}: verified candidate changed; rebuild with a new run-id')
        return
    if job:
        raise RuntimeError(f'{key}: uncertain previous attempt; inspect before using a new run-id')
    if client.query(f'SELECT count() FROM ({target})', settings=SETTINGS).result_rows[0][0]:
        raise RuntimeError(f'{key}: candidate partition is not empty')
    job = {'state': 'checking_source'}
    state['jobs'][key] = job
    save()
    try:
        sql = source_sql(period, month, state['cutoff'])
        job['expected'] = digest(client, sql)
        job['incomplete_buckets'] = client.query(
            f'SELECT count() FROM ({aggregate(period, month, state["cutoff"])}) '
            f'WHERE source_bars != {period // 5}', settings=SETTINGS).result_rows[0][0]
        job['state'] = 'writing'
        save()
        client.command(f'INSERT INTO {table} ({FIELDS}, created_at, id) '
                       f'SELECT {FIELDS}, now(), cityHash64(tuple(code, datetime)) '
                       f'FROM ({sql})', settings=SETTINGS)
        job['state'] = 'validating'
        save()
        job['actual'] = digest(client, target)
        job['source_after'] = digest(client, sql)
        if not (job['expected'] == job['actual'] == job['source_after']):
            raise RuntimeError('Source changed or candidate checksum mismatch')
        if job['actual'][0] != job['actual'][1]:
            raise RuntimeError('Duplicate candidate keys')
        job['state'] = 'verified'
        save()
    except BaseException as exc:
        job.update(state='blocked', error=str(exc)[:2000])
        save()
        raise


def run(args):
    table_name(15, args.run_id)
    if date.fromisoformat(args.cutoff) >= datetime.now(BUSINESS_TZ).date():
        raise ValueError('cutoff must be before today; current-day deltas need separate acceptance')
    root = runtime_path('operations', 'derived_recovery', args.run_id)
    lock = InstanceLock(runtime_path('operations', 'derived_recovery.lock'))
    lock.acquire()
    client = None
    try:
        client = clickhouse_client()
        from urllib3.util import Timeout
        client.timeout = Timeout(connect=10, read=660)
        client.query_retries = 0
        path = root / 'state.json'
        fingerprint = hashlib.sha256(Path(__file__).read_bytes() +
                                     (ROOT / 'scripts/govern_kline_history.py').read_bytes()).hexdigest()
        if path.exists():
            state = json.loads(path.read_text(encoding='utf-8'))
            if state['cutoff'] != args.cutoff or state['fingerprint'] != fingerprint:
                raise ValueError('Existing run uses a different cutoff or implementation')
        else:
            months = [str(r[0]) for r in client.query(
                "SELECT DISTINCT partition FROM system.parts WHERE database=currentDatabase() "
                "AND table='kline_minute_5' AND active ORDER BY partition").result_rows]
            months = [m for m in months if re.fullmatch(r'\d{6}', m) and
                      bounds(m, args.cutoff)[0] <= date.fromisoformat(args.cutoff)]
            if not months:
                raise RuntimeError('No source months found')
            for period in (15, 30, 60):
                if client.command(f'EXISTS TABLE {table_name(period, args.run_id)}'):
                    raise RuntimeError('Candidate already exists without a manifest; use new run-id')
            state = dict(run_id=args.run_id, cutoff=args.cutoff, fingerprint=fingerprint,
                         months=months, jobs={}, promotion='not_performed')
            write_snapshot(state, path)
        if not args.build:
            print(json.dumps(state, ensure_ascii=False), flush=True)
            return
        for period in (15, 30, 60):
            if state['jobs'] and not client.command(f'EXISTS TABLE {table_name(period, args.run_id)}'):
                raise RuntimeError('Candidate disappeared; do not recreate an accepted table')
            client.command(f'CREATE TABLE IF NOT EXISTS {table_name(period, args.run_id)} '
                           '(code Nullable(String), datetime Nullable(DateTime), '
                           'open Nullable(Float64), high Nullable(Float64), low Nullable(Float64), '
                           'close Nullable(Float64), volume Nullable(Int64), amount Nullable(Float64), '
                           'created_at Nullable(DateTime), id UInt64) ENGINE = ReplacingMergeTree '
                           'PARTITION BY toYYYYMM(datetime) ORDER BY (code, datetime) '
                           'SETTINGS allow_nullable_key=1')
        completed = 0
        for month in state['months']:
            for period in (15, 30, 60):
                if protected_session(datetime.now(BUSINESS_TZ)):
                    print('Paused for intraday protection; resume this run after 15:15.', flush=True)
                    return
                if state['jobs'].get(f'{period}:{month}', {}).get('state') == 'verified':
                    build_month(client, state, period, month, lambda: write_snapshot(state, path))
                    continue
                build_month(client, state, period, month, lambda: write_snapshot(state, path))
                completed += 1
                print(json.dumps({'month': month, 'period': period,
                                  **state['jobs'][f'{period}:{month}']}, ensure_ascii=False), flush=True)
                if args.limit and completed >= args.limit:
                    return
    finally:
        if client:
            client.close()
        lock.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--cutoff', default=str(datetime.now(BUSINESS_TZ).date() - timedelta(days=1)))
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    run(parser.parse_args())

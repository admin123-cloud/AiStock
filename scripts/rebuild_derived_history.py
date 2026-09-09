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
LEGACY_FINGERPRINTS = {
    'fb88a8940f185d5e75f568822aeb45f0a0450eb76d2e934de4510336a412fe9e',
    'e05d6623f5c6ed3eb9285111b41c8348312b08ca59426f0c5b86e35699342152',
}


def bounds(month, cutoff):
    first = datetime.strptime(str(month), '%Y%m').date().replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, min(last, date.fromisoformat(cutoff))


def aggregate(period, month, cutoff):
    first, last = bounds(month, cutoff)
    return aggregate_range(period, first, last)


def aggregate_range(period, first, last):
    sql = _derived_aggregate_sql(f'{period}m', first, last, True)
    # Stable chronological summation avoids nondeterministic Float64 merge order.
    sql = sql.replace('sum(amount) AS amount',
                      'if(count(amount) = 0, NULL, arraySum(arrayMap(x -> ifNull(x.2, 0), '
                      'arraySort(x -> x.1, groupArray(tuple(source_datetime, amount)))))) AS amount')
    # Explicit partition predicate is necessary with Nullable(DateTime) on this deployment.
    return sql.replace('WHERE ', f'WHERE toYYYYMM(datetime) = {first.year * 100 + first.month} AND ', 1)


def source_sql(period, month, cutoff):
    return (f'SELECT {FIELDS} FROM ({aggregate(period, month, cutoff)}) '
            f'WHERE source_bars = {period // 5}')


def source_days(client, month, cutoff):
    first, last = bounds(month, cutoff)
    rows = client.query(
        "SELECT DISTINCT toDate(datetime) FROM kline_minute_5 FINAL "
        f"WHERE toYYYYMM(datetime) = {int(month)} AND datetime IS NOT NULL "
        f"AND toDate(datetime) >= toDate('{first}') AND toDate(datetime) <= toDate('{last}') "
        "ORDER BY 1", settings=SETTINGS).result_rows
    return [row[0] for row in rows]


def source_day_sql(period, day):
    return (f'SELECT {FIELDS} FROM ({aggregate_range(period, day, day)}) '
            f'WHERE toDate(datetime) = toDate(\'{day}\') AND source_bars = {period // 5}')


def digest(client, sql):
    # Count and two independent reductions detect content changes, not just row presence.
    row = client.query(f'SELECT count(), uniqExact(tuple(code, datetime)), '
                       f'toString(sumWithOverflow(cityHash64(tuple({FIELDS})))), '
                       f'toString(groupBitXor(cityHash64(tuple({FIELDS})))) '
                       f'FROM ({sql})', settings=SETTINGS).result_rows[0]
    return list(row)


def combine_digests(digests):
    """Combine day-disjoint exact digests with the same UInt64 arithmetic as ClickHouse."""
    count = unique = total = xor = 0
    for row in digests:
        count += int(row[0])
        unique += int(row[1])
        total = (total + int(row[2])) & ((1 << 64) - 1)
        xor ^= int(row[3])
    return [count, unique, str(total), str(xor)]


def digest_days(client, sqls):
    return combine_digests(digest(client, sql) for sql in sqls)


def candidate_codes(client, table, month):
    rows = client.query(
        f'SELECT DISTINCT code FROM {table} '
        f'WHERE toYYYYMM(datetime) = {int(month)} AND code IS NOT NULL ORDER BY 1',
        settings=SETTINGS).result_rows
    return [row[0] for row in rows]


def candidate_digest(client, table, month):
    """Digest code chunks: candidate ORDER BY begins with code, unlike date."""
    codes = candidate_codes(client, table, month)
    sqls = []
    for offset in range(0, len(codes), 200):
        quoted = ', '.join("'" + str(code).replace("'", "''") + "'" for code in codes[offset:offset + 200])
        sqls.append(f'SELECT {FIELDS} FROM {table} WHERE toYYYYMM(datetime) = {int(month)} '
                    f'AND code IN ({quoted})')
    return digest_days(client, sqls)


def revalidation_status(accepted, candidate, source):
    """Classify a source recheck without mutating a candidate table."""
    if candidate[0] != candidate[1]:
        return 'candidate_duplicate_keys'
    if candidate != accepted:
        return 'candidate_changed_since_acceptance'
    if candidate != source:
        return 'source_changed_since_acceptance'
    return 'matched'


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
        if candidate_digest(client, table, month) != job['actual']:
            raise RuntimeError(f'{key}: verified candidate changed; rebuild with a new run-id')
        return
    if job and job.get('state') != 'retry_authorized_empty_candidate':
        raise RuntimeError(f'{key}: uncertain previous attempt; inspect before using a new run-id')
    if client.query(f'SELECT count() FROM ({target})', settings=SETTINGS).result_rows[0][0]:
        raise RuntimeError(f'{key}: candidate partition is not empty')
    history = list(job.get('recovery_history', [])) if job else []
    job = {'state': 'checking_source', 'recovery_history': history}
    state['jobs'][key] = job
    save()
    try:
        days = source_days(client, month, state['cutoff'])
        job['source_days'] = [str(day) for day in days]
        job['expected'] = digest_days(client, (source_day_sql(period, day) for day in days))
        job['incomplete_buckets'] = sum(
            client.query(f'SELECT count() FROM ({aggregate_range(period, day, day)}) '
                         f'WHERE source_bars != {period // 5}', settings=SETTINGS).result_rows[0][0]
            for day in days)
        job['state'] = 'writing'
        save()
        for day in days:
            job['writing_day'] = str(day)
            save()
            sql = source_day_sql(period, day)
            client.command(f'INSERT INTO {table} ({FIELDS}, created_at, id) '
                           f'SELECT {FIELDS}, now(), cityHash64(tuple(code, datetime)) '
                           f'FROM ({sql})', settings=SETTINGS)
        job.pop('writing_day', None)
        job['state'] = 'validating'
        save()
        job['actual'] = candidate_digest(client, table, month)
        after_days = source_days(client, month, state['cutoff'])
        job['source_after'] = digest_days(client, (source_day_sql(period, day) for day in after_days))
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


def authorize_empty_memory_blocked_jobs(client, state, save):
    """Recover only memory-blocked jobs with either an empty or fully revalidated candidate."""
    for key, job in state['jobs'].items():
        if job.get('state') != 'blocked':
            continue
        if 'MEMORY_LIMIT_EXCEEDED' not in str(job.get('error', '')):
            raise RuntimeError(f'{key}: blocked for a non-memory error; refusing retry')
        period, month = key.split(':', 1)
        table = table_name(int(period), state['run_id'])
        count = client.query(f'SELECT count() FROM {table} WHERE toYYYYMM(datetime) = {int(month)}',
                             settings=SETTINGS).result_rows[0][0]
        job['recovery_history'] = [dict(state='blocked', error=job.get('error'),
                                        expected=job.get('expected'),
                                        incomplete_buckets=job.get('incomplete_buckets'))]
        if count:
            actual = candidate_digest(client, table, month)
            days = source_days(client, month, state['cutoff'])
            source_after = digest_days(client, (source_day_sql(int(period), day) for day in days))
            if not (job.get('expected') == actual == source_after) or actual[0] != actual[1]:
                raise RuntimeError(f'{key}: blocked candidate contains {count} rows but failed full recovery validation')
            job.update(state='verified', actual=actual, source_after=source_after,
                       source_days=[str(day) for day in days],
                       retry_reason='memory_limited_validation_recovered_existing_candidate')
            save()
            continue
        job['state'] = 'retry_authorized_empty_candidate'
        job['retry_reason'] = 'explicit_daily_chunk_recovery_after_empty_memory_limited_attempt'
        save()


def migrate_legacy_manifest(client, state, fingerprint, save):
    """Revalidate every accepted checkpoint before changing its implementation fingerprint."""
    if state['fingerprint'] == fingerprint:
        return
    prior_fingerprint = state['fingerprint']
    if prior_fingerprint not in LEGACY_FINGERPRINTS:
        raise ValueError('Existing run uses a different cutoff or implementation')
    for key, job in state['jobs'].items():
        if job.get('state') != 'verified':
            continue
        period, month = key.split(':', 1)
        table = table_name(int(period), state['run_id'])
        actual = candidate_digest(client, table, month)
        if actual != job.get('actual'):
            raise RuntimeError(f'{key}: legacy verified candidate changed; refusing manifest migration')
    state['fingerprint'] = fingerprint
    state['manifest_migration'] = {
        'from_fingerprint': prior_fingerprint,
        'method': 'revalidated_accepted_candidates_then_switched_to_daily_chunks',
    }
    save()


def revalidate_month(client, state, period, month, save):
    """Recheck a verified candidate after a 5m source repair; never write candidates."""
    key = f'{period}:{month}'
    job = state['jobs'].get(key)
    if not job or job.get('state') not in ('verified', 'stale_source'):
        raise RuntimeError(f'{key}: only previously verified candidates can be source-revalidated')
    table = table_name(period, state['run_id'])
    candidate = candidate_digest(client, table, month)
    days = source_days(client, month, state['cutoff'])
    source = digest_days(client, (source_day_sql(period, day) for day in days))
    outcome = revalidation_status(job.get('actual'), candidate, source)
    job['source_revalidation'] = {
        'checked_at': datetime.now(BUSINESS_TZ).isoformat(),
        'candidate': candidate,
        'source': source,
        'source_days': [str(day) for day in days],
        'outcome': outcome,
        'write_authority': 'read_only',
    }
    if outcome != 'matched':
        job['state'] = 'stale_source'
        job['stale_reason'] = outcome
        state['promotion'] = 'blocked_by_source_revalidation'
    elif job.get('state') == 'stale_source':
        job['state'] = 'verified'
        job.pop('stale_reason', None)
    save()
    return outcome


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
            if state['cutoff'] != args.cutoff:
                raise ValueError('Existing run uses a different cutoff or implementation')
            # A source revalidation only compares existing evidence and never writes a candidate.
            # It must remain available when this driver's own implementation fingerprint changes.
            if not args.revalidate_source:
                migrate_legacy_manifest(client, state, fingerprint, lambda: write_snapshot(state, path))
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
        if args.revalidate_source:
            if not args.months:
                raise ValueError('--revalidate-source requires explicit --months')
            selected = [month.strip() for month in args.months.split(',') if month.strip()]
            if not selected or any(not re.fullmatch(r'\d{6}', month) or month not in state['months'] for month in selected):
                raise ValueError('--months must be source months in this run')
            outcomes = {}
            for month in selected:
                for period in (15, 30, 60):
                    outcomes[f'{period}:{month}'] = revalidate_month(
                        client, state, period, month, lambda: write_snapshot(state, path))
            print(json.dumps({'revalidated_months': selected, 'outcomes': outcomes,
                              'promotion': state['promotion']}, ensure_ascii=False), flush=True)
            return
        if not args.build:
            print(json.dumps(state, ensure_ascii=False), flush=True)
            return
        if args.resume_blocked:
            authorize_empty_memory_blocked_jobs(client, state, lambda: write_snapshot(state, path))
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
    parser.add_argument('--resume-blocked', action='store_true',
                        help='Explicitly retry only memory-blocked jobs whose candidate month is empty.')
    parser.add_argument('--revalidate-source', action='store_true',
                        help='Read-only compare accepted candidates with current 5m source for explicit months.')
    parser.add_argument('--months', default='',
                        help='Comma-separated YYYYMM source months required by --revalidate-source.')
    run(parser.parse_args())

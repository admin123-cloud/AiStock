"""Fill one missing sector day from verified QMT membership; no deletion or replacement."""
import argparse
from datetime import date, datetime, time
import hashlib
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from services.operations.health import write_snapshot
from services.operations.ingestion_store import clickhouse_client, clickhouse_query_df
from services.operations.lifecycle import InstanceLock
from scripts.rebuild_shenwan_sector_kline_daily import build_sector_rows, load_daily
from utils.paths import runtime_path

TZ = ZoneInfo('Asia/Shanghai')


def verified_members(manifest, day, now):
    if manifest.get('source') != 'qmt' or manifest.get('verified') is not True:
        raise ValueError('Verified QMT universe evidence required')
    generated = datetime.fromisoformat(manifest['generated_at'])
    if not 0 <= (now-generated).total_seconds() <= 36*3600:
        raise ValueError('QMT membership evidence expired')
    if day < date.fromisoformat(manifest['effective_from']):
        raise ValueError('Current membership must not be projected into older history')
    codes = set(manifest.get('codes', []))
    members = manifest.get('members', {})
    if not codes or any(not members.get(code) for code in codes):
        raise ValueError('QMT member snapshot missing for one or more sectors')
    return pd.DataFrame([{'sector_code': code, 'stock_code': stock}
                         for code in sorted(codes) for stock in sorted(set(members[code]))])


def prepare_rows(members, daily, existing, prior, day):
    missing = set(members.sector_code)-set(existing)
    if not missing:
        return pd.DataFrame()
    selected = members[members.sector_code.isin(missing)]
    present = set(daily.loc[pd.to_datetime(daily.trade_date).dt.date == day, 'code']) if not daily.empty else set()
    absent = set(selected.stock_code)-present
    if absent:
        raise ValueError(f'Incomplete verified member daily data: {len(absent)} securities')
    without_prior = [code for code in missing if float(prior.get(code) or 0) <= 0]
    if without_prior:
        raise ValueError('Prior sector close required; cannot invent a new 1000-point historical baseline')
    rows = build_sector_rows(selected, daily)
    if set(rows.code) != missing or any(pd.to_datetime(rows.trade_date).dt.date != day):
        raise ValueError('Derived result does not match exact requested day and sector universe')
    for column in ('open', 'high', 'low', 'close'):
        rows[column] = rows[column] * rows.code.map(prior).astype(float) / 1000.0
    return rows


def repair(day, *, now=None):
    now = now or datetime.now(TZ)
    if day > now.date() or (day == now.date() and now.time().replace(tzinfo=None) < time(15, 20)):
        raise ValueError('Sector daily repair requires a closed trading day')
    manifest_path = runtime_path('operations', 'sector_universe.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    members = verified_members(manifest, day, now)
    client = clickhouse_client()
    if not client.query("SELECT count() FROM trade_calendar WHERE market='SH' AND is_trading=1 AND trade_date={day:Date}",
                        parameters={'day': day}).result_rows[0][0]:
        raise ValueError('Requested date is not verified by the trading calendar')
    key = hashlib.sha256((day.isoformat()+json.dumps(manifest, sort_keys=True)).encode()).hexdigest()[:20]
    path = runtime_path('operations', 'sector_repair', key+'.json')
    state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if state.get('in_flight'):
        return {'ok': False, 'uncertain': True, 'reason': 'execution_deadline_uncertain', 'checkpoint': str(path)}
    existing = client.query('SELECT code FROM sector_kline_daily FINAL WHERE trade_date={day:Date}',
                            parameters={'day': day}).result_rows
    missing = set(members.sector_code)-{row[0] for row in existing}
    if not missing:
        return {'ok': True, 'inserted': 0, 'reason': 'all_keys_already_present'}
    selected = members[members.sector_code.isin(missing)]
    daily = load_daily(sorted(set(selected.stock_code)), day.isoformat(), day.isoformat(), query_df=clickhouse_query_df)
    prior = dict(client.query('SELECT code,close FROM sector_kline_daily FINAL '
                              'WHERE trade_date=(SELECT max(trade_date) FROM trade_calendar '
                              "WHERE market='SH' AND is_trading=1 AND trade_date<{day:Date})",
                              parameters={'day': day}).result_rows)
    rows = prepare_rows(selected, daily, set(), prior, day)
    # One owner plus an append-only batch. A lost acknowledgment is never blindly replayed.
    write_snapshot({'in_flight': True, 'date': day.isoformat(), 'expected_codes': sorted(missing)}, path)
    try:
        client.insert_df('sector_kline_daily', rows)
        actual = client.query('SELECT code FROM sector_kline_daily FINAL WHERE trade_date={day:Date}',
                              parameters={'day': day}).result_rows
        if not missing <= {row[0] for row in actual}:
            raise RuntimeError('Inserted sector keys could not be verified')
    except Exception as exc:
        return {'ok': False, 'uncertain': True, 'error': str(exc), 'checkpoint': str(path)}
    result = {'ok': True, 'inserted': len(rows), 'date': day.isoformat(), 'in_flight': False,
              'membership_evidence': str(manifest_path), 'source': 'qmt_members_stock_daily_median'}
    write_snapshot(result, path)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--trade-date', required=True, type=date.fromisoformat)
    args = parser.parse_args()
    from services.operations.qmt_download_queue import protected_session
    if protected_session(datetime.now(TZ)):
        if os.getenv('AISTOCK_BACKLOG_REPLAY') != '1':
            from services.operations.ingestion_backlog import enqueue
            enqueue(list(sys.argv[1:]), kind='sector_daily')
        return 75
    lock = InstanceLock(runtime_path('operations', 'sector-daily-repair.lock'))
    lock.acquire()
    try:
        try:
            result = repair(args.trade_date)
        except Exception as exc:
            result = {'ok': False, 'error': str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get('ok') else 76 if result.get('uncertain') else 1
    finally:
        lock.release()


if __name__ == '__main__':
    raise SystemExit(main())

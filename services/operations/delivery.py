"""Read-only, trading-calendar based coverage. Never derives expectations from observed bars."""
from datetime import date, datetime, time, timedelta
from typing import Any
import json
from pathlib import Path

from services.operations.health import BUSINESS_TZ


from utils.trading_sessions import completed_bar_times as bar_times


def delivery_contract():
    return json.loads((Path(__file__).resolve().parents[2]/'config/data_delivery_contract.json').read_text(encoding='utf-8'))


def expected_times(day: str, period: int, now: datetime) -> list[str]:
    if day > now.date().isoformat():
        return []
    return [v for v in bar_times(period) if day < now.date().isoformat() or v <= now.strftime("%H:%M")]


def coverage_cell(day: str, expected: int, actual: int, *, due: bool, exceptions: int = 0) -> dict[str, Any]:
    missing = max(0, expected - actual)
    status = "not_due" if not due else "unknown" if expected == 0 else "complete" if missing == 0 else "partial" if actual else "missing"
    return {"date": day, "status": status, "expected": expected, "actual": actual,
            "missing": missing, "business_exceptions": exceptions,
            "coverage": round(min(actual / expected, 1), 6) if due and expected else None}


def build_delivery_calendar(client, *, days: int = 30, now: datetime | None = None,
                            sector_universe: dict | None = None, query_timeout_seconds: int = 10) -> dict[str, Any]:
    now = now or datetime.now(BUSINESS_TZ)
    policy = delivery_contract()
    daily_deadline = time.fromisoformat(policy['daily_deadline'])
    minute_cutoff = now - timedelta(minutes=policy['minute_delivery_lag_minutes'])
    end = now.date().isoformat()
    days = min(60, max(1, int(days)))
    query_timeout_seconds = min(120, max(1, int(query_timeout_seconds)))
    dates = [str(row[0])[:10] for row in client.query(
        f"SELECT DISTINCT trade_date FROM trade_calendar WHERE market='SH' AND is_trading=1 "
        f"AND trade_date<=toDate('{end}') ORDER BY trade_date DESC LIMIT {days} SETTINGS max_execution_time={query_timeout_seconds}"
    ).result_rows][::-1]
    if not dates:
        raise ValueError("交易日历未就绪，不能计算覆盖率")
    start = dates[0]
    universe_raw = client.query(f"SELECT code, type, list_date, listing_status, delist_date FROM stocks WHERE type IN ('stock','index') SETTINGS max_execution_time={query_timeout_seconds}").result_rows
    # Four-column rows are accepted only for older read-only test adapters;
    # deployed ClickHouse records always contain the explicit lifecycle field.
    universe = [
        (row[0], row[1], row[2], row[3] if len(row) >= 5 else 'active', row[4] if len(row) >= 5 else row[3])
        for row in universe_raw
    ]
    # Newly-created QMT contracts may exist before their first tradable day.
    # Keep them visible in reference diagnostics, but do not make them expected
    # market-data keys or turn them into a strategy/coverage failure.
    pending_listing = {str(code) for code, _typ, _listed, status, _delisted in universe if str(status or 'active') == 'pending_listing'}
    eligible_universe = [row for row in universe if str(row[3] or 'active') != 'pending_listing']
    missing_listing = {kind:[str(code) for code, typ, listed, _status, _ in eligible_universe
                             if typ == kind and (not listed or str(listed)[:10] in ('1970-01-01','0000-00-00'))]
                       for kind in ('stock','index')}
    # Verified business absences only; unresolved QMT empty responses remain missing.
    exclusions = client.query(
        "SELECT code, start_date, end_date FROM kline_daily_market_status_audit FINAL "
        f"WHERE evidence_url != '' AND status IN ('suspended','delisted') AND start_date<=toDate('{end}') AND end_date>=toDate('{start}') SETTINGS max_execution_time={query_timeout_seconds}"
    ).result_rows
    expected = {}
    exceptions = {}
    for day in dates:
        absent = {str(code) for code, first, last in exclusions if str(first)[:10] <= day <= str(last)[:10]}
        for kind in ('stock', 'index'):
            eligible = {str(code) for code, typ, listed, _status, delisted in eligible_universe if typ == kind
                        and (not listed or str(listed)[:10] <= day)
                        and (not delisted or str(delisted)[:10] in ('1970-01-01', '0000-00-00') or day <= str(delisted)[:10])}
            expected[kind, day] = eligible - absent
            exceptions[kind, day] = len(eligible & absent)
    datasets = []
    observed_cache = {}
    for kind in ('stock', 'index'):
        for period in (0, 5, 15, 30, 60):
            label = ('股票' if kind == 'stock' else '指数') + ('日线' if period == 0 else f'{period}分钟')
            dataset = {"id": f"{kind}_{period or 'daily'}", "label": label, "cells": []}
            table = f'kline_minute_{period}' if period else 'kline_daily'
            col = 'datetime' if period else 'trade_date'
            try:
                # Unique business time keys, not physical row counts (ReplacingMergeTree may contain versions).
                sql = f"SELECT code, toDate({col}), "
                sql += "groupUniqArray(formatDateTime(datetime, '%H:%i', 'Asia/Shanghai'))" if period else "count()"
                sql += f" FROM {table} WHERE {col}>=toDate('{start}') AND {col}<toDate('{end}')+1 GROUP BY code,toDate({col}) SETTINGS max_execution_time={query_timeout_seconds}"
                if period not in observed_cache:
                    observed_cache[period] = {(str(code), str(day)[:10]): values for code, day, values in client.query(sql).result_rows}
                observed = observed_cache[period]
                for day in dates:
                    codes = expected[kind, day]
                    times = set(expected_times(day, period, minute_cutoff)) if period else set()
                    due = bool(times) if period else day < end or now.time() >= daily_deadline
                    actual = sum(len(set(observed.get((code, day), [])) & times) if period else int((code, day) in observed) for code in codes)
                    total = len(codes) * (len(times) if period else 1)
                    cell = coverage_cell(day, total, actual, due=due, exceptions=exceptions[kind, day])
                    if missing_listing[kind]:
                        cell['metadata_warning'] = ('部分股票' if kind == 'stock' else '部分指数')+'缺少上市日期，历史预期全集尚未验收'
                        cell['missing_listing_metadata_count'] = len(missing_listing[kind])
                        if cell['status'] == 'complete':
                            cell['status'] = 'unverified'
                    cell['delivery_deadline'] = policy['daily_deadline'] if not period else f"收线后{policy['minute_delivery_lag_minutes']}分钟"
                    cell['remediation_owner'] = policy['stock_index_owner']
                    cell['missing_codes_sample'] = [code for code in sorted(codes)
                        if (len(set(observed.get((code, day), [])) & times) < len(times) if period else (code, day) not in observed)][:30]
                    dataset['cells'].append(cell)
            except Exception as exc:
                dataset['error'] = f'{type(exc).__name__}: {exc}'
                dataset['cells'] = [{'date': day, 'status': 'unknown', 'coverage': None} for day in dates]
            datasets.append(dataset)
    try:
        rows = client.query(f"SELECT trade_date, groupUniqArray(code) FROM sector_kline_daily WHERE trade_date>=toDate('{start}') AND trade_date<=toDate('{end}') GROUP BY trade_date SETTINGS max_execution_time={query_timeout_seconds}").result_rows
        by_date = {str(day)[:10]: set(map(str,codes)) for day,codes in rows}
        sector_cells = sector_coverage_cells(dates,by_date,sector_universe,now=now,daily_deadline=daily_deadline)
    except Exception as exc:
        sector_cells = [{'date': day, 'status': 'unknown', 'error': type(exc).__name__, 'coverage':None} for day in dates]
    datasets.append({'id': 'sector_daily', 'label': '板块日线', 'cells': sector_cells})
    return {'generated_at': now.isoformat(timespec='seconds'), 'dates': dates, 'datasets': datasets,
            'contract': policy,
            'pending_listing_codes': sorted(pending_listing),
            'scope': '唯一时间键覆盖；含上市/退市及有依据的业务豁免。完整表示覆盖齐全，价格质量与主表/stage冲突由G3独立验收。板块以有时效与生效日期的QMT全集验收，缺少证据时分母未知。'}


def sector_coverage_cells(dates, observed, universe, *, now, daily_deadline):
    universe = universe or {}
    try:
        stamp = datetime.fromisoformat(universe['generated_at'])
        age = (now-stamp).total_seconds()
        valid = universe.get('source') == 'qmt' and universe.get('verified') is True and 0 <= age <= 36*3600
        codes = set(universe['codes'])
        valid = valid and bool(codes) and all(isinstance(x,str) and x for x in codes)
    except (KeyError,ValueError,TypeError):
        valid,codes = False,set()
    result = []
    for day in dates:
        actual = observed.get(day,set())
        due = day < now.date().isoformat() or now.time() >= daily_deadline
        if not valid or not universe.get('effective_from') or day < universe['effective_from']:
            result.append({'date':day,'status':'not_due' if not due else 'unverified',
                           'actual':len(actual),'expected':None,'coverage':None,
                           'reason':'qmt_sector_universe_not_verified_for_date'})
            continue
        exempt = {x['code'] for x in universe.get('exemptions',[]) if x.get('evidence_url')
                  and x.get('start_date','9999') <= day <= x.get('end_date','0000')}
        expected = codes-exempt
        result.append({**coverage_cell(day,len(expected),len(actual&expected),due=due,exceptions=len(codes&exempt)),
                       'universe_source':'qmt','universe_generated_at':universe['generated_at']})
    return result

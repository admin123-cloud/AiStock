"""Read models for task ownership, delivery incidents and daily G3 decisions.

No scheduler configuration, broker calls, data repairs or messages on GET paths.
"""
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from services.operations.health import BUSINESS_TZ, read_snapshot, strategy_data_checks
from strategies.contracts import formal_g3_score88_contract, formal_g3_score88_contract_metadata


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def read_csv(path: Path) -> list[dict]:
    try:
        with path.open(encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))
    except (OSError, ValueError):
        return []


def truth(value) -> bool:
    return str(value).lower() in ('true', '1', '1.0')


def number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def task_board(root: Path, manifest: dict, *, now: datetime | None = None, live: dict | None = None) -> dict:
    health = read_snapshot(root / 'health/latest.json', now=now)
    host = read_snapshot(root / 'operations/host_tasks.json', now=now)
    fresh_host = host.get('publisher_status') == 'healthy'
    components = {x['name']: x for x in health.get('components', [])}
    rows = []
    for item in host.get('tasks', []):
        result = item.get('last_result')
        state = item.get('state', '')
        status = 'unknown' if not fresh_host else 'disabled' if state == 'Disabled' else 'running' if state == 'Running' else 'failed' if result not in (0, None, 267009, 267011) else 'never_run' if result == 267011 else 'idle'
        owner = next((a for a in manifest.get('artifacts', []) if a.get('owner') == item.get('name')), {})
        component_name = {'kline_daily': 'daily_kline_coverage', 'kline_minute_after_close': 'qmt_after_close_validation'}.get(owner.get('artifact'))
        evidence = components.get(component_name, {})
        rows.append({**item, 'status': status, 'executor': 'Windows', 'artifact': owner.get('artifact'),
                     'registered': True,
                     'window': item.get('window') or owner.get('window'), 'business_status': evidence.get('status', 'unverified') if health.get('publisher_status') == 'healthy' else 'unknown',
                     'business_reason': evidence.get('reason'), 'source_stale': not fresh_host})
    specs = [
        ('G3候选刷新', 'shadow_monitor_state.json', '120秒 / 交易时段'),
        ('G3影子退出', 'shadow_exit_monitor_state.json', '120秒 / 交易时段'),
        ('G3趋势退出', 'daily_trend_exit_monitor_state.json', '300秒 / 交易时段'),
        ('G3每日观察', 'observation_scheduler_state.json', '15:40 / 次日补验'),
        ('真实账户同步', 'broker_sync_state.json', '17:30 / 交易日'),
    ]
    for name, file, window in specs:
        state = read_json(root / 'gen3_state_alpha' / file)
        rows.append({'name': name, 'executor': 'API', 'window': window,
                     'status': 'unknown' if not state else 'disabled' if state.get('enabled') is False else 'failed' if state.get('last_error') else 'configured',
                     'last_run': state.get('last_run_at'), 'last_success': state.get('last_success_at'),
                     'error': state.get('last_error'), 'business_status': 'unverified',
                     'note': '读取持久化记录；已配置不代表进程在线'})
    api_state = live if live is not None else read_json(root / 'operations/api_tasks.json')
    try:
        age = ((now or datetime.now(BUSINESS_TZ)) - datetime.fromisoformat(api_state['generated_at'])).total_seconds()
        api_fresh = 0 <= age <= 30 and api_state.get('phase') not in ('stopped', 'not_started')
    except (KeyError, ValueError, TypeError):
        api_fresh = False
    if api_state.get('tasks'):
        rows = [x for x in rows if x['executor'] != 'API']
        rows.extend({**x, 'status': x['status'] if api_fresh else 'unknown',
                     'source_stale': not api_fresh} for x in api_state['tasks'])
    discovered = {row['name'] for row in rows}
    for artifact in manifest.get('artifacts', []):
        if artifact.get('trigger') == 'Windows Task Scheduler' and artifact['owner'] not in discovered:
            discovered.add(artifact['owner'])
            rows.append({'name': artifact['owner'], 'executor': 'Windows', 'status': 'not_observed',
                         'registered': False,
                         'artifact': artifact['artifact'], 'window': artifact.get('window'), 'business_status': 'unknown'})
    from services.operations.task_catalog import describe_tasks
    rows, groups = describe_tasks(rows, read_json(root/'operations/task_transitions.json'))
    return {'generated_at': datetime.now(BUSINESS_TZ).isoformat(timespec='seconds'), 'tasks': rows, 'groups':groups,
            'api_runtime': {**api_state, 'fresh': api_fresh, 'source': 'live' if live is not None else 'snapshot',
                            'ready': bool(api_fresh and api_state.get('ready'))},
            'operations_publisher':read_snapshot(root/'operations/latest.json',now=now),
            'host_inventory_status': 'healthy' if fresh_host else 'unknown',
            'host_inventory_at': host.get('generated_at'), 'health': health,
            'summary': {'total': len(rows), 'running': sum(x['status'] == 'running' for x in rows),
                        'registered_windows':sum(x.get('registered') is True and x.get('status')!='retired' for x in rows),
                        'retired':sum(x['group']=='retired' for x in rows),
                        'failed': sum(x['status'] == 'failed' for x in rows),
                        'unknown': sum(x['status'] in ('unknown','not_observed') for x in rows)}}


def mainwave_daily(root: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(BUSINESS_TZ)
    summary = read_json(root / 'gen3_state_alpha/latest_summary.json')
    health = read_snapshot(root / 'health/latest.json', now=now)
    checks = strategy_data_checks(summary, health)
    contract = formal_g3_score88_contract()
    metadata = formal_g3_score88_contract_metadata()
    rows = read_csv(root / 'gen3_state_router_shadow/latest_all_source_candidates.csv')
    from services.operations.batches import read_mainwave_batch
    batch_summary, batch_rows, batch = read_mainwave_batch(root)
    if batch['ok']:
        summary, rows = batch_summary, batch_rows
        checks = strategy_data_checks(summary, health)
    checks.append({'name': 'mainwave_batch_integrity', 'ok': batch['ok'],
                   'message': '完整批次校验通过' if batch['ok'] else '候选批次未验收，需由生产者重新发布完整批次',
                   'reason': batch.get('reason', 'atomic_batch_verified')})
    candidates = []
    for item in rows:
        if item.get('route') != 'institutional_mainwave':
            continue
        conflict = item.get('m30_visibility_status') == 'data_conflict' or number(item.get('m30_conflict_rows')) > 0
        unavailable = conflict or item.get('m30_status') in ('data_unavailable','source_unavailable','data_conflict','missing')
        confirmed = truth(item.get('m30_confirmed'))
        unavailable = unavailable or (item.get('m30_source_ok') not in (None, '') and not truth(item.get('m30_source_ok')))
        qualified = number(item.get('wave_style_score')) >= contract['entry']['minimum_score'] and number(item.get('sector_signal_count')) >= contract['entry']['minimum_same_day_industry_mainwave_count']
        stage = 'data_blocked' if unavailable else 'candidate_observing' if not qualified else 'confirmed' if confirmed else 'waiting_30m'
        candidates.append({'code': item.get('code'), 'name': item.get('name') or item.get('stock_name'),
            'industry': item.get('sw_l2_industry_name') or item.get('industry_name') or item.get('sector_name'),
            'industry_source': item.get('industry_source') or item.get('stock_industry_source'),
            'score': item.get('wave_style_score'), 'resonance': item.get('sector_signal_count'),
            'stage': stage, 'reason': item.get('block_reason') or item.get('m30_error') or '',
            'conflict_rows': item.get('m30_conflict_rows'), 'confirmation_time': item.get('confirm_datetime'),
            'entry_date': item.get('entry_date'), 'decision_date': item.get('decision_date'),
            'reference_price': item.get('reference_close'), 'm30_source': item.get('m30_source')})
    observations = read_json(root / 'gen3_state_alpha/observation_snapshots.json').get('snapshots', [])
    observations = [x for x in observations if x.get('strategy_id') == contract['strategy_id']]
    by_day = {x.get('observation_date'):x for x in sorted(observations,key=lambda x:str(x.get('created_at') or '')) if x.get('observation_date')}
    recent = sorted(by_day.values(), key=lambda x: str(x.get('observation_date') or ''))[-30:]
    history = [{'date': x.get('observation_date'), 'status': x.get('status'), 'accepted': x.get('accepted'),
                'reason': x.get('failure_category'), 'repair_triggered': x.get('minute30_repair_triggered'),
                'action': x.get('next_action')} for x in recent]
    stale_batch = bool(summary.get('entry_date') and str(summary['entry_date'])[:10] < now.date().isoformat())
    coherent = bool(summary.get('entry_date')) and all(
        item.get('entry_date') == summary.get('entry_date')
        and item.get('decision_date') == summary.get('decision_date') for item in candidates)
    try:
        with (root / 'gen3_state_router_shadow/latest_all_source_candidates.csv').open(encoding='utf-8-sig',newline='') as handle:
            fields = csv.DictReader(handle).fieldnames or []
            source_exists = {'route','code','entry_date','decision_date'}.issubset(fields)
    except (OSError,ValueError):
        source_exists = False
    if batch['ok']:
        source_exists = True
    checks.append({'name':'candidate_batch_consistency', 'ok':coherent and source_exists,
                   'message':'候选日期与摘要一致' if coherent and source_exists else '候选文件缺失或与摘要日期不一致，不能作为当天结论'})
    blocked = not summary or any(not x['ok'] for x in checks)
    state = 'data_blocked' if blocked else 'stale_batch' if stale_batch else 'waiting_30m' if summary.get('pending_next_session_confirmation') else 'candidates' if candidates else 'no_candidates'
    snapshots = read_json(root/'operations/mainwave_tracking.json').get('snapshots', [])
    previous = [x for x in snapshots if x.get('contract_hash') == metadata['sha256']
                and x.get('entry_date','') < str(summary.get('entry_date') or '') and x.get('valid')]
    prior = max(previous, key=lambda x:x['entry_date']) if previous else None
    changes = []
    if prior and not blocked and not stale_batch:
        before = {x['code']:x for x in prior.get('candidates', [])}
        after = {x['code']:x for x in candidates}
        for code in sorted(before.keys() | after.keys()):
            old, new = before.get(code), after.get(code)
            if old is None or new is None or old['stage'] != new['stage']:
                changes.append({'code':code,'name':(new or old).get('name'),
                                'before':old['stage'] if old else None, 'after':new['stage'] if new else None})
    return {'generated_at': now.isoformat(timespec='seconds'), 'source_generated_at': summary.get('generated_at'),
            'batch': batch,
            'state': state, 'entry_date': summary.get('entry_date'), 'decision_date': summary.get('decision_date'),
            'stale_batch': stale_batch, 'contract': contract, 'contract_metadata': metadata,
            'checks': checks, 'health': health, 'candidates': candidates, 'history': history,
            'changes': changes, 'comparison_date': prior.get('entry_date') if prior else None,
            'comparison_status': 'data_blocked' if blocked or stale_batch else 'available' if prior else 'no_baseline',
            'summary': {'candidate_count': len(candidates), 'confirmed_count': sum(x['stage'] == 'confirmed' for x in candidates),
                        'data_blocked_count': sum(x['stage'] == 'data_blocked' for x in candidates),
                        'accepted_days': sum(bool(x.get('accepted')) for x in history), 'observed_days': len(history)}}


def record_mainwave_tracking(root: Path, daily: dict) -> None:
    """Persist each source batch without retroactively relabeling old decisions."""
    from services.operations.health import write_snapshot
    path = root/'operations/mainwave_tracking.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshots = read_json(path).get('snapshots', [])
    key = (daily.get('source_generated_at'), daily['contract_metadata']['sha256'])
    if any((x.get('source_generated_at'),x.get('contract_hash'))==key for x in snapshots):
        return
    snapshots.append({'source_generated_at':key[0], 'contract_hash':key[1],
                      'entry_date':daily.get('entry_date') or '', 'recorded_at':daily['generated_at'],
                      'valid':daily['state'] not in ('data_blocked','stale_batch'), 'candidates':daily['candidates']})
    with (root/'operations/mainwave_tracking_events.jsonl').open('a',encoding='utf-8') as handle:
        handle.write(json.dumps(snapshots[-1],ensure_ascii=False)+'\n')
    # Compact read model; immutable source batches remain in the event archive.
    by_day = {(x['entry_date'],x['contract_hash']):x for x in snapshots}
    write_snapshot({'snapshots':sorted(by_day.values(),key=lambda x:x['entry_date'])[-90:]},path)

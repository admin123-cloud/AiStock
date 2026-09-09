import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from services.operations.schedulers import SchedulerRegistry, ObservedScheduler
from services.operations.lifecycle import InstanceLock
from services.operations.incidents import reconcile, dispatch, read_incidents
from services.operations.batches import publish_mainwave_batch, read_mainwave_batch
from services.operations.read_models import task_board, mainwave_daily
from services.operations.health import BUSINESS_TZ, write_snapshot

NOW = datetime(2026, 9, 9, 17, 0, tzinfo=BUSINESS_TZ)


def test_initializer_failure_does_not_skip_later_jobs():
    registry = SchedulerRegistry()
    calls = []
    def fail():
        raise RuntimeError('broken config')
    registry.initialize([('broken', fail), ('later', lambda: calls.append(1) or {'enabled': True})])
    result = registry.snapshot()
    assert calls == [1]
    assert not result['ready'] and result['blockers'] == ['broken']
    assert result['startups'][1]['status'] == 'ready'


def test_explicit_disable_never_calls_initializers():
    registry = SchedulerRegistry()
    registry.initialize([('disabled', lambda: pytest.fail('must not start'))], enabled=False)
    assert registry.snapshot()['startups'][0]['status'] == 'disabled'


def test_task_error_visible_and_return_value_preserved():
    registry = SchedulerRegistry()
    scheduler = ObservedScheduler(telemetry=registry)
    job = scheduler.add_job(lambda: {'ok': False, 'reason': 'source unavailable'}, 'interval', hours=1, id='source')
    scheduler.start(paused=True)
    try:
        assert job.func()['reason'] == 'source unavailable'
        state = registry.snapshot()['tasks'][0]
        assert state['status'] == 'failed'
        assert state['failures'] == 1 and state['runs'] == 1
        assert state['business_status'] == 'blocked'
    finally:
        registry.shutdown()


def test_task_exception_propagates_and_is_observed():
    registry = SchedulerRegistry()
    scheduler = ObservedScheduler(telemetry=registry)
    def fail():
        raise ValueError('collector failed')
    job = scheduler.add_job(fail, 'interval', hours=1, id='collector')
    scheduler.start(paused=True)
    try:
        with pytest.raises(ValueError):
            job.func()
        assert 'collector failed' in registry.snapshot()['tasks'][0]['error']
    finally:
        registry.shutdown()


def test_persisted_business_failure_not_hidden_by_successful_wrapper():
    registry = SchedulerRegistry()
    scheduler = ObservedScheduler(telemetry=registry)
    job = scheduler.add_job(lambda: None, 'interval', hours=1, id='wrapped')
    registry.probes['wrapped'] = lambda: {'last_error': 'child process failed'}
    scheduler.start(paused=True)
    try:
        job.func()
        assert registry.snapshot()['tasks'][0]['status'] == 'failed'
    finally:
        registry.shutdown()


def test_runtime_lock_excludes_second_owner_and_releases(tmp_path):
    first, second = InstanceLock(tmp_path/'owner.lock'), InstanceLock(tmp_path/'owner.lock')
    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_old_api_heartbeat_never_reports_running(tmp_path):
    payload = {'generated_at': (NOW-timedelta(seconds=31)).isoformat(), 'phase': 'ready', 'ready': True,
               'tasks': [{'name':'g3', 'status':'running', 'executor':'API'}]}
    write_snapshot(payload, tmp_path/'operations/api_tasks.json')
    result = task_board(tmp_path, {}, now=NOW)
    assert not result['api_runtime']['ready']
    assert next(x for x in result['tasks'] if x['name']=='g3')['status']=='unknown'


def test_new_missing_day_gets_own_notification(tmp_path):
    path = tmp_path/'events.db'
    check = {'name':'delivery:30m', 'ok':False, 'dates':['2026-09-08']}
    reconcile(path,[check],now=NOW)
    reconcile(path,[check],now=NOW+timedelta(minutes=31))
    sent=[]
    assert dispatch(path,lambda *args:sent.append(args),now=NOW+timedelta(minutes=31))['count']==1
    check['dates'].append('2026-09-09')
    reconcile(path,[check],now=NOW+timedelta(days=1))
    reconcile(path,[check],now=NOW+timedelta(days=1,minutes=31))
    assert dispatch(path,lambda *args:sent.append(args),now=NOW+timedelta(days=1,minutes=31))['count']==1
    assert len(sent)==2


def test_recovery_and_recurrence_keep_history(tmp_path):
    path = tmp_path/'events.db'
    reconcile(path,[{'name':'source','ok':False}],now=NOW)
    reconcile(path,[{'name':'source','ok':True}],now=NOW+timedelta(minutes=1))
    reconcile(path,[{'name':'source','ok':False}],now=NOW+timedelta(minutes=2))
    rows=read_incidents(path)
    assert len(rows)==2 and {x['status'] for x in rows}=={'observing','resolved'}
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM incident_history').fetchone()[0]==3


def test_date_aged_out_of_verifier_is_not_resolved(tmp_path):
    path=tmp_path/'events.db'
    check={'name':'daily','cells':[{'date':'2026-09-01','status':'missing'}]}
    reconcile(path,[check],now=NOW)
    reconcile(path,[{'name':'daily','cells':[{'date':'2026-09-09','status':'complete'}]}],now=NOW)
    assert read_incidents(path)[0]['status']=='observing'


def test_material_worsening_renotifies(tmp_path):
    path=tmp_path/'events.db'
    check={'name':'source','ok':False,'missing_keys':2}
    reconcile(path,[check],now=NOW)
    reconcile(path,[check],now=NOW+timedelta(minutes=31))
    dispatch(path,lambda *args:None,now=NOW+timedelta(minutes=31))
    reconcile(path,[{**check,'missing_keys':100}],now=NOW+timedelta(hours=1))
    assert dispatch(path,lambda *args:None,now=NOW+timedelta(hours=1))['count']==1


def test_batch_corruption_fails_closed(tmp_path):
    summary={'entry_date':'2026-09-09','decision_date':'2026-09-08'}
    csv='route,code,entry_date,decision_date\ninstitutional_mainwave,A,2026-09-09,2026-09-08\n'
    publish_mainwave_batch(tmp_path,summary,csv)
    assert read_mainwave_batch(tmp_path)[2]['ok']
    path=tmp_path/'operations/mainwave_batch.json'
    batch=json.loads(path.read_text(encoding='utf-8'))
    batch['payload']+=' '
    path.write_text(json.dumps(batch),encoding='utf-8')
    assert read_mainwave_batch(tmp_path)[2]['reason']=='batch_checksum_mismatch'


def test_same_day_contract_mismatch_fails_closed(tmp_path):
    publish_mainwave_batch(tmp_path,{'entry_date':'2026-09-09'},'route,code,entry_date,decision_date\n')
    path=tmp_path/'operations/mainwave_batch.json'
    batch=json.loads(path.read_text(encoding='utf-8'));batch['contract_hash']='old-contract'
    path.write_text(json.dumps(batch),encoding='utf-8')
    assert read_mainwave_batch(tmp_path)[2]['reason']=='batch_contract_mismatch'


def test_verified_batch_does_not_depend_on_legacy_files(tmp_path):
    write_snapshot({'generated_at':NOW.isoformat(),'strategy_actionable':True},tmp_path/'health/latest.json')
    publish_mainwave_batch(tmp_path,{'entry_date':'2026-09-09','decision_date':'2026-09-08'},'route,code,entry_date,decision_date\n')
    result=mainwave_daily(tmp_path,now=NOW)
    assert result['batch']['ok']
    assert next(x for x in result['checks'] if x['name']=='candidate_batch_consistency')['ok']


def test_formal_lifespan_and_health_route_with_fake_dependencies(tmp_path, monkeypatch):
    # Execute the actual entrypoint functions without importing trading/data producers.
    import ast
    import os
    import sys
    from types import SimpleNamespace
    from pathlib import Path
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services.operations import schedulers, lifecycle
    from services.operations.health import read_snapshot
    local = SchedulerRegistry()
    monkeypatch.setattr(schedulers, 'registry', local)
    monkeypatch.setattr(lifecycle, 'registry', local)
    calls=[]
    def broken():
        raise RuntimeError('injected startup failure')
    monkeypatch.setattr(lifecycle, 'startup_specs', lambda:[('broken',broken),('later',lambda:calls.append(1) or True)])
    monkeypatch.setitem(sys.modules, 'utils.database', SimpleNamespace(db=SimpleNamespace(ensure_ready_for_startup=lambda:None)))
    monkeypatch.setenv('AISTOCK_STARTUP_SCHEDULERS_ENABLED', '1')
    tree=ast.parse((Path(__file__).resolve().parents[1]/'api/main.py').read_text(encoding='utf-8'))
    functions=[x for x in tree.body if isinstance(x,ast.AsyncFunctionDef) and x.name in ('lifespan','health_check')]
    for fn in functions:
        if fn.name=='health_check':
            fn.decorator_list=[]
    namespace={'FastAPI':FastAPI,'asynccontextmanager':asynccontextmanager,'os':os,
               'runtime_path':lambda *parts:tmp_path.joinpath(*parts),'read_snapshot':read_snapshot}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'api/main.py','exec'),namespace)
    app=FastAPI(lifespan=namespace['lifespan'])
    app.get('/api/health/ready')(namespace['health_check'])
    with TestClient(app) as client:
        response=client.get('/api/health/ready')
        assert response.status_code==503
        assert response.json()['schedulers']['blockers']==['broken']
        assert calls==[1]
    assert local.phase=='stopped'
    lock=InstanceLock(tmp_path/'operations/api-owner.lock')
    lock.acquire();lock.release()


def test_empty_release_state_fails_gate(tmp_path):
    from scripts.check_release_readiness import assess
    result=assess(tmp_path,now=NOW)
    assert not result['ready']
    assert not any(x['ok'] for x in result['gates'])


def test_runtime_batch_preserves_ticket_types_and_rejects_date_mismatch(tmp_path):
    summary={'entry_date':'2026-09-09','decision_date':'2026-09-08'}
    candidates='route,code,entry_date,decision_date\n'
    tickets='code,entry_date,entry_price,m30_confirmed\nA,2026-09-09,12.5,False\n'
    publish_mainwave_batch(tmp_path,summary,candidates,tickets_csv=tickets,diagnostics_csv='route\n')
    _,_,batch=read_mainwave_batch(tmp_path,include_runtime=True)
    assert batch['ok']
    assert batch['tickets'][0]['entry_price']==12.5
    assert batch['tickets'][0]['m30_confirmed'] is False
    publish_mainwave_batch(tmp_path,summary,candidates,tickets_csv=tickets.replace('2026-09-09','2026-09-08'),diagnostics_csv='route\n')
    assert read_mainwave_batch(tmp_path,include_runtime=True)[2]['reason']=='batch_ticket_date_mismatch'


def test_missing_listing_metadata_cannot_claim_verified_coverage():
    from services.operations.delivery import build_delivery_calendar
    from types import SimpleNamespace
    class Client:
        def query(self, sql):
            if 'FROM trade_calendar' in sql:
                rows=[('2026-09-08',)]
            elif 'FROM stocks' in sql:
                rows=[('A','stock',None,None)]
            elif 'FROM kline_daily WHERE' in sql:
                rows=[('A','2026-09-08',1)]
            else:
                rows=[]
            return SimpleNamespace(result_rows=rows)
    result=build_delivery_calendar(Client(),days=1,now=NOW)
    cell=next(x for x in result['datasets'] if x['id']=='stock_daily')['cells'][0]
    assert cell['actual']==cell['expected']==1
    assert cell['status']=='unverified' and cell['missing_listing_metadata_count']==1

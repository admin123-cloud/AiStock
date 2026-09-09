import json
from datetime import datetime, timedelta
from pathlib import Path

from services.runtime_health import BUSINESS_TZ, read_snapshot, write_snapshot, strategy_data_checks, ArtifactRule, evaluate_artifact
from services.data_delivery import bar_times, expected_times, coverage_cell, build_delivery_calendar
from services.operations import mainwave_daily, task_board
from services.operations_incidents import reconcile, dispatch, read_incidents

NOW = datetime(2026, 9, 9, 10, 1, tzinfo=BUSINESS_TZ)


def test_dead_publisher_does_not_leave_a_green_snapshot(tmp_path):
    path = tmp_path/'latest.json'
    write_snapshot({'generated_at':(NOW-timedelta(minutes=16)).isoformat(), 'status':'healthy','strategy_actionable':True},path)
    result = read_snapshot(path,now=NOW)
    assert result['strategy_actionable'] is False
    assert result['reason'] == 'runtime_health_publisher_stale'


def test_future_and_missing_publisher_times_fail_closed(tmp_path):
    path=tmp_path/'latest.json'
    for payload in ({'status':'healthy'}, {'generated_at':(NOW+timedelta(hours=8)).isoformat()}):
        write_snapshot(payload,path)
        assert not read_snapshot(path,now=NOW)['strategy_actionable']


def test_zero_tickets_with_minute_conflicts_block_workflow():
    checks=strategy_data_checks({'minute_data_failure_rows':20,'qualified_shadow_buy_rows':0}, {'strategy_actionable':True})
    assert any(not x['ok'] for x in checks)
    assert next(x for x in checks if x['name']=='minute_source_visibility')['failure_count']==20


def test_fresh_file_from_wrong_day_is_not_accepted(tmp_path):
    path=tmp_path/'validation.json'
    path.write_text(json.dumps({'end_date':'2026-09-07','closed':True}),encoding='utf-8')
    result=evaluate_artifact(ArtifactRule('daily',path,3600,expected_business_date='2026-09-08'))
    assert result['reason']=='business_date_mismatch'


def test_unresolved_source_absence_is_visible_when_missing_count_is_zero(tmp_path):
    path=tmp_path/'coverage.json'
    path.write_text(json.dumps({'status':'degraded','after':{'missing_code_dates':0,'repair_backlog_code_dates':178,'source_absent_code_dates':178}}),encoding='utf-8')
    result=evaluate_artifact(ArtifactRule('daily',path,3600,require_payload_healthy=True))
    assert result['status']=='blocked'
    assert result['repair_backlog_code_dates']==178


def test_intraday_does_not_require_future_or_lunch_bars():
    assert expected_times('2026-09-09',30,NOW)==['10:00']
    assert len(bar_times(5))==48
    assert len(bar_times(30))==8
    assert '12:00' not in bar_times(30)
    assert not expected_times('2026-09-10',30,NOW)


def test_no_samples_and_not_due_are_not_100_percent():
    assert coverage_cell('2026-09-09',0,0,due=True)['coverage'] is None
    assert coverage_cell('2026-09-09',20,0,due=False)['status']=='not_due'


def test_incident_persists_deduplicates_and_resolves_only_with_positive_evidence(tmp_path):
    path=tmp_path/'incidents.db'
    failing=[{'name':'30m','ok':False,'message':'conflict'}]
    reconcile(path,failing,now=NOW)
    events=reconcile(path,failing,now=NOW+timedelta(minutes=31))
    assert len(events)==1 and events[0]['status']=='overdue'
    assert read_incidents(path)[0]['first_seen']==NOW.isoformat(timespec='seconds')
    assert reconcile(path,[],now=NOW+timedelta(minutes=32))[0]['status']=='overdue'
    assert reconcile(path,[{'name':'30m','ok':True}],now=NOW+timedelta(minutes=33))[0]['status']=='resolved'


def test_notifications_wait_for_repair_window_and_retry_without_ticket_dependency(tmp_path):
    path=tmp_path/'incidents.db'
    check=[{'name':'30m','ok':False}]
    reconcile(path,check,now=NOW)
    sent=[]
    sender=lambda *args:sent.append(args)
    assert dispatch(path,sender,now=NOW)['count']==0
    reconcile(path,check,now=NOW+timedelta(minutes=31))
    assert dispatch(path,sender,now=NOW+timedelta(minutes=31))['status']=='smtp_accepted'
    assert dispatch(path,sender,now=NOW+timedelta(hours=1))['count']==0
    assert len(sent)==1


def test_smtp_failure_is_persistent_and_rate_limited(tmp_path):
    path=tmp_path/'incidents.db'
    checks=[{'name':'daily','ok':False}]
    reconcile(path,checks,now=NOW)
    reconcile(path,checks,now=NOW+timedelta(minutes=31))
    def sender(*args): raise RuntimeError('SMTP offline')
    assert dispatch(path,sender,now=NOW+timedelta(minutes=31))['status']=='failed'
    assert dispatch(path,sender,now=NOW+timedelta(minutes=32))['count']==0
    assert read_incidents(path)[0]['attempts']==1


def test_missing_runtime_is_unknown_not_no_opportunity(tmp_path):
    result=mainwave_daily(tmp_path,now=NOW)
    assert result['state']=='data_blocked'
    assert result['contract']['entry']['minimum_score']==88


def test_stale_host_inventory_does_not_claim_tasks_running(tmp_path):
    write_snapshot({'generated_at':(NOW-timedelta(hours=1)).isoformat(),'tasks':[{'name':'collector','state':'Running','last_result':267009}]},tmp_path/'operations/host_tasks.json')
    board=task_board(tmp_path,{},now=NOW)
    assert board['tasks'][0]['status']=='unknown'


def test_paths_expand_committed_environment_defaults(monkeypatch):
    from utils import paths
    monkeypatch.delenv('AISTOCK_DATA_ROOT',raising=False)
    monkeypatch.setattr(paths,'_load_paths_config',lambda:{'data_root':'${AISTOCK_DATA_ROOT:F:\\Stock\\AiStockData\\data}'})
    assert str(paths.data_root())==str(Path(r'F:\Stock\AiStockData\data'))


def test_missing_and_duplicate_bars_do_not_cancel_each_other():
    class Result:
        def __init__(self,rows):self.result_rows=rows
    class Client:
        def query(self,sql):
            if 'FROM trade_calendar' in sql:return Result([('2026-09-08',)])
            if 'FROM stocks' in sql:return Result([('A','stock',None,None),('B','stock',None,None)])
            if 'market_status_audit' in sql:return Result([])
            if 'sector_kline_daily' in sql:return Result([])
            if 'kline_daily' in sql:return Result([('A','2026-09-08',2)])
            return Result([('A','2026-09-08',['10:00','10:00'])])
    data=build_delivery_calendar(Client(),days=1,now=NOW)
    cell=next(x for x in data['datasets'] if x['id']=='stock_30')['cells'][0]
    assert cell['expected']==16 and cell['actual']==1 and cell['missing']==15
    day=next(x for x in data['datasets'] if x['id']=='stock_daily')['cells'][0]
    assert day['expected']==2 and day['actual']==1


def test_notification_ownership_requires_live_working_notifier(tmp_path):
    from services.runtime_health import operations_notification_owner
    path=tmp_path/'latest.json'
    snapshot={'generated_at':NOW.isoformat(),'notifications_enabled':True,
              'notification_transport_ok':True,'notification':{'status':'idle'}}
    write_snapshot(snapshot,path)
    assert operations_notification_owner(path,now=NOW)
    assert not operations_notification_owner(path,now=NOW+timedelta(minutes=16))
    for key in ('notifications_enabled','notification_transport_ok'):
        write_snapshot({**snapshot,key:False},path)
        assert not operations_notification_owner(path,now=NOW)


def test_candidate_date_mismatch_is_blocked_even_with_green_health(tmp_path):
    write_snapshot({'generated_at':NOW.isoformat(),'strategy_actionable':True},tmp_path/'health/latest.json')
    write_snapshot({'entry_date':'2026-09-09','decision_date':'2026-09-08'},tmp_path/'gen3_state_alpha/latest_summary.json')
    source=tmp_path/'gen3_state_router_shadow/latest_all_source_candidates.csv'
    source.parent.mkdir()
    source.write_text('route,code,entry_date,decision_date\ninstitutional_mainwave,A,2026-09-08,2026-09-07\n',encoding='utf-8')
    assert mainwave_daily(tmp_path,now=NOW)['state']=='data_blocked'


def test_bad_failure_count_cannot_claim_minute_sources_healthy():
    checks=strategy_data_checks({'minute_data_failure_rows':'NaN'}, {'strategy_actionable':True})
    assert not next(x for x in checks if x['name']=='minute_source_visibility')['ok']


def test_calendar_api_returns_explicit_unavailable_on_verification_failure(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from api.operations_preview import app
    from api import operations
    from utils import market_warehouse
    monkeypatch.setattr(operations,'runtime_path',lambda *parts:tmp_path.joinpath(*parts))
    monkeypatch.setattr(market_warehouse,'clickhouse_client',lambda:object())
    def unavailable(*args,**kwargs):raise ValueError('calendar missing')
    monkeypatch.setattr(operations,'build_delivery_calendar',unavailable)
    operations._cache.clear()
    response=TestClient(app).get('/api/operations/data-calendar?days=10')
    assert response.status_code==503
    assert '零缺口' in response.json()['detail']

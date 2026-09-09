from datetime import datetime,timedelta
from services.operations.health import BUSINESS_TZ
from services.operations.incidents import reconcile,dispatch,read_incidents
from services.operations.remediation import request_repairs
from services.operations.source_metrics import record_source_request,read_source_metrics
from scripts.external_runtime_watchdog import poll
NOW=datetime(2026,9,9,18,tzinfo=BUSINESS_TZ)


def test_recovery_is_independent_and_deduplicated(tmp_path):
    path=tmp_path/'events.sqlite'
    reconcile(path,[{'name':'stock:2026-09-09','ok':False}],now=NOW,grace_minutes=0)
    reconcile(path,[{'name':'stock:2026-09-09','ok':False}],now=NOW)
    sent=[]
    dispatch(path,lambda *args:sent.append(args),now=NOW)
    reconcile(path,[{'name':'stock:2026-09-09','ok':True}],now=NOW)
    dispatch(path,lambda *args:sent.append(args),now=NOW)
    dispatch(path,lambda *args:sent.append(args),now=NOW)
    assert len(sent)==2
    assert '恢复' in sent[-1][0]
    assert read_incidents(path)[0]['recovery_notification']=='smtp_accepted'


def test_quiet_recovery_and_retry(tmp_path):
    path=tmp_path/'events.sqlite'
    reconcile(path,[{'name':'new','ok':False}],now=NOW)
    reconcile(path,[{'name':'new','ok':True}],now=NOW)
    assert dispatch(path,lambda *args:(_ for _ in ()).throw(AssertionError()),now=NOW)['count']==0


def test_repairs_explicit_and_unique_per_date(tmp_path):
    calendar={'datasets':[{'id':'stock_5','cells':[{'date':'2026-09-09','status':'missing'}]},
                          {'id':'index_30','cells':[{'date':'2026-09-09','status':'partial'}]}]}
    calls=[]
    enqueue=lambda *a,**k:calls.append((a,k)) or 'job'
    assert request_repairs(calendar,tmp_path,enqueue=enqueue)['status']=='not_requested'
    assert not calls
    assert len(request_repairs(calendar,tmp_path,enabled=True,enqueue=enqueue,now=NOW)['jobs'])==1
    assert not request_repairs(calendar,tmp_path,enabled=True,enqueue=enqueue,now=NOW)['jobs']
    assert len(calls)==1


def test_metrics_real_samples_and_unknown_denominator(tmp_path):
    path=tmp_path/'metrics.sqlite'
    assert read_source_metrics(path)['status']=='unknown'
    for outcome in ('success','failed','timeout'):
        record_source_request(source='qmt',operation='tick',outcome=outcome,duration_seconds=2,path=path,request_id=outcome)
    record_source_request(source='qmt',operation='tick',outcome='success',duration_seconds=2,path=path,request_id='success')
    row=read_source_metrics(path)['sources'][0]
    assert row['requests']==3 and row['failures']==1 and row['timeouts']==1
    assert row['coverage'] is None and row['persisted_at'] is None


def test_external_outage_and_recovery(tmp_path):
    sent=[]
    def fail(url): raise TimeoutError()
    path=tmp_path/'watch.sqlite'
    poll('https://example.test',path,fetch=fail,now=NOW,sender=lambda *x:sent.append(x))
    poll('https://example.test',path,fetch=fail,now=NOW+timedelta(minutes=11),sender=lambda *x:sent.append(x))
    poll('https://example.test',path,fetch=lambda url:{'ready':True},now=NOW+timedelta(minutes=12),sender=lambda *x:sent.append(x))
    assert len(sent)==2


def test_sector_denominator_requires_qmt_date_and_evidence():
    from datetime import time
    from services.operations.delivery import sector_coverage_cells
    data={'source':'qmt','verified':True,'generated_at':NOW.isoformat(),'effective_from':'2026-09-09',
          'codes':['A','B'],'exemptions':[{'code':'B','start_date':'2026-09-09','end_date':'2026-09-09','evidence_url':''}]}
    rows=sector_coverage_cells(['2026-09-08','2026-09-09'],{'2026-09-09':{'A'}},data,now=NOW,daily_deadline=time(16,10))
    assert rows[0]['expected'] is None
    assert rows[1]['status']=='partial'
    data['exemptions'][0]['evidence_url']='evidence://suspension'
    rows=sector_coverage_cells(['2026-09-09'],{'2026-09-09':{'A'}},data,now=NOW,daily_deadline=time(16,10))
    assert rows[0]['status']=='complete' and rows[0]['business_exceptions']==1


def test_g3_regeneration_acceptance_rejects_old_or_conflicted_batch(tmp_path):
    from services.operations.batches import publish_mainwave_batch
    from scripts.verify_g3_delivery import verify
    summary={'entry_date':'2026-09-10','decision_date':'2026-09-09'}
    csv='route,code,entry_date,decision_date,m30_visibility_status,m30_conflict_rows\ninstitutional_mainwave,A,2026-09-10,2026-09-09,data_conflict,1\n'
    batch=publish_mainwave_batch(tmp_path,summary,csv,tickets_csv='code,entry_date\n',diagnostics_csv='code\n')
    result=verify(tmp_path,entry_date='2026-09-10',decision_date='2026-09-09',prior_batch_id=batch)
    assert not result['ok']
    assert not next(x for x in result['checks'] if x['name']=='regenerated')['ok']
    assert not next(x for x in result['checks'] if x['name']=='main_stage_conflicts')['ok']


def test_recovery_send_failure_is_persistent_and_bounded(tmp_path):
    path=tmp_path/'events.sqlite'
    reconcile(path,[{'name':'source','ok':False}],now=NOW,grace_minutes=0)
    reconcile(path,[{'name':'source','ok':False}],now=NOW)
    dispatch(path,lambda *args:None,now=NOW)
    reconcile(path,[{'name':'source','ok':True}],now=NOW)
    sent=[]
    def fail(*args):
        sent.append(args)
        raise RuntimeError('SMTP unavailable')
    for hour in range(5):
        dispatch(path,fail,now=NOW+timedelta(hours=hour))
    assert len(sent)==3
    event=read_incidents(path)[0]
    assert event['recovery_notification']=='failed' and event['recovery_attempts']==3


def test_configured_smtp_is_not_verified_transport(tmp_path):
    from services.operations.incidents import notification_transport_status
    path=tmp_path/'incidents.sqlite'
    assert notification_transport_status(path,enabled=True,configured=True,now=NOW)['state']=='configured_unverified'
    reconcile(path,[{'name':'test','ok':False}],now=NOW,grace_minutes=0)
    reconcile(path,[{'name':'test','ok':False}],now=NOW)
    dispatch(path,lambda *args:None,now=NOW)
    assert notification_transport_status(path,enabled=True,configured=True,now=NOW)['ok']
    assert not notification_transport_status(path,enabled=True,configured=True,now=NOW+timedelta(days=2))['ok']


def test_g3_source_unavailable_cannot_pass_zero_summary(tmp_path):
    from services.operations.batches import publish_mainwave_batch
    from scripts.verify_g3_delivery import verify
    summary={'entry_date':'2026-09-10','decision_date':'2026-09-09','minute_data_failure_rows':0}
    publish_mainwave_batch(tmp_path,summary,'route,code,entry_date,decision_date,m30_source_ok\ninstitutional_mainwave,A,2026-09-10,2026-09-09,false\n',tickets_csv='code,entry_date\n',diagnostics_csv='code\n')
    assert not verify(tmp_path,entry_date='2026-09-10',decision_date='2026-09-09')['ok']


def test_index_daily_uses_fixed_index_owner_without_stock_expansion():
    from services.operations.remediation import plan_repairs
    result=plan_repairs({'datasets':[{'id':'index_daily','cells':[{'date':'2026-09-09','status':'missing'}]}]})
    assert result[0]['kind']=='index_daily'
    assert result[0]['arguments']==['--start-date','2026-09-09','--end-date','2026-09-09','--batch-size','40']


def test_sector_owner_only_receives_verified_missing_day():
    from services.operations.remediation import plan_repairs
    result=plan_repairs({'datasets':[{'id':'sector_daily','cells':[{'date':'2026-09-08','status':'unverified'}, {'date':'2026-09-09','status':'partial'}]}]})
    assert len(result)==1 and result[0]['kind']=='sector_daily'
    assert result[0]['arguments']==['--trade-date','2026-09-09']


def test_backup_new_status_file_cannot_hide_stale_backup_or_restore(tmp_path):
    from services.operations.health import backup_health,write_snapshot
    path=tmp_path/'backup.json'
    payload={'generated_at':NOW.isoformat(),'status':'healthy','host_copy_verified':True,
             'last_backup_at':(NOW-timedelta(hours=26)).isoformat(),
             'last_restore_verified_at':(NOW-timedelta(days=8)).isoformat()}
    write_snapshot(payload,path)
    health=backup_health(path,now=NOW)
    assert not health['delivery_ok']
    assert 'backup_older_than_25_hours' in health['reasons']
    assert 'restore_drill_older_than_7_days' in health['reasons']
    payload.update(last_backup_at=NOW.isoformat(),last_restore_verified_at=NOW.isoformat())
    write_snapshot(payload,path)
    assert backup_health(path,now=NOW)['delivery_ok']
    payload['status']='failed'
    write_snapshot(payload,path)
    assert backup_health(path,now=NOW)['status']=='failed'


def test_backup_expiry_notifies_and_recovery_resolves_without_auto_repair(tmp_path):
    from services.operations.health import backup_health,write_snapshot
    from services.operations.remediation import plan_repairs
    path=tmp_path/'backup.json';events=tmp_path/'events.sqlite';sent=[]
    payload={'generated_at':NOW.isoformat(),'status':'healthy','host_copy_verified':True,
             'last_backup_at':(NOW-timedelta(hours=26)).isoformat(),'last_restore_verified_at':NOW.isoformat()}
    write_snapshot(payload,path)
    def check(now):
        health=backup_health(path,now=now)
        return {'name':'backup_recovery','ok':health['delivery_ok'],'reason':','.join(health['reasons'])}
    reconcile(events,[check(NOW)],now=NOW)
    reconcile(events,[check(NOW+timedelta(minutes=31))],now=NOW+timedelta(minutes=31))
    dispatch(events,lambda *args:sent.append(args),now=NOW+timedelta(minutes=31))
    assert len(sent)==1
    payload.update(generated_at=(NOW+timedelta(minutes=32)).isoformat(),last_backup_at=NOW.isoformat())
    write_snapshot(payload,path)
    reconcile(events,[check(NOW+timedelta(minutes=32))],now=NOW+timedelta(minutes=32))
    dispatch(events,lambda *args:sent.append(args),now=NOW+timedelta(minutes=32))
    assert len(sent)==2 and read_incidents(events)[0]['status']=='resolved'
    assert not plan_repairs({'datasets':[{'id':'backup_recovery','cells':[{'date':'2026-09-09','status':'missing'}]}]})

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from services.operations.remediation import request_repairs
from services.operations.ingestion_backlog import process_one
from utils.trading_sessions import BUSINESS_TZ

NOW = datetime(2026, 9, 9, 20, 0, tzinfo=BUSINESS_TZ)


def calendar(now, actual=0, ident='stock_daily', days=('2026-09-08',)):
    return {'generated_at':now.isoformat(), 'datasets':[{'id':ident, 'cells':[
        {'date':day,'status':'complete' if actual==150 else 'partial', 'expected':150,'actual':actual}
        for day in days]}]}


def finish(root, now, execute=lambda payload: {'ok':True}):
    return process_one(execute, path=root/'operations/ingestion_backlog.sqlite', now=now)


def test_fake_cli_repairs_three_batches_then_independent_complete(tmp_path):
    # The executable models a bounded worker that succeeds after at most 60 missing keys.
    state = tmp_path/'actual.json'
    state.write_text('0')
    cli = tmp_path/'worker.py'
    cli.write_text("import json,sys; from pathlib import Path; p=Path(sys.argv[1]); n=int(p.read_text()); p.write_text(str(min(150,n+60)))")
    def execute(payload):
        assert payload['kind'] == 'daily_coverage'
        subprocess.run([sys.executable,str(cli),str(state)],check=True)
        return {'ok':True}
    result=request_repairs(calendar(NOW),tmp_path,enabled=True,now=NOW)
    assert len(result['jobs'])==1
    for batch in range(3):
        now=NOW+timedelta(minutes=10*(batch+1))
        assert finish(tmp_path,now,execute)['state']=='complete'
        actual=int(state.read_text())
        observation=calendar(now,actual)
        assert not request_repairs(observation,tmp_path,enabled=True,now=now)['jobs']
        result=request_repairs(calendar(now+timedelta(seconds=1),actual),tmp_path,enabled=True,now=now+timedelta(seconds=1))
        assert len(result['jobs'])==(0 if actual==150 else 1)
    assert state.read_text()=='150'
    assert finish(tmp_path,NOW+timedelta(hours=1))['state']=='idle'


def test_terminal_failure_unknown_and_stale_do_not_requeue(tmp_path):
    request_repairs(calendar(NOW),tmp_path,enabled=True,now=NOW)
    db=sqlite3.connect(tmp_path/'operations/ingestion_backlog.sqlite')
    for status in ('blocked','running','queued'):
        db.execute('UPDATE jobs SET state=?',(status,)); db.commit()
        assert not request_repairs(calendar(NOW),tmp_path,enabled=True,now=NOW)['jobs']
    db.execute("UPDATE jobs SET state='complete'");db.commit()
    assert not request_repairs(calendar(NOW),tmp_path,enabled=True,now=NOW)['jobs']
    assert not request_repairs(calendar(NOW),tmp_path,enabled=True,now=NOW+timedelta(minutes=1))['jobs']
    evidence=calendar(NOW+timedelta(minutes=2));evidence['datasets'][0]['cells'][0].pop('actual')
    assert not request_repairs(evidence,tmp_path,enabled=True,now=NOW+timedelta(minutes=2))['jobs']


def test_stagnation_budget_and_total_budget(tmp_path):
    request_repairs(calendar(NOW),tmp_path,enabled=True,now=NOW)
    for batch in range(3):
        now=NOW+timedelta(minutes=10*(batch+1))
        assert finish(tmp_path,now)['state']=='complete'
        request_repairs(calendar(now),tmp_path,enabled=True,now=now)
        result=request_repairs(calendar(now+timedelta(seconds=1)),tmp_path,enabled=True,now=now+timedelta(seconds=1))
        assert len(result['jobs'])==(1 if batch<2 else 0)
    assert result['held'][0]['status']=='no_progress_requires_review'
    result=request_repairs(calendar(NOW+timedelta(hours=1)),tmp_path,enabled=True,now=NOW+timedelta(hours=1),max_batches=3)
    assert not result['jobs']


def test_fair_old_dates_before_new_and_one_inflight(tmp_path):
    days=('2026-09-09','2026-09-08','2026-09-07')
    first=request_repairs(calendar(NOW,days=days),tmp_path,enabled=True,now=NOW,max_jobs=1)
    second=request_repairs(calendar(NOW,days=days),tmp_path,enabled=True,now=NOW,max_jobs=1)
    assert first['jobs'][0]['date']=='2026-09-07'
    assert second['jobs'][0]['date']=='2026-09-08'


def test_minute_continuation_uses_new_checkpoint_namespace(tmp_path):
    request_repairs(calendar(NOW,ident='stock_5'),tmp_path,enabled=True,now=NOW)
    now=NOW+timedelta(minutes=10)
    finish(tmp_path,now)
    request_repairs(calendar(now,20,ident='stock_5'),tmp_path,enabled=True,now=now)
    request_repairs(calendar(now+timedelta(seconds=1),20,ident='stock_5'),tmp_path,enabled=True,now=now+timedelta(seconds=1))
    observed=[]
    finish(tmp_path,now+timedelta(minutes=10),lambda payload:observed.append(payload) or {'ok':True})
    assert '--minute-report-dir' in observed[0]
    assert str(tmp_path/'operations/repair_batches/minute_2026-09-08/2') in observed[0]


def test_crashed_dispatch_intent_never_automatically_rearms(tmp_path):
    request_repairs(calendar(NOW),tmp_path,enabled=True,now=NOW)
    finish(tmp_path,NOW+timedelta(minutes=10))
    with sqlite3.connect(tmp_path/'operations/remediation.sqlite') as db:
        db.execute("UPDATE repairs SET status='dispatching'")
    for i in range(2):
        now=NOW+timedelta(hours=i+1)
        result=request_repairs(calendar(now),tmp_path,enabled=True,now=now)
        assert not result['jobs']
        assert result['held'][0]['status']=='requires_review'


def test_index_generation_reaches_fixed_index_cli(tmp_path):
    import argparse
    from scripts.repair_index_daily import canonical_arguments
    request_repairs(calendar(NOW,ident='index_daily'),tmp_path,enabled=True,now=NOW)
    now=NOW+timedelta(minutes=10)
    finish(tmp_path,now)
    request_repairs(calendar(now,40,ident='index_daily'),tmp_path,enabled=True,now=now)
    request_repairs(calendar(now+timedelta(seconds=1),40,ident='index_daily'),tmp_path,enabled=True,now=now+timedelta(seconds=1))
    observed=[]
    finish(tmp_path,now+timedelta(minutes=10),lambda payload:observed.append(payload) or {'ok':True})
    assert observed[0]['kind']=='index_daily'
    assert observed[0]['arguments'][-2:]==['--repair-generation','2']
    args=canonical_arguments(argparse.Namespace(start_date='2026-09-08',end_date='2026-09-08',batch_size=40,repair_generation=2))
    assert args[-2:]==['--repair-generation','2']
    assert args[args.index('--universe')+1]=='index'

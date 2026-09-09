from pathlib import Path
from datetime import datetime,timedelta
from types import SimpleNamespace
import json
import pytest
from services.operations.ingestion_backlog import enqueue,process_one,connect
from services.operations.qmt_snapshot import read_full_snapshot


def test_deferred_job_deduplicates_and_resumes(tmp_path):
    path=tmp_path/'jobs.sqlite';now=datetime(2026,9,9,10)
    assert enqueue(['--mode','date-repair'],path=path,now=now)==enqueue(['--mode','date-repair'],path=path,now=now)
    assert process_one(lambda a: {'ok':True},path=path,now=now)['state']=='deferred'
    seen=[]
    def run(args):seen.append(args);return {'ok':True}
    result=process_one(run,path=path,now=now.replace(hour=20))
    assert result['state']=='complete' and len(seen)==1
    assert process_one(run,path=path,now=now.replace(hour=21))['state']=='idle'


def test_retry_limit_and_uncertain_execution_block(tmp_path):
    path=tmp_path/'jobs.sqlite';now=datetime(2026,9,9,20)
    enqueue(['--mode','date-repair'],path=path,now=now)
    result=process_one(lambda a:{'ok':False,'error':'execution_deadline_uncertain'},path=path,now=now+timedelta(minutes=10))
    assert result['state']=='blocked'
    assert process_one(lambda a: {'ok':True},path=path,now=now+timedelta(hours=2))['state']=='idle'


def test_interrupted_running_job_does_not_duplicate_unknown_child(tmp_path):
    path=tmp_path/'jobs.sqlite';now=datetime(2026,9,9,20)
    enqueue(['--mode','date-repair'],path=path,now=now)
    c=connect(path);c.execute("UPDATE jobs SET state='running'");c.close()
    result=process_one(lambda a:{'ok':True},path=path,now=now+timedelta(minutes=10))
    assert result['state']=='idle'
    c=connect(path);assert c.execute('SELECT state FROM jobs').fetchone()[0]=='blocked';c.close()


def test_snapshot_worker_is_bounded_and_returns_payload(tmp_path):
    def runner(cmd,**kw):
        assert kw['timeout']==45
        output=Path(cmd[cmd.index('--output')+1]);output.write_text(json.dumps({'ticks':{'a':{'lastPrice':1}},'failed_batches':[]}))
        return SimpleNamespace(returncode=0,stderr='')
    assert read_full_snapshot(['a'],root=tmp_path,runner=runner)['ticks']['a']['lastPrice']==1
    assert not list(tmp_path.iterdir())


def test_daily_jobs_resume_through_fixed_entry_point(tmp_path):
    from scripts.run_ingestion_backlog import job_command
    now = datetime(2026, 9, 9, 10)
    path = tmp_path / 'jobs.sqlite'
    args = ['--mode', 'repair', '--end-date', '2026-09-08']
    daily = enqueue(args, path=path, now=now, kind='daily_coverage')
    assert daily != enqueue(args, path=path, now=now)
    seen = []
    process_one(lambda payload: seen.append(job_command(payload)) or {'ok': True},
                path=path, now=now.replace(hour=20))
    process_one(lambda payload: seen.append(job_command(payload)) or {'ok': True},
                path=path, now=now.replace(hour=20))
    assert {Path(cmd[1]).name for cmd in seen} == {'daily_kline_coverage_maintenance.py', 'qmt_xtquant_data_source_task.py'}
    assert all(cmd[2:] == args for cmd in seen)
    with pytest.raises(ValueError):
        job_command({'kind': '../../unknown.py', 'arguments': []})


def test_daily_repair_defers_before_metadata_and_stage_writes(tmp_path, monkeypatch):
    from scripts import daily_kline_coverage_maintenance as daily
    from services.operations import qmt_download_queue, ingestion_backlog
    seen = []
    monkeypatch.setattr(daily.daily, 'ch_client', lambda: object())
    monkeypatch.setattr(qmt_download_queue, 'protected_session', lambda now: True)
    monkeypatch.setattr(ingestion_backlog, 'enqueue', lambda args, **kw: seen.append((args, kw)) or 'job')
    monkeypatch.delenv('AISTOCK_BACKLOG_REPLAY', raising=False)
    args = SimpleNamespace(mode='repair', start_date='', end_date='2026-09-08', scope='latest',
                           max_repair_codes=60, batch_size=30, report=tmp_path/'latest.json',
                           tdx_root=tmp_path, cross_source_fallback=False, unit_audit=False)
    assert daily._run(args) == 75
    assert seen[0][1]['kind'] == 'daily_coverage'
    argv = seen[0][0]
    assert argv[argv.index('--start-date')+1] == '2026-09-08'
    assert '--no-cross-source-fallback' in argv and '--no-unit-audit' in argv
    assert json.loads(args.report.read_text())['status'] == 'deferred'
    monkeypatch.setenv('AISTOCK_BACKLOG_REPLAY', '1')
    assert daily._run(args) == 75
    assert len(seen) == 1

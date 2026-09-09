from pathlib import Path
from datetime import datetime,timedelta
from types import SimpleNamespace
import json
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

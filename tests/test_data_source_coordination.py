from types import SimpleNamespace
from scripts.qmt_xtquant_data_source_task import run_isolated_history
from scripts.run_reference_maintenance import run_steps


def test_history_failure_does_not_stop_later_periods(tmp_path):
    args=SimpleNamespace(minute_timeout_sec=30,minute_report_dir=str(tmp_path),minute_periods='5m,30m,60m',start_date='2026-09-01',end_date='2026-09-08',report='',retry_after_close_source_empty=True)
    seen=[]
    def runner(child):
        seen.append(child.minute_periods)
        assert child.issue_file==''
        if child.minute_periods=='30m':raise RuntimeError('table damaged')
        return {'ok':True}
    result=run_isolated_history(args,runner)
    assert seen==['5m','30m','60m']
    assert result['datasets']['60m']['ok']
    assert not result['ok']


class Manager:
    def __init__(self):self.states={}
    def get_task_status(self,name):return self.states.setdefault(name,{'is_running':False})
    def start_task(self,name,**kw):self.states[name]['is_running']=True;return True
    def set_error(self,name,error):self.states[name].update(error=error,is_running=False)


def test_reference_calendar_failure_blocks_dependent_metadata():
    manager=Manager();calls=[]
    def bad():raise RuntimeError('calendar unavailable')
    result=run_steps(manager,[('calendar',bad),('stocks',lambda:calls.append(1))])
    assert not calls
    assert result[1]['reason']=='calendar_dependency_failed'


def test_reference_requires_new_business_success_not_return_only():
    manager=Manager()
    result=run_steps(manager,[('calendar',lambda:None)])
    assert not result[0]['ok']

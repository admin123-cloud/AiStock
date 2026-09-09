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


def test_reference_success_does_not_hide_unknown_official_pool_metadata():
    manager=Manager()
    def finish(name, metadata=None):
        manager.states[name].update(is_running=False,last_success_at='fresh',results={'metadata':metadata or {}})
    result=run_steps(manager,[('calendar',lambda:finish('calendar')),
                             ('update_stock_list',lambda:finish('update_stock_list',{'metadata_verified':False}))])
    assert result[0]['ok']
    assert not result[1]['ok']
    assert result[1]['reason']=='reference_metadata_unverified'


def test_reference_requires_explicit_metadata_proof():
    for metadata, expected in [({},False),({'metadata_verified':True},True)]:
        manager=Manager()
        def finish():
            manager.states['update_stock_list'].update(is_running=False,last_success_at='fresh',results={'metadata':metadata})
        result=run_steps(manager,[('update_stock_list',finish)])
        assert result[0]['ok'] is expected


def test_task_wrapper_preserves_metadata_proof(monkeypatch):
    import ast
    import logging
    import sys
    from pathlib import Path
    tree=ast.parse((Path(__file__).parents[1]/'api/system_config.py').read_text(encoding='utf-8'))
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='update_stock_list_task')
    captured=[]
    metadata={'metadata_verified':False,'metadata_unknown_codes':['821028.BJ']}
    monkeypatch.setitem(sys.modules,'api.stocks',SimpleNamespace(update_stock_list=lambda:{'success':True,'metadata':metadata}))
    manager=SimpleNamespace(update_progress=lambda *a:None,set_results=lambda name,result:captured.append(result))
    scope={'task_manager':manager,'logger':logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'<reference-task>','exec'),scope)
    scope['update_stock_list_task']()
    assert captured[0]['metadata']==metadata

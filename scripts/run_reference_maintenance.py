"""Daily host reference-data stage, independent from market-bar ingestion."""
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from services.operations.health import write_snapshot
from services.operations.lifecycle import InstanceLock
from utils.paths import runtime_path
import json


def run_steps(manager, steps):
    results=[]
    for name, action in steps:
        if results and not results[0]['ok']:
            results.append({'name':name,'ok':False,'reason':'calendar_dependency_failed'})
            continue
        before=manager.get_task_status(name).get('last_success_at')
        if not manager.start_task(name,trigger_source='reference-maintenance'):
            results.append({'name':name,'ok':False,'reason':'already_running_or_unregistered'})
            continue
        try:
            action()
            state=manager.get_task_status(name)
            ok=bool(not state.get('is_running') and not state.get('error') and state.get('last_success_at') and state.get('last_success_at')!=before)
            results.append({'name':name,'ok':ok,'error':state.get('error') or state.get('last_error')})
        except Exception as exc:
            manager.set_error(name,str(exc))
            results.append({'name':name,'ok':False,'error':str(exc)})
    return results


def main():
    path=runtime_path('operations','reference_maintenance.json')
    lock=InstanceLock(path.with_suffix('.lock'));lock.acquire()
    try:
        today=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        old=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if old.get('date')==today and old.get('ok'):
            return 0
        from api import system_config as system
        steps=[('update_trade_calendar',system.update_trade_calendar_task),
               ('update_stock_list',system.update_stock_list_task),
               ('update_index_list',system.update_index_list_task),
               ('sync_sectors',system.sync_sectors_task)]
        results=run_steps(system.task_manager,steps)
        ok=all(x['ok'] for x in results)
        write_snapshot({'date':today,'generated_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'ok':ok,'steps':results},path)
        return 0 if ok else 2
    finally:
        lock.release()


if __name__=='__main__':raise SystemExit(main())

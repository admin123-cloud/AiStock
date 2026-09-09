"""Daily host reference-data stage, independent from market-bar ingestion."""
import sys
from pathlib import Path
from datetime import datetime, timedelta, time as wall_time
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from services.operations.health import write_snapshot
from services.operations.lifecycle import InstanceLock
from utils.paths import runtime_path
import json
import subprocess
import argparse
import os


def due_date(now):
    day = now.date() if now.time().replace(tzinfo=None) >= wall_time(18, 10) else now.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


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
            result = state.get('results') or {}
            cached = result.get('active_source') == 'local_reference_cache' or str(result.get('validation_status', '')).startswith('degraded')
            metadata = result.get('metadata') or {}
            metadata_verified = metadata.get('metadata_verified') is True if name == 'update_stock_list' else True
            ok=bool(not cached and metadata_verified and not state.get('is_running') and not state.get('error') and state.get('last_success_at') and state.get('last_success_at')!=before)
            results.append({'name':name,'ok':ok,'error':state.get('error') or state.get('last_error'),
                            'metadata_verified':metadata_verified,
                            'reason':'reference_metadata_unverified' if not metadata_verified else None})
        except Exception as exc:
            manager.set_error(name,str(exc))
            results.append({'name':name,'ok':False,'error':str(exc)})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--if-due', action='store_true')
    args = parser.parse_args()
    path=runtime_path('operations','reference_maintenance.json')
    lock=InstanceLock(path.with_suffix('.lock'));lock.acquire()
    try:
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        today = due_date(now) if args.if_due else now.date().isoformat()
        old=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if old.get('date')==today and old.get('ok') and old.get('sector_universe_verified') and old.get('reference_metadata_verified'):
            return 0
        from services.operations.qmt_download_queue import protected_session
        if args.if_due and protected_session(now):
            # Leave the last good record intact; the evening/overnight owner will retry.
            print('Reference maintenance deferred during realtime protection')
            return 75
        from api import system_config as system
        steps=[('update_trade_calendar',system.update_trade_calendar_task),
               ('update_stock_list',system.update_stock_list_task),
               ('update_index_list',system.update_index_list_task),
               ('sync_sectors',system.sync_sectors_task)]
        results=run_steps(system.task_manager,steps)
        metadata_verified = any(x['name']=='update_stock_list' and x.get('metadata_verified') for x in results)
        ok=all(x['ok'] for x in results)
        verified = False
        if ok:
            try:
                completed = subprocess.run([sys.executable, str(ROOT/'scripts/publish_qmt_sector_universe.py')],
                                           cwd=ROOT, timeout=300, capture_output=True,
                                           env={**os.environ, 'PYTHONIOENCODING':'utf-8', 'PYTHONUTF8':'1'},
                                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
                verified = completed.returncode == 0
            except subprocess.TimeoutExpired:
                write_snapshot({'source':'qmt','verified':False,'codes':[],
                                'generated_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                                'error':'reference_snapshot_timeout'},runtime_path('operations','sector_universe.json'))
            results.append({'name':'qmt_sector_universe','ok':verified})
        if not verified:
            write_snapshot({'source':'qmt','verified':False,'codes':[],
                            'generated_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                            'error':'reference_maintenance_failed','steps':results},
                           runtime_path('operations','sector_universe.json'))
        ok = ok and verified
        write_snapshot({'date':today,'generated_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'ok':ok,
                        'sector_universe_verified':verified,'reference_metadata_verified':metadata_verified,'steps':results},path)
        return 0 if ok else 2
    finally:
        lock.release()


if __name__=='__main__':raise SystemExit(main())

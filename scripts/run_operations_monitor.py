"""Single host entrypoint; publish lightweight health before slower verification."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from services.operations.lifecycle import InstanceLock
from services.operations.health import write_snapshot, BUSINESS_TZ
from utils.paths import runtime_path


def phase_commands(*, notify=False):
    return [('health', [sys.executable,'-X','utf8',str(ROOT/'scripts/publish_runtime_health.py')]),
            ('delivery', [sys.executable,'-X','utf8',str(ROOT/'scripts/publish_operations.py')]+(['--notify'] if notify else []))]


def run_phases(commands, runner=subprocess.run):
    rows=[]
    for name, command in commands:
        try:
            result=runner(command,capture_output=True,text=True,encoding='utf-8',errors='replace',
                          timeout=60 if name=='health' else 170,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            rows.append({'name':name,'ok':result.returncode==0,'exit_code':result.returncode,
                         'error':result.stderr[-1500:] if result.returncode else None})
        except Exception as exc:
            rows.append({'name':name,'ok':False,'error':f'{type(exc).__name__}: {exc}'})
    return rows


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--notify',action='store_true')
    parser.add_argument('--plan',action='store_true')
    args=parser.parse_args()
    if args.plan:
        print(json.dumps({'steps':[name for name,_ in phase_commands(notify=args.notify)],'notify':args.notify,'executed':False}))
        return 0
    lock=InstanceLock(runtime_path('operations','monitor-service.lock'))
    lock.acquire()
    try:
        rows=run_phases(phase_commands(notify=args.notify))
        write_snapshot({'generated_at':datetime.now(BUSINESS_TZ).isoformat(), 'steps':rows,
                        'notifications_requested':args.notify}, runtime_path('operations','monitor_service.json'))
        return 0 if all(x['ok'] for x in rows) else 2
    finally:
        lock.release()


if __name__=='__main__':
    raise SystemExit(main())

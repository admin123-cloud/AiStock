"""Resume one deferred canonical job during a host maintenance window."""
import os
import sys
import subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from services.operations.ingestion_backlog import process_one
from services.operations.lifecycle import InstanceLock
from services.operations.health import write_snapshot, BUSINESS_TZ
from utils.paths import runtime_path
from datetime import datetime


def job_command(payload):
    kind = 'ingestion' if isinstance(payload, list) else payload.get('kind') if isinstance(payload, dict) else None
    arguments = payload if isinstance(payload, list) else payload.get('arguments') if isinstance(payload, dict) else None
    scripts = {'ingestion': 'qmt_xtquant_data_source_task.py', 'daily_coverage': 'daily_kline_coverage_maintenance.py'}
    if kind not in scripts or not isinstance(arguments, list) or not all(isinstance(a, str) for a in arguments):
        raise ValueError('Unsupported persisted ingestion job')
    return [sys.executable, str(ROOT / 'scripts' / scripts[kind]), *arguments]


def execute(arguments):
    command = job_command(arguments)
    env=dict(os.environ,AISTOCK_BACKLOG_REPLAY='1')
    proc=subprocess.Popen(command,
                          cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        stdout,stderr=proc.communicate(timeout=900)
    except subprocess.TimeoutExpired:
        if os.name=='nt':
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW,timeout=15)
        else:
            proc.kill()
        return {'ok':False,'error':'execution_deadline_uncertain: stopped owned worker tree; verify checkpoint before retry'}
    return {'ok':proc.returncode==0,'exit_code':proc.returncode,'error':(stderr+'\n'+stdout)[-2000:] if proc.returncode else None}



def main():
    lock=InstanceLock(runtime_path('operations','ingestion-backlog.lock'));lock.acquire()
    try:
        result=process_one(execute)
        write_snapshot({'generated_at':datetime.now(BUSINESS_TZ).isoformat(),**result},runtime_path('operations','ingestion_backlog.json'))
        return 2 if result['state']=='blocked' else 0
    finally:lock.release()


if __name__=='__main__':raise SystemExit(main())

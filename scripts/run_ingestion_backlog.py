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


def execution_budget(command):
    def option(name, default):
        if name not in command:
            return default
        return max(1, int(command[command.index(name) + 1]))
    # The cooperative 15-minute slice is checked BETWEEN durable phases. Allow
    # the current declared phase to finish; never kill a 90-minute phase at 15m.
    if Path(command[1]).name == 'daily_kline_coverage_maintenance.py':
        return 7200  # bounded security batch; current entry has no phase timeout flag
    phase = max(option('--daily-timeout-sec', 5400), option('--minute-timeout-sec', 7200))
    return 900 + phase + 120


def execute(arguments):
    import time
    from services.operations.ingestion_budget import run_owned
    command = job_command(arguments)
    env = dict(os.environ, AISTOCK_BACKLOG_REPLAY='1', AISTOCK_INGESTION_YIELD_AT=str(time.time()+900))
    result = run_owned(command, execution_budget(command), cwd=ROOT, env=env)
    error = result.get('error') or (result.get('stderr_tail', '')+'\n'+result.get('stdout_tail', ''))[-4000:]
    if result.get('returncode') == 76:
        error = 'execution_deadline_uncertain: ' + error
    elif result.get('returncode') == 77:
        error = 'storage_blocked_requires_recovery: ' + error
    return {**result, 'error': error if not result.get('ok') else None}



def main():
    lock=InstanceLock(runtime_path('operations','ingestion-backlog.lock'));lock.acquire()
    try:
        result=process_one(execute)
        write_snapshot({'generated_at':datetime.now(BUSINESS_TZ).isoformat(),**result},runtime_path('operations','ingestion_backlog.json'))
        return 2 if result['state']=='blocked' else 0
    finally:lock.release()


if __name__=='__main__':raise SystemExit(main())

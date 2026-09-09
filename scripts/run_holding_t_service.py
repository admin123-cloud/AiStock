"""One scheduled entrypoint for the existing intraday and 16:00 review steps."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.paths import runtime_path
from utils.trading_sessions import BUSINESS_TZ
from services.operations.lifecycle import InstanceLock
from services.operations.health import write_snapshot


def phase_at(now):
    if now.weekday() >= 5:
        return None
    if now.time() >= time(16):
        return 'review'
    if time(9,30) <= now.time() <= time(15,1):
        return 'monitor'
    return None


def command_for(phase, source_root, executable=sys.executable):
    entry = 'run_g3_holding_t_daily_review.py' if phase == 'review' else 'run_g3_holding_t_paper_monitor.py'
    return [executable, '-X', 'utf8', str(source_root/'scripts'/entry)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--plan', action='store_true')
    args = parser.parse_args()
    now = datetime.now(BUSINESS_TZ)
    phase = phase_at(now)
    if not phase or args.plan:
        print(json.dumps({'phase':phase, 'executed':False, 'source_root':str(args.source_root)}, ensure_ascii=False))
        return 0
    lock = InstanceLock(runtime_path('operations','holding-service.lock'))
    lock.acquire()
    status_path = runtime_path('operations','holding_service.json')
    state = {'generated_at':now.isoformat(), 'phase':phase, 'status':'running', 'source_root':str(args.source_root)}
    try:
        write_snapshot(state,status_path)
        completed = subprocess.run(command_for(phase,args.source_root), capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=1800,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            result = json.loads(completed.stdout)
        except (ValueError,TypeError):
            result = {}
        ok = completed.returncode == 0 and result.get('ok') is True
        state.update(status='complete' if ok else 'failed', result=result,
                     exit_code=completed.returncode, error=completed.stderr[-1500:] if not ok else None)
        return 0 if ok else 2
    except Exception as exc:
        state.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        return 2
    finally:
        state['generated_at'] = datetime.now(BUSINESS_TZ).isoformat()
        try:
            write_snapshot(state,status_path)
        finally:
            lock.release()


if __name__ == '__main__':
    raise SystemExit(main())

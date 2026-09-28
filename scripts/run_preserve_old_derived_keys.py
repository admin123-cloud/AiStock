"""Bounded, resumable driver for independent derived-key preservation."""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from services.operations.health import write_snapshot, BUSINESS_TZ
from services.operations.lifecycle import InstanceLock
from utils.paths import runtime_path


def months_between(start,end):
    left=datetime.strptime(start,'%Y%m'); right=datetime.strptime(end,'%Y%m')
    if left>right or left.strftime('%Y%m')!=start or right.strftime('%Y%m')!=end:
        raise ValueError('invalid ordered YYYYMM range')
    current=left.year*12+left.month-1; stop=right.year*12+right.month-1
    if stop-current>240:raise ValueError('range exceeds 241 months')
    return [f'{i//12:04d}{i%12+1:02d}' for i in range(current,stop+1)]


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--start',default='202001');p.add_argument('--end',required=True)
    p.add_argument('--periods',nargs='+',type=int,choices=(15,30,60),default=[15,30,60])
    p.add_argument('--run-id',default='r20260909_stable2')
    p.add_argument('--output',type=Path,default=runtime_path('operations','derived_recovery','preservation','driver.json'))
    a=p.parse_args(); months=months_between(a.start,a.end)
    lock=InstanceLock(runtime_path('operations','derived-preservation.lock'));lock.acquire()
    try:
        previous=json.loads(a.output.read_text(encoding='utf-8')) if a.output.exists() else {}
        if previous.get('in_flight') or previous.get('state')=='uncertain':
            raise RuntimeError('prior execution outcome unresolved; independently reconcile before another run')
        report={'state':'running','run_id':a.run_id,'range':[a.start,a.end],'results':[],'in_flight':None}
        write_snapshot(report,a.output)
        for month in months:
            for period in dict.fromkeys(a.periods):
                report['in_flight']={'month':month,'period':period};write_snapshot(report,a.output)
                try:
                    result=subprocess.run([sys.executable,str(ROOT/'scripts/preserve_old_derived_keys.py'),
                        '--month',month,'--period',str(period),'--run-id',a.run_id],
                        capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=600,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                except Exception as exc:
                    report.update(state='uncertain',error=str(exc));write_snapshot(report,a.output);raise
                report['results'].append({**report['in_flight'],'returncode':result.returncode,
                    'stdout':result.stdout[-3000:],'stderr':result.stderr[-3000:]})
                report['in_flight']=None;write_snapshot(report,a.output)
        failed=sum(r['returncode']!=0 for r in report['results'])
        report.update(state='completed_with_errors' if failed else 'verified',failed=failed,
                      completed_at=datetime.now(BUSINESS_TZ).isoformat())
        write_snapshot(report,a.output)
        return 2 if failed else 0
    finally:lock.release()


if __name__=='__main__':raise SystemExit(main())

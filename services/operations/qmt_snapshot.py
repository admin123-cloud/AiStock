"""Bound the readonly SDK call separately from the long-lived subscription."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from services.operations.ingestion_store import observe
from utils.paths import runtime_path


def _read_full_snapshot(codes,batch_size=500,timeout=45,*,runner=subprocess.run,root=None):
    root=root or runtime_path('operations','snapshot_workers')
    root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        request=Path(directory)/'request.json';output=Path(directory)/'response.json'
        request.write_bytes(json.dumps({'codes':codes,'batch_size':batch_size}).encode())
        started=time.monotonic()
        result=runner([sys.executable,str(Path(__file__).resolve().parents[2]/'scripts/qmt_snapshot_worker.py'),
                       '--request',str(request),'--output',str(output)],capture_output=True,text=True,
                      encoding='utf-8',errors='replace',timeout=timeout,
                      env=dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1'),
                      creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:
            raise RuntimeError('Snapshot SDK worker failed: '+result.stderr[-1200:])
        value=json.loads(output.read_text(encoding='utf-8'))
        if not isinstance(value.get('ticks'),dict):raise ValueError('Invalid snapshot worker response')
        if runner is subprocess.run and root == runtime_path('operations','snapshot_workers'):
            source_times = []
            for tick in value['ticks'].values():
                try:
                    text = str(int(tick['time']))
                    if len(text) == 13:
                        source_times.append(datetime.fromtimestamp(int(text)/1000, ZoneInfo('Asia/Shanghai')))
                    elif len(text) == 14:
                        source_times.append(datetime.strptime(text, '%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('Asia/Shanghai')))
                except (ValueError, KeyError, TypeError, OverflowError, OSError):
                    continue
            observe(source='qmt',operation='full_tick_round',duration_seconds=time.monotonic()-started,
                    outcome='failed' if value.get('failed_batches') else 'success',requested=len(codes),received=len(value['ticks']),
                    source_at=max(source_times).isoformat() if source_times else None)
        return value


def read_full_snapshot(codes, batch_size=500, timeout=45, *, runner=subprocess.run, root=None):
    started = time.monotonic()
    try:
        return _read_full_snapshot(codes, batch_size, timeout, runner=runner, root=root)
    except Exception as exc:
        if runner is subprocess.run and root is None:
            observe(source='qmt', operation='full_tick_round', duration_seconds=time.monotonic()-started,
                    outcome='timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'failed',
                    requested=len(codes), received=0)
        raise

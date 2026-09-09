"""Bound the readonly SDK call separately from the long-lived subscription."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from utils.paths import runtime_path


def read_full_snapshot(codes,batch_size=500,timeout=45,*,runner=subprocess.run,root=None):
    root=root or runtime_path('operations','snapshot_workers')
    root.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as directory:
        request=Path(directory)/'request.json';output=Path(directory)/'response.json'
        request.write_bytes(json.dumps({'codes':codes,'batch_size':batch_size}).encode())
        result=runner([sys.executable,str(Path(__file__).resolve().parents[2]/'scripts/qmt_snapshot_worker.py'),
                       '--request',str(request),'--output',str(output)],capture_output=True,text=True,
                      encoding='utf-8',errors='replace',timeout=timeout,
                      creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode:
            raise RuntimeError('Snapshot SDK worker failed: '+result.stderr[-1200:])
        value=json.loads(output.read_text(encoding='utf-8'))
        if not isinstance(value.get('ticks'),dict):raise ValueError('Invalid snapshot worker response')
        return value

"""Host-wide cooperative history download queue; realtime reads bypass it."""
from datetime import datetime, time as wall_time
import hashlib
import json
import sqlite3
import time
import uuid
from zoneinfo import ZoneInfo
from services.operations.lifecycle import InstanceLock
from utils.paths import runtime_path


class DownloadDeferred(RuntimeError):
    pass


def protected_session(now):
    # Conservatively protect weekdays including auction and close buffers.
    return now.weekday() < 5 and wall_time(9, 15) <= now.time().replace(tzinfo=None) <= wall_time(15, 15)


def request_priority(codes, start, now):
    return 10 if len(codes) <= 20 and str(start).replace('-', '')[:8] == now.strftime('%Y%m%d') else 50


def coordinated_download(codes, period, start, end, action, *, root=None, wait_seconds=120, now=None, variant=None):
    metrics_path = root/'source_metrics.sqlite' if root is not None else None
    fixed_now = now
    now = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    priority = request_priority(codes, start, now)
    if protected_session(now) and priority > 10:
        raise DownloadDeferred('Historical bulk download deferred during protected realtime session')
    root = root or runtime_path('operations', 'qmt_downloads')
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps([sorted(set(codes)),period,start,end,variant]).encode()).hexdigest()
    request_id = uuid.uuid4().hex
    created = time.time()
    deadline = time.monotonic()+max(0,wait_seconds)
    conn = sqlite3.connect(root/'queue.sqlite', timeout=10, isolation_level=None)
    conn.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, request_key TEXT, priority INTEGER, created REAL, heartbeat REAL, state TEXT, finished REAL, error TEXT)')
    conn.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,NULL,NULL)',(request_id,key,priority,created,created,'waiting'))
    lock = InstanceLock(root/'download.lock')
    acquired = False
    try:
        while True:
            current = fixed_now or datetime.now(ZoneInfo('Asia/Shanghai'))
            if protected_session(current) and priority > 10:
                raise DownloadDeferred('Queued bulk download deferred at realtime session boundary')
            clock = time.time()
            conn.execute("UPDATE requests SET state='abandoned' WHERE state='waiting' AND heartbeat<?",(clock-30,))
            conn.execute('UPDATE requests SET heartbeat=? WHERE id=?',(clock,request_id))
            done = conn.execute("SELECT 1 FROM requests WHERE request_key=? AND state='complete' AND finished>=? LIMIT 1",(key,created)).fetchone()
            if done:
                conn.execute("UPDATE requests SET state='coalesced',finished=? WHERE id=?",(clock,request_id))
                return None
            first = conn.execute("SELECT id FROM requests WHERE state='waiting' ORDER BY priority,created,id LIMIT 1").fetchone()
            if first and first[0] == request_id:
                try:
                    lock.acquire()
                    acquired = True
                except RuntimeError:
                    pass
                if acquired:
                    # A different caller may have completed between the checks and lock acquisition.
                    done = conn.execute("SELECT 1 FROM requests WHERE request_key=? AND state='complete' AND finished>=? LIMIT 1",(key,created)).fetchone()
                    if done:
                        conn.execute("UPDATE requests SET state='coalesced',finished=? WHERE id=?",(time.time(),request_id))
                        return None
                    conn.execute("UPDATE requests SET state='abandoned',finished=? WHERE state='running'",(clock,))
                    conn.execute("UPDATE requests SET state='running',heartbeat=? WHERE id=?",(clock,request_id))
                    from services.operations.ingestion_store import observe
                    sdk_started = time.monotonic()
                    try:
                        result = action()
                    except Exception as exc:
                        observe(source='qmt', operation='history_download:'+period,
                                duration_seconds=time.monotonic()-sdk_started,
                                outcome='timeout' if isinstance(exc, TimeoutError) else 'failed', requested=len(codes), path=metrics_path)
                        raise
                    observe(source='qmt', operation='history_download:'+period,
                            duration_seconds=time.monotonic()-sdk_started, outcome='success', requested=len(codes), path=metrics_path)
                    conn.execute("UPDATE requests SET state='complete',finished=? WHERE id=?",(time.time(),request_id))
                    return result
            if time.monotonic() >= deadline:
                raise DownloadDeferred('QMT history download queue wait deadline exceeded')
            time.sleep(0.1)
    except Exception as exc:
        conn.execute("UPDATE requests SET state='failed',finished=?,error=? WHERE id=?",(time.time(),str(exc)[:1200],request_id))
        raise
    finally:
        if acquired:
            lock.release()
        conn.close()

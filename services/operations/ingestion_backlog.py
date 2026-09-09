"""Durable retry of canonical ingestion jobs, not arbitrary shell commands."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import hashlib
import json
import sqlite3
from utils.paths import runtime_path
from services.operations.qmt_download_queue import protected_session


def connect(path=None):
    path=path or runtime_path('operations','ingestion_backlog.sqlite')
    path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(path,timeout=10,isolation_level=None)
    conn.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, args TEXT, state TEXT, attempts INTEGER, due REAL, updated REAL, error TEXT)')
    return conn


def enqueue(arguments,*,path=None,now=None,kind='ingestion'):
    if kind not in ('ingestion', 'daily_coverage'):
        raise ValueError('Unsupported ingestion job kind')
    if not isinstance(arguments, list) or not all(isinstance(a, str) for a in arguments):
        raise ValueError('Job arguments must be a list of strings')
    now=now or datetime.now(ZoneInfo('Asia/Shanghai'))
    # Preserve legacy canonical job identities and rows while adding a fixed second entry point.
    payload=json.dumps(arguments if kind == 'ingestion' else {'kind':kind,'arguments':arguments},ensure_ascii=False)
    key=hashlib.sha256(payload.encode()).hexdigest()
    due=now+timedelta(minutes=5)
    if protected_session(now):due=now.replace(hour=15,minute=20,second=0,microsecond=0)
    conn=connect(path)
    try:
        conn.execute("INSERT INTO jobs VALUES (?,?,'queued',0,?,?,NULL) ON CONFLICT(id) DO UPDATE SET updated=excluded.updated",(key,payload,due.timestamp(),now.timestamp()))
    finally:
        conn.close()
    return key


def process_one(execute,*,path=None,now=None):
    now=now or datetime.now(ZoneInfo('Asia/Shanghai'))
    if protected_session(now):return {'state':'deferred','reason':'realtime_session'}
    # Caller owns a process lock. A prior parent's death does not prove its child stopped.
    conn=connect(path)
    try:
        conn.execute("UPDATE jobs SET state='blocked',error='interrupted_execution_requires_checkpoint_review' WHERE state='running'")
        row=conn.execute("SELECT id,args,attempts FROM jobs WHERE state='queued' AND due<=? ORDER BY due,id LIMIT 1",(now.timestamp(),)).fetchone()
        if not row:return {'state':'idle'}
        key,payload,attempts=row
        attempts+=1
        conn.execute("UPDATE jobs SET state='running',attempts=?,updated=? WHERE id=?",(attempts,now.timestamp(),key))
        try:
            result=execute(json.loads(payload))
        except Exception as exc:result={'ok':False,'error':str(exc)}
        error=str(result.get('error') or result.get('reason') or '')
        from services.operations.ingestion_checkpoint import storage_failure
        blocked=storage_failure(error) or 'storage_blocked_requires_recovery' in error or 'execution_deadline_uncertain' in error
        deferred = bool(result.get('deferred')) and not blocked
        if deferred:
            attempts -= 1  # A clean boundary yield is scheduling, not a failed attempt.
        state='complete' if result.get('ok') else 'queued' if deferred else 'blocked' if blocked or attempts>=3 else 'queued'
        conn.execute('UPDATE jobs SET state=?,attempts=?,due=?,updated=?,error=? WHERE id=?',
                     (state,attempts,(now+timedelta(minutes=max(5,15*attempts))).timestamp(),now.timestamp(),error[:2000],key))
        return {'id':key,'state':state,'attempts':attempts,'result':result}
    finally:
        conn.close()

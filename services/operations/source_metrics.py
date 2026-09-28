"""Persist observed source calls independently from market-data writes."""
import json
import math
import sqlite3
from datetime import datetime, timedelta
from uuid import uuid4
from utils.paths import runtime_path
from utils.trading_sessions import BUSINESS_TZ


def record_source_request(*, source, operation, duration_seconds, outcome, requested=None,
                          received=None, source_at=None, persisted_at=None, request_id=None, path=None):
    if outcome not in ('success', 'failed', 'timeout'):
        raise ValueError('Unsupported request outcome')
    if not math.isfinite(float(duration_seconds)) or duration_seconds < 0:
        raise ValueError('Invalid duration')
    path = path or runtime_path('operations', 'source_metrics.sqlite')
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(BUSINESS_TZ)
    payload = dict(source=source,operation=operation,duration_seconds=duration_seconds,
                   outcome=outcome,requested=requested,received=received,source_at=source_at,
                   persisted_at=persisted_at,recorded_at=now.isoformat())
    with sqlite3.connect(path, timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, at REAL, payload TEXT)')
        db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?)',
                   (request_id or uuid4().hex, now.timestamp(), json.dumps(payload,default=str)))
        db.execute('DELETE FROM requests WHERE at<?', ((now-timedelta(days=30)).timestamp(),))


def read_source_metrics(path=None, *, now=None, hours=24):
    path = path or runtime_path('operations', 'source_metrics.sqlite')
    now = now or datetime.now(BUSINESS_TZ)
    if not path.exists():
        return {'status':'unknown','window_hours':hours,'sources':[]}
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as db:
            rows = [json.loads(x[0]) for x in db.execute('SELECT payload FROM requests WHERE at>=? ORDER BY at',
                    ((now-timedelta(hours=hours)).timestamp(),))]
    except (sqlite3.Error, ValueError, OSError):
        return {'status':'unknown','window_hours':hours,'sources':[],'reason':'metrics_unreadable'}
    groups = {}
    for row in rows:
        groups.setdefault((row['source'],row['operation']),[]).append(row)
    sources = []
    for (source,operation), values in groups.items():
        durations = sorted(float(v['duration_seconds']) for v in values)
        last = values[-1]
        sources.append(dict(source=source,operation=operation,requests=len(values),
            failures=sum(v['outcome']=='failed' for v in values),
            timeouts=sum(v['outcome']=='timeout' for v in values),
            success_rate=sum(v['outcome']=='success' for v in values)/len(values),
            p95_seconds=durations[max(0,math.ceil(len(durations)*.95)-1)],
            last_request_at=last['recorded_at'],source_at=last.get('source_at'),
            persisted_at=last.get('persisted_at'),requested=last.get('requested'),received=last.get('received'),
            coverage=None if not last.get('requested') or last.get('received') is None else min(1,last['received']/last['requested']),
            coverage_scope='本次请求返回覆盖；有效证券与豁免需独立验收'))
    return {'status':'observed' if sources else 'unknown','window_hours':hours,'sources':sources}

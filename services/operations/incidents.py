"""Durable incidents: business-date identity, episodes, escalation and serialized dispatch."""
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from services.operations.health import BUSINESS_TZ
from services.operations.lifecycle import InstanceLock


class NotificationOutcomeUncertain(RuntimeError):
    """Delivery may have happened; automatic retries could duplicate accepted mail."""


def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS incidents (key TEXT PRIMARY KEY, status TEXT, first_seen TEXT, last_seen TEXT, deadline TEXT, resolved_at TEXT, payload TEXT, notification TEXT, attempts INTEGER DEFAULT 0, last_attempt TEXT, error TEXT)')
    columns = {x[1] for x in db.execute('PRAGMA table_info(incidents)')}
    for name in ('group_key', 'fingerprint', 'recovery_notification', 'recovery_attempt_at'):
        if name not in columns:
            db.execute(f'ALTER TABLE incidents ADD COLUMN {name} TEXT')
    if 'recovery_attempts' not in columns:
        db.execute('ALTER TABLE incidents ADD COLUMN recovery_attempts INTEGER DEFAULT 0')
    db.execute('UPDATE incidents SET group_key=key WHERE group_key IS NULL')
    db.execute('CREATE TABLE IF NOT EXISTS incident_history (id INTEGER PRIMARY KEY, event_key TEXT, at TEXT, status TEXT, payload TEXT)')


def _checks(checks):
    for check in checks:
        # Only explicitly checked dates can resolve; aged-out dates are never inferred healthy.
        cells = check.get('cells')
        if cells is not None:
            for cell in cells:
                if cell['status'] == 'not_due':
                    continue
                yield {**check, 'name': check['name']+':'+cell['date'], 'cells': None,
                       'business_date': cell['date'], 'ok': cell['status'] == 'complete',
                       'reason': cell['status'], 'missing_keys': cell.get('missing'),
                       'message': check.get('message', check['name'])+' / '+cell['date']}
        elif check.get('dates'):
            for day in check['dates']:
                yield {**check, 'name':check['name']+':'+day, 'business_date':day}
        else:
            yield check


def _fingerprint(check):
    missing = max(0, int(check.get('missing_keys') or 0))
    # Escalate orders of magnitude, not every small fluctuation.
    material = [check.get('reason'), int(math.log2(missing)) if missing else 0]
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()


def reconcile(path: Path, checks: list[dict], *, now=None, grace_minutes=30):
    now = now or datetime.now(BUSINESS_TZ)
    stamp = now.isoformat(timespec='seconds')
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        _schema(db)
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        for check in checks:
            if check.get('cells') is not None:
                # Preserve old aggregate incidents as history when date-scoped checks take over.
                db.execute("UPDATE incidents SET status='superseded',last_seen=? WHERE group_key=? AND status NOT IN ('resolved','superseded')", (stamp,check['name']))
        for check in _checks(checks):
            group = check['name']
            payload = json.dumps(check, ensure_ascii=False)
            prior = db.execute("SELECT key,status,deadline,fingerprint,payload FROM incidents WHERE group_key=? AND status NOT IN ('resolved','superseded') ORDER BY first_seen DESC LIMIT 1", (group,)).fetchone()
            if check.get('ok') is True:
                if prior:
                    db.execute("UPDATE incidents SET status='resolved',resolved_at=?,last_seen=?,payload=?,recovery_notification=CASE WHEN notification='smtp_accepted' THEN 'waiting' ELSE 'not_required' END WHERE key=?", (stamp,stamp,payload,prior[0]))
                    db.execute('INSERT INTO incident_history(event_key,at,status,payload) VALUES(?,?,?,?)', (prior[0],stamp,'resolved',payload))
                continue
            fingerprint = _fingerprint(check)
            if prior is None:
                key = group+'#'+uuid4().hex[:12]
                deadline = (now+timedelta(minutes=grace_minutes)).isoformat(timespec='seconds')
                db.execute("INSERT INTO incidents(key,group_key,status,first_seen,last_seen,deadline,payload,notification,attempts,fingerprint) VALUES(?,?,'observing',?,?,?,?,'waiting',0,?)", (key,group,stamp,stamp,deadline,payload,fingerprint))
                db.execute('INSERT INTO incident_history(event_key,at,status,payload) VALUES(?,?,?,?)', (key,stamp,'observing',payload))
            else:
                status = 'overdue' if now >= datetime.fromisoformat(prior[2]) else 'observing'
                old = json.loads(prior[4])
                worsened = fingerprint != prior[3] and (check.get('reason') != old.get('reason') or (check.get('missing_keys') or 0) > (old.get('missing_keys') or 0))
                db.execute('UPDATE incidents SET status=?,last_seen=?,payload=? WHERE key=?', (status,stamp,payload,prior[0]))
                if worsened:
                    db.execute("UPDATE incidents SET fingerprint=?,notification=CASE WHEN notification IN ('sending','uncertain') THEN 'uncertain' ELSE 'waiting' END,attempts=CASE WHEN notification IN ('sending','uncertain') THEN attempts ELSE 0 END,last_attempt=CASE WHEN notification IN ('sending','uncertain') THEN last_attempt ELSE NULL END WHERE key=?", (fingerprint,prior[0]))
                if worsened or status != prior[1]:
                    db.execute('INSERT INTO incident_history(event_key,at,status,payload) VALUES(?,?,?,?)', (prior[0],stamp,'escalated' if worsened else status,payload))
    return read_incidents(path)


def read_incidents(path: Path, *, limit=200):
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        query = 'SELECT * FROM incidents ORDER BY last_seen DESC, rowid DESC'
        rows = [dict(row) for row in db.execute(query + (' LIMIT ?' if limit else ''), (limit,) if limit else ())]
    for row in rows:
        row['detail'] = json.loads(row.pop('payload'))
    return rows


def _dispatch_failures(path: Path, sender, *, now=None):
    now = now or datetime.now(BUSINESS_TZ)
    if not path.exists():
        return {'status':'idle','count':0}
    lock = InstanceLock(path.with_suffix('.send.lock'))
    try:
        lock.acquire()
    except RuntimeError:
        return {'status':'busy','count':0}
    try:
        # Holding the send lock proves any previous sending marker has no active sender.
        # SMTP may have accepted before the process stopped; never guess and resend.
        with sqlite3.connect(path) as db:
            db.execute("UPDATE incidents SET notification='uncertain',error='smtp_outcome_requires_review' WHERE notification='sending'")
            db.execute("UPDATE incidents SET recovery_notification='uncertain',error='smtp_outcome_requires_review' WHERE recovery_notification='sending'")
        due = [x for x in read_incidents(path,limit=None) if x['status']=='overdue'
               and x['notification'] in ('waiting','failed') and x['attempts']<3
               and (not x['last_attempt'] or now-datetime.fromisoformat(x['last_attempt'])>=timedelta(minutes=30))]
        if not due:
            return {'status':'idle','count':0}
        with sqlite3.connect(path) as db:
            for item in due:
                db.execute("UPDATE incidents SET attempts=attempts+1,last_attempt=?,notification='sending' WHERE key=? AND fingerprint IS ?", (now.isoformat(timespec='seconds'),item['key'],item.get('fingerprint')))
        try:
            sender('AiStock 数据交付异常：恢复窗口已超时', '\n\n'.join(
                f"{x['detail']['name']}\n首次发现：{x['first_seen']}\n恢复窗口：{x['deadline']}\n说明：{x['detail'].get('message') or x['detail'].get('reason')}\n责任：{x['detail'].get('remediation_owner') or '运行中心'}" for x in due))
            status,error = 'smtp_accepted',None
        except Exception as exc:
            status,error = ('uncertain' if isinstance(exc,NotificationOutcomeUncertain) else 'failed'),f'{type(exc).__name__}: {exc}'
        with sqlite3.connect(path) as db:
            for item in due:
                db.execute('UPDATE incidents SET notification=?,error=? WHERE key=? AND fingerprint IS ?', (status,error,item['key'],item.get('fingerprint')))
                db.execute('INSERT INTO incident_history(event_key,at,status,payload) VALUES(?,?,?,?)', (item['key'],now.isoformat(timespec='seconds'),status,json.dumps({'error':error},ensure_ascii=False)))
        return {'status':status,'count':len(due)}
    finally:
        lock.release()


def notification_configured() -> bool:
    from utils.config import config
    cfg = config.get('email', {}) or {}
    return cfg.get('enabled', True) is not False and bool(
        cfg.get('to_emails') and cfg.get('smtp_server') and cfg.get('from_email'))


def send_digest(subject: str, body: str) -> None:
    import smtplib
    from email.message import EmailMessage
    from utils.config import config
    cfg = config.get('email', {}) or {}
    if cfg.get('enabled', True) is False:
        raise RuntimeError('email_explicitly_disabled')
    recipients = cfg.get('to_emails') or []
    if isinstance(recipients, str):
        recipients = [recipients]
    if not recipients or not cfg.get('smtp_server') or not cfg.get('from_email'):
        raise RuntimeError('email_configuration_incomplete')
    msg = EmailMessage()
    msg['Subject'], msg['From'], msg['To'] = subject, cfg['from_email'], ', '.join(recipients)
    msg.set_content(body)
    port = int(cfg.get('smtp_port') or 465)
    ssl = cfg.get('use_ssl', False) or port == 465
    with (smtplib.SMTP_SSL if ssl else smtplib.SMTP)(cfg['smtp_server'], port, timeout=20) as server:
        if not ssl:
            server.starttls()
        if cfg.get('smtp_user'):
            server.login(cfg['smtp_user'], cfg.get('smtp_password') or '')
        try:
            refused = server.send_message(msg)
        except (smtplib.SMTPServerDisconnected, TimeoutError, ConnectionError) as exc:
            raise NotificationOutcomeUncertain('smtp_data_outcome_unknown') from exc
        if refused:
            raise NotificationOutcomeUncertain('some_recipients_accepted_others_refused')


def dispatch(path: Path, sender, *, now=None):
    """Send recovery only for a previously accepted failure; persist dedup separately."""
    now = now or datetime.now(BUSINESS_TZ)
    failure = _dispatch_failures(path, sender, now=now)
    if failure['status'] == 'busy' or not path.exists():
        return failure
    lock = InstanceLock(path.with_suffix('.send.lock'))
    try:
        lock.acquire()
    except RuntimeError:
        return failure
    try:
        with sqlite3.connect(path, timeout=10) as db:
            _schema(db)
        due = [x for x in read_incidents(path, limit=None)
               if x['status'] == 'resolved' and x.get('recovery_notification') in ('waiting', 'failed') and (x.get('recovery_attempts') or 0)<3
               and (not x.get('recovery_attempt_at') or now-datetime.fromisoformat(x['recovery_attempt_at']) >= timedelta(minutes=30))]
        if not due:
            return failure
        stamp = now.isoformat(timespec='seconds')
        with sqlite3.connect(path, timeout=10) as db:
            for item in due:
                db.execute("UPDATE incidents SET recovery_notification='sending',recovery_attempts=COALESCE(recovery_attempts,0)+1,recovery_attempt_at=? WHERE key=?", (stamp,item['key']))
        try:
            sender('AiStock 数据交付恢复：重新验收通过', '\n\n'.join(
                f"{x['detail']['name']}\n恢复验收：{x['resolved_at']}" for x in due))
            status, error = 'smtp_accepted', None
        except Exception as exc:
            status, error = ('uncertain' if isinstance(exc,NotificationOutcomeUncertain) else 'failed'), f'{type(exc).__name__}: {exc}'
        with sqlite3.connect(path, timeout=10) as db:
            for item in due:
                db.execute('UPDATE incidents SET recovery_notification=?,error=? WHERE key=?', (status,error,item['key']))
                db.execute('INSERT INTO incident_history(event_key,at,status,payload) VALUES(?,?,?,?)',
                           (item['key'],stamp,'recovery_'+status,json.dumps({'error':error})))
        return {**failure, 'recovery_status':status, 'recovery_count':len(due)}
    finally:
        lock.release()


def notification_transport_status(path: Path, *, enabled, configured, now=None):
    now = now or datetime.now(BUSINESS_TZ)
    if not enabled:
        return {'state':'not_requested','ok':False,'last_smtp_accepted_at':None}
    if not configured:
        return {'state':'configuration_incomplete','ok':False,'last_smtp_accepted_at':None}
    events = read_incidents(path,limit=None)
    stamps = []
    failed = False
    for item in events:
        if item['status']=='superseded':
            continue
        if item.get('notification')=='smtp_accepted' and item.get('last_attempt'):
            stamps.append(item['last_attempt'])
        if item.get('recovery_notification')=='smtp_accepted' and item.get('recovery_attempt_at'):
            stamps.append(item['recovery_attempt_at'])
        failed = failed or (item['status']!='resolved' and item.get('notification') in ('failed','sending'))
        failed = failed or item.get('notification') == 'uncertain'
        failed = failed or item.get('recovery_notification') in ('failed','sending','uncertain')
    accepted = max(stamps,default=None)
    try:
        recent = accepted is not None and 0 <= (now-datetime.fromisoformat(accepted)).total_seconds() <= 86400
    except (ValueError,TypeError):
        recent = False
    return {'state':'failed_or_uncertain' if failed else 'smtp_accepted' if recent else 'configured_unverified',
            'ok':bool(recent and not failed),'last_smtp_accepted_at':accepted}

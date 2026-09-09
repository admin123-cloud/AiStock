"""Durable incident lifecycle, independent of strategy ticket production.

Only the publisher writes this database. API reads use SQLite read-only mode.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from services.runtime_health import BUSINESS_TZ


def reconcile(path: Path, checks: list[dict], *, now: datetime | None = None, grace_minutes: int = 30) -> list[dict]:
    now = now or datetime.now(BUSINESS_TZ)
    stamp = now.isoformat(timespec='seconds')
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS incidents (key TEXT PRIMARY KEY, status TEXT, first_seen TEXT, last_seen TEXT, deadline TEXT, resolved_at TEXT, payload TEXT, notification TEXT, attempts INTEGER DEFAULT 0, last_attempt TEXT, error TEXT)')
        for check in checks:
            key = check['name']
            prior = db.execute('SELECT status,deadline FROM incidents WHERE key=?', (key,)).fetchone()
            payload = json.dumps(check, ensure_ascii=False)
            if check.get('ok') is True:
                if prior and prior[0] != 'resolved':
                    db.execute("UPDATE incidents SET status='resolved',resolved_at=?,last_seen=?,payload=? WHERE key=?", (stamp,stamp,payload,key))
                continue
            if prior is None or prior[0] == 'resolved':
                deadline = (now + timedelta(minutes=grace_minutes)).isoformat(timespec='seconds')
                db.execute("INSERT OR REPLACE INTO incidents(key,status,first_seen,last_seen,deadline,payload,notification,attempts) VALUES(?,?,?,?,?,?,?,0)", (key,'observing',stamp,stamp,deadline,payload,'waiting'))
            else:
                due = now >= datetime.fromisoformat(prior[1])
                db.execute('UPDATE incidents SET status=?,last_seen=?,payload=? WHERE key=?', ('overdue' if due else 'observing',stamp,payload,key))
        db.commit()
    return read_incidents(path)


def read_incidents(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute('SELECT * FROM incidents ORDER BY last_seen DESC LIMIT 200')]
    for row in rows:
        row['detail'] = json.loads(row.pop('payload'))
    return rows


def dispatch(path: Path, sender, *, now: datetime | None = None) -> dict:
    """At most one digest per poll; retry failed SMTP with a 30-minute cooldown, max 3 attempts.

    Must be called by the single-instance publisher, never by a GET handler.
    SMTP accepted is not a delivery/read receipt.
    """
    now = now or datetime.now(BUSINESS_TZ)
    due = [x for x in read_incidents(path) if x['status'] == 'overdue'
           and x['notification'] != 'smtp_accepted' and x['attempts'] < 3
           and (not x['last_attempt'] or now-datetime.fromisoformat(x['last_attempt']) >= timedelta(minutes=30))]
    if not due:
        return {'status': 'idle', 'count': 0}
    with sqlite3.connect(path) as db:
        for item in due:
            db.execute("UPDATE incidents SET attempts=attempts+1,last_attempt=?,notification='sending' WHERE key=?", (now.isoformat(timespec='seconds'),item['key']))
    try:
        sender('AiStock 数据交付异常：自动恢复窗口已超时', '\n\n'.join(
            f"{x['key']}\n首次发现：{x['first_seen']}\n恢复窗口：{x['deadline']}\n说明：{x['detail'].get('message') or x['detail'].get('reason')}\n责任：{x['detail'].get('remediation_owner') or '运行中心'}" for x in due))
        status, error = 'smtp_accepted', None
    except Exception as exc:
        status, error = 'failed', f'{type(exc).__name__}: {exc}'
    with sqlite3.connect(path) as db:
        for item in due:
            db.execute('UPDATE incidents SET notification=?,error=? WHERE key=?', (status,error,item['key']))
    return {'status': status, 'count': len(due)}


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
        refused = server.send_message(msg)
        if refused:
            raise RuntimeError('one_or_more_recipients_refused')

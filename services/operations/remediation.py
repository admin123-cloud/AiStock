"""Only enqueue bounded canonical repairs; verification remains an independent next poll."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from utils.trading_sessions import BUSINESS_TZ


def plan_repairs(calendar):
    plans = {}
    for dataset in calendar.get('datasets', []):
        ident = dataset['id']
        for cell in dataset.get('cells', []):
            if cell.get('status') not in ('missing','partial') or cell.get('metadata_warning'):
                continue
            day = cell['date']
            if ident == 'stock_daily':
                kind = 'daily_coverage'
                args = ['--mode','repair','--scope','latest','--start-date',day,'--end-date',day,
                        '--max-repair-codes','60','--batch-size','30','--no-cross-source-fallback']
                key = 'daily:'+day
            elif ident == 'index_daily':
                kind = 'index_daily'
                args = ['--start-date',day,'--end-date',day,'--batch-size','40']
                key = 'index_daily:'+day
            elif ident == 'sector_daily':
                kind = 'sector_daily'
                args = ['--trade-date',day]
                key = 'sector_daily:'+day
            elif ident.startswith(('stock_','index_')) and ident.split('_')[-1] in ('5','15','30','60'):
                kind = 'ingestion'
                args = ['--mode','minute-gap-repair','--scenario','history','--start-date',day,'--end-date',day,
                        '--minute-periods','5m,15m,30m,60m','--universe','stock,index','--isolate-history-periods',
                        '--max-repair-codes','20','--repair-code-chunk-size','5','--repair-workers','1',
                        '--minute-timeout-sec','1200']
                key = 'minute:'+day
            else:
                continue
            plans[key] = {'key':key,'kind':kind,'arguments':args,'date':day}
    return sorted(plans.values(),key=lambda x:x['date'],reverse=True)


def _evidence(calendar, plan):
    cells = []
    for dataset in calendar.get('datasets', []):
        ident = dataset['id']
        match = ident == {'daily_coverage':'stock_daily', 'index_daily':'index_daily', 'sector_daily':'sector_daily'}.get(plan['kind'])
        match |= plan['kind'] == 'ingestion' and ident.startswith(('stock_', 'index_')) and ident.split('_')[-1] in ('5','15','30','60')
        if match:
            for cell in dataset.get('cells', []):
                if cell.get('date') == plan['date']:
                    if cell.get('status') not in ('complete','partial','missing') or cell.get('metadata_warning'):
                        return None
                    if not isinstance(cell.get('expected'), int) or not isinstance(cell.get('actual'), int):
                        return None
                    cells.append((ident, cell['expected'], max(0,cell['expected']-cell['actual'])))
    return sorted(cells) or None


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=BUSINESS_TZ).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (TypeError, ValueError):
        return 0


def request_repairs(calendar, root, *, enabled=False, enqueue=None, now=None, max_jobs=2,
                    max_batches=512, max_stalled=3):
    plans = plan_repairs(calendar)
    if not enabled:
        return {'status':'not_requested','eligible':len(plans),'jobs':[]}
    from services.operations.ingestion_backlog import enqueue as default_enqueue, connect
    enqueue = enqueue or default_enqueue
    now = now or datetime.now(BUSINESS_TZ)
    path = root/'operations/remediation.sqlite'
    backlog_path = root/'operations/ingestion_backlog.sqlite'
    path.parent.mkdir(parents=True,exist_ok=True)
    rows, held, verified = [], [], []
    with closing(connect(backlog_path)) as backlog, sqlite3.connect(path,timeout=10) as db:
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE IF NOT EXISTS repairs (key TEXT PRIMARY KEY, requested_at TEXT, job_id TEXT)')
        columns = {row[1] for row in db.execute('PRAGMA table_info(repairs)')}
        for name, declaration in [('batches','INTEGER DEFAULT 1'), ('stalled','INTEGER DEFAULT 0'),
                                  ('evidence','TEXT'), ('terminal_seen','REAL DEFAULT 0'),
                                  ('status',"TEXT DEFAULT 'awaiting_verification'")]:
            if name not in columns:
                db.execute('ALTER TABLE repairs ADD COLUMN '+name+' '+declaration)
        db.commit()
        db.execute('BEGIN IMMEDIATE')
        previous = {r['key']:dict(r) for r in db.execute('SELECT * FROM repairs')}
        # A complete calendar cell is independently verified, not merely an exit code.
        for key, old in previous.items():
            prefix, day = key.split(':', 1)
            kind = {'daily':'daily_coverage','minute':'ingestion','index_daily':'index_daily','sector_daily':'sector_daily'}.get(prefix)
            if not kind:
                continue
            proof = _evidence(calendar, {'kind':kind,'date':day})
            job = backlog.execute('SELECT state FROM jobs WHERE id=?',(old['job_id'],)).fetchone()
            if proof and all(x[2] == 0 for x in proof) and job and job[0] == 'complete' and old['status'] != 'dispatching':
                if not old['terminal_seen']:
                    db.execute('UPDATE repairs SET terminal_seen=? WHERE key=?',(now.timestamp(),key))
                elif old['terminal_seen'] < _timestamp(calendar.get('generated_at')) <= now.timestamp():
                    db.execute("UPDATE repairs SET status='verified_complete' WHERE key=?",(key,))
                    verified.append(key)
        plans.sort(key=lambda p: (previous.get(p['key'], {}).get('requested_at', ''), p['date'], p['key']))
        for plan in plans:
            if len(rows) >= max(0, max_jobs):
                break
            old = previous.get(plan['key'])
            evidence = _evidence(calendar, plan)
            if old:
                job = backlog.execute('SELECT state,updated FROM jobs WHERE id=?',(old['job_id'],)).fetchone()
                reason = None
                if old['status'] == 'dispatching':
                    reason = 'requires_review'
                elif not job or job[0] != 'complete':
                    reason = 'awaiting_job' if job and job[0] in ('queued','running') else 'requires_review'
                elif old['batches'] >= min(512,max_batches) or old['stalled'] >= max_stalled:
                    reason = 'budget_exhausted'
                elif not old['terminal_seen']:
                    db.execute('UPDATE repairs SET terminal_seen=? WHERE key=?',(now.timestamp(),plan['key']))
                    reason = 'awaiting_fresh_verification'
                elif not evidence or not max(old['terminal_seen'], job[1]) < _timestamp(calendar.get('generated_at')) <= now.timestamp():
                    reason = 'awaiting_fresh_verification'
                if reason:
                    held.append({'key':plan['key'],'status':reason})
                    if old['status'] != 'dispatching':
                        db.execute('UPDATE repairs SET status=? WHERE key=?',(reason,plan['key']))
                    continue
                before = json.loads(old['evidence']) if old['evidence'] else None
                same = before and [(x[0],x[1]) for x in before] == [(x[0],x[1]) for x in evidence]
                progress = same and sum(x[2] for x in evidence) < sum(x[2] for x in before)
                stalled = 0 if progress else old['stalled']+1
                if stalled >= max_stalled:
                    db.execute("UPDATE repairs SET stalled=?,status='no_progress_requires_review' WHERE key=?",(stalled,plan['key']))
                    held.append({'key':plan['key'],'status':'no_progress_requires_review'})
                    continue
                arguments = list(plan['arguments'])
                generation = old['batches']+1
                if plan['kind'] == 'ingestion':
                    arguments += ['--minute-report-dir', str(root/'operations/repair_batches'/plan['key'].replace(':','_')/str(generation))]
                elif plan['kind'] == 'index_daily':
                    arguments += ['--repair-generation', str(generation)]
                payload = json.dumps(arguments if plan['kind']=='ingestion' else {'kind':plan['kind'],'arguments':arguments},ensure_ascii=False)
                # Durable intent consumes the budget before touching the second database.
                # A crash between these commits is ambiguous and must not dispatch again.
                db.execute("UPDATE repairs SET requested_at=?,batches=?,stalled=?,evidence=?,terminal_seen=0,status='dispatching' WHERE key=?",
                           (now.isoformat(),generation,stalled,json.dumps(evidence),plan['key']))
                db.commit()
                db.execute('BEGIN IMMEDIATE')
                changed = backlog.execute("UPDATE jobs SET args=?,state='queued',attempts=0,due=?,updated=?,error=NULL WHERE id=? AND state='complete' AND updated=?",
                                          (payload,now.timestamp()+300,now.timestamp(),old['job_id'],job[1])).rowcount
                if not changed:
                    continue
                job_id = old['job_id']
                db.execute("UPDATE repairs SET requested_at=?,batches=?,stalled=?,evidence=?,terminal_seen=0,status='queued' WHERE key=?",
                           (now.isoformat(),generation,stalled,json.dumps(evidence),plan['key']))
            else:
                job_id = enqueue(plan['arguments'],kind=plan['kind'],path=backlog_path,now=now)
                db.execute('INSERT INTO repairs (key,requested_at,job_id,evidence) VALUES(?,?,?,?)',
                           (plan['key'],now.isoformat(),job_id,json.dumps(evidence)))
            rows.append({**plan,'job_id':job_id,'status':'queued'})
    return {'status':'queued' if rows else 'awaiting_verification','eligible':len(plans),'jobs':rows,
            'held':held, 'verified':verified, 'policy':f'At most {max_jobs} jobs/poll; least recently served first; {min(512,max_batches)} batches/key, {max_stalled} stalled verifications; uncertain writes require review'}

"""Only enqueue bounded canonical repairs; verification remains an independent next poll."""
import json
import sqlite3
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


def request_repairs(calendar, root, *, enabled=False, enqueue=None, now=None, max_jobs=2):
    plans = plan_repairs(calendar)
    if not enabled:
        return {'status':'not_requested','eligible':len(plans),'jobs':[]}
    from services.operations.ingestion_backlog import enqueue as default_enqueue
    enqueue = enqueue or default_enqueue
    now = now or datetime.now(BUSINESS_TZ)
    path = root/'operations/remediation.sqlite'
    path.parent.mkdir(parents=True,exist_ok=True)
    rows = []
    with sqlite3.connect(path,timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS repairs (key TEXT PRIMARY KEY, requested_at TEXT, job_id TEXT)')
        for plan in plans:
            if len(rows) >= max_jobs:
                break
            if db.execute('SELECT 1 FROM repairs WHERE key=?',(plan['key'],)).fetchone():
                continue
            job = enqueue(plan['arguments'],kind=plan['kind'],path=root/'operations/ingestion_backlog.sqlite',now=now)
            db.execute('INSERT INTO repairs VALUES(?,?,?)',(plan['key'],now.isoformat(),job))
            db.commit()
            rows.append({**plan,'job_id':job,'status':'queued'})
    return {'status':'queued' if rows else 'awaiting_verification','eligible':len(plans),'jobs':rows,
            'policy':'每轮最多2个确定日期任务；共享队列有限重试，执行完成仍须下一轮独立覆盖复验'}

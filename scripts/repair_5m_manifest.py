"""Reconcile QMT cache with existing 5m bars, then insert missing keys only."""
import argparse
from collections import defaultdict
from contextlib import ExitStack
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from services.operations.health import BUSINESS_TZ, write_snapshot
from services.operations.lifecycle import InstanceLock
from utils.paths import runtime_path

FIELDS = ['code','datetime','open','high','low','close','volume','amount']


def timestamp(value):
    value = value if isinstance(value,datetime) else datetime.fromisoformat(str(value))
    if value.tzinfo:
        value = value.astimezone(BUSINESS_TZ)
    return value.replace(tzinfo=None,microsecond=0)


def canonical(row):
    return [str(row[0]),timestamp(row[1]).isoformat(),*[float(x) for x in row[2:]]]


def digest(rows):
    return hashlib.sha256(json.dumps(sorted(canonical(r) for r in rows),separators=(',',':'),allow_nan=False).encode()).hexdigest()


def prepare_day(source, existing):
    from scripts.qmt_xtquant_minute_backfill_validate import REGULAR_ASHARE_5M_TIMES
    src = {timestamp(r[1]):list(r) for r in source}
    old = {timestamp(r[1]):list(r) for r in existing}
    if len(src)!=len(source) or len(old)!=len(existing):
        raise ValueError('duplicate_time_keys')
    if len(src)!=48 or {dt.time() for dt in src}!=REGULAR_ASHARE_5M_TIMES:
        raise ValueError('cache_day_incomplete')
    if len({(r[0],timestamp(r[1]).date()) for r in source+existing})!=1:
        raise ValueError('mixed_code_day')
    overlap = sorted(src.keys() & old.keys())
    if len(overlap)<6:
        raise ValueError('insufficient_overlap_for_unit_validation')
    for r in source:
        values=[float(v) for v in r[2:]]
        if not all(math.isfinite(v) for v in values) or min(values[:4])<=0 or min(values[4:])<0:
            raise ValueError('invalid_source_values')
        if values[1]<max(values[0],values[3]) or values[2]>min(values[0],values[3]):
            raise ValueError('invalid_source_ohlc')
    for dt in overlap:
        for i in (2,3,4,5):
            if old[dt][i] is None or not math.isclose(float(src[dt][i]),float(old[dt][i]),rel_tol=1e-5,abs_tol=0.0001):
                raise ValueError('overlap_price_conflict')
        if old[dt][7] is None or not math.isclose(float(src[dt][7]),float(old[dt][7]),rel_tol=0.0005,abs_tol=2):
            raise ValueError('overlap_amount_conflict')
    factors=[factor for factor in (1.,100.,.01) if all(old[dt][6] is not None and
             math.isclose(float(src[dt][6])*factor,float(old[dt][6]),rel_tol=.005,abs_tol=1) for dt in overlap)]
    if len(factors)!=1:
        raise ValueError('ambiguous_or_conflicting_volume_units')
    missing=[]
    for dt in sorted(src.keys()-old.keys()):
        row=list(src[dt]);row[1]=dt.replace(tzinfo=BUSINESS_TZ);row[6]=int(round(float(row[6])*factors[0]));missing.append(row)
    return missing,{'overlap':len(overlap),'volume_factor':factors[0],'missing':len(missing)}


def load_days(path):
    m=json.loads(path.read_text(encoding='utf-8'))
    if not str(m.get('scope','')).startswith('partial_bucket_only'):
        raise ValueError('unsupported_manifest_scope')
    days=set()
    for row in m['gaps']:
        code,day=row['code'],date.fromisoformat(row['trade_date'])
        if not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)',code):raise ValueError('invalid_security_code')
        days.add((day.isoformat(),code))
    return sorted(days)


def read_day(client, day, codes):
    return client.query(f"SELECT {','.join(FIELDS)} FROM stock.kline_minute_5 FINAL "
                        'WHERE toYYYYMM(datetime)={month:UInt32} AND toDate(datetime)={day:Date} '
                        'AND code IN {codes:Array(String)} ORDER BY code,datetime',
                        parameters={'month':int(day[:7].replace('-','')),'day':date.fromisoformat(day),'codes':codes},
                        settings={'max_threads':1,'max_memory_usage':500_000_000}).result_rows


def stage(args, client):
    from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
    from scripts.qmt_xtquant_minute_backfill_validate import FIELD_LIST,rows_from_qmt,_stable_id
    days=load_days(args.manifest)[args.offset:args.offset+args.limit]
    run=uuid.uuid4().hex[:16]
    out=args.output or runtime_path('operations','derived_recovery','cache_repairs',run)
    out.mkdir(parents=True,exist_ok=False)
    table='stock_repair.five_minute_gap_'+run
    groups=defaultdict(list)
    for day,code in days:groups[day].append(code)
    qmt=QmtMiniMarketClient()
    report={'generated_at':datetime.now(BUSINESS_TZ).isoformat(),'state':'staging','table':table,
            'manifest':str(args.manifest.resolve()),'manifest_sha256':hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
            'code_days':len(days),'offset':args.offset,'accepted':[],'blocked':[],
            'scope':'partial_manifest_code_days_only; no download or existing-key overwrite'}
    write_snapshot(report,out/'stage.json');all_missing=[]
    with (out/'source-evidence.jsonl').open('w',encoding='utf-8',newline='\n') as evidence:
        for day,codes in groups.items():
            for offset in range(0,len(codes),30):
                batch=codes[offset:offset+30]
                raw=qmt.get_market_data(field_list=FIELD_LIST,stock_list=batch,period='5m',
                                        start_time=day.replace('-','')+'000000',end_time=day.replace('-','')+'235959',
                                        dividend_type='none',fill_data=False)
                rows,_=rows_from_qmt(raw,'5m',batch,datetime.now(BUSINESS_TZ),day,day)
                existing=read_day(client,day,batch)
                for code in batch:
                    source=[list(r[1:9]) for r in rows if r[1]==code]
                    prior=[list(r) for r in existing if r[0]==code]
                    try:missing,proof=prepare_day(source,prior)
                    except (ValueError,TypeError) as exc:
                        report['blocked'].append({'day':day,'code':code,'reason':str(exc)});continue
                    all_missing.extend(missing);report['accepted'].append({'day':day,'code':code,**proof})
                    evidence.write(json.dumps({'day':day,'code':code,'source':[canonical(r) for r in source],
                                               'existing':[canonical(r) for r in prior],**proof},allow_nan=False)+'\n')
                write_snapshot(report,out/'stage.json')
    now=datetime.now(BUSINESS_TZ).replace(microsecond=0)
    client.command('CREATE DATABASE IF NOT EXISTS stock_repair')
    client.command(f'CREATE TABLE {table} AS stock.kline_minute_5')
    if all_missing:
        client.insert(table,[r+[now,_stable_id('5m',r[0],timestamp(r[1]))] for r in all_missing],column_names=FIELDS+['created_at','id'])
    stored=client.query(f"SELECT {','.join(FIELDS)} FROM {table} FINAL ORDER BY code,datetime").result_rows
    if len(stored)!=len(all_missing) or digest(stored)!=digest(all_missing):raise RuntimeError('stage_readback_mismatch')
    report.update(state='staged',rows=len(stored),sha256=digest(stored),completed_at=datetime.now(BUSINESS_TZ).isoformat())
    write_snapshot(report,out/'stage.json')
    print(json.dumps({'stage_report':str(out/'stage.json'),'rows':len(stored),'blocked':len(report['blocked'])}))


def apply(args, client):
    from services.operations.backup import sha256
    proof=json.loads(args.backup_confirmation.read_text(encoding='utf-8'))
    if proof.get('state')!='archive_verified' or 'stock.kline_minute_5' not in proof.get('tables',[]):
        raise ValueError('verified_source_backup_required')
    if sha256(args.backup_confirmation.parent/'backup.zip')!=proof.get('archive',{}).get('sha256'):
        raise ValueError('source_backup_hash_mismatch')
    path=args.stage_report;report=json.loads(path.read_text(encoding='utf-8'));table=report['table']
    if not re.fullmatch(r'stock_repair\.five_minute_gap_[a-f0-9]{16}',table):raise ValueError('unexpected_stage_table')
    rows=client.query(f"SELECT {','.join(FIELDS)} FROM {table} FINAL ORDER BY code,datetime").result_rows
    if len(rows)!=report.get('rows') or digest(rows)!=report.get('sha256'):raise ValueError('stage_evidence_changed')
    if report['state'] not in ('staged','applied','applying','outcome_uncertain'):raise ValueError('invalid_stage_state')
    groups=defaultdict(list)
    for row in rows:groups[timestamp(row[1]).date().isoformat()].append(row)
    def verify(require_all=False):
        found=0
        for day,staged in groups.items():
            actual={(r[0],timestamp(r[1])):r for r in read_day(client,day,sorted({r[0] for r in staged}))}
            for row in staged:
                current=actual.get((row[0],timestamp(row[1])))
                if current is not None:
                    if canonical(current)!=canonical(row):raise RuntimeError('existing_key_conflict; no overwrite permitted')
                    found+=1
        if require_all and found!=len(rows):raise RuntimeError('post_insert_missing_keys')
        return found
    existing=verify()
    if report['state'] in ('applying','outcome_uncertain') and existing!=len(rows):
        raise RuntimeError('prior_write_uncertain; do not replay partial outcome')
    report.update(state='applying',backup_confirmation=str(args.backup_confirmation),existing_matching=existing)
    write_snapshot(report,path)
    try:
        for day,staged in groups.items():
            codes=sorted({r[0] for r in staged})
            client.command(f'INSERT INTO stock.kline_minute_5 SELECT * FROM {table} FINAL '
                           'WHERE toDate(datetime)={day:Date} AND (code,datetime) NOT IN '
                           '(SELECT code,datetime FROM stock.kline_minute_5 FINAL '
                           'WHERE toYYYYMM(datetime)={month:UInt32} AND toDate(datetime)={day:Date} '
                           'AND code IN {codes:Array(String)})',
                           parameters={'day':date.fromisoformat(day),'month':int(day[:7].replace('-','')),'codes':codes},
                           settings={'max_threads':1,'max_memory_usage':500_000_000})
        verified=verify(require_all=True)
    except Exception as exc:
        report.update(state='outcome_uncertain',error=str(exc));write_snapshot(report,path);raise
    report.update(state='applied',verified_rows=verified,applied_at=datetime.now(BUSINESS_TZ).isoformat())
    write_snapshot(report,path);print(json.dumps({'state':'applied','verified_rows':verified,'already_matching':existing}))


def main():
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=('plan','stage','apply'),default='plan')
    p.add_argument('--manifest',type=Path);p.add_argument('--offset',type=int,default=0);p.add_argument('--limit',type=int,default=200)
    p.add_argument('--output',type=Path);p.add_argument('--stage-report',type=Path);p.add_argument('--backup-confirmation',type=Path)
    args=p.parse_args()
    if args.offset<0 or not 1<=args.limit<=3000:p.error('offset must be nonnegative and limit 1..3000')
    if args.mode in ('plan','stage') and not args.manifest:p.error('--manifest required')
    if args.mode=='apply' and (not args.stage_report or not args.backup_confirmation):p.error('--stage-report and --backup-confirmation required')
    if args.mode=='plan':print(json.dumps({'code_days':len(load_days(args.manifest)),'writes':False}));return
    from utils.market_warehouse import clickhouse_client
    with ExitStack() as stack:
        for path in (runtime_path('operations','derived_recovery','cache_repair.lock'),runtime_path('operations','qmt_downloads','download.lock')):
            lock=InstanceLock(path);lock.acquire();stack.callback(lock.release)
        if args.mode=='stage':stage(args,clickhouse_client())
        else:apply(args,clickhouse_client())


if __name__=='__main__':main()

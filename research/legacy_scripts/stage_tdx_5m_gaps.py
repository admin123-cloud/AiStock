"""Stage missing 5m keys from local TDX after overlap validation; never writes the formal table."""
from __future__ import annotations
import argparse, hashlib, json, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.build_tdx_minute_periods import _find_files, _iter_lc5
from utils.market_warehouse import clickhouse_client
FIELDS=['code','datetime','open','high','low','close','volume','amount','created_at','id']
VALID={(9,m) for m in range(35,60,5)}|{(10,m) for m in range(0,60,5)}|{(11,m) for m in range(0,35,5)}|{(13,m) for m in range(5,60,5)}|{(14,m) for m in range(0,60,5)}|{(15,0)}
def sid(code,dt): return int.from_bytes(hashlib.blake2b(f'5m|{code}|{dt:%Y-%m-%d %H:%M:%S}'.encode(),digest_size=8).digest(),'big')
def close(a,b): return abs(float(a)-float(b)) <= max(.01,.005*abs(float(b)))
def main():
 p=argparse.ArgumentParser(); p.add_argument('--crosscheck',type=Path,required=True); p.add_argument('--output',type=Path,required=True); p.add_argument('--tdx-root',type=Path,default=Path(r'D:\TDX\vipdoc')); p.add_argument('--dates',default=''); a=p.parse_args()
 x=json.loads(a.crosscheck.read_text(encoding='utf-8')); wanted={d.strip() for d in a.dates.split(',') if d.strip()}; items=[i for i in x['gaps'] if i['tdx_category']=='tdx_full48' and (not wanted or i['trade_date'] in wanted)]; by_date=defaultdict(list); by_code=defaultdict(list)
 for i in items:
   by_date[i['trade_date']].append(i['code']); by_code[i['code']].append(i)
 files=_find_files(a.tdx_root,{'sh','sz','bj'},set(i['code'] for i in items),0); file_by_code={p.stem[2:]+'.'+({'sh':'SH','sz':'SZ','bj':'BJ'}[p.name[:2].lower()]):p for p in files}
 target_days={code:{i['trade_date'] for i in rows} for code,rows in by_code.items()}
 source={}
 for code,path in file_by_code.items():
  days=target_days.get(code,set())
  if not days: continue
  lo=min(datetime.fromisoformat(d) for d in days); hi=max(datetime.fromisoformat(d) for d in days).replace(hour=23,minute=59,second=59)
  source[code]=[r for r in _iter_lc5(path,lo,hi) if (r.dt.hour,r.dt.minute) in VALID and r.dt.date().isoformat() in days]
 c=clickhouse_client(); accepted=[]; blocked=[]
 for day,codes in sorted(by_date.items()):
  rows=c.query('SELECT code,datetime,open,high,low,close,volume,amount FROM kline_minute_5 FINAL WHERE toDate(datetime)={day:Date} AND code IN {codes:Array(String)}',parameters={'day':day,'codes':codes},settings={'max_threads':1,'max_memory_usage':500_000_000}).result_rows
  old=defaultdict(list)
  for r in rows: old[r[0]].append(r)
  for code in codes:
   src=[r for r in source.get(code,[]) if r.dt.date().isoformat()==day]; prior=old.get(code,[])
   if len(src)!=48: blocked.append({'code':code,'trade_date':day,'reason':'tdx_not_48'}); continue
   if any(float(r.volume) < 0 or float(r.amount) < 0 or min(float(r.open),float(r.high),float(r.low),float(r.close)) <= 0 for r in src): blocked.append({'code':code,'trade_date':day,'reason':'invalid_or_zero_tdx_values'}); continue
   sm={r.dt:r for r in src}; om={r[1].replace(tzinfo=None):r for r in prior}; overlap=sorted(sm.keys()&om.keys())
   if len(overlap)<6: blocked.append({'code':code,'trade_date':day,'reason':'insufficient_overlap'}); continue
   if sum(abs(float(sm[d].close)-float(om[d][5])) <= max(.01,.005*abs(float(om[d][5]))) for d in overlap)/len(overlap)<.9: blocked.append({'code':code,'trade_date':day,'reason':'overlap_price_conflict'}); continue
   if any(om[d][7] is None or abs(float(sm[d].amount)-float(om[d][7]))>max(100,.05*abs(float(om[d][7]))) for d in overlap if d.time().minute!=35): blocked.append({'code':code,'trade_date':day,'reason':'overlap_amount_conflict'}); continue
   missing=sorted(set(sm)-set(om)); accepted.append({'code':code,'trade_date':day,'overlap':len(overlap),'missing':len(missing),'rows':[(code,d,sm[d]) for d in missing]})
 out_rows=[]
 now=datetime.now()
 for item in accepted:
  for code,dt,r in item['rows']: out_rows.append((code,dt,r.open,r.high,r.low,r.close,r.volume,r.amount,now,sid(code,dt)))
 payload={'source':'local_tdx_lc5','state':'staged','code_days':len(items),'accepted_code_days':len(accepted),'blocked_code_days':len(blocked),'rows':len(out_rows),'staged_rows':[[r[0],r[1].isoformat(),r[2],r[3],r[4],r[5],r[6],r[7],r[8].isoformat(),r[9]] for r in out_rows],'accepted':[{k:v for k,v in i.items() if k!='rows'} for i in accepted],'blocked':blocked}
 a.output.write_text(json.dumps(payload,indent=2,default=str,ensure_ascii=False),encoding='utf-8'); print(json.dumps({'output':str(a.output),'accepted_code_days':len(accepted),'blocked_code_days':len(blocked),'rows':len(out_rows)},ensure_ascii=False))
if __name__=='__main__': main()

"""Gate and apply TDX 5m fallback rows; never touches derived tables."""
from __future__ import annotations
import argparse, hashlib, json, sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from utils.market_warehouse import clickhouse_client

FIELDS=['code','datetime','open','high','low','close','volume','amount']

def sha256(path):
 h=hashlib.sha256();
 with path.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
 return h.hexdigest()

def main():
 p=argparse.ArgumentParser(); p.add_argument('--staged',type=Path,required=True); p.add_argument('--output',type=Path,required=True); p.add_argument('--tdx-root',type=Path,default=Path(r'D:\TDX\vipdoc')); p.add_argument('--yes',action='store_true'); a=p.parse_args()
 x=json.loads(a.staged.read_text(encoding='utf-8')); c=clickhouse_client()
 stocks={r[0]:r[1:] for r in c.query('SELECT code, market, type, listing_status, list_date, delist_date, quit FROM stocks FINAL').result_rows}
 statuses=c.query("SELECT code,start_date,end_date,status FROM kline_daily_market_status_audit FINAL WHERE review_state = 'verified' AND status IN ('suspended','delisted','not_trading')").result_rows
 items=[]; pending=[]
 for raw in x.get('staged_rows',[]):
  code,dt,*vals=raw; dt=datetime.fromisoformat(str(dt).replace('Z','+00:00')); dt_naive=dt.replace(tzinfo=None); stock=stocks.get(code); reason=None
  if not stock: reason='stock_missing'
  else:
   market,kind,listing,listed,delisted,quit_flag=stock
   if kind!='stock': reason=f'non_stock:{kind}'
   elif listing!='active': reason=f'listing:{listing}'
   elif listed and str(listed)>dt.date().isoformat(): reason='not_listed'
   elif delisted and str(delisted)<dt.date().isoformat(): reason='delisted'
   elif quit_flag: reason='quit'
   elif any(sc==code and str(st)<=dt.date().isoformat()<=str(en) for sc,st,en,_ in statuses): reason='status_suspended'
  tdx_path=a.tdx_root/code.split('.')[1].lower()/'fzline'/(code.split('.')[1].lower()+code.split('.')[0]+'.lc5')
  evidence={'tdx_path':str(tdx_path),'tdx_sha256':sha256(tdx_path) if tdx_path.exists() else None,'new_values':dict(zip(FIELDS[2:],vals[:6]))}
  item={'code':code,'datetime':dt.isoformat(),'old_value':None,'new_value':evidence['new_values'],'evidence':evidence,'authority':'local TDX lc5 fallback after QMT incomplete'}
  if reason: item.update(validity='pending',reason=reason); pending.append(item); continue
  # Current FINAL key and nearest same-code bars are read-only preflight.
  day=dt.date(); rows=c.query('SELECT code,datetime,open,high,low,close,volume,amount FROM kline_minute_5 FINAL WHERE code={code:String} AND datetime >= {lo:DateTime} AND datetime < {hi:DateTime} ORDER BY datetime',parameters={'code':code,'lo':datetime.combine(day,datetime.min.time()),'hi':datetime.combine(day+timedelta(days=1),datetime.min.time())},settings={'max_threads':1}).result_rows
  exact=[r for r in rows if r[1].replace(tzinfo=None)==dt_naive]
  if exact: reason='key_already_exists'
  item['old_value']=[list(r) for r in exact]
  neighbors=[r for r in rows if r[1].replace(tzinfo=None)!=dt_naive]
  if not neighbors: reason=reason or 'no_qmt_neighbors'
  else:
   near=sorted(neighbors,key=lambda r:abs((r[1].replace(tzinfo=None)-dt_naive).total_seconds()))[:2]
   item['evidence']['qmt_neighbors']=[list(r) for r in near]
   close=float(vals[3]); amount=float(vals[5])
   if any(abs(close-float(r[5]))>max(.01,.01*abs(float(r[5]))) for r in near): reason=reason or 'price_continuity_over_1pct'
   if amount and any(abs(amount-float(r[7]))>max(100,.05*abs(float(r[7]))) for r in near if float(r[7])!=0): reason=reason or 'amount_continuity_over_5pct'
  if reason: item.update(validity='pending',reason=reason); pending.append(item)
  else: item.update(validity='accepted'); items.append(item)
 if not a.yes: accepted_to_write=[]
 else: accepted_to_write=items
 if accepted_to_write:
  rows=[(i['code'],datetime.fromisoformat(i['datetime']),i['new_value']['open'],i['new_value']['high'],i['new_value']['low'],i['new_value']['close'],i['new_value']['volume'],i['new_value']['amount'],datetime.now(),abs(hash((i['code'],i['datetime'],'tdx-gated')))) for i in accepted_to_write]
  c.insert('stock.kline_minute_5',rows,column_names=FIELDS+['created_at','id'])
  for i in accepted_to_write:
   check=c.query('SELECT code,datetime,open,high,low,close,volume,amount FROM stock.kline_minute_5 FINAL WHERE code={code:String} AND datetime={dt:DateTime}',parameters={'code':i['code'],'dt':datetime.fromisoformat(i['datetime'])},settings={'max_threads':1}).result_rows
   if len(check)!=1: i.update(validity='pending',reason='post_insert_readback_missing')
   else: i['readback']=list(check[0])
 out={'state':'applied' if a.yes else 'gated_dry_run','read_only':not a.yes,'formal_table_mutation':False,'accepted':items,'pending':pending,'statistics':{'input':len(x.get('staged_rows',[])),'accepted':len(items),'pending':len(pending),'written':len(accepted_to_write)}}
 a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,indent=2,default=str,ensure_ascii=False),encoding='utf-8'); print(json.dumps({'output':str(a.output),**out['statistics'],'state':out['state']}))
if __name__=='__main__': main()

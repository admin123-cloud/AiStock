from __future__ import annotations
import argparse,csv,json,sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.build_tdx_minute_periods import _iter_lc5
from utils.market_warehouse import clickhouse_client
VALID={(9,m) for m in range(35,60,5)}|{(10,m) for m in range(0,60,5)}|{(11,m) for m in range(0,35,5)}|{(13,m) for m in range(5,60,5)}|{(14,m) for m in range(0,60,5)}|{(15,0)}
def main():
 p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args(); items=[r for r in csv.DictReader(a.input.open(encoding='utf-8'),delimiter='\t') if r['trade_date']=='2026-09-11']; by=defaultdict(list)
 for r in items:by[r['code']].extend(r['missing_times'].split(','))
 c=clickhouse_client(); accepted=[];blocked=[]
 for code,times in sorted(by.items()):
  path=Path(r'D:\TDX\vipdoc')/code.split('.')[1].lower()/'fzline'/(code.split('.')[1].lower()+code.split('.')[0]+'.lc5'); tdx={r.dt.strftime('%H:%M'):r for r in _iter_lc5(path,datetime(2026,9,11),datetime(2026,9,11,23,59,59))}; qrows=c.query('SELECT datetime,close FROM kline_minute_5 FINAL WHERE code={c:String} AND toDate(datetime)={d:Date}',parameters={'c':code,'d':'2026-09-11'}).result_rows; q={r[0].strftime('%H:%M'):float(r[1]) for r in qrows}; valid=True; picked=[]
  for tm in times:
   x=tdx.get(tm); dt=datetime.fromisoformat('2026-09-11 '+tm); neighbors=[v for k,v in q.items() if abs((datetime.fromisoformat('2026-09-11 '+k)-dt).total_seconds())<=900]
   if not x or x.open<=0 or x.close<=0 or (neighbors and min(abs(x.close-v)/max(abs(v),.01) for v in neighbors)>.05):valid=False;break
   picked.append((tm,x.close,x.volume,x.amount))
  (accepted if valid else blocked).append({'code':code,'missing_times':times,'tdx_rows':picked})
 payload={'date':'2026-09-11','input_code_days':len(items),'accepted_code_days':len(accepted),'blocked_code_days':len(blocked),'accepted_missing_bars':sum(len(x['tdx_rows']) for x in accepted),'blocked':blocked,'accepted':accepted};a.output.write_text(json.dumps(payload,indent=2,default=str,ensure_ascii=False),encoding='utf-8');print(json.dumps({'output':str(a.output),'accepted_code_days':len(accepted),'accepted_missing_bars':sum(len(x['tdx_rows']) for x in accepted),'blocked_code_days':len(blocked)},ensure_ascii=False))
if __name__=='__main__':main()

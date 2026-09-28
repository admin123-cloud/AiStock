from __future__ import annotations
import argparse,csv,json,sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
from scripts.qmt_xtquant_minute_backfill_validate import FIELD_LIST,rows_from_qmt
from scripts.build_tdx_minute_periods import _iter_lc5

def main():
 p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args(); groups=defaultdict(list)
 for r in csv.DictReader(a.input.open(encoding='utf-8'),delimiter='\t'):groups[r['trade_date']].append(r)
 kept=[];filtered=[];q=QmtMiniMarketClient()
 for day,items in sorted(groups.items()):
  codes=[r['code'] for r in items]; raw=q.get_market_data(field_list=FIELD_LIST,stock_list=codes,period='5m',start_time=day.replace('-','')+'000000',end_time=day.replace('-','')+'235959',dividend_type='none',fill_data=False); rows,_=rows_from_qmt(raw,'5m',codes,datetime.now(),day,day); qm=defaultdict(dict)
  for r in rows:qm[r[1]][r[2].strftime('%H:%M')]=r
  for it in items:
   code=it['code']; missing=it['missing_times'].split(','); path=Path(r'D:\TDX\vipdoc')/code.split('.')[1].lower()/'fzline'/(code.split('.')[1].lower()+code.split('.')[0]+'.lc5'); tm={r.dt.strftime('%H:%M'):r for r in _iter_lc5(path,datetime.fromisoformat(day),datetime.fromisoformat(day).replace(hour=23,minute=59,second=59))}; ok=True
   for t in missing:
    x=qm[code].get(t);y=tm.get(t)
    if not x or not y or float(x[7])<0 or float(x[8])<0 or float(y.volume)<0 or float(y.amount)<0 or max(abs(float(a)-float(b))/max(abs(float(b)),.01) for a,b in zip(x[3:7],(y.open,y.high,y.low,y.close)))>.005:ok=False;break
   (filtered if ok else kept).append(it)
 out=a.output;out.write_text('trade_date\tcode\tmissing_times\tmissing_count\tobserved_count\n'+''.join(f"{x['trade_date']}\t{x['code']}\t{x['missing_times']}\t{x['missing_count']}\t{x['observed_count']}\n" for x in kept),encoding='utf-8'); print(json.dumps({'input_code_days':len(kept)+len(filtered),'filtered_zero_or_dual_source':len(filtered),'remaining_code_days':len(kept),'remaining_bars':sum(int(x['missing_count']) for x in kept),'output':str(out)},ensure_ascii=False))
if __name__=='__main__':main()

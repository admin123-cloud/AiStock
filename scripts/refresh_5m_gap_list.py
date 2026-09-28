"""Refresh a stale baseline gap list against current FINAL 5m data."""
from __future__ import annotations
import argparse,csv,json,sys
from collections import defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from utils.market_warehouse import clickhouse_client
VALID={(9,m) for m in range(35,60,5)}|{(10,m) for m in range(0,60,5)}|{(11,m) for m in range(0,35,5)}|{(13,m) for m in range(5,60,5)}|{(14,m) for m in range(0,60,5)}|{(15,0)}
def main():
 p=argparse.ArgumentParser(); p.add_argument('--manifest',type=Path,required=True); p.add_argument('--output',type=Path,required=True); a=p.parse_args(); raw=a.manifest.read_text(encoding='utf-8'); by=defaultdict(set)
 if a.manifest.suffix.lower()=='.tsv':
  for x in csv.DictReader(raw.splitlines(),delimiter='\t'): by[x['trade_date']].add(x['code'])
 else:
  m=json.loads(raw)
  for x in m['gaps']: by[x['trade_date']].add(x['code'])
 c=clickhouse_client(); out=[]
 for day,codes in sorted(by.items()):
  rows=c.query('SELECT code,datetime FROM kline_minute_5 FINAL WHERE toDate(datetime)={day:Date} AND code IN {codes:Array(String)}',parameters={'day':day,'codes':sorted(codes)},settings={'max_threads':1,'max_memory_usage':500_000_000}).result_rows; seen=defaultdict(set)
  for code,dt in rows: seen[code].add((dt.hour,dt.minute))
  for code in sorted(codes):
   missing=sorted(VALID-seen[code]);
   if missing: out.append({'trade_date':day,'code':code,'missing_times':','.join(f'{h:02d}:{mi:02d}' for h,mi in missing),'missing_count':len(missing),'observed_count':len(seen[code]&VALID)})
 a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text('trade_date\tcode\tmissing_times\tmissing_count\tobserved_count\n'+''.join(f"{x['trade_date']}\t{x['code']}\t{x['missing_times']}\t{x['missing_count']}\t{x['observed_count']}\n" for x in out),encoding='utf-8'); expanded=a.output.with_name(a.output.stem+'_expanded.tsv'); expanded.write_text('trade_date\tcode\tmissing_time\n'+''.join(f"{x['trade_date']}\t{x['code']}\t{t}\n" for x in out for t in x['missing_times'].split(',') if t),encoding='utf-8'); print(json.dumps({'output':str(a.output),'expanded':str(expanded),'remaining_code_days':len(out),'missing_bars':sum(x['missing_count'] for x in out)},ensure_ascii=False))
if __name__=='__main__': main()

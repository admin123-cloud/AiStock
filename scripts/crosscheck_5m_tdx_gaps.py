"""Cross-check unresolved 5m code-days against local TDX lc5 files (read-only)."""
from __future__ import annotations
import argparse, json, sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts.build_tdx_minute_periods import _find_files, _iter_lc5
from utils.market_warehouse import clickhouse_client
VALID={(9,m) for m in range(35,60,5)}|{(10,m) for m in range(0,60,5)}|{(11,m) for m in range(0,35,5)}|{(13,m) for m in range(5,60,5)}|{(14,m) for m in range(0,60,5)}|{(15,0)}

def main():
 p=argparse.ArgumentParser(); p.add_argument('--manifest',type=Path,required=True); p.add_argument('--tdx-root',type=Path,default=Path(r'D:\TDX\vipdoc')); p.add_argument('--output',type=Path,required=True); a=p.parse_args()
 m=json.loads(a.manifest.read_text(encoding='utf-8')); gaps=m['gaps']; by_code=defaultdict(list)
 for x in gaps: by_code[x['code']].append(x)
 files=_find_files(a.tdx_root,{c.split('.')[1].lower() for c in by_code},{*by_code},0)
 found={p.name[:2].lower()+p.stem[2:]:p for p in files}
 result=[]; counts=Counter()
 for code, items in by_code.items():
  path=next((p for p in files if p.stem[2:]==code.split('.')[0] and p.name[:2].lower()==code.split('.')[1].lower()),None)
  rows=[]
  if path:
   lo=min(datetime.fromisoformat(x['trade_date']) for x in items); hi=max(datetime.fromisoformat(x['trade_date']) for x in items).replace(hour=23,minute=59,second=59)
   rows=[r for r in _iter_lc5(path,lo,hi) if (r.dt.hour,r.dt.minute) in VALID]
  for item in items:
   day=item['trade_date']; day_rows=[r for r in rows if r.dt.date().isoformat()==day]; n=len(day_rows)
   category='tdx_full48' if n==48 else ('tdx_partial' if n else 'tdx_none')
   counts[category]+=1; result.append({**item,'tdx_count':n,'tdx_category':category,'tdx_file':str(path) if path else None})
 payload={'source_manifest':str(a.manifest),'tdx_root':str(a.tdx_root),'code_days':len(result),'categories':dict(counts),'gaps':sorted(result,key=lambda x:(x['trade_date'],x['code']))}
 a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(payload,indent=2,ensure_ascii=False),encoding='utf-8'); print(json.dumps({'output':str(a.output),'code_days':len(result),'categories':dict(counts)},ensure_ascii=False))
if __name__=='__main__': main()

import argparse,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();x=json.loads(a.input.read_text(encoding='utf-8')); rows=[r for r in x.get('items',x.get('anomalies',[])) if r.get('validity') == 'blocked_evidence'];
a.output.write_text('trade_date\tcode\tobserved_count\tcategory\treason\n'+''.join(f"{r.get('trade_date',r.get('date'))}\t{r.get('code')}\t{r.get('observed_count',r.get('count',0))}\t{r.get('category')}\t{r.get('reason','')}\n" for r in rows),encoding='utf-8');print({'output':str(a.output),'rows':len(rows)})

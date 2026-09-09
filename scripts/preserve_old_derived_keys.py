import argparse,json,sys
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path
def main():
 p=argparse.ArgumentParser();p.add_argument('--month',required=True);p.add_argument('--period',type=int,choices=(15,30,60),required=True);p.add_argument('--run-id',default='r20260909_stable2');a=p.parse_args();
 if len(a.month)!=6 or not a.month.isdigit() or not (1<=int(a.month[4:])<=12):raise SystemExit('invalid month')
 if not a.run_id.replace('_','').isalnum():raise SystemExit('invalid run-id')
 t=f'kline_minute_{a.period}';keep=f'{t}_old_only_r20260909'; month=int(a.month)
 c=clickhouse_client()
 try:
  c.command('CREATE DATABASE IF NOT EXISTS stock_repair');c.command(f'CREATE TABLE IF NOT EXISTS stock_repair.{keep} AS stock.{t}')
  src=f'SELECT o.* FROM stock.{t} FINAL o LEFT JOIN (SELECT * FROM stock.{t}_recovery_{a.run_id} FINAL WHERE toYYYYMM(datetime)={month}) r ON o.code=r.code AND o.datetime=r.datetime WHERE toYYYYMM(o.datetime)={month} AND r.code IS NULL'
  dig=lambda q:c.query(f'SELECT count(),uniqExact(tuple(code,datetime)),toString(sumWithOverflow(cityHash64(tuple(code,datetime,open,high,low,close,volume,amount)))),toString(groupBitXor(cityHash64(tuple(code,datetime,open,high,low,close,volume,amount)))) FROM ({q})',settings={'max_threads':1,'max_memory_usage':1000000000}).result_rows[0]
  expected=dig(src); dst=f'SELECT * FROM stock_repair.{keep} FINAL WHERE toYYYYMM(datetime)={month}'
  before=dig(dst)
  if before[0] and tuple(before)!=tuple(expected):raise RuntimeError('existing preservation month differs; refusing replay')
  if not before[0]:c.command(f'INSERT INTO stock_repair.{keep} {src}',settings={'max_threads':1,'max_memory_usage':2000000000})
  x=dig(dst)
  if tuple(x)!=tuple(expected) or x[0]!=x[1]:raise RuntimeError('preservation checksum mismatch')
  out={'month':a.month,'period':a.period,'table':'stock_repair.'+keep,'rows':int(x[0]),'unique':int(x[1]),'sum_hash':x[2],'xor_hash':x[3],'created_at':datetime.now().isoformat()}
  path=runtime_path('operations','derived_recovery','preservation',f'{a.period}_{a.month}.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(out,ensure_ascii=False))
 finally:c.close()
if __name__=='__main__':main()

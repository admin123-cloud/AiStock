import argparse,json,sys
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path
def main():
 p=argparse.ArgumentParser();p.add_argument('--month',required=True);p.add_argument('--period',type=int,choices=(15,30,60),required=True);p.add_argument('--run-id',default='r20260909_stable2');a=p.parse_args();t=f'kline_minute_{a.period}';keep=f'{t}_old_only_r20260909'
 c=clickhouse_client()
 try:
  c.command('CREATE DATABASE IF NOT EXISTS stock_repair');c.command(f'CREATE TABLE IF NOT EXISTS stock_repair.{keep} AS stock.{t}')
  c.command(f'INSERT INTO stock_repair.{keep} SELECT o.* FROM stock.{t} o LEFT JOIN stock.{t}_recovery_{a.run_id} r ON o.code=r.code AND o.datetime=r.datetime WHERE toYYYYMM(o.datetime)={a.month} AND r.code IS NULL',settings={'max_threads':1,'max_memory_usage':2000000000})
  q=f'SELECT count(),uniqExact(tuple(code,datetime)),toString(sumWithOverflow(cityHash64(tuple(code,datetime,open,high,low,close,volume,amount)))),toString(groupBitXor(cityHash64(tuple(code,datetime,open,high,low,close,volume,amount)))) FROM stock_repair.{keep} WHERE toYYYYMM(datetime)={a.month}'
  x=c.query(q,settings={'max_threads':1,'max_memory_usage':1000000000}).result_rows[0];out={'month':a.month,'period':a.period,'table':'stock_repair.'+keep,'rows':int(x[0]),'unique':int(x[1]),'sum_hash':x[2],'xor_hash':x[3],'created_at':datetime.now().isoformat()}
  path=runtime_path('operations','derived_recovery','preservation',f'{a.period}_{a.month}.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(out,ensure_ascii=False))
 finally:c.close()
if __name__=='__main__':main()

"""Read-only monthly audit for wholly empty trusted 5m code-days and old-only derived keys."""
import argparse,json,sys
from datetime import date,datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from services.operations.health import BUSINESS_TZ,write_snapshot
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path

def main():
 p=argparse.ArgumentParser();p.add_argument('--month',required=True);p.add_argument('--run-id',default='r20260909_stable2');p.add_argument('--output',default='');a=p.parse_args()
 if len(a.month)!=6 or not a.month.isdigit():raise SystemExit('month must be YYYYMM')
 start=date(int(a.month[:4]),int(a.month[4:]),1);end=date(int(a.month[:4])+ (int(a.month[4:])==12),(int(a.month[4:])%12)+1,1)
 c=clickhouse_client();
 try:
  cal=f"SELECT trade_date FROM trade_calendar WHERE is_trading=1 AND trade_date>=toDate('{start}') AND trade_date<toDate('{end}')"
  present=f"SELECT code,toDate(datetime) d FROM kline_minute_5 WHERE toYYYYMM(datetime)={a.month} GROUP BY code,d"
  q=f"SELECT y.d,count() FROM (SELECT t.trade_date d,s.code FROM ({cal}) AS t CROSS JOIN (SELECT code,list_date,delist_date FROM stocks WHERE type='stock' AND list_date IS NOT NULL) AS s WHERE s.list_date<=t.trade_date AND (s.delist_date IS NULL OR s.delist_date>=t.trade_date)) AS y LEFT JOIN ({present}) AS k ON y.code=k.code AND y.d=k.d WHERE k.code IS NULL GROUP BY y.d ORDER BY y.d"
  empty=c.query(q,settings={'max_threads':1,'max_memory_usage':2000000000}).result_rows
  extra=[]
  for n in (15,30,60):
   q=f"SELECT count() FROM kline_minute_{n} o WHERE toYYYYMM(o.datetime)={a.month} AND NOT EXISTS (SELECT 1 FROM kline_minute_{n}_recovery_{a.run_id} r WHERE r.code=o.code AND r.datetime=o.datetime)"
   extra.append({'period':n,'old_only_keys':int(c.query(q,settings={'max_threads':1,'max_memory_usage':2000000000}).result_rows[0][0])})
  unknown=c.query(f"SELECT count() FROM stocks WHERE type='stock' AND list_date IS NULL",settings={'max_threads':1}).result_rows[0][0]
  out={'generated_at':datetime.now(BUSINESS_TZ).isoformat(),'month':a.month,'trusted_empty_code_days_by_date':[{'date':str(x[0]),'count':int(x[1])} for x in empty],'old_only_derived_keys':extra,'unknown_list_date_stocks':int(unknown),'limitations':'does not infer suspension; empty trusted code-days require QMT/official suspension evidence before repair'}
  path=Path(a.output) if a.output else runtime_path('operations','derived_recovery','empty_audits',a.month+'.json');path.parent.mkdir(parents=True,exist_ok=True);write_snapshot(out,path);print(path);print(json.dumps(out,ensure_ascii=False))
 finally:c.close()
if __name__=='__main__':main()

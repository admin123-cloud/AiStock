from __future__ import annotations
import hashlib, json, zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
import clickhouse_connect
from auto_repair_pre2010_harry_a1_20260922 import ARCHIVE_ROOT, SLOTS, read_days, aggregate

REPORT = Path(r"F:\AiStock\data\data\runtime\purchased_history\harry_a1_corrected_20260922.json")
PERIODS = {
 "5m": ("stock.pre2010_5m_harry_a1_candidate_20260922","stock.pre2010_5m_harry_a1_keys_20260922","stock.kline_minute_5","stock.5m_repair_backup_harry_a1_20260922"),
 "15m": ("stock.pre2010_15m_harry_a1_candidate_20260922","stock.pre2010_15m_harry_a1_keys_20260922","stock.kline_minute_15","stock.15m_repair_backup_harry_a1_20260922"),
 "30m": ("stock.pre2010_30m_harry_a1_candidate_20260922","stock.pre2010_30m_harry_a1_keys_20260922","stock.kline_minute_30","stock.30m_repair_backup_harry_a1_20260922"),
 "60m": ("stock.pre2010_60m_harry_a1_candidate_20260922","stock.pre2010_60m_harry_a1_keys_20260922","stock.kline_minute_60","stock.60m_repair_backup_harry_a1_20260922"),
}

def main():
 c=clickhouse_connect.get_client(host='localhost',port=8123,username='default',settings={'max_execution_time':600,'max_partitions_per_insert_block':1000})
 targets=defaultdict(lambda:defaultdict(lambda:defaultdict(set)))
 for p,(cand,_,_,_) in PERIODS.items():
  for code,day in c.query(f'SELECT code,toDate(datetime) FROM {cand} GROUP BY code,toDate(datetime)').result_rows: targets[day.year][str(code)][day].add(p)
 daily={}
 for y,by_code in targets.items():
  wanted=set(by_code)
  for code,day,op,hi,lo,cl,vol,amt in c.query('SELECT code,trade_date,open,high,low,close,volume,amount FROM stock.kline_daily WHERE trade_date BETWEEN %(a)s AND %(b)s',parameters={'a':date(y,1,1),'b':date(y,12,31)}).result_rows:
   if str(code) in wanted: daily[(str(code),day)]=tuple(float(x or 0) for x in (op,hi,lo,cl,vol,amt))
 rows_by=defaultdict(list); valid=defaultdict(set); stats=defaultdict(int)
 for y,by_code in sorted(targets.items()):
  with zipfile.ZipFile(ARCHIVE_ROOT/f'{y}_5min.zip') as z:
   members={n.rsplit('/',1)[-1].lower():n for n in z.namelist() if n.lower().endswith('.csv')}
   for code,by_day in by_code.items():
    prefix=code.split('.')[1].lower()+code.split('.')[0]; parsed=read_days(z,members.get(f'{prefix}_{y}.csv'.lower()),set(by_day))
    for day,periods in by_day.items():
     dv=daily.get((code,day)); bars=parsed.get(day,{})
     for p in periods:
      rs=aggregate(code,day,bars,p); expected={'5m':48,'15m':16,'30m':8,'60m':4}[p]
      ok=False
      if dv and len(rs)==expected:
       agg=(rs[0][2],max(r[3] for r in rs),min(r[4] for r in rs),rs[-1][5],sum(r[6] for r in rs),sum(r[7] for r in rs))
       dif=[abs(agg[i]-dv[i])/max(abs(dv[i]),1.0) for i in range(6)]
       ok=max(dif[:4])<=.01 and dif[4]<=.01 and dif[5]<=.01
      stats[(p,'A' if ok else 'not_A')]+=1
      if ok: valid[p].add((code,day)); rows_by[p].extend(rs)
 for p,(cand,key,formal,backup) in PERIODS.items():
  allkeys=f'stock._tmp_recompute_{p}_all'; c.command(f'DROP TABLE IF EXISTS {allkeys}'); c.command(f'CREATE TABLE {allkeys} (code String,trade_date Date) ENGINE=MergeTree ORDER BY (code,trade_date)'); c.command(f'INSERT INTO {allkeys} SELECT code,toDate(datetime) FROM {cand} GROUP BY code,toDate(datetime)')
  c.command(f'ALTER TABLE {formal} DELETE WHERE (code,toDate(datetime)) IN (SELECT code,trade_date FROM {allkeys}) SETTINGS mutations_sync=1')
  now=datetime.now(); vals=[(r[0],r[1],r[2],r[3],r[4],r[5],r[6],r[7],now,int(hashlib.sha256(f'{r[0]}|{r[1]}|{p}|corrected'.encode()).hexdigest()[:16],16)) for r in rows_by[p]]
  if vals: c.insert(formal,vals,column_names=['code','datetime','open','high','low','close','volume','amount','created_at','id'])
  c.command(f'DROP TABLE IF EXISTS {key}'); c.command(f'CREATE TABLE {key} (code String,trade_date Date) ENGINE=MergeTree ORDER BY (code,trade_date)')
  if valid[p]: c.insert(key,list(valid[p]),column_names=['code','trade_date'])
  # Replace candidate contents with only corrected A rows for traceability.
  c.command(f'TRUNCATE TABLE {cand}')
  if vals: c.insert(cand,vals,column_names=['code','datetime','open','high','low','close','volume','amount','created_at','id'])
 out={'run_id':'harry_a1_1pct_corrected_20260922','stats':{f'{p}:{g}':n for (p,g),n in stats.items()},'valid_keys':{p:len(valid[p]) for p in valid},'rows':{p:len(rows_by[p]) for p in rows_by}}
 REPORT.parent.mkdir(parents=True,exist_ok=True); REPORT.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(out,ensure_ascii=False,indent=2))
if __name__=='__main__': main()

import io, json, zipfile
from datetime import datetime, date
from pathlib import Path
import clickhouse_connect
import pandas as pd

RUN='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v4_daily_purchase_volume'
ZIP=r'E:\\AStockHistory\\harry\\2006_5min.zip'
RAW='stock.pre2010_harry_2006_5m_repair_candidate_20260922'
ACCEPT='stock.pre2010_harry_2006_5m_accepted_20260922'
AUDIT='stock.pre2010_harry_2006_5m_repair_audit_20260922'
OUT=Path(r'F:\\AiStock\\data\\data\\runtime\\purchased_history\\harry_2006_repair_promotion_20260922.json')

def rel(a,b):
    if b is None or abs(float(b))<1e-12: return 0.0 if abs(float(a))<1e-12 else 1.0
    return abs(float(a)-float(b))/abs(float(b))

c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
keys=c.query(f"SELECT code,trade_date FROM stock.purchased_history_data_review_items WHERE run_id='{RUN}' AND period='5m' AND source_batch_id='purchased_5m_stage_2006_20260917_r1'").result_rows
keyset={(str(a),str(b)) for a,b in keys}
daily_rows=c.query("SELECT code,trade_date,open,high,low,close,volume,amount FROM stock.kline_daily FINAL WHERE toYear(trade_date)=2006").result_rows
daily={(str(a),str(b)):(float(o),float(h),float(l),float(cl),float(v),float(am)) for a,b,o,h,l,cl,v,am in daily_rows}
z=zipfile.ZipFile(ZIP); names=set(z.namelist()); raw_rows=[]; audits=[]; accepted=[]
for code,day in sorted(keyset):
    fn=('sh' if code.endswith('.SH') else 'sz')+code[:6]+'_2006.csv'
    if fn not in names: continue
    df=pd.read_csv(io.BytesIO(z.read(fn)),encoding='utf-8-sig'); ts=pd.to_datetime(df.iloc[:,0],errors='coerce')
    mask=ts.dt.date.astype(str)==day; part=df.loc[mask]; pts=ts.loc[mask]
    if len(part)!=48: continue
    cols=[pd.to_numeric(part.iloc[:,i],errors='coerce') for i in (3,4,5,6,7,8)]
    if any(x.isna().any() for x in cols): continue
    op,cl,hi,lo,vo,am=cols; vals=(float(op.iloc[0]),float(hi.max()),float(lo.min()),float(cl.iloc[-1]),float(vo.sum()),float(am.sum())); d=daily.get((code,day))
    diffs=(1,1,1) if not d else (rel(vals[3],d[3]),rel(vals[4],d[4]),rel(vals[5],d[5])); grade='A' if d and diffs[0]<=.005 and diffs[1]<=.001 and diffs[2]<=.01 else 'C'
    dv=d if d else (0,0,0,0,0,0)
    audits.append((code,date.fromisoformat(day),48,*vals,*dv,*diffs,grade,'harry_2006_full_48'))
    if grade=='A': accepted.append((code,day))
    for dt,row in zip(pts,part.itertuples(index=False)):
        raw_rows.append((code,dt.to_pydatetime(),float(row[3]),float(row[5]),float(row[6]),float(row[4]),int(round(float(row[7]))),float(row[8]),datetime.now(),0))

for t in [RAW,ACCEPT,AUDIT]:
    c.command(f'DROP TABLE IF EXISTS {t}')
c.command(f'CREATE TABLE {RAW} ENGINE=MergeTree ORDER BY (code,datetime) SETTINGS allow_nullable_key=1 AS SELECT * FROM stock.kline_minute_5 LIMIT 0')
c.insert(RAW,raw_rows,column_names=['code','datetime','open','high','low','close','volume','amount','created_at','id'])
schema='''CREATE TABLE {table} (code String,trade_date Date,source_rows UInt16,candidate_open Float64,candidate_high Float64,candidate_low Float64,candidate_close Float64,candidate_volume Float64,candidate_amount Float64,daily_open Float64,daily_high Float64,daily_low Float64,daily_close Float64,daily_volume Float64,daily_amount Float64,price_diff Float64,volume_diff Float64,amount_diff Float64,grade LowCardinality(String),reason String) ENGINE=MergeTree ORDER BY (trade_date,code)'''
c.command(schema.format(table=AUDIT))
c.insert(AUDIT,audits,column_names=['code','trade_date','source_rows','candidate_open','candidate_high','candidate_low','candidate_close','candidate_volume','candidate_amount','daily_open','daily_high','daily_low','daily_close','daily_volume','daily_amount','price_diff','volume_diff','amount_diff','grade','reason'])
c.command(f'CREATE TABLE {ACCEPT} ENGINE=MergeTree ORDER BY (code,datetime) SETTINGS allow_nullable_key=1 AS SELECT * FROM {RAW} LIMIT 0')
if accepted:
    cond=','.join(f"('{a}','{b}')" for a,b in accepted)
    c.command(f'INSERT INTO {ACCEPT} SELECT r.* FROM {RAW} r WHERE (r.code,toDate(r.datetime)) IN ({cond})')
result={'run_id':RUN,'source_zip':ZIP,'target_code_days':len(keyset),'materialized_rows':len(raw_rows),'accepted_code_days':len(accepted),'accepted_rows':len(accepted)*48,'audit_table':AUDIT,'candidate_table':ACCEPT,'report':str(OUT)}
OUT.parent.mkdir(parents=True,exist_ok=True)
OUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))

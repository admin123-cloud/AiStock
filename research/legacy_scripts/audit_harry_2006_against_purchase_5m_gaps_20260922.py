import clickhouse_connect, zipfile, io, pandas as pd, json
from collections import Counter

c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
q="SELECT code,trade_date,actual_count,missing_count FROM stock.purchased_history_data_review_items WHERE run_id='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v4_daily_purchase_volume' AND period='5m' AND source_batch_id='purchased_5m_stage_2006_20260917_r1'"
keys=c.query(q).result_rows
zip_path=r'E:\AStockHistory\harry\2006_5min.zip'
z=zipfile.ZipFile(zip_path); names=set(z.namelist()); out=[]
for code,d,actual,missing in keys:
    fn=('sh' if code.endswith('.SH') else 'sz')+code[:6]+'_2006.csv'
    if fn not in names:
        out.append((code,str(d),actual,missing,None,0,'file_missing')); continue
    try:
        df=pd.read_csv(io.BytesIO(z.read(fn)),encoding='utf-8-sig')
        dt=pd.to_datetime(df.iloc[:,0],errors='coerce').dt.date
        cnt=int((dt==d).sum())
        out.append((code,str(d),actual,missing,fn,cnt,'full' if cnt>=48 else 'partial' if cnt else 'no_day'))
    except Exception as e:
        out.append((code,str(d),actual,missing,fn,0,'error:'+str(e)[:80]))
counts=Counter(x[-1] for x in out)
payload={'source_zip':zip_path,'target_rows':len(out),'classes':dict(counts),'rows':[{'code':a,'trade_date':b,'purchase_actual':c,'purchase_missing':d,'harry_file':e,'harry_bars':f,'status':g} for a,b,c,d,e,f,g in out]}
out_path=r'F:\AiStock\data\data\runtime\purchased_history\harry_2006_vs_purchase_5m_gap_audit_20260922.json'
open(out_path,'w',encoding='utf-8').write(json.dumps(payload,ensure_ascii=False,indent=2))
print(json.dumps({'target_rows':len(out),'classes':dict(counts),'report':out_path},ensure_ascii=False,indent=2))

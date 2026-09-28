import json
from datetime import datetime
from pathlib import Path
import clickhouse_connect

c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
audit='stock.pre2010_bc_reaggregate_30m_audit_20260922_v4'
raw='stock.pre2010_bc_reaggregate_30m_raw_20260922_v4'
rows=c.query(f"SELECT code,trade_date,candidate_close,candidate_volume,candidate_amount,price_relative_diff,volume_relative_diff,amount_relative_diff FROM {audit} WHERE grade='A' AND price_relative_diff<=0.005 AND volume_relative_diff<=0.001 AND amount_relative_diff<=0.01 ORDER BY trade_date,code").result_rows
selected=[]; checked=[]
for code,day,cc,cv,ca,cp,cvd,cad in rows:
    f=c.query(f"SELECT argMin(open,datetime),max(high),min(low),argMax(close,datetime),sum(volume),sum(amount),count() FROM stock.kline_minute_30 WHERE code='{code}' AND toDate(datetime)=toDate('{day}')").result_rows[0]
    if int(f[6])==0:
        chosen=True; fe=None
    else:
        fe=sum(abs(float(x)-float(y))/max(abs(float(y)),1.0) for x,y in [(cc,f[3]),(cv,f[4]),(ca,f[5])])
        ce=float(cp)+float(cvd)+float(cad); chosen=ce < fe
    checked.append({'code':code,'trade_date':str(day),'candidate_error':float(cp)+float(cvd)+float(cad),'formal_error':fe,'chosen':chosen})
    if chosen: selected.append((code,day))
c.command('DROP TABLE IF EXISTS stock.pre2010_30m_strict_a_selected_20260922')
c.command('CREATE TABLE stock.pre2010_30m_strict_a_selected_20260922 (code String, trade_date Date) ENGINE=MergeTree ORDER BY (trade_date,code)')
if selected: c.insert('stock.pre2010_30m_strict_a_selected_20260922',selected,column_names=['code','trade_date'])
c.command('DROP TABLE IF EXISTS stock.pre2010_30m_strict_a_candidate_20260922')
c.command('CREATE TABLE stock.pre2010_30m_strict_a_candidate_20260922 AS stock.kline_minute_30')
c.command(f"INSERT INTO stock.pre2010_30m_strict_a_candidate_20260922 SELECT r.* FROM {raw} r INNER JOIN stock.pre2010_30m_strict_a_selected_20260922 k ON r.code=k.code AND toDate(r.datetime)=k.trade_date")
result={'strict_a_keys':len(rows),'selected_better_keys':len(selected),'candidate_rows':int(c.query('SELECT count() FROM stock.pre2010_30m_strict_a_candidate_20260922').result_rows[0][0]),'checked':checked,'created_at':datetime.now().astimezone().isoformat()}
Path(r'F:\AiStock\data\data\runtime\purchased_history\30m_strict_a_selection_20260922.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))

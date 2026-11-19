"""Cross-check unresolved daily keys against free Sina/Eastmoney/Tencent feeds."""
from __future__ import annotations
import json
from datetime import date, datetime, timedelta
from pathlib import Path
import requests
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audit_purchased_review_against_external_daily import _fetch_provider
import clickhouse_connect

RUN='purchased_history_review_20260922_refit_a_b_confirmed_20260922_v2'
OUT=Path(r'F:\AiStock\data\data\runtime\purchased_history\daily_free_source_audit_20260922.json')

def tencent(code: str, year: int):
    sym=('sh' if code.endswith('.SH') else 'sz')+code[:6]
    u=f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,{year}-01-01,{year}-12-31,640,'
    obj=requests.get(u,timeout=20).json()
    return obj.get('data',{}).get(sym,{}).get('day',[])

def main():
    c=clickhouse_connect.get_client(host='localhost',port=8123,username='default')
    rows=c.query(f"SELECT code,trade_date FROM stock.purchased_history_data_review_items WHERE run_id='{RUN}' AND issue_type='purchased_daily_baostock_unresolved' ORDER BY trade_date,code").result_rows
    results=[]
    for code, day in rows:
        day=str(day); year=int(day[:4]); item={'code':code,'trade_date':day,'providers':{},'local_5m_bars':0}
        for provider in ('sina','eastmoney'):
            try:
                frame=_fetch_provider(code,year,provider)
                hit=frame[frame['date']==day]
                dates=frame['date'].tolist()
                item['providers'][provider]={'returned_rows':len(frame),'exact_hit':not hit.empty,'row':hit.to_dict('records')[0] if not hit.empty else None,'nearest_before':max([x for x in dates if x<day],default=None),'nearest_after':min([x for x in dates if x>day],default=None)}
            except Exception as exc:
                item['providers'][provider]={'error':str(exc)[:300],'exact_hit':False}
        try:
            raw=tencent(code,year)
            hit=[x for x in raw if x[0]==day]
            dates=[x[0] for x in raw]
            item['providers']['tencent']={'returned_rows':len(raw),'exact_hit':bool(hit),'row':hit[0] if hit else None,'nearest_before':max([x for x in dates if x<day],default=None),'nearest_after':min([x for x in dates if x>day],default=None)}
        except Exception as exc:
            item['providers']['tencent']={'error':str(exc)[:300],'exact_hit':False}
        q=f"SELECT count() FROM stock.kline_minute_5 WHERE code='{code}' AND toDate(datetime)=toDate('{day}')"
        item['local_5m_bars']=int(c.query(q).result_rows[0][0])
        item['decision']='no_free_provider_exact_bar' if not any(v.get('exact_hit') for v in item['providers'].values()) else 'free_provider_bar_found'
        results.append(item)
    summary={'run_id':RUN,'target_rows':len(results),'exact_bar_found':sum(x['decision']=='free_provider_bar_found' for x in results),'no_exact_bar':sum(x['decision']=='no_free_provider_exact_bar' for x in results),'formal_table_mutation':False,'generated_at':datetime.now().astimezone().isoformat()}
    OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps({'summary':summary,'results':results},ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'summary':summary,'report':str(OUT)},ensure_ascii=False,indent=2))
if __name__=='__main__': main()

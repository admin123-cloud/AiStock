"""强势板块深跌后，龙头 -5%~-10% 恐慌低开的事件研究。"""
from __future__ import annotations

import sys as _bootstrap_sys
from pathlib import Path as _BootstrapPath
_bootstrap_sys.path.insert(0, str(_BootstrapPath(__file__).resolve().parents[2]))
from research.bootstrap import prepare_script, PROJECT_ROOT as _PROJECT_ROOT
from utils.paths import report_path as _report_path, data_path as _data_path, artifacts_root as _artifacts_root, logs_root as _logs_root
prepare_script()

import json, sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=_PROJECT_ROOT
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from scripts import backtest_sector_first_pullback_panic_v1 as base
from scripts.backtest_sector_pullback_leader_v1 import _event_values
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path

OUT=report_path('sector_deep_panic_open_v1'); COST=.002

def deep_windows(sectors: pd.DataFrame) -> pd.DataFrame:
    rows=[]
    for code,f in sectors.groupby('code',sort=False):
        f=f.sort_values('trade_date').reset_index(drop=True); c=f.close.to_numpy(float); d=f.trade_date.to_numpy()
        ret=np.zeros(len(f)); ret[1:]=c[1:]/c[:-1]-1
        arm=False; peak=0.; prev_dd=0.
        for i in range(15,len(f)-9):
            w=ret[i-14:i+1]; ups=(w>.0001).sum(); downs=(w<-.0001).sum(); pos=w[w>0].sum(); neg=-w[w<0].sum()
            strong=c[i]/c[i-15]-1>=.20 and ups>=max(6,2*downs) and pos>=max(.001,2*neg)
            if not arm and strong: arm=True; peak=c[i]; prev_dd=0.; continue
            if not arm: continue
            if c[i]>=peak: peak=c[i]; prev_dd=0.; continue
            dd=c[i]/peak-1
            if dd<=-.08:
                rows.append({'sector_code':str(code),'deep_date':pd.Timestamp(d[i]),'window_end':pd.Timestamp(d[i+5]),'sector_drawdown':dd})
                arm=False
            else: prev_dd=dd
    return pd.DataFrame(rows)

def load_stock_windows(events: pd.DataFrame) -> pd.DataFrame:
    e=events.rename_axis('event_id').reset_index()[['event_id','sector_code','deep_date','window_end']]
    q=[]
    for r in e.itertuples(index=False): q.append(f"SELECT {r.event_id} event_id, '{r.sector_code}' sector_code, toDate('{r.deep_date:%Y-%m-%d}') deep_date, toDate('{r.window_end:%Y-%m-%d}') window_end")
    sql=f"""WITH e AS ({' UNION ALL '.join(q)}) SELECT e.event_id,ss.stock_code code,k.trade_date,k.open,k.high,k.low,k.close FROM e INNER JOIN sector_stocks ss ON ss.sector_code=e.sector_code INNER JOIN kline_daily k ON k.code=ss.stock_code WHERE k.trade_date BETWEEN e.deep_date-INTERVAL 45 DAY AND e.window_end+INTERVAL 5 DAY ORDER BY event_id,code,k.trade_date"""
    x=clickhouse_query_df(sql); x.trade_date=pd.to_datetime(x.trade_date)
    for col in ['open','high','low','close']: x[col]=pd.to_numeric(x[col],errors='coerce')
    return x

def limit(code:str)->float:
    return .195 if str(code).startswith(('300','688')) else (.295 if str(code).endswith('.BJ') else .095)

def trades(events:pd.DataFrame, prices:pd.DataFrame)->pd.DataFrame:
    out=[]
    for eid,panel in prices.groupby('event_id',sort=False):
        ev=events.loc[eid]; date=pd.Timestamp(ev.deep_date); end=pd.Timestamp(ev.window_end); chosen=[]
        for date_i in pd.date_range(date+pd.Timedelta(days=1),end,freq='D'):
            candidates=[]
            for code,s in panel.groupby('code',sort=False):
                s=s.sort_values('trade_date').reset_index(drop=True); ix=s.index[s.trade_date==date_i]
                if len(ix)!=1: continue
                i=int(ix[0])
                if i<16 or i+3>=len(s): continue
                prev=float(s.at[i-1,'close']); prior=float(s.at[i-16,'close'])
                if prev<=0 or prior<=0 or float(s.at[i,'open'])<=0: continue
                gap=float(s.at[i,'open'])/prev-1
                runup=prev/prior-1
                # 开盘即跌停没有开盘成交可得性，排除；其余按开盘成交。
                if -.10<=gap<=-.05 and gap>-limit(str(code)) and float(s.at[i,'open'])>float(s.at[i,'low']):
                    candidates.append((runup,str(code),s,i,gap))
            if candidates:
                chosen=sorted(candidates,reverse=True)[:3]; break
        if not chosen: continue
        row={'event_id':eid,'entry_date':chosen[0][2].at[chosen[0][3],'trade_date'],'sector_drawdown':ev.sector_drawdown,'leader_count':len(chosen),'leader_codes':'|'.join(z[1] for z in chosen),'avg_leader_runup':np.mean([z[0] for z in chosen]),'avg_gap':np.mean([z[4] for z in chosen])}
        for h in (1,2,3):
            rr=[]
            for _,_,s,i,_ in chosen: rr.append(float(s.at[i+h-1,'close'])/float(s.at[i,'open'])-1-COST)
            row[f'net_ret_{h}d']=np.mean(rr)
        out.append(row)
    return pd.DataFrame(out)

def main()->int:
    sec=base._load_sectors('2020-01-01','2025-12-31'); broad=base._load_market_breadth('2020-01-01','2025-12-31')
    ev=deep_windows(sec).reset_index(drop=True); px=load_stock_windows(ev); t=trades(ev,px).merge(broad[['trade_date','down_ratio']],left_on='entry_date',right_on='trade_date',how='left').drop(columns='trade_date')
    rows=[]
    for threshold in (0,.60,.65,.70):
        x=t if threshold==0 else t[t.down_ratio>=threshold]
        for h in (1,2,3):
            r=x[f'net_ret_{h}d']; rows.append({'down_ratio_min':threshold,'hold_days':h,'n':len(x),'avg_net_ret':r.mean(),'win_rate':(r>0).mean(),'p25':r.quantile(.25),'worst':r.min()})
    sm=pd.DataFrame(rows); OUT.mkdir(parents=True,exist_ok=True); ev.to_csv(OUT/'deep_sector_events.csv',index=False,encoding='utf-8-sig');t.to_csv(OUT/'trades.csv',index=False,encoding='utf-8-sig');sm.to_csv(OUT/'summary.csv',index=False,encoding='utf-8-sig')
    lines=['# 深跌后龙头恐慌低开回测','', '- 板块先在 15 日内上涨至少 20% 且涨多跌少；随后从峰值跌破 8%。','- 此后 5 个交易日内，板块成员按前一日可见的 15 日涨幅选最强的最多 3 只；首次出现相对前收盘低开 5%--10%、且非开盘跌停、开盘后有成交区间时按开盘买入。','- 收益为开盘买入至第 N 个交易日收盘，扣双边 20bp。','', '| 大盘下跌家数阈值 | 持有日 | 事件数 | 平均净收益 | 胜率 | P25 | 最差 |','|---:|---:|---:|---:|---:|---:|---:|']
    for r in sm.itertuples(index=False): lines.append(f"| {'不加' if r.down_ratio_min==0 else '>='+str(round(r.down_ratio_min*100))+'%'} | {r.hold_days} | {r.n} | {base._pct(r.avg_net_ret)} | {base._pct(r.win_rate)} | {base._pct(r.p25)} | {base._pct(r.worst)} |")
    (OUT/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');(OUT/'summary.json').write_text(json.dumps({'deep_events':len(ev),'trades':len(t),'summary':sm.to_dict('records')},ensure_ascii=False,indent=2,default=str),encoding='utf-8');print(json.dumps({'out':str(OUT),'deep_events':len(ev),'trades':len(t)},ensure_ascii=False));return 0
if __name__=='__main__': raise SystemExit(main())

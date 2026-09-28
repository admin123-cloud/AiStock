"""Research-only PIT sector leader replay using the public SW2014 interval file."""
from __future__ import annotations
import argparse, json, time, sys
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path

MEM_PATH=report_path('sector_mainwave_leader_rebuild_v2','hf_sw2014_membership_history.parquet')

def run(args):
    t0=time.perf_counter()
    mem=pd.read_parquet(MEM_PATH)
    mem=mem[(mem.sector_level==args.level)&(mem.in_date.notna())].copy()
    mem['canonical_code']=mem.canonical_code.astype(str)
    codes=sorted(mem.canonical_code.unique())
    parts=[]
    for i in range(0,len(codes),500):
        q=','.join("'"+c.replace("'","\\'")+"'" for c in codes[i:i+500])
        parts.append(clickhouse_query_df(f"SELECT code,trade_date,open,close,amount FROM kline_daily WHERE code IN ({q}) AND trade_date BETWEEN '{args.start}' AND '{args.end}'"))
    d=pd.concat(parts,ignore_index=True) if parts else pd.DataFrame()
    print(f'loaded prices {len(d)} rows in {time.perf_counter()-t0:.1f}s',flush=True)
    d['trade_date']=pd.to_datetime(d.trade_date,errors='coerce'); d['open']=pd.to_numeric(d.open,errors='coerce'); d['close']=pd.to_numeric(d.close,errors='coerce'); d['amount']=pd.to_numeric(d.amount,errors='coerce')
    d=d.dropna(subset=['code','trade_date','open','close']).drop_duplicates(['code','trade_date']).sort_values(['code','trade_date'])
    d=d[(d.open>0)&(d.close>0)]
    d['ret15']=d.groupby('code').close.transform(lambda s:s/s.shift(15)-1)
    d['ma20']=d.groupby('code').close.transform(lambda s:s.rolling(20,min_periods=15).mean())
    d['amount20']=d.groupby('code').amount.transform(lambda s:s.rolling(20,min_periods=10).mean())
    # Expand only the interval relations that overlap the loaded price window.
    mem['in_date']=pd.to_datetime(mem.in_date); mem['out_date']=pd.to_datetime(mem.out_date)
    mem=mem[(mem.out_date.isna())|(mem.out_date>pd.Timestamp(args.start))]
    joined=d.merge(mem[['canonical_code','sector_code','sector_name','in_date','out_date']],left_on='code',right_on='canonical_code',how='inner')
    joined=joined[(joined.trade_date>=joined.in_date)&(joined.out_date.isna()|(joined.trade_date<joined.out_date))]
    joined=joined.drop_duplicates(['code','trade_date','sector_code'])
    joined['above']=joined.close.ge(joined.ma20)&joined.ma20.notna()
    dates=sorted(joined.trade_date.unique()); prices={c:g.set_index('trade_date').open for c,g in d.groupby('code')}
    rows=[]
    for i,(day,today) in enumerate(joined.groupby('trade_date',sort=True)):
        if i+1>=len(dates): continue
        sector=today.groupby('sector_code').agg(n=('code','nunique'),above=('above','sum'),avg=('ret15','mean')).reset_index()
        sector['breadth']=sector.above/sector.n.clip(lower=1)
        sector=sector[(sector.n>=20)&(sector.breadth>=args.breadth)&(sector.avg>0)].sort_values(['breadth','avg'],ascending=False)
        picks=[]
        for sec in sector.sector_code:
            sub=today[(today.sector_code==sec)&today.ret15.notna()&today.ma20.notna()&today.above&(today.amount20>=args.min_amount)].copy()
            sub=sub[sub.ret15>=sub.ret15.mean()+args.min_excess].sort_values(['ret15','amount20'],ascending=False)
            for code in sub.code:
                if code not in picks: picks.append(code); break
            if len(picks)>=args.slots: break
        entry=dates[i+1]
        for code in picks:
            p=prices.get(code)
            if p is None or entry not in p.index: continue
            r={'signal_date':str(pd.Timestamp(day).date()),'entry_date':str(pd.Timestamp(entry).date()),'code':code}
            for h in (3,5):
                x=dates[i+1+h] if i+1+h<len(dates) else None
                r[f'ret_{h}d']=None if x is None or x not in p.index else float(p.loc[x]/p.loc[entry]-1-args.cost_bps/10000)
            rows.append(r)
    out={'research_only':True,'execution_valid':False,'industry_version':'SW2014','sector_level':args.level,'membership_source':str(MEM_PATH),'start':args.start,'end':args.end,'records':len(rows),'rows':rows,'limitations':['SW2014_not_SW2021','overlapping_events_not_account_returns','historical_price_fill_and_survivorship_require_further_audit']}
    for h in (3,5):
        v=[r[f'ret_{h}d'] for r in rows if r[f'ret_{h}d'] is not None]; out[f'mean_return_{h}d']=sum(v)/len(v) if v else None; out[f'positive_rate_{h}d']=sum(x>0 for x in v)/len(v) if v else None
    path=report_path('sector_mainwave_leader_rebuild_v2',f'pit_sw2014_l{args.level}_backtest_v1.json'); path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in out.items() if k!='rows'},ensure_ascii=False,indent=2)); return 0

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--start',default='2020-01-01'); p.add_argument('--end',default='2025-10-31'); p.add_argument('--level',type=int,choices=[1,2,3],default=3); p.add_argument('--breadth',type=float,default=.55); p.add_argument('--min-amount',type=float,default=100_000_000); p.add_argument('--min-excess',type=float,default=.20); p.add_argument('--slots',type=int,default=3); p.add_argument('--cost-bps',type=float,default=200); raise SystemExit(run(p.parse_args()))

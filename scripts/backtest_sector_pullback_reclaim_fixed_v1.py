"""Compare direct leader chasing with pullback/reclaim entries on frozen members."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path

def run(a):
    mem=pd.read_parquet(report_path("sector_mainwave_leader_rebuild_v2","membership_snapshots",f"membership_{a.snapshot}.parquet"))
    mem["stock_code"]=mem.canonical_code.astype(str); mem=mem.drop_duplicates(["sector_code","stock_code"])
    codes=sorted(mem.stock_code.unique()); parts=[]
    for j in range(0,len(codes),400):
        q=",".join("'"+x.replace("'","\\'")+"'" for x in codes[j:j+400])
        parts.append(clickhouse_query_df(f"SELECT code,trade_date,open,close,amount FROM kline_daily WHERE code IN ({q}) AND trade_date BETWEEN '{a.start}' AND '{a.end}'"))
    d=pd.concat(parts,ignore_index=True); d.trade_date=pd.to_datetime(d.trade_date,errors="coerce")
    for c in ["open","close","amount"]: d[c]=pd.to_numeric(d[c],errors="coerce")
    d=d.dropna(subset=["trade_date","open","close"]).sort_values(["code","trade_date"]).drop_duplicates(["code","trade_date"])
    g=d.groupby("code",group_keys=False)
    d["ret15"]=g.close.transform(lambda s:s/s.shift(15)-1); d["ma5"]=g.close.transform(lambda s:s.rolling(5,min_periods=5).mean()); d["ma20"]=g.close.transform(lambda s:s.rolling(20,min_periods=15).mean()); d["amount20"]=g.amount.transform(lambda s:s.rolling(20,min_periods=10).mean()); d["high5"]=g.close.transform(lambda s:s.rolling(5,min_periods=5).max().shift(1)); d["dd5"]=d.close/g.close.transform(lambda s:s.rolling(5,min_periods=5).max())-1
    d["above20"]=d.close.ge(d.ma20)&d.ma20.notna()
    groups={day:g for day,g in d.groupby("trade_date",sort=True)}; dates=sorted(groups); prices={c:g[["trade_date","open"]].drop_duplicates("trade_date",keep="last").set_index("trade_date").open for c,g in d.groupby("code")}; rec=[]
    for i,day in enumerate(dates[:-6]):
        today=groups[day].merge(mem[["sector_code","stock_code"]],left_on="code",right_on="stock_code",how="inner"); s=today.groupby("sector_code").agg(n=("code","nunique"),breadth=("above20","mean"),avg=("ret15","mean")).reset_index(); s=s[(s.n>=20)&(s.breadth>=a.breadth)&(s.avg>0)].sort_values(["breadth","avg"],ascending=False)
        eligible=today[today.sector_code.isin(s.sector_code)&today.ret15.notna()&today.amount20.ge(a.min_amount)&today.above20]
        avg_map=s.set_index("sector_code")["avg"].to_dict()
        eligible=eligible.assign(excess15=eligible.ret15-eligible.sector_code.map(avg_map))
        variants={
          "direct": eligible.sort_values(["ret15","amount20"],ascending=False),
          "relative": eligible[eligible.excess15>=a.min_excess].sort_values(["excess15","ret15","amount20"],ascending=False),
          "pullback": eligible[(eligible.ret15>=a.min_ret15)&(eligible.dd5.between(-a.max_pullback,-a.min_pullback))].sort_values(["ret15","amount20"],ascending=False),
          "reclaim": eligible[(eligible.ret15>=a.min_ret15)&(eligible.close>eligible.ma5)&(eligible.close.shift(0)>eligible.high5)].sort_values(["ret15","amount20"],ascending=False),
        }
        for variant, cand in variants.items():
            picks=[]
            for sec in s.sector_code:
                sub=cand[cand.sector_code.eq(sec)&~cand.code.isin(picks)]
                if not sub.empty: picks.append(sub.iloc[0].code)
                if len(picks)>=a.slots: break
            for code in picks:
                p=prices[code]; entry=dates[i+1]; row={"variant":variant,"signal_date":str(pd.Timestamp(day).date()),"entry_date":str(pd.Timestamp(entry).date()),"code":code}
                if entry not in p.index: continue
                for h in [3,5]:
                    x=dates[i+1+h]; row[f"ret_{h}d"]=float(p.loc[x]/p.loc[entry]-1-a.cost_bps/10000) if x in p.index else None
                rec.append(row)
    out={"research_only":True,"execution_valid":False,"frozen_membership_diagnostic":True,"historical_membership_bias":True,"start":a.start,"snapshot":a.snapshot,"end":a.end,"records":len(rec),"rows":rec,"params":vars(a)}
    for v in sorted(set(r['variant'] for r in rec)):
        out[v]={}
        for h in [3,5]:
            z=[r[f'ret_{h}d'] for r in rec if r['variant']==v and r[f'ret_{h}d'] is not None]; out[v][f'{h}d']={"n":len(z),"mean":sum(z)/len(z) if z else None,"positive_rate":sum(x>0 for x in z)/len(z) if z else None,"median":float(pd.Series(z).median()) if z else None}
    path=report_path('sector_mainwave_leader_rebuild_v2',getattr(a,'output_name','pullback_reclaim_fixed_v1.json')); path.write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str),encoding='utf-8'); return {k:v for k,v in out.items() if k!='rows'}

if __name__=='__main__':
 p=argparse.ArgumentParser(); p.add_argument('--start',default='2020-01-01'); p.add_argument('--snapshot',default='2026-07-06'); p.add_argument('--end',default='2026-09-24'); p.add_argument('--breadth',type=float,default=.55); p.add_argument('--min-amount',type=float,default=100_000_000); p.add_argument('--min-ret15',type=float,default=.10); p.add_argument('--min-excess',type=float,default=.10); p.add_argument('--min-pullback',type=float,default=.02); p.add_argument('--max-pullback',type=float,default=.12); p.add_argument('--slots',type=int,default=3); p.add_argument('--cost-bps',type=float,default=200); print(json.dumps(run(p.parse_args()),ensure_ascii=False,indent=2))

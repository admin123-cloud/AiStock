"""Short forward-style replay using one frozen membership snapshot."""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path

def run(args):
    started=time.perf_counter()
    mem_path=report_path("sector_mainwave_leader_rebuild_v2","membership_snapshots",f"membership_{args.snapshot}.parquet")
    mem=pd.read_parquet(mem_path); mem["stock_code"]=mem["canonical_code"].astype(str)
    mem=mem.drop_duplicates(["sector_code","stock_code"])
    codes=sorted(mem.stock_code.unique())
    parts=[]
    for start in range(0, len(codes), 400):
        q=",".join("'"+c.replace("'","\\'")+"'" for c in codes[start:start+400])
        parts.append(clickhouse_query_df(f"SELECT code,trade_date,open,close,amount FROM kline_daily WHERE code IN ({q}) AND trade_date BETWEEN '{args.snapshot}' AND '{args.end}'"))
    d=pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    print(f"loaded {len(d)} price rows in {time.perf_counter()-started:.2f}s", flush=True)
    d["trade_date"]=pd.to_datetime(d.trade_date,errors="coerce")
    for c in ["open","close","amount"]: d[c]=pd.to_numeric(d[c],errors="coerce")
    d=d.dropna(subset=["trade_date","open","close"]).sort_values(["code","trade_date"])
    d=d.drop_duplicates()
    if d.duplicated(["code","trade_date"]).any():
        raise ValueError("Conflicting daily rows require source reconciliation")
    d=d[(d.open>0)&(d.close>0)]
    d["ret15"]=d.groupby("code").close.transform(lambda s:s/s.shift(15)-1)
    d["ma20"]=d.groupby("code").close.transform(lambda s:s.rolling(20,min_periods=15).mean())
    d["amount20"]=d.groupby("code").amount.transform(lambda s:s.rolling(20,min_periods=10).mean())
    prices={code:g.set_index("trade_date").open for code,g in d.groupby("code")}
    d["above"]=d.close.ge(d.ma20)&d.ma20.notna()
    d=d.merge(mem[["sector_code","stock_code"]],left_on="code",right_on="stock_code",how="inner")
    print(f"features and membership join: {len(d)} rows in {time.perf_counter()-started:.2f}s", flush=True)
    dates=sorted(d.trade_date.unique()); rec=[]
    for i,(day,today) in enumerate(d.groupby("trade_date",sort=True)):
        if i+1>=len(dates): break
        s=today.groupby("sector_code").agg(n=("code","nunique"),above=("above","sum"),avg=("ret15","mean")).reset_index()
        s["breadth"]=s.above/s.n.clip(lower=1); s=s[(s.n>=20)&(s.breadth>=args.breadth)&(s.avg>0)].sort_values(["breadth","avg"],ascending=False)
        picks=[]
        for sec in s.sector_code:
            sub=today[(today.sector_code==sec)&today.ret15.notna()&(today.ma20.notna())&(today.close>=today.ma20)&(today.amount20>=args.min_amount)].sort_values(["ret15","amount20"],ascending=False)
            sub=sub[~sub.code.isin(picks)]
            if not sub.empty: picks.append(sub.iloc[0].code)
            if len(picks)>=args.slots: break
        entry=dates[i+1]
        for code in picks:
            p=prices[code]
            if entry not in p.index: continue
            row={"signal_date":str(pd.Timestamp(day).date()),"entry_date":str(pd.Timestamp(entry).date()),"code":code}
            for h in [3,5]:
                x=dates[i+1+h] if i+1+h<len(dates) else None
                row[f"ret_{h}d"]=None if x is None or x not in p.index else float(p.loc[x]/p.loc[entry]-1-args.cost_bps/10000)
            rec.append(row)
    out={"research_only":True,"proxy_type":"frozen_membership_forward_replay","snapshot":args.snapshot,"end":args.end,"records":len(rec),"rows":rec}
    out.update(execution_valid=False,is_independent_forward=False,elapsed_seconds=time.perf_counter()-started,
               limitations=["retrospective_event_study_not_account_returns", "overlapping_events", "fills_and_price_basis_unverified", "snapshot_availability_timestamp_unverified", "TDX_research_membership_not_QMT_canonical"])
    for h in [3,5]:
        vals=[r[f"ret_{h}d"] for r in rec if r[f"ret_{h}d"] is not None]; out[f"mean_return_{h}d"]=sum(vals)/len(vals) if vals else None; out[f"positive_rate_{h}d"]=sum(v>0 for v in vals)/len(vals) if vals else None
    path=report_path("sector_mainwave_leader_rebuild_v2","fixed_membership_forward_v1.json"); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8"); return {k:v for k,v in out.items() if k!='rows'}

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--snapshot',default='2026-07-06'); p.add_argument('--end',default='2026-09-24'); p.add_argument('--breadth',type=float,default=.55); p.add_argument('--min-amount',type=float,default=100_000_000); p.add_argument('--slots',type=int,default=3); p.add_argument('--cost-bps',type=float,default=200); print(json.dumps(run(p.parse_args()),ensure_ascii=False,indent=2))

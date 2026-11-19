"""Audit point-in-time sector membership availability before backtesting."""
from __future__ import annotations
import json, os, sys
from pathlib import Path
import pandas as pd
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path

def run():
    tables=clickhouse_query_df("SELECT name,total_rows FROM system.tables WHERE database=currentDatabase() AND (lower(name) LIKE '%sector%' OR lower(name) LIKE '%member%') ORDER BY name").to_dict('records')
    src=clickhouse_query_df("SELECT source,min(snapshot_date) min_date,max(snapshot_date) max_date,uniqExact(snapshot_date) snapshot_days,count() rows FROM source_sector_stocks GROUP BY source").to_dict('records')
    canonical=clickhouse_query_df("SELECT min(created_at) min_created,max(created_at) max_created,count() rows FROM sector_stocks").to_dict('records')
    pit_tables=clickhouse_query_df("SELECT industry_version,min(in_date) min_in_date,max(in_date) max_in_date,max(out_date) max_out_date,count() rows,uniqExact(sector_code) sectors,uniqExact(canonical_code) stocks,countIf(out_date IS NOT NULL) out_date_rows FROM source_sector_membership_history GROUP BY industry_version ORDER BY industry_version").to_dict('records') if any(t.get('name')=='source_sector_membership_history' for t in tables) else []
    snap_dir=report_path('sector_mainwave_leader_rebuild_v2','membership_snapshots')
    files=sorted(snap_dir.glob('membership_*.parquet')) if snap_dir.exists() else []
    local=[]
    for f in files:
        try:
            d=pd.read_parquet(f,columns=['snapshot_date','sector_code','canonical_code'])
            local.append({'file':str(f),'min_date':str(d.snapshot_date.min()),'max_date':str(d.snapshot_date.max()),'rows':len(d),'sectors':int(d.sector_code.nunique()),'stocks':int(d.canonical_code.nunique())})
        except Exception as e: local.append({'file':str(f),'error':f'{type(e).__name__}: {e}'})
    interval_path=report_path('sector_mainwave_leader_rebuild_v2','ftshare_sw_membership_history.parquet')
    ftshare_interval={}
    if interval_path.exists():
        try:
            d=pd.read_parquet(interval_path,columns=['sector_code','canonical_code','in_date','out_date'])
            ftshare_interval={'file':str(interval_path),'rows':len(d),'sectors':int(d.sector_code.nunique()),'stocks':int(d.canonical_code.nunique()),'min_in_date':str(pd.to_datetime(d.in_date,errors='coerce').min().date()),'max_in_date':str(pd.to_datetime(d.in_date,errors='coerce').max().date()),'out_date_rows':int(d.out_date.notna().sum()),'point_in_time_usable':bool(d.out_date.notna().any())}
        except Exception as e:
            ftshare_interval={'file':str(interval_path),'error':f'{type(e).__name__}: {e}'}
    pit_available=bool(pit_tables and any(int(x.get('out_date_rows') or 0)>0 for x in pit_tables))
    out={'research_only':True,'as_of':'2026-09-26','historical_membership_available':pit_available,'tables':tables,'source_snapshots':src,'canonical_current_mapping':canonical,'local_snapshots':local,'ftshare_interval_source':ftshare_interval,'pit_interval_sources':pit_tables,'external_sources':{'tushare_token_configured':bool(os.getenv('TUSHARE_TOKEN')),'akshare_current_only':True,'ftshare_public_endpoint_verified':bool(ftshare_interval),'ftshare_out_dates_verified':bool(ftshare_interval.get('out_date_rows',0))},'decision':'allow_version_locked_pit_stock_leader_research' if pit_available else 'block_historical_stock_leader_backtest_until_point_in_time_membership_source','allowed_modes':['sector_index_proxy','forward_snapshot_paper','pit_stock_leader_research_sw2014'] if pit_available else ['sector_index_proxy','forward_snapshot_paper'],'required_import_schema':['source','sector_code','sector_name','canonical_code','snapshot_date','snapshot_at','provenance_uri','provenance_hash']}
    path=report_path('sector_mainwave_leader_rebuild_v2','membership_history_audit.json'); path.write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str),encoding='utf8'); return out

if __name__=='__main__': print(json.dumps(run(),ensure_ascii=False,indent=2,default=str))

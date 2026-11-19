"""Import the verified FTShare current SW membership snapshot into source tables."""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime
from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import report_path

PARQUET = report_path("sector_mainwave_leader_rebuild_v2") / "ftshare_sw_membership_history.parquet"


def suffix(code: str) -> str:
    code = str(code).zfill(6)
    return f"{code}.SH" if code.startswith(("6", "9")) else f"{code}.BJ" if code.startswith(("4", "8")) else f"{code}.SZ"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    df = pd.read_parquet(PARQUET)
    df = df[df["out_date"].isna()].copy()
    snapshot_at = datetime.now().astimezone().replace(tzinfo=None)
    snapshot_date = pd.Timestamp("2026-09-24").date()
    sectors = df[["sector_code", "sector_name", "sector_level"]].drop_duplicates().sort_values(["sector_level", "sector_code"])
    member_rows = []
    for r in df.itertuples(index=False):
        code = suffix(r.canonical_code)
        ident = hashlib.sha256(f"ftshare:{r.sector_code}:{code}:{snapshot_date}".encode()).hexdigest()
        member_rows.append(["ftshare", r.sector_code, r.sector_name, "sw_industry", code, code, "active", "ftshare_history_current", 0.99, snapshot_date, snapshot_at, ident])
    sector_rows = []
    for r in sectors.itertuples(index=False):
        ident = hashlib.sha256(f"ftshare:{r.sector_code}:{snapshot_date}".encode()).hexdigest()
        sector_rows.append(["ftshare", r.sector_code, r.sector_name, "sw_industry", int(r.sector_level), r.sector_code, r.sector_name, "ftshare_history_current", 0.99, "active", snapshot_date, snapshot_at, ident])
    result = {"execute": bool(args.execute), "source": "ftshare", "snapshot_date": str(snapshot_date), "sector_rows": len(sector_rows), "member_rows": len(member_rows), "historical_interval_file": str(PARQUET), "out_date_rows": int(pd.read_parquet(PARQUET, columns=["out_date"])["out_date"].notna().sum())}
    if args.execute:
        ch = clickhouse_client()
        ch.command("""CREATE TABLE IF NOT EXISTS source_sectors (source String, sector_code String, sector_name String, sector_type String, level Int32 DEFAULT 0, canonical_sector_code String DEFAULT '', canonical_sector_name String DEFAULT '', match_method String DEFAULT '', confidence Float64 DEFAULT 0, alias_status String DEFAULT '', snapshot_date Date, snapshot_at DateTime, id String) ENGINE = MergeTree ORDER BY (source, snapshot_date, sector_code)""")
        ch.command("""CREATE TABLE IF NOT EXISTS source_sector_stocks (source String, sector_code String, sector_name String, sector_type String, source_stock_code String, canonical_code String, alias_status String DEFAULT '', match_method String DEFAULT '', confidence Float64 DEFAULT 0, snapshot_date Date, snapshot_at DateTime, id String) ENGINE = MergeTree ORDER BY (source, snapshot_date, sector_code, canonical_code, source_stock_code)""")
        ch.insert("source_sectors", sector_rows, column_names=["source","sector_code","sector_name","sector_type","level","canonical_sector_code","canonical_sector_name","match_method","confidence","alias_status","snapshot_date","snapshot_at","id"])
        ch.insert("source_sector_stocks", member_rows, column_names=["source","sector_code","sector_name","sector_type","source_stock_code","canonical_code","alias_status","match_method","confidence","snapshot_date","snapshot_at","id"])
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

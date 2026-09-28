"""Persist the latest dated sector membership snapshot for forward research."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_query_df
from utils.paths import report_path


def run(as_of: str | None = None) -> dict:
    where = f"AND snapshot_date <= '{as_of}'" if as_of else ""
    df = clickhouse_query_df(
        f"""SELECT source, sector_code, sector_name, sector_type,
                   source_stock_code, canonical_code, alias_status,
                   snapshot_date, snapshot_at
            FROM source_sector_stocks
            WHERE 1=1 {where}"""
    )
    if df.empty:
        raise RuntimeError("no sector membership snapshot")
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    latest = str(df["snapshot_date"].dropna().max())
    df = df[df["snapshot_date"] == latest].copy()
    out_dir = report_path("sector_mainwave_leader_rebuild_v2", "membership_snapshots")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"membership_{latest}.parquet"
    df.to_parquet(path, index=False)
    meta = {"as_of": latest, "rows": int(len(df)), "sectors": int(df.sector_code.nunique()), "stocks": int(df.canonical_code.nunique()), "generated_at_utc": datetime.now(timezone.utc).isoformat(), "research_only": True}
    (out_dir / f"membership_{latest}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--as-of")
    print(json.dumps(run(p.parse_args().as_of), ensure_ascii=False, indent=2))

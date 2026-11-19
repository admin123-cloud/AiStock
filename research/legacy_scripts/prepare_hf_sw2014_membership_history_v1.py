"""Normalize the public Hugging Face SW2014 point-in-time membership file."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
import sys
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.market_warehouse import clickhouse_client
from utils.paths import report_path

REPORT_DIR = report_path("sector_mainwave_leader_rebuild_v2")
INPUT = REPORT_DIR / "hf_equal_industry_202510.parquet"
SOURCE_URI = "https://huggingface.co/buckets/q1232990/Quant_DataLake-bucket/resolve/Equal_Industry/202510.parquet?download=true"
TABLE = "source_sector_membership_history"


def _code(v: object) -> str:
    return str(v or "").strip()


def normalize(raw: pd.DataFrame, file_hash: str) -> pd.DataFrame:
    d = raw.copy()
    d = d[d["exchangeCD"].isin(["XSHG", "XSHE"])].copy()
    d = d[d["industryVersionCD"].astype(str) == "010303"].copy()
    d["canonical_code"] = d["secID"].astype(str).str.replace(".XSHG", ".SH", regex=False).str.replace(".XSHE", ".SZ", regex=False)
    d["in_date"] = pd.to_datetime(d["intoDate"], errors="coerce").dt.date
    d["out_date"] = pd.to_datetime(d["outDate"], errors="coerce").dt.date
    rows: list[dict[str, object]] = []
    for r in d.itertuples(index=False):
        l3 = _code(r.industrySymbol)
        if len(l3) != 6 or not l3.isdigit():
            continue
        levels = [
            (1, l3[:2] + "0000", _code(r.industryName1)),
            (2, l3[:4] + "00", _code(r.industryName2)),
            (3, l3, _code(r.industryName3)),
        ]
        for level, code, name in levels:
            # The downloaded file has a few names decoded as replacement chars;
            # keep the stable code and expose a deterministic fallback name.
            if not name or "�" in name:
                name = f"SW2014-{code}"
            rows.append({
                "source": "huggingface_quant_datalake",
                "industry_version": "SW2014",
                "sector_level": level,
                "sector_code": f"{code}.SI",
                "sector_name": name,
                "canonical_code": r.canonical_code,
                "stock_name": _code(r.secShortName),
                "in_date": r.in_date,
                "out_date": r.out_date,
                "source_uri": SOURCE_URI,
                "source_hash": file_hash,
            })
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.drop_duplicates(["industry_version", "sector_level", "sector_code", "canonical_code", "in_date", "out_date"]).sort_values(["sector_level", "sector_code", "canonical_code", "in_date"])


def insert_clickhouse(df: pd.DataFrame) -> None:
    ch = clickhouse_client()
    ch.command(f"""CREATE TABLE IF NOT EXISTS {TABLE}
    (
      source String,
      industry_version String,
      sector_level UInt8,
      sector_code String,
      sector_name String,
      canonical_code String,
      stock_name String,
      in_date Date,
      out_date Nullable(Date),
      source_uri String,
      source_hash String
    ) ENGINE = MergeTree ORDER BY (industry_version, sector_level, sector_code, canonical_code, in_date)""")
    rows = []
    for r in df.itertuples(index=False):
        rows.append([r.source, r.industry_version, int(r.sector_level), r.sector_code, r.sector_name, r.canonical_code, r.stock_name, r.in_date, None if pd.isna(r.out_date) else r.out_date, r.source_uri, r.source_hash])
    if rows:
        ch.insert(TABLE, rows, column_names=["source","industry_version","sector_level","sector_code","sector_name","canonical_code","stock_name","in_date","out_date","source_uri","source_hash"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    if not INPUT.exists():
        raise SystemExit(f"missing input: {INPUT}")
    raw_bytes = INPUT.read_bytes()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    df = normalize(pd.read_parquet(INPUT), file_hash)
    out_path = REPORT_DIR / "hf_sw2014_membership_history.parquet"
    df.to_parquet(out_path, index=False)
    meta = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_uri": SOURCE_URI,
        "source_hash": file_hash,
        "input_rows": int(pd.read_parquet(INPUT).shape[0]),
        "normalized_rows": int(len(df)),
        "sector_count": int(df.sector_code.nunique()) if not df.empty else 0,
        "stock_count": int(df.canonical_code.nunique()) if not df.empty else 0,
        "min_in_date": str(df.in_date.min()) if not df.empty else None,
        "max_out_date": str(pd.to_datetime(df.out_date, errors="coerce").max().date()) if not df.empty and pd.to_datetime(df.out_date, errors="coerce").notna().any() else None,
        "out_date_rows": int(df.out_date.notna().sum()) if not df.empty else 0,
        "industry_version": "SW2014",
        "execute": bool(args.execute),
        "table": TABLE if args.execute else None,
        "research_only": True,
    }
    if args.execute:
        insert_clickhouse(df)
    (REPORT_DIR / "hf_sw2014_membership_history.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

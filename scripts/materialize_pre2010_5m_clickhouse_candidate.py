from __future__ import annotations

"""Load isolated pre-2010 5m detail JSONL into a non-formal candidate table.

The table name must use the ``pre2010_repair_5m_candidate_`` namespace. This
script never touches ``stock.kline_minute_5`` or any formal derived table.
Use ``--write`` explicitly; without it only counts the source detail rows.
"""

import argparse
import gzip
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

TZ = ZoneInfo("Asia/Shanghai")
TABLE_RE = re.compile(r"^stock\.pre2010_repair_5m_candidate_[A-Za-z0-9_]+$")
COLUMNS = ["batch_id", "code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_rows(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def ddl(table: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {table}
    (
      batch_id String,
      code String,
      datetime DateTime('Asia/Shanghai'),
      open Float64,
      high Float64,
      low Float64,
      close Float64,
      volume Int64,
      amount Float64,
      created_at DateTime('Asia/Shanghai'),
      id UInt64
    ) ENGINE = MergeTree
    PARTITION BY toYYYYMM(datetime)
    ORDER BY (batch_id, code, datetime)
    """


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--detail", type=Path, required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--batch-size", type=int, default=50000)
    p.add_argument("--write", action="store_true")
    args = p.parse_args()
    if not 2000 <= args.year <= 2009:
        raise SystemExit("year must remain in frozen 2000-2009")
    if not TABLE_RE.fullmatch(args.table):
        raise SystemExit("table must use isolated pre2010_repair_5m_candidate_ namespace")
    if not args.detail.is_file():
        raise SystemExit(f"missing detail file: {args.detail}")
    args.output_root.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    batch = f"pre2010_repair_5m_20260921_{args.year}"
    source_rows = 0
    for _ in iter_rows(args.detail):
        source_rows += 1
    if args.write:
        client.command(ddl(args.table))
        existing = client.query(f"SELECT count() FROM {args.table} WHERE batch_id=%(batch)s", parameters={"batch": batch}).result_rows[0][0]
        if int(existing or 0):
            raise SystemExit(f"refusing duplicate batch {batch}: {existing} rows already present")
        created_at = datetime.now(TZ).replace(microsecond=0)
        rows: list[list[Any]] = []
        ordinal = 0
        for raw in iter_rows(args.detail):
            ordinal += 1
            dt = datetime.fromisoformat(str(raw["datetime"])).replace(tzinfo=TZ)
            volume = float(raw.get("volume_lots") or 0.0)
            if volume != int(volume):
                raise ValueError(f"non-integral volume at ordinal {ordinal}")
            rows.append([
                batch,
                str(raw["code"]),
                dt,
                float(raw["open"]),
                float(raw["high"]),
                float(raw["low"]),
                float(raw["close"]),
                int(volume),
                float(raw.get("amount_yuan") or 0.0),
                created_at,
                ordinal,
            ])
            if len(rows) >= args.batch_size:
                client.insert(args.table, rows, column_names=COLUMNS)
                rows.clear()
        if rows:
            client.insert(args.table, rows, column_names=COLUMNS)
    actual = client.query(f"SELECT count(), uniqExact(tuple(code,datetime)) FROM {args.table} WHERE batch_id=%(batch)s", parameters={"batch": batch}).result_rows[0] if args.write else (0, 0)
    manifest = {
        "schema_version": "pre2010-5m-clickhouse-candidate-v1",
        "year": args.year,
        "table": args.table,
        "batch_id": batch,
        "source_detail": str(args.detail),
        "source_detail_sha256": sha256(args.detail),
        "source_rows": source_rows,
        "inserted_rows": int(actual[0]),
        "unique_keys": int(actual[1]),
        "write": bool(args.write),
        "formal_clickhouse_written": False,
        "formal_mutation": False,
        "status": "complete" if args.write and int(actual[0]) == source_rows and int(actual[1]) == source_rows else ("dry_run" if not args.write else "review"),
    }
    out = args.output_root / str(args.year)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

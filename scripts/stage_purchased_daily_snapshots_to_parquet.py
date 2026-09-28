from __future__ import annotations

"""Stage purchased daily snapshots as a partitioned Parquet dataset.

The daily archive contains a wide 30-column snapshot.  Keeping this stage in
Parquet preserves every source field while avoiding a large wide INSERT into
the local ClickHouse memory budget.  File-level universe audit rows are still
written to ClickHouse; no formal K-line table is modified.
"""

import argparse
import csv
import hashlib
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import warehouse_path
from import_purchased_daily_snapshots_to_clickhouse import (
    AUDIT_COLUMNS,
    DAILY_STAGE,
    DEFAULT_SOURCE,
    EXPECTED_HEADER,
    STAGE_COLUMNS,
    UNIVERSE_AUDIT,
    DirectorySource,
    iter_entries,
    load_master,
    process_entry,
)


DEFAULT_BATCH = "purchased_daily_snapshot_20260916"
DEFAULT_OUTPUT = warehouse_path("purchased_history", "daily_snapshot", DEFAULT_BATCH)

STRING_COLUMNS = {
    "source",
    "source_archive",
    "source_entry",
    "batch_id",
    "source_code",
    "code",
    "security_type",
    "universe_decision",
    "quality_status",
    "stock_name",
    "row_hash",
}
NUMERIC_COLUMNS = {
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "change_amount",
    "change_pct",
    "source_volume_lots",
    "source_amount_thousand_yuan",
    "volume_lots",
    "amount_yuan",
    "turnover_rate",
    "free_float_turnover_rate",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dividend_yield",
    "dividend_yield_ttm",
    "total_share_10k",
    "float_share_10k",
    "free_float_share_10k",
    "total_market_cap_10k",
    "float_market_cap_10k",
    "adjustment_factor",
    "limit_up",
    "limit_down",
}


def parquet_schema() -> pa.Schema:
    fields: list[pa.Field] = []
    for column in STAGE_COLUMNS:
        if column in STRING_COLUMNS:
            dtype = pa.string()
        elif column == "trade_date":
            dtype = pa.date32()
        elif column == "ingested_at":
            dtype = pa.timestamp("ms")
        elif column == "id":
            dtype = pa.uint64()
        elif column in NUMERIC_COLUMNS:
            dtype = pa.float64()
        else:
            raise ValueError(f"unmapped parquet column: {column}")
        fields.append(pa.field(column, dtype, nullable=True))
    return pa.schema(fields)


def rows_to_table(rows: list[list[Any]], schema: pa.Schema) -> pa.Table:
    arrays = []
    for index, field in enumerate(schema):
        values = [row[index] for row in rows]
        arrays.append(pa.array(values, type=field.type))
    return pa.Table.from_arrays(arrays, schema=schema)


def write_rows(
    rows: list[list[Any]],
    output: Path,
    schema: pa.Schema,
    writers: dict[str, pq.ParquetWriter],
) -> int:
    by_partition: dict[str, list[list[Any]]] = {}
    for row in rows:
        # A single security spans hundreds of months.  Year-level physical
        # partitions keep writer count bounded while trade_date preserves
        # exact day/month filtering for DuckDB and later promotion.
        partition = row[9].strftime("%Y")
        by_partition.setdefault(partition, []).append(row)
    written = 0
    for partition, partition_rows in by_partition.items():
        partition_dir = output / f"trade_year={partition}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        writer = writers.get(partition)
        if writer is None:
            writer = pq.ParquetWriter(
                partition_dir / "part-00000.parquet",
                schema,
                compression="zstd",
                use_dictionary=True,
            )
            writers[partition] = writer
        writer.write_table(rows_to_table(partition_rows, schema))
        written += len(partition_rows)
    return written


def create_audit_table(client: Any) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {UNIVERSE_AUDIT}
        (
            `source` LowCardinality(String),
            `source_archive` String,
            `source_entry` String,
            `batch_id` String,
            `source_code` String,
            `code` String,
            `security_type` LowCardinality(String),
            `universe_decision` LowCardinality(String),
            `reason` String,
            `source_row_count` UInt32,
            `valid_row_count` UInt32,
            `blank_ohlc_row_count` UInt32,
            `min_trade_date` Nullable(Date),
            `max_trade_date` Nullable(Date),
            `file_size` UInt64,
            `row_hash` String,
            `audited_at` DateTime('Asia/Shanghai')
        )
        ENGINE = ReplacingMergeTree(audited_at)
        ORDER BY (batch_id, source_entry)
        SETTINGS index_granularity = 8192
        """
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--batch-id", default=DEFAULT_BATCH)
    parser.add_argument("--include-pending", action="store_true")
    parser.add_argument("--limit-files", type=int, default=0)
    parser.add_argument("--batch-rows", type=int, default=50000)
    parser.add_argument("--write-audit", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.source.exists():
        print(f"source_missing={args.source}", file=sys.stderr)
        return 2
    if args.output.exists() and any(args.output.iterdir()):
        print(f"output_not_empty={args.output}; refusing duplicate stage", file=sys.stderr)
        return 3
    args.output.mkdir(parents=True, exist_ok=True)

    client = clickhouse_client()
    master = load_master(client)
    schema = parquet_schema()
    stage_rows: list[list[Any]] = []
    audit_rows: list[list[Any]] = []
    decisions: Counter[str] = Counter()
    total_entries = 0
    total_source_rows = 0
    total_valid_rows = 0
    total_blank_ohlc = 0
    written_rows = 0
    writers: dict[str, pq.ParquetWriter] = {}

    source_reader = DirectorySource(args.source) if args.source.is_dir() else ZipFile(args.source)
    try:
        with source_reader as source:
            for info in iter_entries(source):
                if args.limit_files and total_entries >= args.limit_files:
                    break
                audit, rows = process_entry(
                    source,
                    info,
                    args.source,
                    args.batch_id,
                    master,
                    include_pending=args.include_pending,
                    audit_only=False,
                )
                total_entries += 1
                decisions[audit["universe_decision"]] += 1
                total_source_rows += audit["source_row_count"]
                total_valid_rows += audit["valid_row_count"]
                total_blank_ohlc += audit["blank_ohlc_row_count"]
                audit_rows.append([audit[column] for column in AUDIT_COLUMNS])
                stage_rows.extend(rows)
                while len(stage_rows) >= args.batch_rows:
                    block, stage_rows = stage_rows[: args.batch_rows], stage_rows[args.batch_rows :]
                    written_rows += write_rows(block, args.output, schema, writers)
                if total_entries % 500 == 0:
                    print(
                        f"progress entries={total_entries} source_rows={total_source_rows} parquet_rows={written_rows + len(stage_rows)}",
                        flush=True,
                    )
        if stage_rows:
            written_rows += write_rows(stage_rows, args.output, schema, writers)
    finally:
        for writer in writers.values():
            writer.close()

    if args.write_audit and audit_rows:
        create_audit_table(client)
        client.insert(UNIVERSE_AUDIT, audit_rows, column_names=AUDIT_COLUMNS)

    manifest = {
        "source": str(args.source),
        "batch_id": args.batch_id,
        "output": str(args.output),
        "entries": total_entries,
        "source_rows": total_source_rows,
        "valid_rows": total_valid_rows,
        "blank_ohlc_rows": total_blank_ohlc,
        "parquet_rows_written": written_rows,
        "decisions": dict(decisions),
        "source_header_sha256": hashlib.sha256(",".join(EXPECTED_HEADER).encode("utf-8")).hexdigest(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (args.output / "_manifest.json").write_text(
        __import__("json").dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("--- purchased daily snapshot parquet stage ---")
    for key, value in manifest.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

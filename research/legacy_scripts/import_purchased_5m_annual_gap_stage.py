from __future__ import annotations

"""Import a bounded early-date slice from the purchased annual 5m archive.

The annual archive contains one long CSV per stock, unlike the daily monthly
archives.  This script streams only the requested chronological slice and
stores one source-audit row per code/day, so it can safely fill a gap without
re-importing the overlapping annual data.
"""

import argparse
import csv
import hashlib
import math
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from io import TextIOWrapper
from pathlib import Path
from typing import Any
from zipfile import ZipFile
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.import_purchased_5m_stage import (
    AUDIT_COLUMNS,
    DEFAULT_INDEX_LIST,
    MINUTE_STAGE,
    SOURCE_AUDIT,
    STAGE_COLUMNS,
    canonical_stock,
    clickhouse_client,
    load_master,
    source_code_from_entry,
)

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_ARCHIVE = Path(
    r"E:\AStockHistory\A股数据\A股分时数据\A股_分时数据_沪深\5分钟_按年汇总\2000_2005_5min.zip"
)


def parse_float(value: str) -> float | None:
    value = str(value or "").strip()
    if not value:
        return None
    parsed = float(value.replace(",", ""))
    return parsed if math.isfinite(parsed) else None


def parse_datetime(value: str) -> datetime:
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=BUSINESS_TZ)
        except ValueError:
            continue
    raise ValueError(f"unsupported purchased 5m timestamp: {text!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2000, 6, 9))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2000, 6, 29))
    parser.add_argument("--batch-id", default="purchased_5m_hs_20000609_20000629_20260916_r1")
    parser.add_argument("--batch-rows", type=int, default=5000)
    parser.add_argument("--include-pending", action="store_true")
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    if not args.archive.exists() or args.start_date > args.end_date:
        print("invalid_archive_or_date_range", file=sys.stderr)
        return 2
    client = clickhouse_client()
    master = load_master(client)
    archive_sha256 = sha256_file(args.archive)
    if args.write:
        existing_stage = client.query(
            f"SELECT count() FROM {MINUTE_STAGE} WHERE batch_id=%(batch)s",
            parameters={"batch": args.batch_id},
        ).result_rows[0][0]
        existing_audit = client.query(
            f"SELECT count() FROM {SOURCE_AUDIT} WHERE batch_id=%(batch)s",
            parameters={"batch": args.batch_id},
        ).result_rows[0][0]
        if existing_stage or existing_audit:
            print(f"existing_batch stage={existing_stage} audit={existing_audit}", file=sys.stderr)
            return 3

    stage_rows: list[list[Any]] = []
    audit_rows: list[list[Any]] = []
    total_entries = 0
    total_source_rows = 0
    total_valid = 0
    total_invalid = 0
    decisions: dict[str, int] = defaultdict(int)

    with ZipFile(args.archive) as archive:
        for info in archive.infolist():
            if not info.filename.lower().endswith(".csv"):
                continue
            total_entries += 1
            source_entry = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            source_code = source_code_from_entry(source_entry)
            code = canonical_stock(source_code)
            security_type = "stock" if code else "unknown"
            decision = "accepted_stock" if code in master and master[code] == "stock" else "historical_stock_pending"
            if not code:
                decision = "rejected_unclassified"
            decisions[decision] += 1
            by_day: dict[date, dict[str, Any]] = {}
            with archive.open(info) as raw_stream:
                text_stream = TextIOWrapper(raw_stream, encoding="utf-8-sig", newline="")
                reader = csv.reader(text_stream)
                next(reader, None)
                for ordinal, cells in enumerate(reader, start=1):
                    if len(cells) not in (10, 11):
                        continue
                    try:
                        dt = parse_datetime(cells[0])
                    except (TypeError, ValueError):
                        continue
                    if dt.date() < args.start_date:
                        continue
                    if dt.date() > args.end_date:
                        break
                    bucket = by_day.setdefault(
                        dt.date(), {"rows": 0, "valid": 0, "invalid": 0, "datetimes": set(), "stage": []}
                    )
                    bucket["rows"] += 1
                    try:
                        open_, close, high, low = (parse_float(cells[i]) for i in (3, 4, 5, 6))
                        if any(value is None or value <= 0 for value in (open_, close, high, low)):
                            bucket["invalid"] += 1
                            continue
                        bucket["valid"] += 1
                        bucket["datetimes"].add(dt)
                        if decision not in ("accepted_stock", "historical_stock_pending"):
                            continue
                        if decision == "historical_stock_pending" and not args.include_pending:
                            continue
                        volume = parse_float(cells[7])
                        amount = parse_float(cells[8])
                        row_hash = hashlib.sha256(f"{args.archive}:{source_entry}:{ordinal}".encode("utf-8")).hexdigest()
                        bucket["stage"].append(
                            [
                                "purchased_a_share_history", "purchased_hs_5m", str(args.archive),
                                source_entry, args.batch_id, cells[1].strip(), code, security_type, decision,
                                "5m", dt, open_, high, low, close, volume, amount, "none", row_hash,
                                datetime.now(BUSINESS_TZ), int.from_bytes(bytes.fromhex(row_hash[:16]), "big"),
                            ]
                        )
                    except (TypeError, ValueError, IndexError):
                        bucket["invalid"] += 1

            for trade_date, bucket in sorted(by_day.items()):
                total_source_rows += bucket["rows"]
                total_valid += bucket["valid"]
                total_invalid += bucket["invalid"]
                audit_rows.append(
                    [
                        "stock_hs", str(args.archive), f"{source_entry}#trade_date={trade_date}", args.batch_id,
                        source_code, code or "", security_type, decision,
                        "annual archive bounded daily slice", bucket["rows"], bucket["valid"], 48,
                        len(bucket["datetimes"]), bucket["invalid"],
                        min(bucket["datetimes"]) if bucket["datetimes"] else None,
                        max(bucket["datetimes"]) if bucket["datetimes"] else None,
                        info.file_size, archive_sha256,
                        datetime.now(BUSINESS_TZ),
                    ]
                )
                stage_rows.extend(bucket["stage"])
                if args.write and len(stage_rows) >= args.batch_rows:
                    client.insert(MINUTE_STAGE, stage_rows, column_names=STAGE_COLUMNS)
                    stage_rows = []
            if total_entries % 100 == 0:
                print(f"progress entries={total_entries} source_rows={total_source_rows}", flush=True)

    if args.write:
        if stage_rows:
            client.insert(MINUTE_STAGE, stage_rows, column_names=STAGE_COLUMNS)
        if audit_rows:
            client.insert(SOURCE_AUDIT, audit_rows, column_names=AUDIT_COLUMNS)
    print("--- purchased annual gap stage audit ---")
    print(f"entries={total_entries}")
    print(f"source_rows={total_source_rows}")
    print(f"valid_ohlc_rows={total_valid}")
    print(f"invalid_rows={total_invalid}")
    print(f"decisions={dict(decisions)}")
    print(f"stage_rows_written={total_valid if args.write else 0}")
    print(f"audit_rows_written={len(audit_rows) if args.write else 0}")
    print(f"write={args.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

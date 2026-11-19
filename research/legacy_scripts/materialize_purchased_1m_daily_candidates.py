from __future__ import annotations

"""Materialize audit-only daily and 5m candidates confirmed by source 1m.

This consumes the 1m audit report and writes isolated Parquet candidates under
the external warehouse.  It never inserts into a ClickHouse K-line table.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from zipfile import ZipFile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.paths import runtime_path, warehouse_path
from scripts.audit_purchased_review_against_1m import (
    DEFAULT_BJ_ROOT,
    DEFAULT_HS_ROOT,
    DEFAULT_INDEX_ROOT,
    _derive_5m,
    _daily_from_5m,
    _read_source_days,
    _source_root,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="purchased_history_review_20260917_snapshot_recon_r3")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Existing reconciliation.parquet; defaults to the run's external report",
    )
    parser.add_argument("--hs-root", type=Path, default=DEFAULT_HS_ROOT)
    parser.add_argument("--index-root", type=Path, default=DEFAULT_INDEX_ROOT)
    parser.add_argument("--bj-root", type=Path, default=DEFAULT_BJ_ROOT)
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report_dir = warehouse_path("purchased_history", "one_minute_cross_validation", args.run_id)
    report_path = args.report or report_dir / "reconciliation.parquet"
    if not report_path.exists():
        raise SystemExit(f"report missing: {report_path}")

    import pyarrow.parquet as pq

    report_rows = pq.read_table(report_path).to_pylist()
    targets = [
        row for row in report_rows
        if row.get("decision") == "one_minute_rebuilds_purchased_5m_stage"
    ]
    if not targets:
        raise SystemExit("no rebuildable candidates in report")

    grouped: dict[tuple[Path, int, str], set[date]] = defaultdict(set)
    for row in targets:
        code = str(row["code"])
        trade_date = row["trade_date"]
        root = _source_root(code, str(row["security_type"]), args.hs_root, args.index_root, args.bj_root)
        grouped[(root, trade_date.year, code)].add(trade_date)

    daily_candidates: list[dict[str, object]] = []
    minute_candidates: list[dict[str, object]] = []
    archives: dict[tuple[Path, int], ZipFile] = {}
    names: dict[tuple[Path, int], set[str]] = {}
    try:
        for (root, year, code), target_dates in sorted(grouped.items(), key=lambda item: (str(item[0][0]), item[0][1], item[0][2])):
            archive_path = root / f"{year}_1min.zip"
            if not archive_path.exists():
                raise RuntimeError(f"source archive missing: {archive_path}")
            cache_key = (root, year)
            if cache_key not in archives:
                archives[cache_key] = ZipFile(archive_path)
                names[cache_key] = set(archives[cache_key].namelist())
            symbol, market = code.split(".", 1)
            security_type = next(str(row["security_type"]) for row in targets if row["code"] == code)
            entry_name = f"{symbol}_{year}.txt" if security_type == "index" else f"{market.lower()}{symbol}_{year}.txt"
            if entry_name not in names[cache_key]:
                raise RuntimeError(f"source entry missing: {entry_name} in {archive_path}")
            by_day, _ = _read_source_days(archives[cache_key], entry_name, target_dates)
            for trade_date in sorted(target_dates):
                source_rows = by_day.get(trade_date, [])
                derived, _, duplicates = _derive_5m(source_rows)
                if len(derived) != 48 or duplicates:
                    raise RuntimeError(f"candidate is no longer complete: {code}@{trade_date} rows={len(derived)} duplicates={duplicates}")
                report_row = next(row for row in targets if row["code"] == code and row["trade_date"] == trade_date)
                daily = _daily_from_5m(derived)
                daily_candidates.append({
                    "run_id": args.run_id,
                    "issue_type": report_row["issue_type"],
                    "code": code,
                    "trade_date": trade_date,
                    "security_type": report_row["security_type"],
                    "source_entry": entry_name,
                    "derived_5m_rows": len(derived),
                    "open": daily["open"], "high": daily["high"],
                    "low": daily["low"], "close": daily["close"],
                    "volume_lots": daily["volume_lots"],
                    "stage_open": report_row.get("stage_daily_open"),
                    "stage_high": report_row.get("stage_daily_high"),
                    "stage_low": report_row.get("stage_daily_low"),
                    "stage_close": report_row.get("stage_daily_close"),
                    "stage_volume_lots": report_row.get("stage_daily_volume_lots"),
                    "stage_price_relative_diff": report_row.get("stage_price_relative_diff"),
                    "stage_rows": report_row.get("stage_rows"),
                    "stage_unique_minutes": report_row.get("stage_unique_minutes"),
                    "formal_table_mutation": False,
                })
                for bar in derived:
                    bar_minute = int(bar["minute"])
                    bar_time = datetime(
                        trade_date.year,
                        trade_date.month,
                        trade_date.day,
                        bar_minute // 100,
                        bar_minute % 100,
                    )
                    minute_candidates.append({
                        "run_id": args.run_id,
                        "code": code,
                        "trade_date": trade_date,
                        "security_type": report_row["security_type"],
                        "datetime": bar_time,
                        "datetime_label": bar_minute,
                        "open": bar["open"], "high": bar["high"],
                        "low": bar["low"], "close": bar["close"],
                        "volume_lots": bar["volume_lots"],
                        "source_minute_rows": bar["source_minute_rows"],
                        "source_entry": entry_name,
                        "formal_table_mutation": False,
                    })
    finally:
        for archive in archives.values():
            archive.close()

    summary = {
        "run_id": args.run_id,
        "daily_candidate_rows": len(daily_candidates),
        "five_minute_candidate_rows": len(minute_candidates),
        "formal_table_mutation": False,
        "daily_report": str(report_dir / "index_daily_rebuild_candidates.parquet"),
        "five_minute_report": str(report_dir / "index_5m_rebuild_candidates.parquet"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    if args.write:
        import pyarrow as pa

        report_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(daily_candidates), report_dir / "index_daily_rebuild_candidates.parquet")
        pq.write_table(pa.Table.from_pylist(minute_candidates), report_dir / "index_5m_rebuild_candidates.parquet")
        manifest_path = runtime_path("purchased_history", f"one_minute_candidate_{args.run_id}.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({**summary, "manifest": str(manifest_path)}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

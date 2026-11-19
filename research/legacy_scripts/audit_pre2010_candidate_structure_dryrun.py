from __future__ import annotations

"""Read-only 5m structure audit for one frozen pre-2010 candidate year.

This deliberately audits only the frozen candidate source, so a large formal
derived-table aggregation cannot make a completed 5m structural check appear
failed.  It does not create or modify any ClickHouse object.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client


CANDIDATE = "stock.purchased_history_minute_5_formal_candidate_20260916"
SHARDS = tuple("0123456789") + ("other",)
EXPECTED_TIMES = (
    "'09:35','09:40','09:45','09:50','09:55','10:00','10:05','10:10',"
    "'10:15','10:20','10:25','10:30','10:35','10:40','10:45','10:50',"
    "'10:55','11:00','11:05','11:10','11:15','11:20','11:25','11:30',"
    "'13:05','13:10','13:15','13:20','13:25','13:30','13:35','13:40',"
    "'13:45','13:50','13:55','14:00','14:05','14:10','14:15','14:20',"
    "'14:25','14:30','14:35','14:40','14:45','14:50','14:55','15:00'"
)
SETTINGS = {
    "max_threads": 1,
    "max_memory_usage": 2 * 1024 * 1024 * 1024,
    "max_bytes_before_external_group_by": 512 * 1024 * 1024,
    "max_execution_time": 120,
}


def bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start.isoformat(), end.isoformat()


def shard_filter(shard: str) -> str:
    if shard == "other":
        return "(code IS NULL OR code = '' OR substring(code, 1, 1) NOT IN ('0','1','2','3','4','5','6','7','8','9'))"
    return f"substring(code, 1, 1) = '{shard}'"


def audit(client: Any, start: str, end: str, shard: str) -> dict[str, int]:
    where = (
        f"datetime >= toDate('{start}') AND datetime < toDate('{end}') "
        f"AND {shard_filter(shard)} AND code IS NOT NULL AND code != ''"
    )
    sql = f"""
    SELECT count() AS code_days,
           countIf(is_complete) AS complete_code_days,
           countIf(NOT is_complete) AS review_code_days,
           sum(bar_count) AS raw_rows,
           sum(bar_count - unique_datetimes) AS duplicate_extra_rows,
           sum(greatest(bar_count - 48, 0)) AS surplus_rows,
           sum(greatest(48 - unique_datetimes, 0)) AS missing_unique_slots,
           sum(greatest(48 - expected_slots, 0)) AS missing_expected_slots,
           sum(greatest(48 - valid_ohlc_rows, 0)) AS invalid_ohlc_rows
    FROM (
      SELECT code, toDate(datetime) AS trade_date,
             count() AS bar_count,
             uniqExact(datetime) AS unique_datetimes,
             uniqExactIf(formatDateTime(datetime, '%H:%i'), formatDateTime(datetime, '%H:%i') IN ({EXPECTED_TIMES})) AS expected_slots,
             countIf(open > 0 AND high > 0 AND low > 0 AND close > 0) AS valid_ohlc_rows,
             bar_count = 48 AND unique_datetimes = 48 AND expected_slots = 48
                 AND valid_ohlc_rows = 48 AS is_complete
      FROM {CANDIDATE}
      WHERE {where}
      GROUP BY code, trade_date
    )
    """
    row = client.query(sql, settings=SETTINGS).result_rows[0]
    keys = (
        "code_days", "complete_code_days", "review_code_days", "raw_rows",
        "duplicate_extra_rows", "surplus_rows", "missing_unique_slots",
        "missing_expected_slots", "invalid_ohlc_rows",
    )
    return {key: int(value or 0) for key, value in zip(keys, row)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if not 2000 <= args.year <= 2009:
        raise SystemExit("year must be within frozen 2000..2009")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    client = clickhouse_client()
    report: dict[str, Any] = {
        "schema_version": "pre2010-candidate-structure-dryrun-v1",
        "year": args.year,
        "candidate_table": CANDIDATE,
        "units_contract": "volume_lots;amount_yuan;raw_unadjusted_price",
        "formal_mutation": False,
        "rows": [],
    }
    for month in range(1, 13):
        start, end = bounds(args.year, month)
        for shard in SHARDS:
            row: dict[str, Any] = {"month": month, "code_shard": shard, "status": "checked"}
            try:
                row.update(audit(client, start, end, shard))
            except Exception as exc:
                row.update({"status": "not_checked", "error": f"{type(exc).__name__}: {exc}"})
            report["rows"].append(row)
            (args.out_dir / "report.partial.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            print(json.dumps(row, ensure_ascii=False), flush=True)
    (args.out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

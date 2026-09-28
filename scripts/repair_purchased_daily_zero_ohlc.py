"""Repair requested zero-OHLC daily rows from the purchased snapshot.

The script is deliberately two-phase:

* without ``--write`` it only builds and validates an in-memory candidate;
* with ``--write`` it persists an isolated candidate and backup, removes only
  the exact requested zero-OHLC keys, then inserts the validated source rows.

It never changes rows outside the requested years and keeps the old rows in a
recoverable ClickHouse backup table before the targeted mutation.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow.dataset as ds

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DAILY_TABLE = "stock.kline_daily"
CANDIDATE_TABLE = "stock.purchased_history_daily_zero_ohlc_candidate_20260916"
BACKUP_TABLE = "stock.purchased_history_daily_zero_ohlc_backup_20260916"
DEFAULT_DAILY_ROOT = Path(
    r"F:\AiStock\data\data\warehouse\purchased_history\daily_snapshot\purchased_daily_snapshot_20260916_r1"
)
FORMAL_COLUMNS = [
    "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
    "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id",
]
SOURCE_COLUMNS = [
    "code", "trade_date", "open", "high", "low", "close", "prev_close",
    "change_amount", "change_pct", "volume_lots", "amount_yuan", "turnover_rate",
    "source_archive", "source_entry", "batch_id", "row_hash", "adjustment_factor",
    "id",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", action="append", type=int, dest="years")
    parser.add_argument("--daily-root", type=Path, default=DEFAULT_DAILY_ROOT)
    parser.add_argument("--batch-size", type=int, default=20_000)
    parser.add_argument("--run-id", default="daily_zero_ohlc_repair_20260916")
    parser.add_argument("--write", action="store_true")
    return parser.parse_args()


def _as_float(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if value != value else value


def _date_value(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value)[:10])


def _ensure_tables(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {CANDIDATE_TABLE}
        (
            repair_run_id String,
            code String,
            trade_date Date,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume Float64,
            amount Float64,
            amplitude Float64,
            change_pct Float64,
            change_amount Float64,
            turnover_rate Float64,
            created_at DateTime('Asia/Shanghai'),
            id UInt64,
            source_archive String,
            source_entry String,
            source_batch_id String,
            source_row_hash String,
            source_adjustment_factor Float64
        )
        ENGINE = MergeTree
        PARTITION BY toYear(trade_date)
        ORDER BY (repair_run_id, code, trade_date)
        """
    )
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {BACKUP_TABLE}
        (
            repair_run_id String,
            code String,
            trade_date Date,
            open Float64,
            high Float64,
            low Float64,
            close Float64,
            volume Float64,
            amount Float64,
            amplitude Float64,
            change_pct Float64,
            change_amount Float64,
            turnover_rate Float64,
            created_at Nullable(DateTime),
            id UInt64,
            backed_up_at DateTime('Asia/Shanghai')
        )
        ENGINE = MergeTree
        PARTITION BY toYear(trade_date)
        ORDER BY (repair_run_id, code, trade_date)
        """
    )


def _load_formal_zero_keys(client, years: list[int]) -> dict[int, dict[str, tuple[Any, ...]]]:
    result: dict[int, dict[str, tuple[Any, ...]]] = defaultdict(dict)
    for year in years:
        rows = client.query(
            f"""
            SELECT code, trade_date, open, high, low, close, volume, amount,
                   amplitude, change_pct, change_amount, turnover_rate, created_at, id
            FROM {DAILY_TABLE}
            WHERE trade_date >= toDate(%(start)s)
              AND trade_date < toDate(%(end)s)
              AND (open <= 0 OR high <= 0 OR low <= 0 OR close <= 0)
            ORDER BY code, trade_date
            """,
            parameters={"start": f"{year}-01-01", "end": f"{year + 1}-01-01"},
        ).result_rows
        for row in rows:
            key = f"{row[0]}|{row[1]}"
            if key in result[year]:
                raise RuntimeError(f"duplicate formal zero-OHLC key: {key}")
            result[year][key] = tuple(row)
    return result


def _scan_candidates(dataset, bad: dict[int, dict[str, tuple[Any, ...]]], years: list[int]):
    filter_expr = (
        ds.field("trade_year").isin(years)
        & (ds.field("security_type") == "stock")
        & (ds.field("quality_status") == "valid")
    )
    seen: set[str] = set()
    candidates: list[list[Any]] = []
    matched_by_year: defaultdict[int, int] = defaultdict(int)
    for batch in dataset.scanner(
        columns=SOURCE_COLUMNS,
        filter=filter_expr,
        batch_size=100_000,
    ).to_batches():
        values = batch.to_pydict()
        for index, code_value in enumerate(values["code"]):
            code = str(code_value)
            trade_date = _date_value(values["trade_date"][index])
            year = trade_date.year
            key = f"{code}|{trade_date}"
            if key not in bad.get(year, {}):
                continue
            if key in seen:
                raise RuntimeError(f"duplicate source key: {key}")
            seen.add(key)
            open_value = _as_float(values["open"][index])
            high_value = _as_float(values["high"][index])
            low_value = _as_float(values["low"][index])
            close_value = _as_float(values["close"][index])
            if min(open_value, high_value, low_value, close_value) <= 0:
                raise RuntimeError(f"source candidate is not positive OHLC: {key}")
            prev_close = _as_float(values["prev_close"][index])
            amplitude = ((high_value - low_value) / prev_close * 100.0) if prev_close else 0.0
            candidates.append([
                code,
                trade_date,
                open_value,
                high_value,
                low_value,
                close_value,
                _as_float(values["volume_lots"][index]),
                _as_float(values["amount_yuan"][index]),
                amplitude,
                _as_float(values["change_pct"][index]),
                _as_float(values["change_amount"][index]),
                _as_float(values["turnover_rate"][index]),
                datetime.now(BUSINESS_TZ).replace(microsecond=0),
                int(values["id"][index] or 0),
                str(values["source_archive"][index] or ""),
                str(values["source_entry"][index] or ""),
                str(values["batch_id"][index] or ""),
                str(values["row_hash"][index] or ""),
                _as_float(values["adjustment_factor"][index]),
            ])
            matched_by_year[year] += 1
    expected = sum(len(bad.get(year, {})) for year in years)
    if len(candidates) != expected:
        missing = expected - len(candidates)
        raise RuntimeError(f"candidate coverage mismatch: expected={expected}, matched={len(candidates)}, missing={missing}")
    candidates.sort(key=lambda row: (row[0], row[1]))
    return candidates, dict(matched_by_year)


def _insert_batches(client, table: str, rows: list[list[Any]], columns: list[str], batch_size: int) -> int:
    written = 0
    for offset in range(0, len(rows), batch_size):
        chunk = rows[offset:offset + batch_size]
        client.insert(table, chunk, column_names=columns)
        written += len(chunk)
    return written


def _validate_candidate(client, run_id: str, expected: int) -> None:
    row = client.query(
        f"SELECT count(), uniqExact(code, trade_date), countIf(open <= 0 OR high <= 0 OR low <= 0 OR close <= 0) FROM {CANDIDATE_TABLE} WHERE repair_run_id=%(run_id)s",
        parameters={"run_id": run_id},
    ).result_rows[0]
    if tuple(map(int, row)) != (expected, expected, 0):
        raise RuntimeError(f"candidate validation failed: {row}")


def _validate_backup(client, run_id: str, expected: int) -> None:
    row = client.query(
        f"SELECT count(), uniqExact(code, trade_date) FROM {BACKUP_TABLE} WHERE repair_run_id=%(run_id)s",
        parameters={"run_id": run_id},
    ).result_rows[0]
    if tuple(map(int, row)) != (expected, expected):
        raise RuntimeError(f"backup validation failed: {row}")


def _latest_manifest_path() -> Path:
    return runtime_path("purchased_history", "daily_zero_ohlc_repair_20260916.json")


def _write_manifest(payload: dict[str, Any], run_id: str) -> Path:
    latest_path = _latest_manifest_path()
    safe_run_id = re.sub(r"[^A-Za-z0-9._-]+", "_", run_id).strip("._") or "run"
    run_path = latest_path.with_name(f"daily_zero_ohlc_repair_{safe_run_id}.json")
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    for path in dict.fromkeys((run_path, latest_path)):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    return run_path


def main() -> int:
    args = _parse_args()
    years = sorted(set(args.years or list(range(2000, 2006))))
    if not years or any(year < 1990 or year > 2100 for year in years):
        raise SystemExit("invalid years")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")
    if not args.daily_root.exists():
        raise SystemExit(f"daily snapshot not found: {args.daily_root}")

    client = clickhouse_client()
    bad = _load_formal_zero_keys(client, years)
    expected = sum(len(keys) for keys in bad.values())
    dataset = ds.dataset(args.daily_root, format="parquet", partitioning="hive")
    candidates, matched_by_year = _scan_candidates(dataset, bad, years)
    print(json.dumps({
        "years": years,
        "formal_zero_ohlc": expected,
        "candidate_rows": len(candidates),
        "matched_by_year": matched_by_year,
        "source_positive_ohlc": len(candidates),
        "write": args.write,
    }, ensure_ascii=False))
    if not args.write:
        return 0
    if not candidates:
        print("nothing_to_write")
        return 0

    _ensure_tables(client)
    existing_candidate = int(client.query(
        f"SELECT count() FROM {CANDIDATE_TABLE} WHERE repair_run_id=%(run_id)s",
        parameters={"run_id": args.run_id},
    ).result_rows[0][0])
    existing_backup = int(client.query(
        f"SELECT count() FROM {BACKUP_TABLE} WHERE repair_run_id=%(run_id)s",
        parameters={"run_id": args.run_id},
    ).result_rows[0][0])
    if existing_candidate or existing_backup:
        raise RuntimeError(f"run already exists: candidate={existing_candidate}, backup={existing_backup}")

    candidate_rows = [[args.run_id, *row] for row in candidates]
    _insert_batches(
        client,
        CANDIDATE_TABLE,
        candidate_rows,
        ["repair_run_id", *["code", "trade_date", "open", "high", "low", "close", "volume", "amount", "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id", "source_archive", "source_entry", "source_batch_id", "source_row_hash", "source_adjustment_factor"]],
        args.batch_size,
    )
    _validate_candidate(client, args.run_id, expected)

    start_date = min(f"{year}-01-01" for year in years)
    end_date = max(f"{year + 1}-01-01" for year in years)
    backup_rows = client.query(
        f"""
        SELECT %(run_id)s, code, trade_date, open, high, low, close, volume, amount,
               amplitude, change_pct, change_amount, turnover_rate, created_at, id,
               now('Asia/Shanghai')
        FROM {DAILY_TABLE}
        WHERE trade_date >= toDate(%(start)s)
          AND trade_date < toDate(%(end)s)
          AND (open <= 0 OR high <= 0 OR low <= 0 OR close <= 0)
        """,
        parameters={"run_id": args.run_id, "start": start_date, "end": end_date},
    ).result_rows
    if len(backup_rows) != expected:
        raise RuntimeError(f"backup source count mismatch: expected={expected}, actual={len(backup_rows)}")
    _insert_batches(
        client,
        BACKUP_TABLE,
        backup_rows,
        ["repair_run_id", "code", "trade_date", "open", "high", "low", "close", "volume", "amount", "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at", "id", "backed_up_at"],
        args.batch_size,
    )
    _validate_backup(client, args.run_id, expected)

    client.command(
        f"""
        ALTER TABLE {DAILY_TABLE} DELETE
        WHERE trade_date >= toDate(%(start)s)
          AND trade_date < toDate(%(end)s)
          AND (open <= 0 OR high <= 0 OR low <= 0 OR close <= 0)
        SETTINGS mutations_sync = 2
        """,
        parameters={"start": start_date, "end": end_date},
    )

    formal_rows = [row[1:15] for row in candidate_rows]
    _insert_batches(client, DAILY_TABLE, formal_rows, FORMAL_COLUMNS, args.batch_size)
    validation = {}
    for year in years:
        row = client.query(
            f"""
            SELECT count(), uniqExact(code, trade_date),
                   countIf(open <= 0 OR high <= 0 OR low <= 0 OR close <= 0)
            FROM {DAILY_TABLE}
            WHERE trade_date >= toDate(%(start)s) AND trade_date < toDate(%(end)s)
            """,
            parameters={"start": f"{year}-01-01", "end": f"{year + 1}-01-01"},
        ).result_rows[0]
        validation[str(year)] = [int(value) for value in row]
        if int(row[2]) != 0:
            raise RuntimeError(f"post-write zero-OHLC validation failed: year={year}, row={row}")

    manifest = {
        "schema_version": 1,
        "run_id": args.run_id,
        "years": years,
        "formal_table": DAILY_TABLE,
        "candidate_table": CANDIDATE_TABLE,
        "backup_table": BACKUP_TABLE,
        "replaced_rows": expected,
        "matched_by_year": matched_by_year,
        "post_write_validation": validation,
        "source_snapshot": str(args.daily_root),
        "written_at": datetime.now(BUSINESS_TZ).replace(microsecond=0).isoformat(),
    }
    path = _write_manifest(manifest, args.run_id)
    print(json.dumps({"manifest": str(path), "latest_manifest": str(_latest_manifest_path()), **manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

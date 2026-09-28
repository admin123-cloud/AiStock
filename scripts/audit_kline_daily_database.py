"""Full-database audit and explicit repair for daily volume/amount units.

The recurring coverage task must not mutate historical unit values. This
command is intentionally separate: it scans the persisted database in bounded
date chunks, compares exact ``(code, trade_date)`` keys with local TDX day
files, writes a report, and only repairs when ``--apply`` is supplied.

The storage contract is volume in lots and amount in yuan. The audit covers
every distinct code in ``kline_daily``. Only clear source matches are
repairable: zero values or approximately 100x unit shifts. Other provider
conflicts remain in the report and are never auto-rewritten.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.kline_units import (
    DAILY_UNIT_REPAIRABLE_CLASSES,
    classify_daily_unit_pair,
    normalize_tdx_day_units,
)
from utils.market_warehouse import clickhouse_client
from utils.paths import report_path


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
DAY_RECORD = struct.Struct("<IIIIIfII")
STAGE_TABLE = "kline_daily_unit_full_audit_stage"
TARGET_COLUMNS = [
    "code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "amplitude",
    "change_pct",
    "change_amount",
    "turnover_rate",
    "created_at",
    "id",
]
REPAIRABLE_CLASSES = DAILY_UNIT_REPAIRABLE_CLASSES
KNOWN_UNMATCHED_INDEX_CODES = frozenset({"999999.SH"})


def _quote(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ratio_bucket(value: float) -> str:
    ratio = float(value or 0.0)
    if 0.8 <= ratio <= 1.2:
        return "near_1"
    if 80.0 <= ratio <= 120.0:
        return "near_100"
    if 8_000.0 <= ratio <= 12_000.0:
        return "near_10000"
    if 0.008 <= ratio <= 0.012:
        return "near_0.01"
    if ratio < 0.008:
        return "below_0.008"
    return "other"


def _query_settings() -> dict[str, int]:
    return {"max_threads": 1, "max_memory_usage": 3_500_000_000}


def _date_chunks(start_day: date, end_day: date, chunk_days: int) -> Iterable[tuple[date, date]]:
    width = max(1, int(chunk_days))
    cursor = start_day
    while cursor <= end_day:
        chunk_end = min(end_day, cursor + timedelta(days=width - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _db_date_range(client) -> tuple[date, date]:
    row = client.query(
        "SELECT min(trade_date), max(trade_date) FROM kline_daily FINAL",
        settings=_query_settings(),
    ).first_row
    if not row or row[0] is None or row[1] is None:
        raise RuntimeError("kline_daily is empty")
    return _as_date(row[0]), _as_date(row[1])


def _load_db_chunk(client, start_day: date, end_day: date) -> pd.DataFrame:
    start_sql = _quote(start_day.isoformat())
    end_sql = _quote(end_day.isoformat())
    sql = f"""
        SELECT
            k.code,
            k.trade_date,
            k.open,
            k.high,
            k.low,
            k.close,
            k.volume,
            k.amount,
            k.amplitude,
            k.change_pct,
            k.change_amount,
            k.turnover_rate,
            k.created_at,
            k.id,
            s.type AS instrument_type
        FROM
        (
            SELECT {", ".join(TARGET_COLUMNS)}
            FROM kline_daily
            WHERE trade_date >= toDate({start_sql})
              AND trade_date <= toDate({end_sql})
        ) AS k
        INNER JOIN stocks AS s ON s.code = k.code
        WHERE s.type IN ('stock', 'index')
        ORDER BY k.code, k.trade_date
    """
    return client.query_df(sql, settings=_query_settings())


def _infer_unmatched_instrument_type(code: str) -> str:
    """Return a conservative type hint for codes absent from ``stocks``."""

    normalized = str(code or "").strip().upper()
    return "index" if normalized in KNOWN_UNMATCHED_INDEX_CODES else "unmatched"


def _load_database_instrument_codes(client) -> dict[str, str]:
    """Load every persisted code, retaining explicit master types when present."""

    rows = client.query(
        "SELECT code, type FROM stocks WHERE type IN ('stock', 'index') ORDER BY code",
        settings=_query_settings(),
    ).result_rows
    master_types = {
        str(code).upper(): str(instrument_type or "stock").lower()
        for code, instrument_type in rows
    }
    all_codes = client.query(
        "SELECT DISTINCT code FROM kline_daily FINAL ORDER BY code",
        settings=_query_settings(),
    ).result_rows
    result: dict[str, str] = {}
    for (code,) in all_codes:
        normalized = str(code or "").strip().upper()
        if not normalized:
            continue
        result[normalized] = master_types.get(
            normalized,
            _infer_unmatched_instrument_type(normalized),
        )
    return result


def _load_db_code_batch(
    client,
    codes: list[str],
    start_day: date,
    end_day: date,
) -> pd.DataFrame:
    if not codes:
        return pd.DataFrame(columns=TARGET_COLUMNS)
    code_sql = ", ".join(_quote(code) for code in codes)
    sql = f"""
        SELECT {", ".join(TARGET_COLUMNS)}
        FROM kline_daily
        WHERE code IN ({code_sql})
          AND trade_date >= toDate({_quote(start_day.isoformat())})
          AND trade_date <= toDate({_quote(end_day.isoformat())})
        ORDER BY code, trade_date
    """
    return client.query_df(sql, settings=_query_settings())


def _load_database_baseline(client, start_day: date, end_day: date) -> dict[str, Any]:
    start_sql = _quote(start_day.isoformat())
    end_sql = _quote(end_day.isoformat())
    scoped = f"""
        FROM kline_daily FINAL
        WHERE trade_date >= toDate({start_sql})
          AND trade_date <= toDate({end_sql})
    """
    row = client.query(
        f"SELECT count(), uniqExact(code), uniqExact(trade_date), min(trade_date), max(trade_date) {scoped}",
        settings=_query_settings(),
    ).first_row
    raw_row = client.query(
        f"SELECT count() FROM kline_daily WHERE trade_date >= toDate({start_sql}) AND trade_date <= toDate({end_sql})",
        settings=_query_settings(),
    ).first_row
    type_rows = client.query(
        f"""
        SELECT s.type, count(), uniqExact(k.code),
               countIf(k.volume <= 0 OR k.amount <= 0),
               countIf(k.volume > 0 AND k.amount > 0 AND
                       abs(k.volume * k.close * 100 - k.amount) / k.amount > 0.2)
        FROM (SELECT code, trade_date, close, volume, amount {scoped}) AS k
        INNER JOIN stocks AS s ON s.code = k.code
        GROUP BY s.type
        ORDER BY s.type
        """,
        settings=_query_settings(),
    ).result_rows
    invalid = client.query(
        f"""
        SELECT
            countIf(volume < 0 OR amount < 0 OR close <= 0),
            countIf(volume = 0 AND amount > 0),
            countIf(volume > 0 AND amount = 0)
        {scoped}
        """,
        settings=_query_settings(),
    ).first_row
    unmatched = client.query(
        f"""
        SELECT count(), uniqExact(k.code)
        FROM (SELECT code {scoped}) AS k
        LEFT JOIN stocks AS s ON s.code = k.code
        WHERE s.code = '' OR s.code IS NULL
        """,
        settings=_query_settings(),
    ).first_row
    non_trading = client.query(
        f"""
        SELECT count()
        FROM (SELECT trade_date {scoped}) AS k
        LEFT JOIN trade_calendar AS c
          ON c.market = 'SH' AND c.trade_date = k.trade_date
        WHERE c.trade_date IS NULL OR c.is_trading = 0
        """,
        settings=_query_settings(),
    ).first_row[0]
    return {
        "final_rows": int(row[0] or 0),
        "raw_rows": int(raw_row[0] or 0),
        "uniq_codes": int(row[1] or 0),
        "uniq_trade_dates": int(row[2] or 0),
        "min_trade_date": str(row[3]) if row[3] else None,
        "max_trade_date": str(row[4]) if row[4] else None,
        "type_breakdown": [
            {
                "instrument_type": str(item[0]),
                "rows": int(item[1] or 0),
                "codes": int(item[2] or 0),
                "nonpositive_volume_or_amount": int(item[3] or 0),
                "internal_stock_relation_outliers": int(item[4] or 0),
            }
            for item in type_rows
        ],
        "invalid_negative_or_nonpositive_close": int(invalid[0] or 0),
        "volume_zero_amount_positive": int(invalid[1] or 0),
        "volume_positive_amount_zero": int(invalid[2] or 0),
        "unmatched_stock_master_rows": int(unmatched[0] or 0),
        "unmatched_stock_master_codes": int(unmatched[1] or 0),
        "non_trading_day_rows": int(non_trading or 0),
    }


def _parse_day(raw: int) -> date | None:
    text = str(int(raw))
    if len(text) != 8:
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _tdx_code(path: Path, market: str) -> str:
    stem = path.stem
    raw = stem[2:] if stem[:2].lower() in {"sh", "sz", "bj"} else stem
    return f"{raw}.{market.upper()}"


def _load_tdx_values(
    root: Path,
    db_keys: set[tuple[str, date]],
    start_day: date,
    end_day: date,
) -> tuple[dict[tuple[str, date], tuple[float, float]], int, date | None]:
    values: dict[tuple[str, date], tuple[float, float]] = {}
    code_set = {code for code, _ in db_keys}
    files_used = 0
    latest_source_day: date | None = None
    for market in ("sh", "sz", "bj"):
        folder = root / market / "lday"
        if not folder.exists():
            continue
        for path in folder.glob("*.day"):
            code = _tdx_code(path, market)
            if code not in code_set:
                continue
            raw_rows: list[dict[str, float | date]] = []
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(DAY_RECORD.size)
                    if not chunk or len(chunk) < DAY_RECORD.size:
                        break
                    raw_day, _open_i, _high_i, _low_i, _close_i, amount_f, volume_i, _reserved = DAY_RECORD.unpack(chunk)
                    trade_day = _parse_day(raw_day)
                    if trade_day is None or trade_day < start_day or trade_day > end_day:
                        continue
                    key = (code, trade_day)
                    if key not in db_keys:
                        continue
                    raw_rows.append(
                        {
                            "trade_date": trade_day,
                            "volume": float(volume_i or 0),
                            "amount": float(amount_f or 0.0),
                        }
                    )
                    latest_source_day = max(latest_source_day, trade_day) if latest_source_day else trade_day
            if not raw_rows:
                continue
            files_used += 1
            normalized = normalize_tdx_day_units(pd.DataFrame(raw_rows))
            for item in normalized.itertuples(index=False):
                if float(item.volume) > 0 and float(item.amount) > 0:
                    values[(code, _as_date(item.trade_date))] = (
                        float(item.volume),
                        float(item.amount),
                    )
    return values, files_used, latest_source_day


def _repair_row(row: Any, source_volume: float, source_amount: float) -> tuple[Any, ...]:
    values = {column: getattr(row, column) for column in TARGET_COLUMNS}
    values["volume"] = source_volume
    values["amount"] = source_amount
    return tuple(values[column] for column in TARGET_COLUMNS)


def _ensure_stage(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {STAGE_TABLE}
        (
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
            id UInt64
        )
        ENGINE = MergeTree
        ORDER BY (code, trade_date)
        """
    )
    client.command(f"TRUNCATE TABLE {STAGE_TABLE}")


def _apply_repairs(client, repair_rows: list[tuple[Any, ...]], batch_size: int) -> int:
    if not repair_rows:
        return 0
    _ensure_stage(client)
    size = max(1, int(batch_size))
    for offset in range(0, len(repair_rows), size):
        client.insert(
            STAGE_TABLE,
            repair_rows[offset : offset + size],
            column_names=TARGET_COLUMNS,
        )
    # Read the delete keys from the bounded stage table instead of expanding
    # hundreds of thousands of code/date literals into one SQL statement.
    client.command(
        f"""
        ALTER TABLE kline_daily
        DELETE WHERE (code, trade_date) IN (
            SELECT code, trade_date
            FROM {STAGE_TABLE}
        )
        SETTINGS mutations_sync = 1
        """
    )
    client.command(
        f"""
        INSERT INTO kline_daily ({", ".join(TARGET_COLUMNS)})
        SELECT {", ".join(TARGET_COLUMNS)}
        FROM {STAGE_TABLE}
        """
    )
    return len(repair_rows)


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def audit_database(args: argparse.Namespace) -> dict[str, Any]:
    client = clickhouse_client()
    db_start, db_end = _db_date_range(client)
    start_day = date.fromisoformat(args.start_date) if args.start_date else db_start
    end_day = date.fromisoformat(args.end_date) if args.end_date else db_end
    if start_day > end_day:
        raise ValueError("start-date must not be later than end-date")
    root = Path(args.tdx_root)
    baseline = _load_database_baseline(client, start_day, end_day)
    if baseline["raw_rows"] != baseline["final_rows"]:
        raise RuntimeError(
            "kline_daily raw_rows and final_rows differ; refuse code-batch "
            "audit until duplicate keys are resolved"
        )
    instrument_types = _load_database_instrument_codes(client)
    codes = sorted(instrument_types)
    classification_counts: Counter[str] = Counter()
    type_counts: Counter[tuple[str, str]] = Counter()
    ratio_counts: Counter[tuple[str, str, str, str]] = Counter()
    code_counts: Counter[tuple[str, str, str]] = Counter()
    samples: dict[str, dict[str, Any]] = {}
    repair_rows: list[tuple[Any, ...]] = []
    chunk_reports: list[dict[str, Any]] = []
    db_rows_checked = 0
    tdx_overlap_rows = 0
    tdx_missing_rows = 0
    tdx_files_used = 0
    latest_tdx_day: date | None = None

    for offset in range(0, len(codes), max(1, args.code_batch_size)):
        batch_codes = codes[offset : offset + max(1, args.code_batch_size)]
        frame = _load_db_code_batch(client, batch_codes, start_day, end_day)
        chunk_db_rows = int(len(frame))
        db_rows_checked += chunk_db_rows
        chunk_overlap = 0
        chunk_mismatch = 0
        chunk_repairable = 0
        chunk_missing = 0
        if not frame.empty and root.exists():
            frame["trade_date"] = pd.to_datetime(frame["trade_date"]).dt.date
            frame["instrument_type"] = frame["code"].map(
                lambda value: instrument_types.get(str(value).upper(), "unknown")
            )
            keys = {(str(row.code), row.trade_date) for row in frame.itertuples(index=False)}
            tdx_values, files_used, source_day = _load_tdx_values(
                root,
                keys,
                start_day,
                end_day,
            )
            tdx_files_used += files_used
            if source_day and (latest_tdx_day is None or source_day > latest_tdx_day):
                latest_tdx_day = source_day
            for row in frame.itertuples(index=False):
                key = (str(row.code), row.trade_date)
                source = tdx_values.get(key)
                if source is None:
                    chunk_missing += 1
                    continue
                chunk_overlap += 1
                source_volume, source_amount = source
                classification = classify_daily_unit_pair(
                    _as_float(row.volume),
                    _as_float(row.amount),
                    source_volume,
                    source_amount,
                )
                classification_counts[classification] += 1
                type_counts[(str(row.instrument_type), classification)] += 1
                if classification != "ok":
                    volume_ratio = _as_float(row.volume) / source_volume if source_volume > 0 else 0.0
                    amount_ratio = _as_float(row.amount) / source_amount if source_amount > 0 else 0.0
                    ratio_counts[
                        (
                            str(row.instrument_type),
                            classification,
                            _ratio_bucket(volume_ratio),
                            _ratio_bucket(amount_ratio),
                        )
                    ] += 1
                    code_counts[
                        (
                            str(row.code),
                            str(row.instrument_type),
                            classification,
                        )
                    ] += 1
                if classification != "ok":
                    chunk_mismatch += 1
                    samples.setdefault(
                        classification,
                        {
                            "code": str(row.code),
                            "trade_date": str(row.trade_date),
                            "instrument_type": str(row.instrument_type),
                            "db_volume": _as_float(row.volume),
                            "source_volume": source_volume,
                            "db_amount": _as_float(row.amount),
                            "source_amount": source_amount,
                        },
                    )
                    if classification in REPAIRABLE_CLASSES:
                        repair_rows.append(_repair_row(row, source_volume, source_amount))
                        chunk_repairable += 1
            tdx_overlap_rows += chunk_overlap
            tdx_missing_rows += chunk_missing
        elif chunk_db_rows:
            tdx_missing_rows += chunk_db_rows
        chunk_reports.append(
            {
                "code_offset": offset,
                "code_count": len(batch_codes),
                "first_code": batch_codes[0] if batch_codes else None,
                "last_code": batch_codes[-1] if batch_codes else None,
                "database_rows": chunk_db_rows,
                "tdx_overlap_rows": chunk_overlap,
                "tdx_missing_rows": chunk_missing,
                "mismatch_rows": chunk_mismatch,
                "repairable_rows": chunk_repairable,
            }
        )

    applied_rows = 0
    if args.apply and repair_rows:
        applied_rows = _apply_repairs(client, repair_rows, args.apply_batch_size)

    payload = {
        "schema_version": 1,
        "checked_at": datetime.now(BUSINESS_TZ).isoformat(timespec="seconds"),
        "timezone": "Asia/Shanghai",
        "mode": "apply" if args.apply else "audit",
        "start_date": start_day.isoformat(),
        "end_date": end_day.isoformat(),
        "unit_contract": {
            "volume": "lots",
            "amount": "yuan",
            "source": "local TDX .day converted with volume shares / 100; amount unchanged",
        },
        "source_scope": {
            "source": "local_tdx_day",
            "tdx_root": str(root),
            "tdx_root_exists": root.exists(),
            "tdx_files_used": tdx_files_used,
            "latest_tdx_source_day": latest_tdx_day.isoformat() if latest_tdx_day else None,
            "comparison_key": "(code, trade_date)",
            "database_code_scope": "all distinct kline_daily codes; stocks master types retained, explicit unmatched index hints allowed",
            "scan_strategy": "kline_daily code batches; each matching TDX day file is parsed once per run",
        },
        "baseline": baseline,
        "database_rows_checked": db_rows_checked,
        "tdx_overlap_rows": tdx_overlap_rows,
        "tdx_missing_rows": tdx_missing_rows,
        "classification_counts": dict(sorted(classification_counts.items())),
        "classification_by_instrument_type": {
            f"{instrument_type}:{classification}": count
            for (instrument_type, classification), count in sorted(type_counts.items())
        },
        "mismatch_by_ratio_bucket": {
            f"{instrument_type}:{classification}:volume_{volume_bucket}:amount_{amount_bucket}": count
            for (
                instrument_type,
                classification,
                volume_bucket,
                amount_bucket,
            ), count in sorted(ratio_counts.items())
        },
        "top_mismatch_codes": [
            {
                "code": code,
                "instrument_type": instrument_type,
                "classification": classification,
                "rows": count,
            }
            for (code, instrument_type, classification), count in code_counts.most_common(100)
        ],
        "mismatch_rows": int(sum(count for key, count in classification_counts.items() if key != "ok")),
        "repairable_rows": len(repair_rows),
        "applied_rows": applied_rows,
        "repair_policy": {
            "apply_requires_explicit_flag": True,
            "repairable_classes": sorted(REPAIRABLE_CLASSES),
            "nonrepairable_classes": ["other", "source_nonpositive"],
            "historical_scale_inference_is_not_used_for_writes": True,
        },
        "sample_by_class": samples,
        "chunks": chunk_reports,
        "status": "degraded" if sum(
            count for classification, count in classification_counts.items()
            if classification != "ok"
        ) else "healthy",
    }
    return payload


def parse_args() -> argparse.Namespace:
    now = datetime.now(BUSINESS_TZ)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--tdx-root", default=r"D:\TDX\vipdoc")
    parser.add_argument("--code-batch-size", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-batch-size", type=int, default=10_000)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path(report_path(
            "kline_daily_database_audit",
            f"units_{now:%Y%m%d_%H%M%S}.json",
        )),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = audit_database(args)
    _write_report(args.report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "mode": report["mode"],
                "report": str(args.report),
                "database_rows_checked": report["database_rows_checked"],
                "tdx_overlap_rows": report["tdx_overlap_rows"],
                "mismatch_rows": report["mismatch_rows"],
                "repairable_rows": report["repairable_rows"],
                "applied_rows": report["applied_rows"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "healthy" or not args.apply else 2


if __name__ == "__main__":
    raise SystemExit(main())

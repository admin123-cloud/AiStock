"""Materialize purchased-history K-line issues for the manual data-review queue.

This writes only an isolated ClickHouse review-item table and a small runtime
manifest.  It never writes a formal K-line table or triggers a repair job.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.dataset as pa_dataset

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path, warehouse_path


TABLE = "stock.purchased_history_data_review_items"
MANIFEST_PATH = runtime_path("purchased_history", "data_review_manifest.json")
DEFAULT_PARQUET_ROOT = warehouse_path(
    "purchased_history",
    "daily_snapshot",
    "purchased_daily_snapshot_20260916_r1",
)
COLUMNS = [
    "run_id",
    "review_key",
    "issue_type",
    "source",
    "code",
    "trade_date",
    "period",
    "security_type",
    "expected_count",
    "actual_count",
    "missing_count",
    "source_batch_id",
    "source_archive",
    "source_entry",
    "evidence_ref",
    "evidence_sha256",
    "observed_at",
]
SH_TZ = ZoneInfo("Asia/Shanghai")


def _sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _date_text(value: object) -> str:
    return str(value)[:10]


def _infer_source_date(source_archive: str, source_entry: str) -> date | None:
    """Infer a stable queue date for a source-file-only 5m failure.

    Empty/invalid 5m files have no row datetime.  The purchased archive names
    carry a YYYYMMDD snapshot date (for example ``20020114_5min.zip``), which
    is sufficient to key a file-level review item without inventing a bar.
    """
    for value in (source_archive, source_entry):
        match = re.search(r"(?<!\d)(\d{8})(?!\d)", str(value or ""))
        if match:
            try:
                return date.fromisoformat(f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:8]}")
            except ValueError:
                continue
    return None


def _review_key(issue_type: str, period: str, code: str, trade_date: object) -> str:
    return f"purchased_history|{issue_type}|{period}|{code}|{_date_text(trade_date)}"


def _row(
    *,
    run_id: str,
    issue_type: str,
    source: str,
    code: str,
    trade_date: object,
    period: str,
    security_type: str,
    expected_count: int,
    actual_count: int,
    source_batch_id: str,
    source_archive: str,
    source_entry: str,
    evidence_ref: str,
    observed_at: datetime,
) -> list[object]:
    trade_date_text = _date_text(trade_date)
    trade_date_value = date.fromisoformat(trade_date_text)
    evidence = {
        "issue_type": issue_type,
        "source": source,
        "code": code,
        "trade_date": trade_date_text,
        "period": period,
        "expected_count": int(expected_count),
        "actual_count": int(actual_count),
        "source_batch_id": source_batch_id,
        "source_archive": source_archive,
        "source_entry": source_entry,
        "evidence_ref": evidence_ref,
    }
    return [
        run_id,
        _review_key(issue_type, period, code, trade_date_text),
        issue_type,
        source,
        code,
        trade_date_value,
        period,
        security_type,
        int(expected_count),
        int(actual_count),
        max(0, int(expected_count) - int(actual_count)),
        source_batch_id,
        source_archive,
        source_entry,
        evidence_ref,
        _sha256(evidence),
        observed_at,
    ]


def _ensure_table(client) -> None:
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE}
        (
            run_id String,
            review_key String,
            issue_type LowCardinality(String),
            source LowCardinality(String),
            code String,
            trade_date Date,
            period LowCardinality(String),
            security_type LowCardinality(String),
            expected_count UInt16,
            actual_count UInt16,
            missing_count UInt16,
            source_batch_id String,
            source_archive String,
            source_entry String,
            evidence_ref String,
            evidence_sha256 FixedString(64),
            observed_at DateTime('Asia/Shanghai')
        )
        ENGINE = MergeTree
        PARTITION BY toYear(trade_date)
        ORDER BY (run_id, issue_type, trade_date, code)
        """
    )


def _flush(client, rows: list[list[object]]) -> int:
    if not rows:
        return 0
    client.insert(TABLE, rows, column_names=COLUMNS)
    written = len(rows)
    rows.clear()
    return written


def _append_daily_missing_ohlc(
    *,
    client,
    run_id: str,
    parquet_root: Path,
    observed_at: datetime,
    batch_rows: int,
) -> int:
    dataset = pa_dataset.dataset(parquet_root, format="parquet", partitioning="hive")
    required = {"code", "trade_date", "quality_status", "security_type", "source_archive", "source_entry", "batch_id"}
    missing_columns = sorted(required - set(dataset.schema.names))
    if missing_columns:
        raise RuntimeError(f"daily parquet lacks required columns: {missing_columns}")
    scanner = dataset.scanner(
        columns=sorted(required),
        filter=pa_dataset.field("quality_status") == "missing_ohlc",
        batch_size=batch_rows,
    )
    evidence_ref = str(parquet_root / "_manifest.json")
    rows: list[list[object]] = []
    written = 0
    for arrow_batch in scanner.to_batches():
        values = arrow_batch.to_pydict()
        for index, code in enumerate(values["code"]):
            rows.append(
                _row(
                    run_id=run_id,
                    issue_type="daily_source_missing_ohlc",
                    source="purchased_daily_snapshot",
                    code=str(code),
                    trade_date=values["trade_date"][index],
                    period="1d",
                    security_type=str(values["security_type"][index] or "stock"),
                    expected_count=1,
                    actual_count=0,
                    source_batch_id=str(values["batch_id"][index] or ""),
                    source_archive=str(values["source_archive"][index] or ""),
                    source_entry=str(values["source_entry"][index] or ""),
                    evidence_ref=evidence_ref,
                    observed_at=observed_at,
                )
            )
            if len(rows) >= batch_rows:
                written += _flush(client, rows)
                print(f"daily_missing_ohlc_written={written}", flush=True)
    written += _flush(client, rows)
    return written


def _append_project_daily_missing_for_5m(
    *,
    client,
    run_id: str,
    batch_prefix: str,
    observed_at: datetime,
    batch_rows: int,
) -> int:
    bounds = client.query(
        """
        SELECT min(trade_date), max(trade_date)
        FROM stock.purchased_history_minute_5_daily_recon_audit
        WHERE batch_id LIKE %(batch_prefix)s
          AND daily_rows = 0
        """,
        parameters={"batch_prefix": batch_prefix},
    ).result_rows[0]
    min_trade_date, max_trade_date = bounds
    if min_trade_date is None:
        return 0

    result = client.query(
        """
        SELECT
            recon.code,
            recon.trade_date,
            recon.security_type,
            recon.source_batches,
            recon.minute_rows
        FROM
        (
            SELECT
                code,
                trade_date,
                any(security_type) AS security_type,
                arrayStringConcat(arraySort(groupUniqArray(batch_id)), ',') AS source_batches,
                max(minute_rows) AS minute_rows
            FROM stock.purchased_history_minute_5_daily_recon_audit
            WHERE batch_id LIKE %(batch_prefix)s
              AND daily_rows = 0
            GROUP BY code, trade_date
        ) AS recon
        LEFT ANTI JOIN
        (
            SELECT code, trade_date
            FROM stock.kline_daily
            WHERE trade_date >= %(min_trade_date)s
              AND trade_date <= %(max_trade_date)s
              AND open > 0
              AND high > 0
              AND low > 0
              AND close > 0
            GROUP BY code, trade_date
        ) AS formal_daily USING (code, trade_date)
        ORDER BY recon.trade_date, recon.code
        """,
        parameters={
            "batch_prefix": batch_prefix,
            "min_trade_date": min_trade_date,
            "max_trade_date": max_trade_date,
        },
    )
    evidence_ref = "ClickHouse:stock.purchased_history_minute_5_daily_recon_audit"
    rows: list[list[object]] = []
    written = 0
    for code, trade_date, security_type, source_batches, minute_rows in result.result_rows:
        rows.append(
            _row(
                run_id=run_id,
                issue_type="project_daily_missing_for_5m",
                source="purchased_5m_daily_reconciliation",
                code=str(code),
                trade_date=trade_date,
                period="5m_to_1d",
                security_type=str(security_type),
                expected_count=1,
                actual_count=0,
                source_batch_id=str(source_batches),
                source_archive="",
                source_entry="",
                evidence_ref=f"{evidence_ref}; minute_rows={int(minute_rows)}",
                observed_at=observed_at,
            )
        )
        if len(rows) >= batch_rows:
            written += _flush(client, rows)
    written += _flush(client, rows)
    return written


def _append_incomplete_5m_source_files(
    *,
    client,
    run_id: str,
    batch_prefix: str,
    observed_at: datetime,
    batch_rows: int,
) -> int:
    result = client.query(
        """
        SELECT
            code,
            toDate(min_datetime) AS trade_date,
            any(security_type) AS security_type,
            arrayStringConcat(arraySort(groupUniqArray(batch_id)), ',') AS source_batches,
            arrayStringConcat(arraySort(groupUniqArray(source_archive)), ',') AS source_archives,
            arrayStringConcat(arraySort(groupUniqArray(source_entry)), ',') AS source_entries,
            max(expected_bar_count) AS expected_bars,
            min(valid_ohlc_row_count) AS actual_bars
        FROM stock.purchased_history_minute_5_source_audit_v2
        WHERE batch_id LIKE %(batch_prefix)s
          AND (
              valid_ohlc_row_count != expected_bar_count
              OR unique_datetime_count != expected_bar_count
              OR invalid_row_count > 0
          )
        GROUP BY code, trade_date
        ORDER BY trade_date, code
        """,
        parameters={"batch_prefix": batch_prefix},
    )
    evidence_ref = "ClickHouse:stock.purchased_history_minute_5_source_audit_v2"
    candidates = [
        (str(code), trade_date, int(expected_bars), int(actual_bars), str(security_type), str(source_batches), str(source_archives), str(source_entries))
        for code, trade_date, security_type, source_batches, source_archives, source_entries, expected_bars, actual_bars in result.result_rows
    ]
    resolved_formal = _formally_complete_code_days(
        client,
        [(code, trade_date, expected_bars) for code, trade_date, expected_bars, *_ in candidates],
    )
    rows: list[list[object]] = []
    written = 0
    for code, trade_date, expected_bars, actual_bars, security_type, source_batches, source_archives, source_entries in candidates:
        key = (code, _date_text(trade_date))
        if key in resolved_formal:
            # The source audit remains immutable evidence, but a code-day that
            # now has complete formal 5m, 15m, 30m, 60m and valid daily rows
            # is no longer an actionable current queue item.
            continue
        if trade_date is None:
            inferred = _infer_source_date(str(source_archives), str(source_entries))
            if inferred is None:
                raise RuntimeError(f"incomplete 5m source has no parsable date: {code}")
            trade_date = inferred
            issue_type = "source_5m_file_invalid_no_datetime"
        else:
            issue_type = "source_5m_incomplete_bars"
        rows.append(
            _row(
                run_id=run_id,
                issue_type=issue_type,
                source="purchased_5m_source_audit",
                code=str(code),
                trade_date=trade_date,
                period="5m",
                security_type=str(security_type),
                expected_count=int(expected_bars),
                actual_count=int(actual_bars),
                source_batch_id=str(source_batches),
                source_archive=str(source_archives),
                source_entry=str(source_entries),
                evidence_ref=evidence_ref,
                observed_at=observed_at,
            )
        )
        if len(rows) >= batch_rows:
            written += _flush(client, rows)
    written += _flush(client, rows)
    return written


def _formal_key_tuples(keys: list[tuple[str, str, int]]) -> str:
    values: list[str] = []
    for code, trade_date, _expected in keys:
        safe_code = code.replace("'", "''")
        values.append(f"('{safe_code}', toDate('{trade_date}'))")
    return ", ".join(values)


def _formally_complete_code_days(
    client,
    keys: list[tuple[str, object, int]],
) -> set[tuple[str, str]]:
    """Return keys already complete across formal 5m/derived/daily tables.

    This is deliberately a current-state projection rule.  The immutable
    source-audit row is retained in ClickHouse for provenance, while the
    manual queue only carries unresolved formal delivery gaps.
    """
    normalized = [
        (str(code), _date_text(trade_date), int(expected))
        for code, trade_date, expected in keys
        if trade_date is not None
    ]
    if not normalized:
        return set()
    expected_by_key = {(code, day): expected for code, day, expected in normalized}
    stats: dict[tuple[str, str], dict[str, tuple[int, int, int]]] = defaultdict(dict)
    normalized.sort(key=lambda item: (item[1], item[0]))
    # First do one non-FINAL probe over the candidate set.  It is enough to
    # identify code-days that have any complete formal 5m baseline; only those
    # few keys need the more expensive FINAL checks below.
    all_tuples = _formal_key_tuples(normalized)
    min_day = normalized[0][1]
    upper_day = date.fromisoformat(normalized[-1][1]) + timedelta(days=1)
    m5_probe = client.query(
        f"""
        SELECT code, toDate(datetime) AS trade_date,
               count() AS rows,
               uniqExact(datetime) AS unique_rows,
               countIf(open > 0 AND high > 0 AND low > 0 AND close > 0) AS valid_rows
        FROM stock.kline_minute_5
        PREWHERE datetime >= toDate('{min_day}')
             AND datetime < toDate('{upper_day}')
        WHERE (code, toDate(datetime)) IN ({all_tuples})
        GROUP BY code, trade_date
        """,
        settings={"max_threads": 1},
    )
    m5_candidates: list[tuple[str, str, int]] = []
    for code, trade_date, row_count, unique_rows, valid_rows in m5_probe.result_rows:
        key = (str(code), _date_text(trade_date))
        expected = expected_by_key.get(key, 48)
        stats[key]["5m_probe"] = (int(row_count), int(unique_rows), int(valid_rows))
        if int(unique_rows) == expected and int(valid_rows) >= expected:
            m5_candidates.append((key[0], key[1], expected))
    if not m5_candidates:
        return set()

    m5_candidates.sort(key=lambda item: (item[1], item[0]))
    tuples = _formal_key_tuples(m5_candidates)
    min_day = m5_candidates[0][1]
    upper_day = date.fromisoformat(m5_candidates[-1][1]) + timedelta(days=1)
    for label, table, expected_factor in (
        ("5m", "stock.kline_minute_5", 1),
        ("15m", "stock.kline_minute_15", 3),
        ("30m", "stock.kline_minute_30", 6),
        ("60m", "stock.kline_minute_60", 12),
    ):
        result = client.query(
            f"""
            SELECT code, toDate(datetime) AS trade_date,
                   count() AS rows,
                   uniqExact(datetime) AS unique_rows,
                   countIf(open > 0 AND high > 0 AND low > 0 AND close > 0) AS valid_rows
            FROM {table} FINAL
            WHERE datetime >= toDate('{min_day}')
              AND datetime < toDate('{upper_day}')
              AND (code, toDate(datetime)) IN ({tuples})
            GROUP BY code, trade_date
            """,
            settings={"max_threads": 1},
        )
        for code, trade_date, row_count, unique_rows, valid_rows in result.result_rows:
            key = (str(code), _date_text(trade_date))
            stats[key][label] = (int(row_count), int(unique_rows), int(valid_rows))

    daily = client.query(
            f"""
            SELECT code, trade_date,
                   count() AS rows,
                   countIf(open > 0 AND high > 0 AND low > 0 AND close > 0) AS valid_rows
            FROM stock.kline_daily FINAL
            WHERE trade_date >= toDate('{min_day}')
              AND trade_date < toDate('{upper_day}')
              AND (code, trade_date) IN ({tuples})
            GROUP BY code, trade_date
            """,
            settings={"max_threads": 1},
        )
    for code, trade_date, row_count, valid_rows in daily.result_rows:
        stats[(str(code), _date_text(trade_date))]["daily"] = (int(row_count), int(row_count), int(valid_rows))

    complete: set[tuple[str, str]] = set()
    for key, expected in ((key, expected_by_key[key]) for key in [(code, day) for code, day, _ in m5_candidates]):
        periods_ok = True
        for label in ("5m", "15m", "30m", "60m"):
            expected_rows = expected // {"5m": 1, "15m": 3, "30m": 6, "60m": 12}[label]
            rows, unique_rows, valid_rows = stats.get(key, {}).get(label, (0, 0, 0))
            periods_ok = periods_ok and (rows == expected_rows and unique_rows == expected_rows and valid_rows == expected_rows)
        daily_rows, _daily_unique, daily_valid = stats.get(key, {}).get("daily", (0, 0, 0))
        if periods_ok and daily_rows == 1 and daily_valid == 1:
            complete.add(key)
    return complete


def _write_manifest(payload: dict[str, object]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = MANIFEST_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(MANIFEST_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--parquet-root", type=Path, default=DEFAULT_PARQUET_ROOT)
    parser.add_argument("--batch-prefix", default="purchased_5m_%")
    parser.add_argument("--batch-rows", type=int, default=50_000)
    parser.add_argument("--skip-daily-missing-ohlc", action="store_true")
    args = parser.parse_args()
    if args.batch_rows < 1:
        raise SystemExit("--batch-rows must be positive")
    if not args.parquet_root.exists():
        raise SystemExit(f"daily parquet root does not exist: {args.parquet_root}")

    observed_at = datetime.now(SH_TZ).replace(microsecond=0)
    run_id = args.run_id or f"purchased_history_review_{observed_at:%Y%m%d_%H%M%S}"
    client = clickhouse_client()
    _ensure_table(client)
    existing_rows = client.query(
        f"SELECT count() FROM {TABLE} WHERE run_id = %(run_id)s",
        parameters={"run_id": run_id},
    ).result_rows[0][0]
    if existing_rows:
        raise SystemExit(f"review_run_exists={run_id}; refusing duplicate materialization")

    counts: Counter[str] = Counter()
    if not args.skip_daily_missing_ohlc:
        counts["daily_source_missing_ohlc"] = _append_daily_missing_ohlc(
            client=client,
            run_id=run_id,
            parquet_root=args.parquet_root,
            observed_at=observed_at,
            batch_rows=args.batch_rows,
        )
    counts["project_daily_missing_for_5m"] = _append_project_daily_missing_for_5m(
        client=client,
        run_id=run_id,
        batch_prefix=args.batch_prefix,
        observed_at=observed_at,
        batch_rows=args.batch_rows,
    )
    counts["source_5m_incomplete_bars"] = _append_incomplete_5m_source_files(
        client=client,
        run_id=run_id,
        batch_prefix=args.batch_prefix,
        observed_at=observed_at,
        batch_rows=args.batch_rows,
    )

    validation = client.query(
        f"""
        SELECT count() AS rows, uniqExact(review_key) AS unique_review_keys
        FROM {TABLE}
        WHERE run_id = %(run_id)s
        """,
        parameters={"run_id": run_id},
    ).result_rows[0]
    expected_rows = sum(counts.values())
    actual_rows, unique_keys = (int(validation[0]), int(validation[1]))
    if actual_rows != expected_rows or unique_keys != expected_rows:
        raise RuntimeError(
            f"review queue validation failed: expected={expected_rows}, rows={actual_rows}, unique={unique_keys}"
        )

    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "table": TABLE,
        "generated_at": observed_at.isoformat(),
        "counts": dict(counts),
        "rows": expected_rows,
        "unique_review_keys": unique_keys,
        "batch_prefix": args.batch_prefix,
        "daily_parquet_root": str(args.parquet_root),
        "formal_table_mutation": False,
    }
    _write_manifest(manifest)
    print(json.dumps({"manifest": str(MANIFEST_PATH), **manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

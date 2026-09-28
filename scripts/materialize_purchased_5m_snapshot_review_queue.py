"""Expose purchased 5m/daily-snapshot review rows to the manual queue.

The source reconciliation table remains authoritative.  This script creates a
new immutable queue projection that carries forward the previous purchased
history items and adds only ``overall_status=review`` rows from the snapshot
reconciliation.  The API treats the new issue type as keep-pending-only; it
never enqueues QMT/TDX repair and never writes a formal K-line table.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


TABLE = "stock.purchased_history_data_review_items"
RECON_TABLE = "stock.purchased_history_minute_5_daily_snapshot_recon"
MANIFEST_PATH = runtime_path("purchased_history", "data_review_manifest.json")
BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
COLUMNS = [
    "run_id", "review_key", "issue_type", "source", "code", "trade_date", "period", "security_type",
    "expected_count", "actual_count", "missing_count", "source_batch_id", "source_archive", "source_entry",
    "evidence_ref", "evidence_sha256", "observed_at", "reason",
]
NEW_ISSUE_TYPE = "purchased_5m_daily_snapshot_reconciliation"


def _sha256(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _ensure_table(client) -> None:
    client.command(
        f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS reason String DEFAULT ''"
    )


def _ensure_recon_columns(client) -> None:
    """Allow queue refresh after the additive price-ratio audit upgrade."""
    client.command(
        f"ALTER TABLE {RECON_TABLE} ADD COLUMN IF NOT EXISTS price_relative_diff Nullable(Float64)"
    )


def _recon_price_column_expression(client) -> str:
    columns = client.query(
        """
        SELECT name
        FROM system.columns
        WHERE database='stock' AND table=%(table)s AND name='price_relative_diff'
        """,
        parameters={"table": RECON_TABLE.split(".", 1)[1]},
    ).result_rows
    if columns:
        return "price_relative_diff"
    return "CAST(NULL, 'Nullable(Float64)') AS price_relative_diff"


def _review_key(code: str, trade_date: date) -> str:
    return f"purchased_history|{NEW_ISSUE_TYPE}|5m_to_1d|{code}|{trade_date}"


def _row(run_id: str, source_row: tuple, observed_at: datetime) -> list[object]:
    (
        batch_id, code, trade_date, security_type, minute_rows, unique_datetime_rows,
        daily_snapshot_rows, daily_quality_status, price_status, volume_status,
        amount_status, overall_status, max_price_abs_diff, price_relative_diff,
        volume_relative_diff, amount_relative_diff, reason,
    ) = source_row
    trade_date = trade_date if isinstance(trade_date, date) else date.fromisoformat(str(trade_date)[:10])
    code = str(code)
    reason = str(reason or "")
    expected_count = 48
    actual_count = int(minute_rows)
    evidence = {
        "issue_type": NEW_ISSUE_TYPE,
        "source": "purchased_5m_daily_snapshot_reconciliation",
        "batch_id": str(batch_id),
        "code": code,
        "trade_date": str(trade_date),
        "minute_rows": actual_count,
        "unique_datetime_rows": int(unique_datetime_rows),
        "daily_snapshot_rows": int(daily_snapshot_rows),
        "daily_quality_status": str(daily_quality_status),
        "price_status": str(price_status),
        "max_price_abs_diff": max_price_abs_diff,
        "price_relative_diff": price_relative_diff,
        "volume_status": str(volume_status),
        "volume_relative_diff": volume_relative_diff,
        "amount_status": str(amount_status),
        "amount_relative_diff": amount_relative_diff,
        "overall_status": str(overall_status),
        "reason": reason,
    }
    evidence_ref = (
        f"ClickHouse:{RECON_TABLE}; minute_rows={actual_count}; "
        f"daily_quality_status={daily_quality_status}; "
        f"max_price_abs_diff={max_price_abs_diff}; "
        f"price_relative_diff={price_relative_diff}; reason={reason}"
    )
    return [
        run_id,
        _review_key(code, trade_date),
        NEW_ISSUE_TYPE,
        "purchased_5m_daily_snapshot_reconciliation",
        code,
        trade_date,
        "5m_to_1d",
        str(security_type or "stock"),
        expected_count,
        actual_count,
        max(0, expected_count - actual_count),
        str(batch_id),
        "",
        "",
        evidence_ref,
        _sha256(evidence),
        observed_at,
        reason,
    ]


def _insert(client, rows: list[list[object]], batch_size: int) -> int:
    written = 0
    for start in range(0, len(rows), batch_size):
        chunk = rows[start:start + batch_size]
        client.insert(TABLE, chunk, column_names=COLUMNS)
        written += len(chunk)
    return written


def _safe_key_tuple(code: object, trade_date: object) -> tuple[str, str] | None:
    code_text = str(code or "")
    date_text = str(trade_date or "")[:10]
    if not code_text or not date_text:
        return None
    if any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for char in code_text):
        return None
    if len(date_text) != 10 or date_text[4] != "-" or date_text[7] != "-":
        return None
    return code_text, date_text


def _current_daily_keys(client, keys: list[tuple[str, str]]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for start in range(0, len(keys), 500):
        chunk = keys[start:start + 500]
        tuples = ",".join(f"('{code}', toDate('{day}'))" for code, day in chunk)
        if not tuples:
            continue
        result = client.query(
            f"SELECT code, trade_date FROM stock.kline_daily WHERE (code, trade_date) IN ({tuples})"
        )
        found.update((str(code), str(day)[:10]) for code, day in result.result_rows)
    return found


def _carry_rows(client, previous_run_id: str) -> tuple[list[list[object]], Counter[str]]:
    result = client.query(
        f"SELECT {', '.join(COLUMNS)} FROM {TABLE} WHERE run_id=%(run_id)s AND issue_type != %(issue_type)s ORDER BY trade_date, code, issue_type",
        parameters={"run_id": previous_run_id, "issue_type": NEW_ISSUE_TYPE},
    )
    rows = [list(row) for row in result.result_rows]
    project_keys = [key for key in (_safe_key_tuple(row[4], row[5]) for row in rows if str(row[2]) == "project_daily_missing_for_5m") if key]
    current_daily = _current_daily_keys(client, project_keys)
    kept: list[list[object]] = []
    for row in rows:
        if str(row[2]) == "project_daily_missing_for_5m" and _safe_key_tuple(row[4], row[5]) in current_daily:
            continue
        row[0] = "__NEW_RUN__"
        kept.append(row)
    return kept, Counter(str(row[2]) for row in kept)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="purchased_history_review_20260916_snapshot_recon_r2")
    parser.add_argument("--batch-size", type=int, default=20_000)
    parser.add_argument("--year", action="append", type=int, dest="years")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")
    years = sorted(set(args.years or list(range(2000, 2006))))
    if any(year < 1990 or year > 2100 for year in years):
        raise SystemExit("invalid year")
    if not MANIFEST_PATH.exists():
        raise SystemExit(f"manifest missing: {MANIFEST_PATH}")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    previous_run_id = str(manifest.get("run_id") or "")
    if not previous_run_id:
        raise SystemExit("manifest has no current run_id")

    client = clickhouse_client()
    if args.write:
        _ensure_recon_columns(client)
    price_column_expression = _recon_price_column_expression(client)
    previous_count, previous_unique = client.query(
        f"SELECT count(), uniqExact(review_key) FROM {TABLE} WHERE run_id=%(run_id)s",
        parameters={"run_id": previous_run_id},
    ).result_rows[0]
    if int(previous_count) != int(previous_unique):
        raise RuntimeError(f"previous queue is not unique: rows={previous_count}, keys={previous_unique}")
    carry_rows, carry_counts = _carry_rows(client, previous_run_id)

    review_rows = client.query(
        f"""
        SELECT batch_id, code, trade_date, security_type, minute_rows, unique_datetime_rows,
               daily_snapshot_rows, daily_quality_status, price_status, volume_status,
               amount_status, overall_status, max_price_abs_diff,
               {price_column_expression},
               volume_relative_diff, amount_relative_diff, reason
        FROM (
            SELECT batch_id, code, trade_date, security_type, minute_rows, unique_datetime_rows,
                   daily_snapshot_rows, daily_quality_status, price_status, volume_status,
                   amount_status, overall_status, max_price_abs_diff,
                   {price_column_expression},
                   volume_relative_diff, amount_relative_diff, reason, checked_at
            FROM {RECON_TABLE}
            WHERE trade_date >= toDate(%(start)s)
              AND trade_date < toDate(%(end)s)
            ORDER BY code, trade_date, checked_at DESC, batch_id DESC
            LIMIT 1 BY code, trade_date
        )
        WHERE overall_status='review'
        ORDER BY trade_date, code, batch_id
        """,
        parameters={"start": f"{min(years)}-01-01", "end": f"{max(years) + 1}-01-01"},
    ).result_rows
    source_keys = [_review_key(str(row[1]), row[2]) for row in review_rows]
    if len(source_keys) != len(set(source_keys)):
        raise RuntimeError("snapshot review source contains duplicate review keys")
    counts = Counter(str(row[11]) for row in review_rows)
    print(json.dumps({
        "previous_run_id": previous_run_id,
        "years": years,
        "previous_rows": int(previous_count),
        "carry_rows": len(carry_rows),
        "carry_issue_counts": dict(carry_counts),
        "snapshot_review_rows": len(review_rows),
        "snapshot_review_status_counts": dict(counts),
        "write": args.write,
    }, ensure_ascii=False))
    if not args.write:
        return 0

    _ensure_table(client)

    existing = int(client.query(
        f"SELECT count() FROM {TABLE} WHERE run_id=%(run_id)s",
        parameters={"run_id": args.run_id},
    ).result_rows[0][0])
    if existing:
        raise RuntimeError(f"target run already exists: {args.run_id}, rows={existing}")

    observed_at = datetime.now(BUSINESS_TZ).replace(microsecond=0)
    for start in range(0, len(carry_rows), args.batch_size):
        chunk = carry_rows[start:start + args.batch_size]
        for row in chunk:
            row[0] = args.run_id
        client.insert(TABLE, chunk, column_names=COLUMNS)
    rows = [_row(args.run_id, source_row, observed_at) for source_row in review_rows]
    _insert(client, rows, args.batch_size)

    expected_total = len(carry_rows) + len(rows)
    validation = client.query(
        f"SELECT count(), uniqExact(review_key), countIf(issue_type=%(issue_type)s) FROM {TABLE} WHERE run_id=%(run_id)s",
        parameters={"run_id": args.run_id, "issue_type": NEW_ISSUE_TYPE},
    ).result_rows[0]
    if tuple(map(int, validation)) != (expected_total, expected_total, len(rows)):
        raise RuntimeError(f"queue projection validation failed: {validation}, expected={expected_total}")

    new_manifest = {
        **manifest,
        "run_id": args.run_id,
        "generated_at": observed_at.isoformat(),
        "previous_run_id": previous_run_id,
        "counts": {
            **dict(carry_counts),
            NEW_ISSUE_TYPE: len(rows),
        },
        "rows": expected_total,
        "unique_review_keys": expected_total,
        "snapshot_recon_table": RECON_TABLE,
        "snapshot_recon_years": years,
        "formal_table_mutation": False,
    }
    temporary = MANIFEST_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(new_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(MANIFEST_PATH)
    print(json.dumps({"manifest": str(MANIFEST_PATH), **new_manifest}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

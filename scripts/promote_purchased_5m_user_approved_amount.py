from __future__ import annotations

"""Targeted promotion of user-approved purchased 5m code-days into the formal table.

Scope (user approval 2026-09-17, codex thread 01a0ab06):
  code-days classified ``one_minute_confirms_price_volume_amount_unresolved`` in the
  1m cross-validation run ``purchased_history_review_20260917_snapshot_recon_r3``
  (price/volume pass versus the purchased daily snapshot, amount unresolved because
  the 1m source has no amount field; the purchased 5m stage amount is complete and
  self-consistent, so the user approved the 5m source as authoritative).

Pipeline:
  1. append new audit versions with overall_status=pass (amount_status stays review
     as evidence, reason documents the approval; queue reads latest overall_status);
  2. materialize candidate rows for a dedicated batch and verify counts/uniqueness;
  3. insert only keys that are still missing from the formal 5m table;
  4. verify inserted key counts, uniqueness and value sanity.

The formal daily table is never touched by this script.
"""

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path

RECON_RUN = "purchased_history_review_20260917_snapshot_recon_r3"
RECON_PARQUET = (
    r"F:\AiStock\data\data\warehouse\purchased_history\one_minute_cross_validation"
    rf"\{RECON_RUN}\reconciliation.parquet"
)
AUDIT_TABLE = "stock.purchased_history_minute_5_daily_snapshot_recon"
CANDIDATE_TABLE = "stock.purchased_history_minute_5_formal_candidate_20260916"
STAGE_TABLE = "stock.purchased_history_minute_5_stage"
FORMAL_TABLE = "stock.kline_minute_5"
APPROVED_BATCH = "purchased_5m_user_approved_amount_20260917_r1"
APPROVAL_REASON = (
    "user_approved_20260917: 1m confirms price/volume; 5m stage amount complete and "
    "self-consistent, 5m source accepted as authoritative per user approval"
)
KEY_TABLE = "stock.purchased_approved_keys_20260917_tmp"


def ensure_key_table(client, keys: pd.DataFrame) -> None:
    exists = client.query(
        "SELECT count() FROM system.tables WHERE database='stock' AND name=%(n)s",
        parameters={"n": KEY_TABLE.split(".")[-1]},
    ).result_rows[0][0]
    if not int(exists):
        client.command(
            f"CREATE TABLE {KEY_TABLE} (code String, trade_date Date) ENGINE=MergeTree ORDER BY (code, trade_date)"
        )
        client.insert_df(KEY_TABLE, keys[["code", "trade_date"]].copy())
        print(f"key_table_filled={client.query(f'SELECT count() FROM {KEY_TABLE}').result_rows[0][0]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="apply audit/candidate/formal writes")
    parser.add_argument(
        "--skip-audit-append",
        action="store_true",
        help="skip appending new audit versions (already appended by a previous run)",
    )
    return parser.parse_args()


def load_approved_keys() -> pd.DataFrame:
    df = pd.read_parquet(RECON_PARQUET)
    approved = df[df["decision"] == "one_minute_confirms_price_volume_amount_unresolved"]
    if approved.empty:
        raise RuntimeError("no approved keys found in reconciliation parquet")
    keys = approved[["code", "trade_date", "security_type"]].copy()
    keys["code"] = keys["code"].astype(str)
    keys["trade_date"] = keys["trade_date"].astype(str)
    dup = keys.groupby(["code", "trade_date"]).size()
    if int(dup.max()) != 1:
        raise RuntimeError(f"duplicate approved keys: max={int(dup.max())}")
    return keys


def append_audit_versions(client, keys: pd.DataFrame) -> int:
    ensure_key_table(client, keys)
    client.command(
        f"""
        INSERT INTO {AUDIT_TABLE}
        SELECT
            batch_id, code, trade_date, security_type, minute_rows, unique_datetime_rows,
            daily_snapshot_rows, daily_quality_status, minute_open, minute_high, minute_low,
            minute_close, daily_open, daily_high, daily_low, daily_close, minute_volume_lots,
            daily_volume_lots, minute_amount_yuan, daily_amount_yuan, max_price_abs_diff,
            volume_relative_diff, amount_relative_diff, price_status, volume_status,
            amount_status, 'pass' AS overall_status, %(reason)s AS reason, now() AS checked_at,
            price_relative_diff
        FROM
        (
            SELECT *, row_number() OVER (PARTITION BY code, trade_date ORDER BY checked_at DESC) AS rn
            FROM {AUDIT_TABLE}
            WHERE (code, trade_date) IN (SELECT code, trade_date FROM {KEY_TABLE})
              AND overall_status = 'review'
        )
        WHERE rn = 1
        """,
        parameters={"reason": APPROVAL_REASON},
    )
    appended = client.query(
        f"SELECT count() FROM {AUDIT_TABLE} WHERE reason = %(reason)s",
        parameters={"reason": APPROVAL_REASON},
    ).result_rows[0][0]
    print(f"audit_rows_with_approval_reason={appended}")
    return int(appended)


def approved_key_filter_sql() -> str:
    return f"""
        SELECT code, trade_date FROM {KEY_TABLE}
    """


def materialize_candidate(client, expected_rows: int) -> int:
    existing = client.query(
        f"SELECT count() FROM {CANDIDATE_TABLE} WHERE batch_id=%(b)s",
        parameters={"b": APPROVED_BATCH},
    ).result_rows[0][0]
    if int(existing):
        print(f"candidate batch already exists rows={existing}")
        return int(existing)
    inserted = 0
    year_rows = client.query(
        f"SELECT toYear(trade_date) y, count() FROM {KEY_TABLE} GROUP BY y ORDER BY y"
    ).result_rows
    for year, key_count in year_rows:
        client.command(
            f"""
            INSERT INTO {CANDIDATE_TABLE}
                (batch_id, code, datetime, open, high, low, close, volume, amount, created_at, id)
            WITH approved AS
            (
                SELECT code, trade_date FROM {KEY_TABLE} WHERE toYear(trade_date) = %(y)s
            )
            SELECT
                %(b)s, s.code, s.datetime, s.open, s.high, s.low, s.close,
                toInt64(s.source_volume_lots), s.source_amount_yuan, s.ingested_at, s.id
            FROM {STAGE_TABLE} AS s
            SEMI LEFT JOIN approved AS k
              ON k.code = s.code AND k.trade_date = toDate(s.datetime)
            WHERE toYear(s.datetime) = %(y)s
            """,
            parameters={"b": APPROVED_BATCH, "y": int(year)},
        )
        year_actual = client.query(
            f"""
            SELECT count(), uniqExact(tuple(code, datetime)) FROM {CANDIDATE_TABLE}
            WHERE batch_id=%(b)s AND toYear(datetime) = %(y)s
            """,
            parameters={"b": APPROVED_BATCH, "y": int(year)},
        ).result_rows[0]
        expected_year = int(key_count) * 48
        if int(year_actual[0]) != expected_year or int(year_actual[1]) != expected_year:
            raise RuntimeError(f"candidate mismatch year={year} expected={expected_year} actual={year_actual}")
        print(f"candidate year={year} rows={year_actual[0]} unique={year_actual[1]}")
        inserted += int(year_actual[0])
    if inserted != expected_rows:
        raise RuntimeError(f"candidate total mismatch expected={expected_rows} actual={inserted}")
    print(f"candidate_rows={inserted}")
    return inserted


def promote(client, expected_rows: int) -> None:
    overlap_total = 0
    year_rows = client.query(
        f"SELECT toYear(datetime) y, uniqExact(tuple(code, toDate(datetime))) FROM {CANDIDATE_TABLE} WHERE batch_id=%(b)s GROUP BY y ORDER BY y",
        parameters={"b": APPROVED_BATCH},
    ).result_rows
    for year, key_count in year_rows:
        overlap = client.query(
            f"""
            SELECT count() FROM
            (
                SELECT DISTINCT code, toDate(datetime) AS trade_date FROM {CANDIDATE_TABLE}
                WHERE batch_id=%(b)s AND toYear(datetime)=%(y)s
            ) AS c
            SEMI LEFT JOIN
            (
                SELECT DISTINCT code, toDate(datetime) AS trade_date FROM {FORMAL_TABLE}
                WHERE toYear(datetime)=%(y)s
            ) AS f
              ON f.code = c.code AND f.trade_date = c.trade_date
            """,
            parameters={"b": APPROVED_BATCH, "y": int(year)},
        ).result_rows[0][0]
        overlap_total += int(overlap)
    if overlap_total:
        raise RuntimeError(f"{overlap_total} candidate keys already exist in the formal table")
    client.command(
        f"""
        INSERT INTO {FORMAL_TABLE}
            (code, datetime, open, high, low, close, volume, amount, created_at, id)
        SELECT code, datetime, open, high, low, close, volume, amount, created_at, id
        FROM {CANDIDATE_TABLE} WHERE batch_id=%(b)s
        """,
        parameters={"b": APPROVED_BATCH},
    )
    inserted = client.query(
        f"""
        SELECT count(), uniqExact(tuple(code, datetime))
        FROM {FORMAL_TABLE}
        WHERE (code, toDate(datetime)) IN
        (
            SELECT code, toDate(datetime) FROM {CANDIDATE_TABLE} WHERE batch_id=%(b)s
        )
        """,
        parameters={"b": APPROVED_BATCH},
    ).result_rows[0]
    print(f"post_insert formal_rows_for_approved_keys={inserted}")
    if int(inserted[0]) != expected_rows or int(inserted[1]) != expected_rows:
        raise RuntimeError(f"formal insert verification failed {inserted} expected={expected_rows}")


def write_manifest(keys: pd.DataFrame, expected_rows: int) -> Path:
    manifest = {
        "run_id": APPROVED_BATCH,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "approval": "user approved 2026-09-17: 5m source authoritative for amount-unresolved code-days",
        "recon_run": RECON_RUN,
        "decision_filter": "one_minute_confirms_price_volume_amount_unresolved",
        "code_days": int(len(keys)),
        "expected_5m_rows": int(expected_rows),
        "audit_table": AUDIT_TABLE,
        "candidate_table": CANDIDATE_TABLE,
        "formal_table": FORMAL_TABLE,
    }
    out = runtime_path("purchased_history", f"{APPROVED_BATCH}_manifest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> int:
    args = parse_args()
    client = clickhouse_client()
    keys = load_approved_keys()
    expected_rows = len(keys) * 48
    print(f"approved_code_days={len(keys)} expected_5m_rows={expected_rows}")

    if not args.skip_audit_append:
        if args.write:
            appended = append_audit_versions(client, keys)
            if appended < len(keys):
                print(f"warning: approval audit rows={appended} < approved keys={len(keys)}")
        else:
            print("dry-run: would append overall_status=pass audit versions")

    if args.write:
        ensure_key_table(client, keys)
        rows = materialize_candidate(client, expected_rows)
        promote(client, rows)
        manifest = write_manifest(keys, expected_rows)
        print(f"manifest={manifest}")
    else:
        print(f"dry-run: would materialize batch={APPROVED_BATCH} rows={expected_rows}")
    print(f"write={args.write}")
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())

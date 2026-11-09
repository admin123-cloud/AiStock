"""Execute the guarded ``stock.kline_daily`` schema migration.

The plan, source-copy compatibility gates and the candidate DDL all come from
``scripts.dry_run_index_schema_migration``; this module only adds guarded
execution:

1. acquire the DDL write lock ``stock.write_lock_kline_daily_schema_migration``;
2. preflight: reuse the dry-run source-copy compatibility checks plus a live
   schema snapshot; any blocker aborts before a single write;
3. DROP + CREATE ``stock.kline_daily__migration_candidate`` from the planner
   candidate DDL;
4. copy per month with ``created_at`` NULLs normalised to the epoch, verifying
   row counts and an aggregate checksum for every month right after the batch;
5. verify totals (raw rows, ``uniqExact(code,trade_date)``, min/max trade_date);
6. swap atomically with one ``RENAME TABLE``;
7. verify the promoted table (engine contract, raw+FINAL counts, sampled key
   readback) and reverse the rename if verification fails;
8. release the lock.

Every step is journaled as JSONL with atomic segment writes.  ``--plan`` (the
default) prints the plan and issues no writes at all.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dry_run_index_schema_migration import (
    FORMAL_TABLE,
    READONLY_SETTINGS,
    _candidate_schema_proposals,
    _dict_rows,
    _read_live_schema,
    _source_copy_compatibility,
)
from utils.clickhouse_schema import (
    KLINE_DAILY_ENGINE_FULL_PREFIX,
    KLINE_DAILY_PARTITION_KEY,
    KLINE_DAILY_REQUIRED_COLUMN_TYPES,
    KLINE_DAILY_SORTING_KEY,
)
from utils.db_write_lock import DbWriteLock

DATABASE = "stock"
SOURCE_TABLE = f"{DATABASE}.{FORMAL_TABLE}"
CANDIDATE_TABLE = f"{DATABASE}.kline_daily__migration_candidate"
PRE_MIGRATION_TABLE = f"{DATABASE}.kline_daily__pre_migration_20261101"
MIGRATION_SCOPE = "kline_daily_schema_migration"
LOCK_TABLE = f"{DATABASE}.write_lock_{MIGRATION_SCOPE}"
MIGRATION_PURPOSE = "kline_daily schema migration: monthly partitions + ReplacingMergeTree(created_at)"
DEFAULT_JOURNAL_PATH = Path("F:/AiStock/data/data/runtime/schema_migration/kline_daily_20261101/journal.jsonl")

EPOCH_DATETIME_SQL = "toDateTime('1970-01-01 00:00:00')"
CHECKSUM_EXPRESSION = (
    "cityHash64(code, trade_date, open, high, low, close, volume, amount, "
    f"ifNull(created_at, {EPOCH_DATETIME_SQL}))"
)
COPY_COLUMNS = tuple(name for name, _ in KLINE_DAILY_REQUIRED_COLUMN_TYPES)

SWAP_SQL = f"RENAME TABLE {SOURCE_TABLE} TO {PRE_MIGRATION_TABLE}, {CANDIDATE_TABLE} TO {SOURCE_TABLE}"
REVERSE_SWAP_SQL = f"RENAME TABLE {SOURCE_TABLE} TO {CANDIDATE_TABLE}, {PRE_MIGRATION_TABLE} TO {SOURCE_TABLE}"

# Live schema problems that block the migration.  The contract-incompatibility
# markers (engine/partition/version/sorting) are the migration's reason to
# exist and must NOT block; only missing/unreadable source state does.
_PREFLIGHT_SCHEMA_BLOCKERS = frozenset(
    {
        "kline_daily:table_missing",
        "kline_daily:aggregate_read_failed",
        "kline_daily:monthly_partition_read_failed",
    }
)

_SAMPLE_SQL_TEMPLATE = (
    "SELECT code, trade_date, open, close, volume, "
    f"ifNull(created_at, {EPOCH_DATETIME_SQL}) AS created_at "
    "FROM {table} ORDER BY code, trade_date LIMIT 3"
)

_PLAN_STEPS = (
    f"acquire write lock {LOCK_TABLE} (scope {MIGRATION_SCOPE})",
    "preflight: reuse dry-run source-copy compatibility checks + live schema snapshot (SELECT only)",
    f"DROP TABLE IF EXISTS + CREATE {CANDIDATE_TABLE} from the planner candidate DDL",
    "per month: INSERT SELECT with created_at NULLs normalised to the epoch, then verify rows + aggregate checksum against the source",
    "verify totals: raw rows, uniqExact(code,trade_date), min/max trade_date on source and candidate",
    f"atomic swap: {SWAP_SQL}",
    "post-swap verification: engine_full contract, raw+FINAL counts, sampled key readback; reverse RENAME on failure",
    f"release write lock {LOCK_TABLE}",
)


class MigrationError(RuntimeError):
    """Base class for migration failures."""


class PreflightBlocked(MigrationError):
    def __init__(self, blockers):
        self.blockers = [str(item) for item in blockers]
        super().__init__("preflight blocked: " + ", ".join(self.blockers))


class VerificationFailed(MigrationError):
    """Raised when a copy or post-swap verification gate fails."""


class JsonlJournal:
    """Append-only JSONL journal; every record is an atomic segment write.

    The record is fully serialized first, written to a same-directory segment
    file, fsynced, then appended to the journal with ``O_APPEND`` and fsynced
    again, so the journal never contains a half-written record and a
    serialization failure leaves previous records untouched.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence = 0

    def write(self, event: dict[str, Any]) -> dict[str, Any]:
        self._sequence += 1
        record = {"seq": self._sequence, "ts": datetime.now().isoformat(timespec="seconds"), **dict(event)}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        segment = self.path.with_name(f"{self.path.name}.seg.{os.getpid()}.{self._sequence}.tmp")
        try:
            with segment.open("w", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            try:
                os.write(descriptor, line.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if segment.exists():
                segment.unlink()
        return record


def _real_client():
    from utils.market_warehouse import clickhouse_client

    return clickhouse_client()


def run_preflight(client: Any) -> dict[str, Any]:
    """Reuse the dry-run gates read-only; return blockers, months and the DDL."""
    schema = _read_live_schema(client)
    live_columns = (schema.get("columns_by_table") or {}).get(FORMAL_TABLE, [])
    aggregate = (schema.get("aggregate") or [{}])[0]
    compatibility = _source_copy_compatibility(live_columns, aggregate)
    validation, proposals = _candidate_schema_proposals()
    candidate_status = (validation.get("tables") or {}).get(FORMAL_TABLE) or {}
    candidate_ddl = proposals.get(FORMAL_TABLE)
    months = [
        int(row["month"])
        for row in _dict_rows(
            client.query(
                f"SELECT DISTINCT toYYYYMM(trade_date) AS month FROM {SOURCE_TABLE} ORDER BY month",
                settings=READONLY_SETTINGS,
            )
        )
    ]

    blockers: list[str] = []
    blockers.extend(str(item) for item in compatibility.get("blockers") or [])
    blockers.extend(
        problem for problem in (schema.get("problems") or []) if problem in _PREFLIGHT_SCHEMA_BLOCKERS
    )
    if not candidate_ddl:
        blockers.append("candidate_ddl_missing")
    if candidate_status.get("status") != "ready":
        blockers.append("candidate_contract_not_ready")
    if not months:
        blockers.append("source_has_no_month_partitions")

    # The copy regenerates id deterministically as cityHash64(code, trade_date),
    # so non-unique source ids (legacy id=0 sentinel rows) are expected and safe;
    # candidate-side id uniqueness is verified after the copy instead.
    id_regeneration: Optional[dict[str, Any]] = None
    if "source_rows_exceed_unique_ids" in blockers:
        blockers.remove("source_rows_exceed_unique_ids")
        id_regeneration = {
            "strategy": "cityHash64(code, trade_date)",
            "rows_without_unique_source_id": max(
                0, int(aggregate.get("rows") or 0) - int(aggregate.get("unique_ids") or 0)
            ),
        }

    return {
        "status": "blocked" if blockers else "ready",
        "blockers": list(dict.fromkeys(blockers)),
        "id_regeneration": id_regeneration,
        "months": months,
        "candidate_ddl": candidate_ddl,
        "metadata_snapshot_sha256": schema.get("metadata_snapshot_sha256"),
        "source_aggregate": {
            key: aggregate.get(key)
            for key in (
                "rows",
                "unique_keys",
                "unique_ids",
                "null_created_at",
                "nonnull_created_at",
                "first_trade_date",
                "last_trade_date",
            )
        },
        "compatibility_status": compatibility.get("status"),
    }


def build_plan_document(client: Any) -> dict[str, Any]:
    """Read-only plan for ``--plan`` mode; issues no writes."""
    preflight = run_preflight(client)
    return {
        "mode": "plan",
        "readonly": True,
        "status": preflight["status"],
        "blockers": preflight["blockers"],
        "source_table": SOURCE_TABLE,
        "candidate_table": CANDIDATE_TABLE,
        "pre_migration_table": PRE_MIGRATION_TABLE,
        "lock_table": LOCK_TABLE,
        "candidate_ddl": preflight["candidate_ddl"],
        "months": preflight["months"],
        "expected_rows": preflight["source_aggregate"].get("rows"),
        "null_created_at_in_source": preflight["source_aggregate"].get("null_created_at"),
        "created_at_normalization": f"ifNull(created_at, {EPOCH_DATETIME_SQL})",
        "checksum_expression": CHECKSUM_EXPRESSION,
        "steps": list(_PLAN_STEPS),
        "commands_issued": {"ddl": 0, "dml": 0, "rename": 0, "lock": 0},
    }


def _insert_month_sql(month: int) -> str:
    columns = ", ".join(COPY_COLUMNS)
    projection = ", ".join(
        f"ifNull(created_at, {EPOCH_DATETIME_SQL}) AS created_at"
        if name == "created_at"
        else (f"cityHash64(code, trade_date) AS id" if name == "id" else name)
        for name in COPY_COLUMNS
    )
    return (
        f"INSERT INTO {CANDIDATE_TABLE} ({columns}) "
        f"SELECT {projection} FROM {SOURCE_TABLE} WHERE toYYYYMM(trade_date) = {month}"
    )


def _month_fingerprint(client: Any, table: str, month: int) -> dict[str, Any]:
    sql = (
        f"SELECT count() AS rows, sum({CHECKSUM_EXPRESSION}) AS checksum "
        f"FROM {table} WHERE toYYYYMM(trade_date) = {month}"
    )
    records = _dict_rows(client.query(sql, settings=READONLY_SETTINGS))
    row = records[0] if records else {}
    return {"rows": int(row.get("rows") or 0), "checksum": row.get("checksum")}


def _table_totals(client: Any, table: str) -> dict[str, Any]:
    sql = (
        f"SELECT count() AS rows, uniqExact((code,trade_date)) AS unique_keys, "
        f"min(trade_date) AS min_trade_date, max(trade_date) AS max_trade_date FROM {table}"
    )
    records = _dict_rows(client.query(sql, settings=READONLY_SETTINGS))
    row = records[0] if records else {}
    return {
        "rows": int(row.get("rows") or 0),
        "unique_keys": row.get("unique_keys"),
        "min_trade_date": row.get("min_trade_date"),
        "max_trade_date": row.get("max_trade_date"),
    }


def _candidate_id_uniqueness(client: Any) -> dict[str, int]:
    records = _dict_rows(
        client.query(
            f"SELECT count() AS rows, uniqExact(id) AS candidate_unique_ids FROM {CANDIDATE_TABLE}",
            settings=READONLY_SETTINGS,
        )
    )
    row = records[0] if records else {}
    return {"rows": int(row.get("rows") or 0), "unique_ids": int(row.get("candidate_unique_ids") or 0)}


def _sample_rows(client: Any, table: str) -> list[tuple]:
    result = client.query(_SAMPLE_SQL_TEMPLATE.format(table=table), settings=READONLY_SETTINGS)
    return [tuple(row) for row in getattr(result, "result_rows", ()) or ()]


def _count_rows(client: Any, table: str, *, final: bool) -> int:
    alias = "final_rows" if final else "raw_rows"
    suffix = " FINAL" if final else ""
    records = _dict_rows(
        client.query(f"SELECT count() AS {alias} FROM {table}{suffix}", settings=READONLY_SETTINGS)
    )
    return int((records[0] if records else {}).get(alias) or 0)


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _engine_problems(client: Any) -> list[str]:
    records = _dict_rows(
        client.query(
            "SELECT name, engine_full FROM system.tables "
            f"WHERE database='{DATABASE}' AND name='{FORMAL_TABLE}'",
            settings=READONLY_SETTINGS,
        )
    )
    engine_full = str((records[0] if records else {}).get("engine_full") or "")
    compact = _compact(engine_full)
    problems: list[str] = []
    if not compact:
        problems.append("engine_full_missing")
    if not compact.startswith(_compact(KLINE_DAILY_ENGINE_FULL_PREFIX)):
        problems.append("engine_full_prefix_mismatch")
    if _compact(KLINE_DAILY_PARTITION_KEY) not in compact:
        problems.append("partition_key_mismatch")
    if _compact(KLINE_DAILY_SORTING_KEY) not in compact:
        problems.append("sorting_key_mismatch")
    return problems


def run_migration(
    client: Any,
    *,
    journal_path: Path | str,
    owner: str,
    keep_candidate: bool = False,
    force_break_stale_lock: bool = False,
) -> dict[str, Any]:
    """Run the guarded migration; raise (and clean up) on any failure."""
    journal = JsonlJournal(journal_path)
    lock = DbWriteLock(client, journal=journal.write)
    journal.write(
        {
            "event": "migration_started",
            "source_table": SOURCE_TABLE,
            "candidate_table": CANDIDATE_TABLE,
            "pre_migration_table": PRE_MIGRATION_TABLE,
            "lock_table": LOCK_TABLE,
            "owner": owner,
            "keep_candidate": keep_candidate,
        }
    )
    acquired = False
    swapped = False
    candidate_created = False
    status = "failed"
    try:
        lock.acquire(MIGRATION_SCOPE, owner, MIGRATION_PURPOSE, force=force_break_stale_lock)
        acquired = True
        journal.write({"event": "lock_acquired", "scope": MIGRATION_SCOPE, "owner": owner})

        preflight = run_preflight(client)
        journal.write(
            {
                "event": "preflight_completed",
                **{
                    key: preflight[key]
                    for key in ("status", "blockers", "id_regeneration", "months", "metadata_snapshot_sha256", "source_aggregate")
                },
            }
        )
        if preflight["status"] != "ready":
            raise PreflightBlocked(preflight["blockers"])

        client.command(f"DROP TABLE IF EXISTS {CANDIDATE_TABLE}")
        client.command(preflight["candidate_ddl"])
        candidate_created = True
        journal.write({"event": "candidate_created", "table": CANDIDATE_TABLE, "ddl": preflight["candidate_ddl"]})

        for month in preflight["months"]:
            client.command(_insert_month_sql(month))
            journal.write({"event": "month_batch_inserted", "month": month, "table": CANDIDATE_TABLE})
            source_print = _month_fingerprint(client, SOURCE_TABLE, month)
            candidate_print = _month_fingerprint(client, CANDIDATE_TABLE, month)
            if source_print != candidate_print:
                raise VerificationFailed(
                    f"month_batch_verification_mismatch:{month}: "
                    f"source {source_print} != candidate {candidate_print}"
                )
            journal.write(
                {
                    "event": "month_batch_verified",
                    "month": month,
                    "rows": source_print["rows"],
                    "checksum": source_print["checksum"],
                }
            )

        source_totals = _table_totals(client, SOURCE_TABLE)
        candidate_totals = _table_totals(client, CANDIDATE_TABLE)
        if source_totals != candidate_totals:
            raise VerificationFailed(
                f"totals_verification_mismatch: source {source_totals} != candidate {candidate_totals}"
            )
        id_uniqueness = _candidate_id_uniqueness(client)
        journal.write({"event": "candidate_id_uniqueness_verified", **id_uniqueness})
        if id_uniqueness["unique_ids"] != id_uniqueness["rows"]:
            raise VerificationFailed(f"candidate_id_uniqueness_mismatch: {id_uniqueness}")
        sample = _sample_rows(client, SOURCE_TABLE)
        journal.write(
            {
                "event": "totals_verified",
                "source": source_totals,
                "candidate": candidate_totals,
                "sampled_rows": len(sample),
            }
        )

        client.command(SWAP_SQL)
        swapped = True
        journal.write({"event": "tables_swapped", "swap_sql": SWAP_SQL})

        problems = _engine_problems(client)
        raw_rows = _count_rows(client, SOURCE_TABLE, final=False)
        final_rows = _count_rows(client, SOURCE_TABLE, final=True)
        if raw_rows != source_totals["rows"] or final_rows != raw_rows:
            problems.append(
                f"post_swap_row_count_mismatch: raw={raw_rows}, final={final_rows}, expected={source_totals['rows']}"
            )
        if _sample_rows(client, SOURCE_TABLE) != sample:
            problems.append("post_swap_sample_readback_mismatch")
        if problems:
            journal.write({"event": "post_swap_verification_failed", "problems": problems})
            raise VerificationFailed("post_swap_verification_failed: " + ", ".join(problems))
        journal.write(
            {"event": "post_swap_verified", "engine_full_checked": True, "raw_rows": raw_rows, "final_rows": final_rows}
        )

        status = "completed"
        result = {
            "status": status,
            "source_table": SOURCE_TABLE,
            "pre_migration_table": PRE_MIGRATION_TABLE,
            "months": preflight["months"],
            "rows": source_totals["rows"],
            "journal_path": str(journal.path),
        }
        journal.write({"event": "migration_completed", **result})
        return result
    except Exception as exc:
        journal.write(
            {
                "event": "migration_failed",
                "error": f"{type(exc).__name__}: {exc}",
                "swapped": swapped,
                "keep_candidate": keep_candidate,
            }
        )
        if swapped:
            # post-swap failure: put the untouched pre-migration table back
            try:
                client.command(REVERSE_SWAP_SQL)
                swapped = False
                journal.write({"event": "reverse_rename_completed", "reverse_swap_sql": REVERSE_SWAP_SQL})
            except Exception as reverse_exc:
                journal.write(
                    {"event": "reverse_rename_failed", "error": f"{type(reverse_exc).__name__}: {reverse_exc}"}
                )
        elif candidate_created and not keep_candidate:
            # pre-swap failure: the old table was never touched; drop the candidate
            try:
                client.command(f"DROP TABLE IF EXISTS {CANDIDATE_TABLE}")
                journal.write({"event": "candidate_dropped_on_failure", "table": CANDIDATE_TABLE})
            except Exception as drop_exc:
                journal.write({"event": "candidate_drop_failed", "error": f"{type(drop_exc).__name__}: {drop_exc}"})
        raise
    finally:
        if acquired:
            try:
                lock.release(MIGRATION_SCOPE, owner)
                journal.write({"event": "lock_released", "scope": MIGRATION_SCOPE, "owner": owner})
            except Exception as release_exc:
                journal.write(
                    {"event": "lock_release_failed", "error": f"{type(release_exc).__name__}: {release_exc}"}
                )
        journal.write({"event": "migration_finished", "status": status})


def main(argv: Optional[list[str]] = None, client: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="print the migration plan without issuing any writes (default)")
    parser.add_argument("--execute", action="store_true", help="execute the guarded migration")
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL_PATH)
    parser.add_argument("--owner", default=None, help="lock owner id (default host:pid)")
    parser.add_argument("--keep-candidate", action="store_true", help="keep the candidate table when a pre-swap step fails")
    parser.add_argument("--force-break-stale-lock", action="store_true", help="break a stale write lock (journaled)")
    args = parser.parse_args(argv)
    if args.plan and args.execute:
        parser.error("--plan and --execute are mutually exclusive")

    if client is None:
        client = _real_client()

    if not args.execute:
        plan = build_plan_document(client)
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        return 0 if plan["status"] == "ready" else 1

    owner = args.owner or f"{socket.gethostname()}:{os.getpid()}"
    try:
        result = run_migration(
            client,
            journal_path=args.journal,
            owner=owner,
            keep_candidate=args.keep_candidate,
            force_break_stale_lock=args.force_break_stale_lock,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "journal": str(args.journal)},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

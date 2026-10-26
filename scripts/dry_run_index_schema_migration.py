"""Build a read-only ClickHouse schema migration and rollback plan.

The live ``stock.kline_daily`` table is currently unpartitioned and has no
ReplacingMergeTree version expression.  This script reads the live metadata,
parts and bounded aggregate baselines, validates the local registry/member
payloads, and writes an explicit migration proposal.  It never executes DDL,
DML, RENAME, OPTIMIZE, or mutations.
"""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.clickhouse_schema import MARKET_TABLE_DDL
from utils.market_warehouse import clickhouse_client


DATABASE = "stock"
FORMAL_TABLE = "kline_daily"
REGISTRY_TABLE = "daily_date_universe_registry"
MEMBER_TABLE = "daily_date_universe_member"
READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 240, "max_threads": 2}
EXPECTED_PAYLOAD_FIELDS = {
    "registry": {"code", "dataset_id", "date_set_sha256", "member_count", "completeness_state"},
    "member": {"code", "trade_date", "dataset_id", "date_set_sha256"},
    "accepted": {"code", "dates"},
}


def _quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dict_rows(result: Any) -> list[dict[str, Any]]:
    names = list(getattr(result, "column_names", ()) or ())
    return [dict(zip(names, row)) for row in getattr(result, "result_rows", ())]


def _normalise(value: Any) -> str:
    return re.sub(r"[\s`()]+", "", str(value or "")).lower()


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_jsonl(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open("r", encoding="utf-8")


def _file_meta(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists() and path.is_file(),
        "bytes": path.stat().st_size if path.exists() and path.is_file() else None,
        "sha256": _sha256_file(path),
    }


def _payload_stats(path: Path, kind: str) -> dict[str, Any]:
    meta = _file_meta(path)
    result: dict[str, Any] = {"kind": kind, **meta, "row_count": 0, "parse_errors": 0, "missing_fields": 0}
    if not meta["exists"]:
        result["status"] = "missing"
        return result
    expected = EXPECTED_PAYLOAD_FIELDS[kind]
    code_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    seen_codes: set[str] = set()
    first_key: tuple[str, str] | None = None
    last_key: tuple[str, str] | None = None
    adjacent_duplicates = 0
    order_violations = 0
    total_dates = 0
    with _open_jsonl(path) as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                result["parse_errors"] += 1
                continue
            result["row_count"] += 1
            if not expected.issubset(row):
                result["missing_fields"] += 1
            code = str(row.get("code") or "")
            code_counts[code] += 1
            seen_codes.add(code)
            if kind == "accepted":
                dates = row.get("dates") or []
                total_dates += len(dates) if isinstance(dates, list) else 0
                continue
            if kind == "registry":
                status_counts[str(row.get("completeness_state") or "")] += 1
                key = (code, "")
            else:
                key = (code, str(row.get("trade_date") or "")[:10])
            if first_key is None:
                first_key = key
            if last_key is not None:
                if key == last_key:
                    adjacent_duplicates += 1
                if key < last_key:
                    order_violations += 1
            last_key = key
    result.update(
        {
            "code_count": len(seen_codes),
            "code_count_by_row": dict(sorted(code_counts.items())) if kind == "registry" else None,
            "completeness_state_counts": dict(sorted(status_counts.items())) if kind == "registry" else None,
            "total_dates": total_dates if kind == "accepted" else None,
            "first_key": first_key,
            "last_key": last_key,
            "adjacent_duplicate_count": adjacent_duplicates if kind == "member" else None,
            "order_violation_count": order_violations if kind == "member" else None,
            "status": "ready" if result["parse_errors"] == 0 and result["missing_fields"] == 0 else "blocked",
        }
    )
    return result


def _ddl_for(name: str) -> str:
    for ddl in MARKET_TABLE_DDL:
        if re.search(rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(name)}\b", ddl, re.IGNORECASE):
            return " ".join(line.strip() for line in ddl.strip().splitlines())
    return ""


def _candidate_kline_ddl(live_columns: list[dict[str, Any]]) -> str:
    """Preserve the live column set while making version/partition explicit."""

    definitions: list[str] = []
    for row in live_columns:
        name = str(row.get("name") or "")
        type_name = str(row.get("type") or "")
        if not name or not type_name:
            continue
        if name == "created_at":
            # Nullable version expressions are not accepted for a deterministic
            # ReplacingMergeTree winner.  Null source values are normalized in
            # the copy SELECT and must be counted before promotion.
            definitions.append("`created_at` DateTime DEFAULT toDateTime('1970-01-01 00:00:00')")
        else:
            definitions.append(f"`{name}` {type_name}")
    return (
        "CREATE TABLE stock.kline_daily__migration_candidate ("
        + ", ".join(definitions)
        + ") ENGINE = ReplacingMergeTree(created_at) "
        "PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)"
    )


def _candidate_registry_ddl(table: str) -> str:
    ddl = _ddl_for(table)
    return re.sub(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\b",
        f"CREATE TABLE stock.{table}__migration_candidate",
        ddl,
        count=1,
        flags=re.IGNORECASE,
    )


def _read_live_schema(client: Any) -> dict[str, Any]:
    select_query_count = 0
    names = (FORMAL_TABLE, REGISTRY_TABLE, MEMBER_TABLE)
    name_sql = ",".join(_quote(name) for name in names)
    select_query_count += 1
    tables = _dict_rows(
        client.query(
            "SELECT database,name,engine,engine_full,partition_key,sorting_key,primary_key,create_table_query "
            f"FROM system.tables WHERE database={_quote(DATABASE)} AND name IN ({name_sql}) ORDER BY name",
            settings=READONLY_SETTINGS,
        )
    )
    select_query_count += 1
    columns = _dict_rows(
        client.query(
            "SELECT database,table,name,type,default_kind,default_expression,is_in_partition_key,is_in_sorting_key,is_in_primary_key "
            f"FROM system.columns WHERE database={_quote(DATABASE)} AND table IN ({name_sql}) ORDER BY table,position",
            settings=READONLY_SETTINGS,
        )
    )
    select_query_count += 1
    parts = _dict_rows(
        client.query(
            "SELECT database,table,partition,count() AS active_parts,sum(rows) AS rows,sum(bytes_on_disk) AS bytes,"
            "min(min_date) AS min_date,max(max_date) AS max_date "
            f"FROM system.parts WHERE database={_quote(DATABASE)} AND table={_quote(FORMAL_TABLE)} AND active "
            "GROUP BY database,table,partition ORDER BY partition",
            settings=READONLY_SETTINGS,
        )
    )
    select_query_count += 1
    aliases = _dict_rows(
        client.query(
            "SELECT database,name,engine,partition_key,sorting_key FROM system.tables "
            "WHERE lower(name) LIKE '%daily%date%universe%' OR lower(name) LIKE '%date_universe%' "
            "OR lower(name) LIKE '%registry%' OR lower(name) LIKE '%member%' ORDER BY database,name",
            settings=READONLY_SETTINGS,
        )
    )
    aggregate: list[dict[str, Any]] = []
    aggregate_error = None
    try:
        select_query_count += 1
        aggregate = _dict_rows(
            client.query(
                "SELECT count() AS rows,uniqExact((code,trade_date)) AS unique_keys,"
                "uniqExact(id) AS unique_ids,countIf(isNull(created_at)) AS null_created_at,"
                "countIf(isNotNull(created_at)) AS nonnull_created_at,min(trade_date) AS first_trade_date,"
                "max(trade_date) AS last_trade_date FROM stock.kline_daily",
                settings=READONLY_SETTINGS,
            )
        )
    except Exception as exc:
        aggregate_error = f"{type(exc).__name__}:{exc}"
    monthly: list[dict[str, Any]] = []
    monthly_error = None
    try:
        select_query_count += 1
        monthly = _dict_rows(
            client.query(
                "SELECT uniqExact(toYYYYMM(trade_date)) AS distinct_month_partitions FROM stock.kline_daily",
                settings=READONLY_SETTINGS,
            )
        )
    except Exception as exc:
        monthly_error = f"{type(exc).__name__}:{exc}"
    by_table: dict[str, dict[str, Any]] = {str(row.get("name")): row for row in tables}
    columns_by_table: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    for row in columns:
        columns_by_table.setdefault(str(row.get("table")), []).append(row)
    formal = by_table.get(FORMAL_TABLE) or {}
    engine_full = str(formal.get("engine_full") or "")
    version_match = re.search(r"ReplacingMergeTree\s*\(([^)]*)\)", engine_full, re.IGNORECASE)
    exact_present = all(name in by_table for name in names)
    schema_problems: list[str] = []
    if FORMAL_TABLE not in by_table:
        schema_problems.append("kline_daily:table_missing")
    else:
        if _normalise(formal.get("partition_key")) != _normalise("toYYYYMM(trade_date)"):
            schema_problems.append("kline_daily:partition_key_incompatible")
        if not version_match:
            schema_problems.append("kline_daily:replacing_version_expression_missing")
    for name in (REGISTRY_TABLE, MEMBER_TABLE):
        if name not in by_table:
            schema_problems.append(f"{name}:table_missing")
    metadata_payload = {"tables": tables, "columns": columns, "parts": parts, "aggregate": aggregate, "monthly": monthly}
    metadata_sha = hashlib.sha256(json.dumps(metadata_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return {
        "status": "ready" if not schema_problems and aggregate and monthly else "blocked",
        "problems": schema_problems + (["kline_daily:aggregate_read_failed"] if aggregate_error else []) + (["kline_daily:monthly_partition_read_failed"] if monthly_error else []),
        "tables": tables,
        "columns": columns,
        "columns_by_table": columns_by_table,
        "parts": parts,
        "database_table_search": {"rows": aliases, "exact_expected_tables_present": exact_present},
        "aggregate": aggregate,
        "aggregate_error": aggregate_error,
        "monthly_partition_count": monthly,
        "monthly_partition_error": monthly_error,
        "select_query_count": select_query_count,
        "metadata_snapshot_sha256": metadata_sha,
        "query_settings": READONLY_SETTINGS,
    }


def _payload_checks(*, registry_candidate: Path, member_candidate: Path, candidate_summary: Path, accepted_manifest: Path) -> dict[str, Any]:
    registry = _payload_stats(registry_candidate, "registry")
    member = _payload_stats(member_candidate, "member")
    accepted = _payload_stats(accepted_manifest, "accepted")
    summary_meta = _file_meta(candidate_summary)
    summary: dict[str, Any] = {"path": str(candidate_summary), "exists": summary_meta["exists"], "sha256": summary_meta["sha256"], "parse_error": None, "candidate_summary": {}}
    if summary_meta["exists"]:
        try:
            summary["candidate_summary"] = json.loads(candidate_summary.read_text(encoding="utf-8"))
        except Exception as exc:
            summary["parse_error"] = f"{type(exc).__name__}:{exc}"
    expected_registry_rows = (summary["candidate_summary"] or {}).get("candidate_code_count")
    expected_member_rows = (summary["candidate_summary"] or {}).get("candidate_member_total_across_source_backed_codes")
    comparisons = {
        "registry_row_count_matches_summary": expected_registry_rows is None or registry["row_count"] == int(expected_registry_rows),
        "member_row_count_matches_summary": expected_member_rows is None or member["row_count"] == int(expected_member_rows),
        "accepted_manifest_is_present": accepted["exists"],
        "candidate_summary_is_present": summary_meta["exists"] and summary["parse_error"] is None,
    }
    status = "ready" if all(comparisons.values()) and registry["status"] == "ready" and member["status"] == "ready" and accepted["status"] == "ready" else "blocked"
    return {
        "status": status,
        "registry_candidate": registry,
        "member_candidate": member,
        "candidate_summary": summary,
        "accepted_manifest": accepted,
        "comparisons": comparisons,
        "publish_state": "local_only_not_published",
    }


def build_plan(*, client: Any, registry_candidate: Path, member_candidate: Path, candidate_summary: Path, accepted_manifest: Path) -> dict[str, Any]:
    schema = _read_live_schema(client)
    payloads = _payload_checks(
        registry_candidate=registry_candidate,
        member_candidate=member_candidate,
        candidate_summary=candidate_summary,
        accepted_manifest=accepted_manifest,
    )
    live_columns = schema.get("columns_by_table", {}).get(FORMAL_TABLE, [])
    aggregate = (schema.get("aggregate") or [{}])[0]
    parts = schema.get("parts") or []
    live_table_row = next((row for row in schema.get("tables", []) if str(row.get("name")) == FORMAL_TABLE), {})
    expected = {
        "kline_daily_contract_ddl": _candidate_kline_ddl(live_columns),
        "registry_contract_ddl": _candidate_registry_ddl(REGISTRY_TABLE),
        "member_contract_ddl": _candidate_registry_ddl(MEMBER_TABLE),
    }
    copy_columns = [str(row.get("name")) for row in live_columns if row.get("name")]
    select_columns = ", ".join(
        "ifNull(created_at,toDateTime('1970-01-01 00:00:00')) AS created_at" if name == "created_at" else name
        for name in copy_columns
    )
    copy_plan = {
        "source_table": "stock.kline_daily",
        "candidate_table": "stock.kline_daily__migration_candidate",
        "select_expression": f"SELECT {select_columns} FROM stock.kline_daily ORDER BY toYYYYMM(trade_date), code, trade_date",
        "expected_rows": aggregate.get("rows"),
        "expected_unique_keys": aggregate.get("unique_keys"),
        "expected_null_created_at_after_normalization": 0,
        "source_parts": parts,
        "partition_plan": {
            "source_partition_key": live_table_row.get("partition_key"),
            "target_partition_key": "toYYYYMM(trade_date)",
            "source_active_part_count": sum(int(row.get("active_parts") or 0) for row in parts),
            "source_partition_count": len(parts),
            "target_expected_month_partition_count": (schema.get("monthly_partition_count") or [{}])[0].get("distinct_month_partitions"),
            "copy_order": "toYYYYMM(trade_date), code, trade_date",
        },
        "row_hash_contract": {
            "metadata_snapshot_sha256": schema.get("metadata_snapshot_sha256"),
            "row_value_hash": "not_materialized_in_dry_run",
            "per_partition_hash_query_template": "SELECT toYYYYMM(trade_date),count(),uniqExact((code,trade_date)),groupBitXor(cityHash64(code,toString(trade_date),toString(open),toString(high),toString(low),toString(close),toString(volume),toString(amount))) FROM <table> GROUP BY toYYYYMM(trade_date)",
            "required_compare": "source and candidate row_count, unique_keys and per-month value hash must match before rename",
        },
    }
    rollback = {
        "status": "blocked_until_explicit_migration_review",
        "pre_migration_snapshot": {
            "schema_metadata_sha256": schema.get("metadata_snapshot_sha256"),
            "parts": parts,
            "aggregate": aggregate,
            "payloads": payloads,
        },
        "post_migration_compare": [
            "candidate schema matches partition/version contract",
            "candidate raw count and uniqExact(code,trade_date) equal pre-migration baseline",
            "candidate per-month row counts, unique keys and value hashes equal source",
            "candidate FINAL has the same exact key set as candidate raw",
            "registry/member candidate digests and member counts match local manifests",
        ],
        "rollback_preconditions": [
            "old stock.kline_daily remains intact and is not dropped before post-promotion validation",
            "candidate table has no active inserts or mutations",
            "pre-migration metadata/parts snapshot and payload SHAs are immutable",
            "rename/exchange is performed only by a separately approved migration command",
            "rollback target and exact pre/post key snapshots are available",
        ],
        "rollback_action_proposal": "retain old table and reverse the table rename only after post-switch readback fails",
        "commands_issued": {"select_queries": schema.get("select_query_count", 0), "ddl": 0, "dml": 0, "rename": 0, "mutation": 0},
    }
    blocked = schema.get("status") != "ready" or payloads.get("status") != "ready"
    return {
        "schema_version": 1,
        "readonly": True,
        "status": "blocked" if blocked else "ready_for_explicit_migration_review",
        "gate": "blocked_live_schema_or_payload" if blocked else "review_only_no_commands_issued",
        "live_schema_readback": schema,
        "local_payload_checks": payloads,
        "expected_ddl_proposal_not_executed": expected,
        "migration_strategy": {
            "kind": "rebuild_candidate_then_validate_then_explicit_rename",
            "steps": [
                "freeze and snapshot source schema/parts/row counts/hashes",
                "build a candidate table with monthly partitions and non-null created_at version expression",
                "copy rows with NULL created_at normalized to the documented epoch and compare every partition",
                "build registry/member candidate tables from local payloads and verify digests",
                "pause and obtain explicit approval before any rename or publish",
                "after switch, run raw/FINAL exact-key readback and retain old table until acceptance",
            ],
            "write_execution": False,
        },
        "data_copy_plan": copy_plan,
        "rollback_validation": rollback,
        "production_mutation": {"ddl": False, "dml": False, "rename": False, "mutation": False, "registry_publish": False},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-candidate", type=Path, required=True)
    parser.add_argument("--member-candidate", type=Path, required=True)
    parser.add_argument("--candidate-summary", type=Path, required=True)
    parser.add_argument("--accepted-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    client = clickhouse_client()
    plan = build_plan(
        client=client,
        registry_candidate=args.registry_candidate,
        member_candidate=args.member_candidate,
        candidate_summary=args.candidate_summary,
        accepted_manifest=args.accepted_manifest,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "status": plan["status"], "gate": plan["gate"], "production_mutation": plan["production_mutation"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

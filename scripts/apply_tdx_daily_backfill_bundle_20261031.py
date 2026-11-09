"""Source-bound TDX daily backfill with exact-key guards and rollback journal.

The default mode is read-only. ``--apply`` holds both the host-local runner
file lock and the database-scoped distributed lock
(``stock.daily_backfill_distributed_lock``): the lock state lives in
ClickHouse, so any writer that can reach the database — including proxy/tunnel
writers — contends on the same CAS row, arbitrated by the server-serialized
mutation queue. No credentials are accepted on the command line.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.cross_strategy.import_local_tdx_daily_to_clickhouse import _iter_day_bars
from utils.clickhouse_schema import (
    STARTUP_TABLE_CONTRACTS,
    _compact_sql_fragment,
    _server_rendered_constraint_fragment,
)
from utils.daily_date_universe import (
    BUSINESS_TZ,
    DailyDateUniverseReader,
    DailyExactKeyPermit,
    DateUniverseContractError,
    normalize_code,
    normalize_trade_date,
)
from utils.daily_write_guard import DailyWriteScope, validate_daily_dataframe
from utils.kline_store import insert_kline_dataframe
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path
from utils.security_market_policy import assert_no_neeq_codes
from utils.tdx_daily_backfill import (
    KLINE_DAILY_TDX_COLUMNS,
    MEMBER_COLUMNS,
    REGISTRY_COLUMNS,
    TdxDailyBackfillApplyLock,
    acquire_tdx_daily_distributed_lock,
    build_daily_insert_frame,
    build_tdx_daily_backfill_plan,
    build_tdx_rollback_registry_rows,
    execute_tdx_daily_apply,
    load_reviewed_tdx_candidate_bundle,
    member_rollback_sql_for_dataset_ids,
    member_rows_by_month,
    parse_windows_tcp_listener_addresses,
    publish_tdx_date_universe,
    require_tdx_daily_distributed_lock,
    rollback_sql_for_ids_created_at,
    stable_tdx_daily_id,
    status_conflicts_for_candidates,
    validate_baostock_crosscheck,
    validate_kline_daily_exact_readback,
    validate_owned_kline_rows_for_rollback,
    validate_single_writer_deployment,
    validate_tdx_formal_overlap,
    validate_tdx_source_target_coverage,
    verify_tdx_daily_distributed_lock_provider,
)

DEFAULT_BUNDLE = runtime_path("daily_kline_coverage", "tdx_backfill_preflight_20261031")
DEFAULT_CROSSCHECK_SHA256 = "cbe3527aba869b5a41688df614edcd50dcb6f801a2a32fff1283d57849534d31"
CONTROL_START = date(2026, 10, 20)
TARGET_START = date(2026, 10, 22)
TARGET_END = date(2026, 10, 30)
MAX_PARTS_PER_MEMBER_PARTITION = 100
CLICKHOUSE_HTTP_PORT = 8123
FLOAT_COLUMNS = {
    "open", "high", "low", "close", "volume", "amount", "amplitude",
    "change_pct", "change_amount", "turnover_rate",
}
TARGET_TABLES = ("kline_daily", "daily_date_universe_registry", "daily_date_universe_member")
CONCURRENCY_TABLES = TARGET_TABLES + ("trade_calendar", "stocks", "kline_daily_market_status_audit")

EXPECTED_COLUMNS = {
    "kline_daily": {
        "code": "String", "trade_date": "Date", "open": "Float64", "high": "Float64",
        "low": "Float64", "close": "Float64", "volume": "Float64", "amount": "Float64",
        "amplitude": "Float64", "change_pct": "Float64", "change_amount": "Float64",
        "turnover_rate": "Float64", "created_at": "DateTime", "id": "UInt64",
    },
    "daily_date_universe_registry": {
        "dataset_id": "String", "code": "String", "security_type": "LowCardinality(String)",
        "identity_status": "String", "source_kind": "String", "source_ref": "String",
        "source_sha256": "String", "coverage_start": "Nullable(Date)",
        "coverage_end": "Nullable(Date)", "completeness_state": "LowCardinality(String)",
        "date_set_sha256": "String", "member_count": "UInt64",
        "published_at": "DateTime('Asia/Shanghai')", "is_active": "UInt8",
    },
    "daily_date_universe_member": {
        "dataset_id": "String", "code": "String", "trade_date": "Date",
        "member_ordinal": "UInt32", "date_set_sha256": "String",
        "published_at": "DateTime('Asia/Shanghai')",
    },
    "trade_calendar": {"trade_date": "Date", "market": "String", "is_trading": "UInt8"},
    "stocks": {
        "code": "String", "name": "String", "market": "String", "type": "String",
        "listing_status": "LowCardinality(String)", "industry": "Nullable(String)",
        "region": "Nullable(String)", "list_date": "Nullable(Date)",
        "delist_date": "Nullable(Date)", "quit": "UInt8", "st": "UInt8",
        "industry_code": "Nullable(String)", "self_selected": "UInt8", "holding": "UInt8",
        "float_share": "Nullable(Float64)", "total_share": "Nullable(Float64)",
        "created_at": "DateTime", "updated_at": "DateTime",
    },
    "kline_daily_market_status_audit": {
        "code": "String", "start_date": "Date", "end_date": "Date", "status": "String",
        "evidence_url": "String", "reason": "String", "review_state": "String", "evidence_json": "String",
    },
}
EXPECTED_TABLES = {
    "kline_daily": (
        "ReplacingMergeTree", "toYYYYMM(trade_date)", "code, trade_date",
        "ReplacingMergeTree(created_at)",
    ),
    "daily_date_universe_registry": (
        "ReplacingMergeTree", "", "code, dataset_id", "ReplacingMergeTree(published_at) ORDER BY (code, dataset_id)"
    ),
    "daily_date_universe_member": (
        "MergeTree", "toYYYYMM(trade_date)", "dataset_id, code, trade_date",
        "MergeTree PARTITION BY toYYYYMM(trade_date) ORDER BY (dataset_id, code, trade_date)"
    ),
    "trade_calendar": ("ReplacingMergeTree", "", "trade_date, market", "ReplacingMergeTree ORDER BY (trade_date, market)"),
    "stocks": (
        "ReplacingMergeTree", "", "code", "ReplacingMergeTree(updated_at) ORDER BY code"
    ),
    "kline_daily_market_status_audit": (
        "ReplacingMergeTree", "", "code, start_date, end_date, status",
        "ReplacingMergeTree(observed_at) ORDER BY (code, start_date, end_date, status)"
    ),
}


def _quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _in_list(values: Iterable[object], *, dates: bool = False) -> str:
    vals = list(values)
    if not vals:
        raise ValueError("SQL IN list must not be empty")
    if dates:
        return ",".join(f"toDate({_quote(normalize_trade_date(value).isoformat())})" for value in vals)
    return ",".join(_quote(value) for value in vals)


def _dict_rows(client, sql: str, *, max_execution_time: int = 120) -> list[dict[str, Any]]:
    result = client.query(
        sql,
        settings={
            "readonly": 1,
            "max_execution_time": max_execution_time,
            "max_threads": 4,
            "max_query_size": 2_000_000,
        },
    )
    names = list(result.column_names)
    return [dict(zip(names, row)) for row in result.result_rows]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def _identity(client) -> dict[str, Any]:
    # This pure SELECT intentionally does not set readonly=1: getSetting('readonly')
    # must report the principal's effective permission, not the query override.
    # Every data-bearing preflight SELECT still explicitly uses readonly=1.
    result = client.query(
        "SELECT currentUser() AS user, getSetting('readonly') AS readonly, "
        "version() AS version, timezone() AS timezone, currentDatabase() AS database, now() AS server_now",
        settings={"max_execution_time": 20, "max_threads": 2},
    )
    names = list(result.column_names)
    return dict(zip(names, result.result_rows[0]))


def _windows_clickhouse_listener_addresses(port: int) -> tuple[str, ...]:
    if os.name != "nt":
        raise RuntimeError("single-writer listener verification is supported only on Windows")
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            check=True,
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("could not verify the local ClickHouse TCP listener") from exc
    return parse_windows_tcp_listener_addresses(result.stdout, port)


def _single_writer_topology(client) -> dict[str, Any]:
    cluster_rows = _dict_rows(
        client,
        "SELECT cluster,host_name,port,is_local FROM system.clusters "
        "WHERE cluster='default' ORDER BY shard_num,replica_num",
    )
    listener_addresses = _windows_clickhouse_listener_addresses(CLICKHOUSE_HTTP_PORT)
    return validate_single_writer_deployment(
        clickhouse_url=getattr(client, "url", ""),
        default_cluster_rows=cluster_rows,
        listener_addresses=listener_addresses,
    )


def _inspect_schema(client) -> dict[str, Any]:
    table_names = tuple(EXPECTED_TABLES)
    in_sql = _in_list(table_names)
    tables = _dict_rows(
        client,
        "SELECT name,engine,engine_full,partition_key,sorting_key,primary_key,create_table_query "
        f"FROM system.tables WHERE database='stock' AND name IN ({in_sql}) ORDER BY name",
    )
    columns = _dict_rows(
        client,
        "SELECT table,name,type FROM system.columns "
        f"WHERE database='stock' AND table IN ({in_sql}) ORDER BY table,position",
    )
    table_by_name = {str(row["name"]): row for row in tables}
    if set(table_by_name) != set(EXPECTED_TABLES):
        raise RuntimeError(f"required ClickHouse tables missing: {sorted(set(EXPECTED_TABLES) - set(table_by_name))}")
    columns_by_table: dict[str, dict[str, str]] = defaultdict(dict)
    for row in columns:
        columns_by_table[str(row["table"])][str(row["name"])] = str(row["type"])
    contract_by_name = {contract.name: contract for contract in STARTUP_TABLE_CONTRACTS}
    for name, (engine, partition, sorting, engine_prefix) in EXPECTED_TABLES.items():
        table = table_by_name[name]
        if (table["engine"], table["partition_key"], table["sorting_key"]) != (engine, partition, sorting):
            raise RuntimeError(f"ClickHouse table engine/key contract changed for {name}: {table}")
        if not str(table["engine_full"]).startswith(engine_prefix):
            raise RuntimeError(f"ClickHouse engine version contract changed for {name}: {table['engine_full']}")
        contract = contract_by_name.get(name)
        if contract and contract.required_check_constraints:
            compact_ddl = _compact_sql_fragment(table.get("create_table_query")).lower()
            for constraint_name, expression in contract.required_check_constraints:
                expected_fragment = _server_rendered_constraint_fragment(constraint_name, expression).lower()
                if expected_fragment not in compact_ddl:
                    raise RuntimeError(
                        f"ClickHouse CHECK constraint contract changed for {name}.{constraint_name}"
                    )
        actual = columns_by_table.get(name, {})
        missing = {key: value for key, value in EXPECTED_COLUMNS[name].items() if actual.get(key) != value}
        if missing:
            raise RuntimeError(f"ClickHouse required columns/types changed for {name}: {missing}")
    schema = {
        "tables": {name: table_by_name[name] for name in sorted(table_by_name)},
        "columns": {name: columns_by_table[name] for name in sorted(columns_by_table)},
    }
    schema_sha = hashlib.sha256(
        json.dumps(schema, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"schema": schema, "schema_sha256": schema_sha}


def _write_state(client) -> dict[str, Any]:
    process_rows = _dict_rows(
        client,
        "SELECT query_id,query,elapsed FROM system.processes WHERE query!='' ORDER BY elapsed DESC",
    )
    active_writes = []
    for row in process_rows:
        query = str(row.get("query") or "")
        words = query.lstrip().split(None, 1)
        verb = words[0].upper() if words else ""
        if verb in {"SELECT", "WITH", "SHOW", "DESCRIBE", "EXPLAIN", "EXISTS"}:
            continue
        lower = query.lower()
        if any(table in lower for table in CONCURRENCY_TABLES):
            active_writes.append(
                {"query_id": row.get("query_id"), "verb": verb, "elapsed": row.get("elapsed")}
            )
    table_sql = ",".join(_quote(table) for table in CONCURRENCY_TABLES)
    mutations = _dict_rows(
        client,
        "SELECT mutation_id,table,is_done,is_killed FROM system.mutations "
        f"WHERE database='stock' AND table IN ({table_sql}) "
        "AND is_done=0 AND is_killed=0 ORDER BY table,mutation_id",
    )
    return {"active_writes": active_writes, "pending_mutations": mutations}


def _assert_quiet(client) -> dict[str, Any]:
    state = _write_state(client)
    if state["active_writes"] or state["pending_mutations"]:
        raise RuntimeError(f"concurrent ClickHouse write/mutation detected: {state}")
    return state


def _read_candidate_stocks(client, codes: Sequence[str]) -> tuple[list[dict[str, Any]], set[str]]:
    rows = _dict_rows(
        client,
        "SELECT code,list_date,delist_date,listing_status,quit FROM stock.stocks FINAL "
        f"WHERE type='stock' AND code IN ({_in_list(codes)})",
    )
    active = {
        normalize_code(row["code"])
        for row in rows
        if str(row.get("listing_status") or "").lower() == "active" and int(row.get("quit") or 0) == 0
    }
    return rows, active


def _check_lifecycle(candidate_rows: Sequence[Mapping[str, Any]], stock_rows: Sequence[Mapping[str, Any]], active_codes: set[str]) -> None:
    by_code = {normalize_code(row["code"]): row for row in stock_rows}
    for candidate in candidate_rows:
        code = normalize_code(candidate["code"])
        trade_day = normalize_trade_date(candidate["trade_date"])
        stock = by_code.get(code)
        if code not in active_codes or stock is None:
            raise RuntimeError(f"candidate code is not current active stock master: {code}")
        list_date = stock.get("list_date")
        delist_date = stock.get("delist_date")
        if list_date is None or normalize_trade_date(list_date) > trade_day:
            raise RuntimeError(f"candidate predates listing lifecycle: {code}@{trade_day}")
        if delist_date is not None and normalize_trade_date(delist_date) < trade_day:
            raise RuntimeError(f"candidate postdates delisting lifecycle: {code}@{trade_day}")


def _read_calendar(client, target_dates: set[date]) -> set[date]:
    rows = _dict_rows(
        client,
        "SELECT trade_date FROM stock.trade_calendar FINAL "
        f"WHERE market='SH' AND is_trading=1 AND trade_date>=toDate('{CONTROL_START}') "
        f"AND trade_date<=toDate('{TARGET_END}') ORDER BY trade_date",
    )
    sessions = {normalize_trade_date(row["trade_date"]) for row in rows}
    missing = target_dates - sessions
    if missing:
        raise RuntimeError(f"candidate dates absent from live Shanghai trade calendar: {sorted(missing)}")
    return sessions


def _read_status_rows(client, codes: Sequence[str]) -> list[dict[str, Any]]:
    return _dict_rows(
        client,
        "SELECT code,start_date,end_date,status,evidence_url,reason,review_state,evidence_json "
        "FROM stock.kline_daily_market_status_audit FINAL "
        f"WHERE code IN ({_in_list(codes)}) AND start_date<=toDate('{TARGET_END}') "
        f"AND end_date>=toDate('{TARGET_START}') AND "
        "((review_state='verified' AND evidence_url!='' AND status IN ('suspended','delisted','not_trading')) "
        "OR review_state='conflict')",
    )


def _registry_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (str(row.get("code")), str(row.get("dataset_id"))),
    )
    return hashlib.sha256(
        json.dumps(ordered, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _member_fingerprint(rows: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(
        (
            str(row.get("dataset_id")), str(row.get("code")),
            normalize_trade_date(row.get("trade_date")).isoformat(),
            int(row.get("member_ordinal") or 0), str(row.get("date_set_sha256")),
        )
        for row in rows
    )
    return hashlib.sha256(
        json.dumps(ordered, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_registry_rows(client, codes: Sequence[str]) -> list[dict[str, Any]]:
    return _dict_rows(
        client,
        f"SELECT {','.join(REGISTRY_COLUMNS)} FROM stock.daily_date_universe_registry FINAL "
        f"WHERE code IN ({_in_list(codes)}) ORDER BY code,dataset_id",
    )


def _read_active_members(client, registry_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    dataset_ids = sorted(
        {str(row.get("dataset_id") or "").strip() for row in registry_rows if int(row.get("is_active") or 0) == 1}
    )
    if not dataset_ids:
        return []
    result: list[dict[str, Any]] = []
    for offset in range(0, len(dataset_ids), 250):
        batch = dataset_ids[offset : offset + 250]
        result.extend(
            _dict_rows(
                client,
                f"SELECT {','.join(MEMBER_COLUMNS)} FROM stock.daily_date_universe_member "
                f"WHERE dataset_id IN ({_in_list(batch)}) ORDER BY dataset_id,code,trade_date",
            )
        )
    return result


def _read_formal_target(client, codes: Sequence[str], target_dates: set[date]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    code_sql = _in_list(codes)
    date_sql = _in_list(sorted(target_dates), dates=True)
    where = f"code IN ({code_sql}) AND trade_date IN ({date_sql})"
    raw_rows = _dict_rows(client, f"SELECT {','.join(KLINE_DAILY_TDX_COLUMNS)} FROM stock.kline_daily WHERE {where}")
    final_rows = _dict_rows(client, f"SELECT {','.join(KLINE_DAILY_TDX_COLUMNS)} FROM stock.kline_daily FINAL WHERE {where}")
    return raw_rows, final_rows


def _read_formal_overlap(client, codes: Sequence[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    code_sql = _in_list(codes)
    range_sql = f"code IN ({code_sql}) AND trade_date>=toDate('{CONTROL_START}') AND trade_date<=toDate('{TARGET_END}')"
    final_rows = _dict_rows(
        client,
        f"SELECT {','.join(KLINE_DAILY_TDX_COLUMNS)} FROM stock.kline_daily FINAL WHERE {range_sql}",
    )
    duplicates = _dict_rows(
        client,
        "SELECT code,trade_date,count() AS row_count FROM stock.kline_daily WHERE "
        f"{range_sql} GROUP BY code,trade_date HAVING row_count>1 ORDER BY code,trade_date",
    )
    return final_rows, duplicates


def _query_id_collisions(
    client, rows: Sequence[Mapping[str, Any]], *, attempt_nonce: str = ""
) -> dict[int, list[dict[str, Any]]]:
    ids = [
        stable_tdx_daily_id(row["code"], row["trade_date"], attempt_nonce=attempt_nonce)
        for row in rows
    ]
    if len(set(ids)) != len(ids):
        raise RuntimeError("attempt-scoped TDX candidate IDs collide within candidate bundle")
    found = _dict_rows(
        client,
        "SELECT id,code,trade_date FROM stock.kline_daily "
        f"WHERE id IN ({','.join(str(value) for value in sorted(set(ids)))})",
        max_execution_time=180,
    )
    collisions: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in found:
        collisions[int(row["id"])].append(row)
    return dict(collisions)


def _check_unused_dataset_ids(client, dataset_ids: Sequence[str]) -> int:
    total = 0
    for offset in range(0, len(dataset_ids), 250):
        batch = dataset_ids[offset : offset + 250]
        rows = _dict_rows(
            client,
            "SELECT count() AS row_count FROM stock.daily_date_universe_member "
            f"WHERE dataset_id IN ({_in_list(batch)})",
        )
        total += int(rows[0]["row_count"]) if rows else 0
    if total:
        raise RuntimeError(f"planned date-universe dataset IDs already have {total} member rows")
    return total


def _member_part_budget(client, plan) -> dict[str, Any]:
    existing_rows = _dict_rows(
        client,
        "SELECT partition,count() AS part_count,sum(rows) AS row_count FROM system.parts "
        "WHERE database='stock' AND table='daily_date_universe_member' AND active=1 "
        "GROUP BY partition ORDER BY partition",
    )
    existing = {str(row["partition"]): int(row["part_count"]) for row in existing_rows}
    planned = member_rows_by_month(plan)
    projected = {partition: existing.get(partition, 0) + 1 for partition in planned}
    over = {partition: count for partition, count in projected.items() if count > MAX_PARTS_PER_MEMBER_PARTITION}
    if over:
        raise RuntimeError(f"date-universe member part budget exceeded: {over}")
    existing_counts = [existing.get(partition, 0) for partition in planned]
    return {
        "partitions_to_insert": len(planned),
        "member_rows_to_insert": sum(len(rows) for rows in planned.values()),
        "projected_max_parts_per_partition": max(projected.values(), default=0),
        "existing_parts_min": min(existing_counts, default=0),
        "existing_parts_max": max(existing_counts, default=0),
    }


def _parse_source_rows(bundle: Mapping[str, Any], calendar_sessions: set[date]) -> list[dict[str, Any]]:
    files_by_code: dict[str, Path] = {}
    hashes_by_path: dict[Path, str] = {}
    for row in bundle["candidate_rows"]:
        code = normalize_code(row["code"])
        path = Path(row["tdx_file"]).resolve()
        previous = files_by_code.setdefault(code, path)
        if previous != path:
            raise RuntimeError(f"candidate TDX source path changed within {code}")
        expected_hash = str(row["tdx_file_sha256"]).lower()
        if hashes_by_path.setdefault(path, expected_hash) != expected_hash:
            raise RuntimeError(f"candidate TDX file hash changed within {path}")
    if len(files_by_code) != len(bundle["candidate_codes"]):
        raise RuntimeError("candidate source file/code mapping is incomplete")
    source_rows: list[dict[str, Any]] = []
    for code, path in sorted(files_by_code.items()):
        bars = _iter_day_bars(path, CONTROL_START, TARGET_END)
        for bar in bars:
            if bar.code != code:
                raise RuntimeError(f"TDX parser identity mismatch: {path} produced {bar.code}, expected {code}")
            if bar.trade_date not in calendar_sessions:
                continue
            source_rows.append(
                {
                    "code": bar.code,
                    "trade_date": bar.trade_date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "amount": bar.amount,
                    "prev_close": bar.prev_close,
                }
            )
    for path, expected_hash in hashes_by_path.items():
        if _sha256_file(path) != expected_hash:
            raise RuntimeError(f"TDX source changed while parsing: {path}")
    return source_rows


def _exact_snapshot(
    bundle: Mapping[str, Any],
    plan,
    raw_counts: Mapping[tuple[str, date], int],
    final_rows: Sequence[Mapping[str, Any]],
    id_collisions: Mapping[int, Sequence[Mapping[str, Any]]],
    *,
    attempt_nonce: str,
) -> dict[str, Any]:
    final_keys = {
        (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
        for row in final_rows
    }
    final_candidate = {key for key in final_keys if key in {
        (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
        for row in bundle["candidate_rows"]
    }}
    if final_candidate:
        raise RuntimeError(f"candidate exact keys appeared in formal FINAL before write: {len(final_candidate)}")
    plan_keys = set(plan.keys)
    csv_rows = []
    candidate_by_key = {}
    for source in bundle["candidate_rows"]:
        code = normalize_code(source["code"])
        trade_day = normalize_trade_date(source["trade_date"])
        key = (code, trade_day)
        candidate_by_key[key] = source
        row_id = stable_tdx_daily_id(code, trade_day, attempt_nonce=attempt_nonce)
        collisions = list(id_collisions.get(row_id, ()))
        existing = "|".join(
            f"{normalize_code(item['code'])}@{normalize_trade_date(item['trade_date'])}"
            for item in collisions
        )
        csv_rows.append(
            {
                "code": code,
                "trade_date": trade_day.isoformat(),
                "plan_write": int(key in plan_keys),
                "blocked_reason": plan.blocked_by_code.get(code, ""),
                "formal_raw_count": int(raw_counts.get(key, 0)),
                "formal_final_present": int(key in final_keys),
                "attempt_nonce": attempt_nonce,
                "stable_id": row_id,
                "id_collision_existing_key": existing,
                "tdx_file_sha256": source["tdx_file_sha256"],
            }
        )
    if len(csv_rows) != len(bundle["candidate_rows"]):
        raise RuntimeError("exact-key snapshot row count mismatch")
    buffer_path = Path(bundle["bundle_dir"]) / "tdx_exact_key_snapshot.tmp"
    fieldnames = list(csv_rows[0])
    with buffer_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)
    snapshot_sha = _sha256_file(buffer_path)
    snapshot_path = Path(bundle["bundle_dir"]) / f"tdx_exact_key_snapshot_{plan.plan_sha256[:12]}_{snapshot_sha[:12]}.csv"
    os.replace(buffer_path, snapshot_path)
    return {
        "path": str(snapshot_path),
        "sha256": snapshot_sha,
        "rows": len(csv_rows),
        "candidate_keys_with_raw_rows": sum(1 for row in csv_rows if int(row["formal_raw_count"]) > 0),
        "candidate_keys_with_final_rows": sum(1 for row in csv_rows if int(row["formal_final_present"]) > 0),
        "candidate_keys_with_id_collisions": sum(1 for row in csv_rows if row["id_collision_existing_key"]),
        "planned_write_rows": len(plan.candidate_rows),
        "blocked_candidate_rows": len(plan.blocked_candidate_rows),
    }


def _current_formal_snapshot(client, codes: Sequence[str], target_dates: set[date]) -> dict[str, Any]:
    raw_rows, final_rows = _read_formal_target(client, codes, target_dates)
    raw_counts = Counter(
        (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
        for row in raw_rows
    )
    final_by_key = {
        (normalize_code(row["code"]), normalize_trade_date(row["trade_date"])): row
        for row in final_rows
    }
    if len(final_by_key) != len(final_rows):
        raise RuntimeError("duplicate FINAL exact keys in candidate target-date scope")
    if any(count != 1 for count in raw_counts.values()):
        raise RuntimeError("duplicate raw exact keys in candidate target-date scope")
    if set(raw_counts) != set(final_by_key):
        raise RuntimeError("raw/FINAL target-date key sets disagree")
    return {
        "raw_rows": raw_rows,
        "final_rows": final_rows,
        "raw_counts": dict(raw_counts),
        "final_by_key": final_by_key,
    }


def _preflight(client, *, bundle_dir: Path, plan_nonce: str, expected_candidate_sha256: str | None,
               expected_crosscheck_sha256: str | None, require_write_access: bool) -> dict[str, Any]:
    attempt_nonce = uuid.uuid4().hex
    single_writer_topology = _single_writer_topology(client)
    identity = _identity(client)
    if identity["database"] != "stock":
        raise RuntimeError(f"unexpected ClickHouse database: {identity['database']}")
    if require_write_access and int(identity["readonly"]) != 0:
        raise RuntimeError(f"ClickHouse principal is read-only: {identity['user']}")
    schema = _inspect_schema(client)
    quiet = _assert_quiet(client)
    bundle = load_reviewed_tdx_candidate_bundle(
        bundle_dir,
        expected_candidate_sha256=expected_candidate_sha256,
        expected_crosscheck_sha256=expected_crosscheck_sha256,
        verify_source_files=True,
    )
    target_dates = set(bundle["target_trade_dates"])
    codes = sorted(bundle["candidate_codes"])
    if any(not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code) for code in codes):
        raise RuntimeError("bundle contains non-canonical mainland stock code")
    assert_no_neeq_codes(codes, context="TDX daily backfill")

    stocks, active_codes = _read_candidate_stocks(client, codes)
    _check_lifecycle(bundle["candidate_rows"], stocks, active_codes)
    calendar_sessions = _read_calendar(client, target_dates)
    status_rows = _read_status_rows(client, codes)
    status_conflicts = status_conflicts_for_candidates(bundle["candidate_rows"], status_rows)
    if status_conflicts:
        raise RuntimeError(f"market-status conflicts on candidate keys: {status_conflicts[:20]}")

    formal = _current_formal_snapshot(client, codes, target_dates)
    candidate_keys = {
        (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
        for row in bundle["candidate_rows"]
    }
    formal_intersection = candidate_keys & (set(formal["raw_counts"]) | set(formal["final_by_key"]))
    if formal_intersection:
        raise RuntimeError(f"candidate exact keys are no longer missing: {len(formal_intersection)}")

    overlap_rows, raw_overlap_duplicates = _read_formal_overlap(client, codes)
    if raw_overlap_duplicates:
        raise RuntimeError(f"raw duplicate TDX/formal overlap keys: {raw_overlap_duplicates[:10]}")
    source_rows = _parse_source_rows(bundle, calendar_sessions)
    crosscheck_summary = bundle["manifest"].get("independent_baostock_crosscheck") or {}
    sample_codes = tuple(normalize_code(code) for code in crosscheck_summary.get("sample_codes", ()))
    if len(sample_codes) != len(set(sample_codes)):
        raise RuntimeError("declared Baostock sample contains duplicate codes")
    declared_crosscheck_keys = {
        (code, trade_day) for code in sample_codes for trade_day in target_dates
    }
    validate_baostock_crosscheck(
        bundle["baostock_crosscheck"],
        expected_rows=int(crosscheck_summary.get("records_checked", 0)),
        allowed_keys={
            (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
            for row in source_rows
            if normalize_trade_date(row["trade_date"]) in target_dates
        },
        expected_keys=declared_crosscheck_keys,
    )
    overlap_result = validate_tdx_formal_overlap(
        bundle["candidate_rows"], source_rows, overlap_rows,
        price_relative_tolerance=0.01,
        quantity_relative_tolerance=0.03,
    )
    stocks_by_code = {normalize_code(row["code"]): row for row in stocks}
    expected_dates_by_code: dict[str, tuple[date, ...]] = {}
    for code in codes:
        stock = stocks_by_code.get(code)
        if stock is None or stock.get("list_date") is None:
            raise RuntimeError(f"stock lifecycle missing while building target window for {code}")
        list_date = normalize_trade_date(stock["list_date"])
        delist_date = normalize_trade_date(stock["delist_date"]) if stock.get("delist_date") is not None else None
        expected_dates = tuple(
            day for day in sorted(target_dates)
            if day >= list_date and (delist_date is None or day <= delist_date)
        )
        if not expected_dates:
            raise RuntimeError(f"no target sessions within stock lifecycle for {code}")
        expected_dates_by_code[code] = expected_dates
    target_coverage = validate_tdx_source_target_coverage(
        bundle["candidate_rows"], source_rows, formal["final_rows"],
        target_trade_dates=target_dates,
        expected_dates_by_code=expected_dates_by_code,
        status_rows=status_rows,
    )
    if target_coverage["candidate_rows"] != len(bundle["candidate_rows"]):
        raise RuntimeError("source-target coverage count does not match candidate CSV")

    registry_rows = _read_registry_rows(client, codes)
    member_rows = _read_active_members(client, registry_rows)
    plan = build_tdx_daily_backfill_plan(
        bundle["candidate_rows"],
        candidate_sha256=bundle["candidate_sha256"],
        bundle_ref=str(bundle["manifest_path"]),
        plan_nonce=plan_nonce,
        active_stock_codes=active_codes,
        registry_rows=registry_rows,
        member_rows=member_rows,
        existing_formal_keys=formal["raw_counts"].keys(),
        expected_candidate_rows=len(bundle["candidate_rows"]),
        expected_candidate_codes=len(bundle["candidate_codes"]),
        expected_trade_dates=target_dates,
        member_dates_by_code=target_coverage["member_dates_by_code"],
        source_blocked_codes={
            code: "incomplete_source_target_window:" + ",".join(day.isoformat() for day in days)
            for code, days in target_coverage["incomplete_codes"].items()
        },
        published_at=datetime.now(BUSINESS_TZ).replace(microsecond=0),
    )
    if len(plan.candidate_rows) + len(plan.blocked_candidate_rows) != len(bundle["candidate_rows"]):
        raise RuntimeError("plan disposition count does not reconcile with candidate bundle")
    # created_at must round-trip: ClickHouse renders DateTime in the server
    # timezone, and the readback verification compares the frame value verbatim,
    # so build it as naive server-timezone wall clock (not machine-local time —
    # this runner may execute on a host whose local timezone differs from the
    # ClickHouse server timezone).
    frame = build_daily_insert_frame(
        plan,
        created_at=datetime.now(ZoneInfo(identity["timezone"])).replace(tzinfo=None, microsecond=0),
        attempt_nonce=attempt_nonce,
    )
    all_id_collisions = _query_id_collisions(
        client, bundle["candidate_rows"], attempt_nonce=attempt_nonce
    )
    planned_ids = {int(value) for value in frame["id"].tolist()}
    planned_collisions = planned_ids & set(all_id_collisions)
    if planned_collisions:
        sample = sorted(planned_collisions)[:10]
        raise RuntimeError(f"stable IDs for planned exact keys already exist: {sample}")
    unused_dataset_member_rows = _check_unused_dataset_ids(client, plan.dataset_ids)
    part_budget = _member_part_budget(client, plan)
    snapshot = _exact_snapshot(
        bundle, plan, formal["raw_counts"], formal["final_rows"], all_id_collisions,
        attempt_nonce=attempt_nonce,
    )
    current_registry_codes = {
        normalize_code(row["code"])
        for row in registry_rows
        if int(row.get("is_active") or 0) == 1 and str(row.get("completeness_state") or "").lower() == "complete"
    }
    lock_provider = verify_tdx_daily_distributed_lock_provider(client)
    apply_allowed = bool(lock_provider.get("available"))
    report = {
        "status": "ready_for_explicit_apply" if apply_allowed else "preflight_complete_apply_blocked",
        "apply_allowed": apply_allowed,
        "apply_blockers": [] if apply_allowed else ["database_scoped_distributed_lock_unavailable"],
        "lock_provider": lock_provider,
        "write_performed": False,
        "plan_nonce": plan_nonce,
        "attempt_nonce": attempt_nonce,
        "created_at": datetime.now(BUSINESS_TZ).isoformat(timespec="seconds"),
        "single_writer_topology": single_writer_topology,
        "clickhouse": {
            "user": identity["user"], "readonly": int(identity["readonly"]),
            "version": identity["version"], "timezone": identity["timezone"],
            "database": identity["database"], "server_now": identity["server_now"],
        },
        "schema_sha256": schema["schema_sha256"],
        "candidate_sha256": bundle["candidate_sha256"],
        "crosscheck_sha256": bundle["crosscheck_sha256"],
        "source_files_checked": bundle["source_files_checked"],
        "candidate_rows": len(bundle["candidate_rows"]),
        "candidate_codes": len(bundle["candidate_codes"]),
        "candidate_dates": sorted(day.isoformat() for day in target_dates),
        "baostock_sample_rows": len(bundle["baostock_crosscheck"]["results"]),
        "source_target_coverage": {
            key: value for key, value in target_coverage.items() if key != "member_dates_by_code"
        },
        "formal_overlap_validation": overlap_result,
        "formal_target_baseline": {
            "raw_rows": len(formal["raw_rows"]),
            "final_rows": len(formal["final_rows"]),
            "unique_keys": len(formal["raw_counts"]),
            "candidate_key_intersection": len(formal_intersection),
        },
        "active_complete_candidate_codes_before": len(current_registry_codes),
        "planned_rows": len(plan.candidate_rows),
        "planned_codes": len(plan.payloads),
        "blocked_rows": len(plan.blocked_candidate_rows),
        "blocked_codes": dict(plan.blocked_by_code),
        "plan_sha256": plan.plan_sha256,
        "dataset_id_count": len(plan.dataset_ids),
        "dataset_id_sha256": hashlib.sha256("\n".join(plan.dataset_ids).encode("utf-8")).hexdigest(),
        "dataset_id_samples": list(plan.dataset_ids[:5]),
        "new_registry_rows": len(plan.registry_rows),
        "retired_registry_rows": len(plan.retired_registry_rows),
        "member_rows": len(plan.member_rows),
        "new_dataset_existing_member_rows": unused_dataset_member_rows,
        "member_part_budget": part_budget,
        "concurrent_write_state": quiet,
        "exact_key_snapshot": snapshot,
        "source_window_keys_sha256": target_coverage["source_window_keys_sha256"],
    }
    return {
        "report": report,
        "attempt_nonce": attempt_nonce,
        "bundle": bundle,
        "plan": plan,
        "frame": frame,
        "active_codes": active_codes,
        "registry_rows": registry_rows,
        "member_rows": member_rows,
        "registry_fingerprint": _registry_fingerprint(registry_rows),
        "member_fingerprint": _member_fingerprint(member_rows),
        "formal_baseline": formal,
        "calendar_sessions": calendar_sessions,
        "source_rows": source_rows,
        "single_writer_topology": single_writer_topology,
        "schema": schema,
        "id_collisions": all_id_collisions,
        "target_dates": target_dates,
        "codes": codes,
        "server_timezone": identity["timezone"],
    }


def _insert_dict_rows(client, table: str, rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> None:
    require_tdx_daily_distributed_lock()
    allowed_columns = {
        "stock.daily_date_universe_registry": tuple(REGISTRY_COLUMNS),
        "stock.daily_date_universe_member": tuple(MEMBER_COLUMNS),
    }
    expected_columns = allowed_columns.get(table)
    if expected_columns is None:
        raise DateUniverseContractError(f"date-universe insert destination is not allowlisted: {table}")
    if tuple(columns) != expected_columns:
        raise DateUniverseContractError(
            f"insert columns do not match the allowlisted schema for {table}"
        )
    if not rows:
        return
    expected_key_set = set(expected_columns)
    missing = sorted({column for row in rows for column in expected_columns if column not in row})
    if missing:
        raise DateUniverseContractError(
            f"insert rows are missing allowlisted columns for {table}: {missing}"
        )
    extra = sorted(
        {str(key) for row in rows for key in row if key not in expected_key_set}
    )
    if extra:
        raise DateUniverseContractError(
            f"insert rows contain non-allowlisted columns for {table}: {extra}"
        )
    values = [tuple(row[column] for column in expected_columns) for row in rows]
    client.insert(table, values, column_names=list(expected_columns))


def _assert_formal_append_columns(frame) -> None:
    actual_columns = tuple(str(column) for column in frame.columns)
    expected_columns = tuple(KLINE_DAILY_TDX_COLUMNS)
    if actual_columns != expected_columns:
        raise DateUniverseContractError(
            "formal append columns do not match exact TDX contract: "
            f"expected={expected_columns!r}, actual={actual_columns!r}"
        )


def _load_exact_permit(client, plan) -> DailyExactKeyPermit:
    reader = DailyDateUniverseReader.from_clickhouse(client, database="stock")
    keys = sorted(plan.keys)
    combined_keys = set()
    dataset_ids = set()
    digests: dict[str, str] = {}
    for offset in range(0, len(keys), 400):
        batch = keys[offset : offset + 400]
        partial = reader.exact_key_permit(batch, source=f"tdx-backfill:{plan.plan_sha256}")
        if partial.keys != frozenset(batch):
            raise DateUniverseContractError("official date-universe reader returned a non-exact permit batch")
        combined_keys.update(partial.keys)
        dataset_ids.update(partial.dataset_ids)
        for code, digest in partial.date_set_digests.items():
            previous = digests.setdefault(code, digest)
            if previous != digest:
                raise DateUniverseContractError(f"registry digest changed between permit batches for {code}")
    return DailyExactKeyPermit.from_keys(
        combined_keys,
        source=f"tdx-backfill:{plan.plan_sha256}",
        dataset_ids=dataset_ids,
        date_set_digests=digests,
    )


def _publish_date_universe(client, ctx: Mapping[str, Any], journal) -> dict[str, Any]:
    plan = ctx["plan"]
    baseline_codes = sorted(ctx["codes"])
    baseline_verified = False

    def assert_registry_baseline() -> None:
        nonlocal baseline_verified
        if baseline_verified:
            return
        _assert_quiet(client)
        current = _read_registry_rows(client, baseline_codes)
        if _registry_fingerprint(current) != ctx["registry_fingerprint"]:
            raise RuntimeError("date-universe registry changed after the apply preflight")
        baseline_verified = True

    def member_sink(partition: str, rows: Sequence[Mapping[str, Any]]) -> None:
        _assert_quiet(client)
        _insert_dict_rows(client, "stock.daily_date_universe_member", rows, MEMBER_COLUMNS)
        journal("member_partition_inserted", {"partition": partition, "rows": len(rows)})

    def member_loader(dataset_ids: Sequence[str]) -> list[dict[str, Any]]:
        result = []
        for offset in range(0, len(dataset_ids), 250):
            batch = dataset_ids[offset : offset + 250]
            result.extend(
                _dict_rows(
                    client,
                    f"SELECT {','.join(MEMBER_COLUMNS)} FROM stock.daily_date_universe_member "
                    f"WHERE dataset_id IN ({_in_list(batch)}) ORDER BY dataset_id,code,trade_date",
                )
            )
        return result

    def retire_sink(rows: Sequence[Mapping[str, Any]]) -> None:
        assert_registry_baseline()
        _insert_dict_rows(client, "stock.daily_date_universe_registry", rows, REGISTRY_COLUMNS)
        journal("old_registry_retired", {"rows": len(rows)})

    def registry_sink(rows: Sequence[Mapping[str, Any]]) -> None:
        assert_registry_baseline()
        _insert_dict_rows(client, "stock.daily_date_universe_registry", rows, REGISTRY_COLUMNS)
        journal("new_registry_activated", {"rows": len(rows)})

    def registry_loader(codes: Sequence[str]) -> list[dict[str, Any]]:
        return _read_registry_rows(client, codes)

    result = publish_tdx_date_universe(
        plan,
        initial_registry_rows=ctx["registry_rows"],
        member_partition_sink=member_sink,
        member_loader=member_loader,
        retire_registry_sink=retire_sink,
        registry_sink=registry_sink,
        registry_loader=registry_loader,
    )
    journal("date_universe_readback_verified", result)
    return result


def _check_fresh_before_append(client, ctx: Mapping[str, Any], args, plan_nonce: str) -> dict[str, Any]:
    identity = _identity(client)
    if int(identity["readonly"]) != 0:
        raise RuntimeError(f"ClickHouse principal became read-only before apply: {identity['user']}")
    if _inspect_schema(client)["schema_sha256"] != ctx["schema"]["schema_sha256"]:
        raise RuntimeError("ClickHouse schema changed since the approved preflight")
    quiet = _assert_quiet(client)
    bundle = load_reviewed_tdx_candidate_bundle(
        args.bundle,
        expected_candidate_sha256=ctx["bundle"]["candidate_sha256"],
        expected_crosscheck_sha256=args.expected_crosscheck_sha256,
        verify_source_files=True,
    )
    if bundle["candidate_sha256"] != ctx["bundle"]["candidate_sha256"]:
        raise RuntimeError("candidate CSV changed immediately before apply")
    if bundle["crosscheck_sha256"] != ctx["bundle"]["crosscheck_sha256"]:
        raise RuntimeError("independent cross-check changed immediately before apply")
    stock_rows, active_codes = _read_candidate_stocks(client, ctx["codes"])
    _check_lifecycle(bundle["candidate_rows"], stock_rows, active_codes)
    _read_calendar(client, ctx["target_dates"])
    status_rows = _read_status_rows(client, ctx["codes"])
    status_conflicts = status_conflicts_for_candidates(bundle["candidate_rows"], status_rows)
    if status_conflicts:
        raise RuntimeError(f"market-status conflict appeared before append: {status_conflicts[:20]}")
    overlap_rows, raw_overlap_duplicates = _read_formal_overlap(client, ctx["codes"])
    if raw_overlap_duplicates:
        raise RuntimeError("raw formal overlap duplicates appeared before append")
    validate_tdx_formal_overlap(
        bundle["candidate_rows"], ctx["source_rows"], overlap_rows,
        price_relative_tolerance=0.01, quantity_relative_tolerance=0.03,
    )
    current = _current_formal_snapshot(client, ctx["codes"], ctx["target_dates"])
    if _registry_fingerprint(_read_registry_rows(client, ctx["codes"])) != ctx["registry_fingerprint"]:
        raise RuntimeError("date-universe registry changed after preflight")
    current_registry = _read_registry_rows(client, ctx["codes"])
    current_members = _read_active_members(client, current_registry)
    if _member_fingerprint(current_members) != ctx["member_fingerprint"]:
        raise RuntimeError("date-universe active members changed after preflight")
    current_keys = set(current["raw_counts"]) | set(current["final_by_key"])
    if set(ctx["plan"].keys) & current_keys:
        raise RuntimeError("one or more approved exact keys appeared before publication")
    baseline_final = ctx["formal_baseline"]["final_by_key"]
    if set(current["final_by_key"]) != set(baseline_final):
        raise RuntimeError("preexisting formal target keys changed before publication")
    for key, row in baseline_final.items():
        if current["final_by_key"][key] != row:
            raise RuntimeError(f"preexisting formal target row changed before publication: {key}")
    collisions = _query_id_collisions(
        client, ctx["bundle"]["candidate_rows"], attempt_nonce=ctx["attempt_nonce"]
    )
    planned_ids = {int(value) for value in ctx["frame"]["id"].tolist()}
    if planned_ids & set(collisions):
        raise RuntimeError("stable IDs became occupied before formal append")
    _check_unused_dataset_ids(client, ctx["plan"].dataset_ids)
    return {
        "readonly": int(identity["readonly"]),
        "schema_sha256": ctx["schema"]["schema_sha256"],
        "concurrent_write_state": quiet,
        "candidate_sha256": bundle["candidate_sha256"],
        "crosscheck_sha256": bundle["crosscheck_sha256"],
        "raw_target_rows": len(current["raw_rows"]),
        "final_target_rows": len(current["final_rows"]),
        "registry_fingerprint": ctx["registry_fingerprint"],
        "member_fingerprint": ctx["member_fingerprint"],
        "plan_nonce": plan_nonce,
        "attempt_nonce": ctx["attempt_nonce"],
    }


def _verify_registry_restored(client, ctx: Mapping[str, Any]) -> None:
    current = _read_registry_rows(client, ctx["codes"])
    expected_active: dict[str, set[str]] = defaultdict(set)
    current_active: dict[str, set[str]] = defaultdict(set)
    baseline_by_id = {}
    for row in ctx["registry_rows"]:
        code = normalize_code(row["code"])
        dataset_id = str(row["dataset_id"])
        baseline_by_id[dataset_id] = row
        if int(row.get("is_active") or 0) == 1:
            expected_active[code].add(dataset_id)
    for row in current:
        if int(row.get("is_active") or 0) == 1:
            current_active[normalize_code(row["code"])].add(str(row["dataset_id"]))
    for code in ctx["codes"]:
        if expected_active.get(code, set()) != current_active.get(code, set()):
            raise RuntimeError(f"registry rollback active set mismatch for {code}")
    new_code_by_id = {payload.registry.dataset_id: payload.registry.code for payload in ctx["plan"].payloads}
    for dataset_id in ctx["plan"].dataset_ids:
        code = new_code_by_id[dataset_id]
        if dataset_id in current_active.get(code, set()):
            raise RuntimeError(f"new dataset remained active after rollback: {dataset_id}")
    for row in current:
        dataset_id = str(row.get("dataset_id") or "")
        old = baseline_by_id.get(dataset_id)
        if old and int(row.get("is_active") or 0) == 1:
            for field in ("date_set_sha256", "member_count", "source_sha256", "source_kind"):
                if str(row.get(field)) != str(old.get(field)):
                    raise RuntimeError(f"restored registry content mismatch for {dataset_id} field={field}")


def _rollback(client, ctx: Mapping[str, Any], journal, *, publication_started: bool,
              formal_write_started: bool, error: str) -> None:
    require_tdx_daily_distributed_lock()
    errors = []
    plan = ctx["plan"]
    if formal_write_started:
        try:
            frame_rows = ctx["frame"].to_dict(orient="records")
            ids = [int(row["id"]) for row in frame_rows]
            created_at_value = frame_rows[0]["created_at"]
            created_at = (
                created_at_value.to_pydatetime()
                if hasattr(created_at_value, "to_pydatetime")
                else created_at_value
            )
            sql = rollback_sql_for_ids_created_at(ids, created_at)
            observed = _dict_rows(
                client,
                "SELECT id,code,trade_date,created_at FROM stock.kline_daily "
                f"WHERE id IN ({','.join(str(value) for value in sorted(set(ids)))})",
            )
            expected_rows = [
                {"id": row["id"], "code": row["code"], "trade_date": row["trade_date"]}
                for row in frame_rows
            ]
            matching = validate_owned_kline_rows_for_rollback(
                expected_rows, observed, created_at=created_at
            )
            if matching:
                _assert_quiet(client)
                client.command(sql)
            remaining_raw, remaining_final = _read_formal_target(client, ctx["codes"], ctx["target_dates"])
            remaining_raw_keys = {
                (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
                for row in remaining_raw
            }
            remaining_final_keys = {
                (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
                for row in remaining_final
            }
            if remaining_raw_keys & set(plan.keys) or remaining_final_keys & set(plan.keys):
                raise RuntimeError(
                    "formal exact keys remain after rollback: "
                    f"raw={len(remaining_raw_keys & set(plan.keys))}, "
                    f"FINAL={len(remaining_final_keys & set(plan.keys))}"
                )
            journal("formal_rows_rolled_back", {"matching_rows_deleted": len(matching), "sql_sha256": hashlib.sha256(sql.encode()).hexdigest()})
        except Exception as exc:
            errors.append(f"formal-row rollback: {exc}")

    if publication_started:
        try:
            current_registry = _read_registry_rows(client, ctx["codes"])
            active_new_ids = [
                str(row["dataset_id"])
                for row in current_registry
                if str(row.get("dataset_id")) in set(plan.dataset_ids) and int(row.get("is_active") or 0) == 1
            ]
            restore = build_tdx_rollback_registry_rows(
                plan,
                active_new_dataset_ids=active_new_ids,
                rollback_at=datetime.now(BUSINESS_TZ).replace(microsecond=0),
            )
            _assert_quiet(client)
            _insert_dict_rows(client, "stock.daily_date_universe_registry", restore["deactivate_new_rows"], REGISTRY_COLUMNS)
            _insert_dict_rows(client, "stock.daily_date_universe_registry", restore["reactivate_old_rows"], REGISTRY_COLUMNS)
            _verify_registry_restored(client, ctx)
            journal(
                "registry_rollback_verified",
                {
                    "deactivated_new_rows": len(restore["deactivate_new_rows"]),
                    "reactivated_old_rows": len(restore["reactivate_old_rows"]),
                    "rollback_at": restore["rollback_at"],
                },
            )
            member_sql = member_rollback_sql_for_dataset_ids(restore["delete_member_dataset_ids"])
            _assert_quiet(client)
            client.command(member_sql)
            member_counts = _dict_rows(
                client,
                "SELECT count() AS row_count FROM stock.daily_date_universe_member "
                f"WHERE dataset_id IN ({_in_list(restore['delete_member_dataset_ids'])})",
            )
            remaining_members = int(member_counts[0]["row_count"]) if member_counts else 0
            if remaining_members:
                raise RuntimeError(f"new member rows remain after rollback: {remaining_members}")
            journal("member_rows_rolled_back", {"dataset_ids": len(restore["delete_member_dataset_ids"])})
        except Exception as exc:
            errors.append(f"date-universe rollback: {exc}")

    if errors:
        journal("rollback_failed", {"original_error": error, "errors": errors})
        raise RuntimeError("; ".join(errors))
    journal("rollback_completed", {"original_error": error})


def _apply(client, ctx: Mapping[str, Any], args, plan_nonce: str, journal_path: Path) -> dict[str, Any]:
    require_tdx_daily_distributed_lock()
    journal_state: dict[str, Any] = {
        "status": "in_progress",
        "plan_nonce": plan_nonce,
        "attempt_nonce": ctx["attempt_nonce"],
        "plan_sha256": ctx["plan"].plan_sha256,
        "candidate_sha256": ctx["bundle"]["candidate_sha256"],
        "crosscheck_sha256": ctx["bundle"]["crosscheck_sha256"],
        "exact_key_snapshot": ctx["report"]["exact_key_snapshot"],
        "steps": [],
    }

    def journal(stage: str, details: Mapping[str, Any] | None = None) -> None:
        journal_state["stage"] = stage
        journal_state["steps"].append(
            {"at": datetime.now(BUSINESS_TZ).isoformat(timespec="seconds"), "stage": stage, "details": dict(details or {})}
        )
        _atomic_json(journal_path, journal_state)

    def preflight_check():
        return _check_fresh_before_append(client, ctx, args, plan_nonce)

    def publish_registry():
        return _publish_date_universe(client, ctx, journal)

    def permit_builder():
        permit = _load_exact_permit(client, ctx["plan"])
        journal("exact_key_permit_readback", {"keys": len(permit.keys), "permit_digest": permit.permit_digest})
        return permit

    def append_formal_rows(permit: DailyExactKeyPermit) -> int:
        scope = DailyWriteScope(
            source=f"tdx_backfill:{ctx['plan'].plan_sha256}",
            current_stock_codes=frozenset(ctx["active_codes"]),
            historical_stock_codes=frozenset(),
            exact_key_permit=permit,
        )
        guarded = validate_daily_dataframe(ctx["frame"], scope=scope, operation="insert")
        _assert_formal_append_columns(guarded)
        # The driver encodes naive datetimes using machine-local wall-clock
        # rules while ClickHouse stores/renders DateTime in the server
        # timezone, so a naive value only round-trips when the runner host
        # shares the server timezone. Attach the server timezone explicitly:
        # aware datetimes encode to the exact instant regardless of the
        # machine timezone, making the stored wall clock equal the frame value.
        guarded = guarded.copy()
        guarded["created_at"] = guarded["created_at"].dt.tz_localize(
            ZoneInfo(ctx["server_timezone"])
        )
        guarded_keys = {
            (normalize_code(code), normalize_trade_date(day))
            for code, day in zip(guarded["code"].tolist(), guarded["trade_date"].tolist())
        }
        if guarded_keys != set(ctx["plan"].keys):
            raise RuntimeError("daily_write_guard normalized a different exact-key set")
        inserted = insert_kline_dataframe("1d", guarded, on_conflict="ignore", daily_scope=scope)
        return int(inserted)

    def verify_formal_rows():
        _assert_quiet(client)
        raw_rows, final_rows = _read_formal_target(client, ctx["codes"], ctx["target_dates"])
        return validate_kline_daily_exact_readback(
            ctx["frame"].to_dict("records"),
            raw_rows,
            final_rows,
            ctx["formal_baseline"]["final_rows"],
            existing_raw_counts=ctx["formal_baseline"]["raw_counts"],
        )

    def rollback(**kwargs):
        try:
            _rollback(client, ctx, journal, **kwargs)
        except Exception:
            journal_state["status"] = "rollback_failed"
            _atomic_json(journal_path, journal_state)
            raise
        journal_state["status"] = "rolled_back"
        _atomic_json(journal_path, journal_state)

    try:
        result = execute_tdx_daily_apply(
            ctx["plan"],
            preflight_check=preflight_check,
            publish_registry=publish_registry,
            permit_builder=permit_builder,
            append_formal_rows=append_formal_rows,
            verify_formal_rows=verify_formal_rows,
            rollback=rollback,
            journal=journal,
        )
    except Exception as exc:
        if journal_state["status"] == "in_progress":
            stage = str(journal_state.get("stage") or "")
            journal_state["status"] = "failed_before_writes" if stage in {"preflight_started", "preflight_passed"} else "rollback_incomplete"
        journal_state["error"] = f"{type(exc).__name__}: {exc}"
        _atomic_json(journal_path, journal_state)
        raise
    journal_state["status"] = "completed"
    journal_state["result"] = result
    journal("completed", {"inserted_rows": result["inserted_rows"]})
    journal_state["status"] = "completed"
    _atomic_json(journal_path, journal_state)
    return result


class _CompositeApplyLock:
    """Hold the host-local file lock and the database-scoped lock together."""

    def __init__(self, file_lock, database_lock):
        self._file_lock = file_lock
        self._database_lock = database_lock

    def release(self) -> None:
        errors = []
        try:
            self._database_lock.release()
        except Exception as exc:  # noqa: BLE001 - release must still try the file lock
            errors.append(f"database lock release: {exc}")
        try:
            self._file_lock.release()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"file lock release: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))


def _acquire_distributed_apply_lock(client):
    """Serialize local runners with the file lock, all DB writers with the CAS lock."""
    file_lock = TdxDailyBackfillApplyLock(
        runtime_path("daily_kline_coverage", "tdx_backfill_preflight_20261031", "tdx_daily_apply.lock")
    )
    file_lock.acquire()
    try:
        database_lock = acquire_tdx_daily_distributed_lock(
            client, note="apply_tdx_daily_backfill_bundle_20261031"
        )
    except Exception:
        file_lock.release()
        raise
    return _CompositeApplyLock(file_lock, database_lock)


def _parse_args(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only preflight (default)")
    mode.add_argument("--apply", action="store_true", help="writes while holding the file + database-scoped distributed locks")
    parser.add_argument("--plan-nonce", help="unique 1-64 character nonce from the approved dry-run")
    parser.add_argument("--expected-candidate-sha256")
    parser.add_argument("--expected-crosscheck-sha256", default=DEFAULT_CROSSCHECK_SHA256)
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--expected-write-rows", type=int)
    parser.add_argument("--expected-blocked-rows", type=int)
    parser.add_argument("--expected-payload-codes", type=int)
    args = parser.parse_args(argv)
    if args.apply:
        missing = [
            name for name in (
                "plan_nonce", "expected_candidate_sha256", "expected_crosscheck_sha256",
                "expected_plan_sha256", "expected_write_rows", "expected_blocked_rows", "expected_payload_codes",
            ) if getattr(args, name) is None
        ]
        if missing:
            parser.error("--apply requires " + ", ".join("--" + name.replace("_", "-") for name in missing))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.plan_nonce):
            parser.error("--plan-nonce must use 1-64 letters, digits, underscores, or hyphens")
    elif not args.plan_nonce:
        args.plan_nonce = uuid.uuid4().hex
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    client = None
    report_path = None
    journal_path = None
    apply_result = None
    apply_lock = None
    try:
        client = clickhouse_client()
        if args.apply:
            apply_lock = _acquire_distributed_apply_lock(client)
        ctx = _preflight(
            client,
            bundle_dir=args.bundle.resolve(),
            plan_nonce=args.plan_nonce,
            expected_candidate_sha256=args.expected_candidate_sha256,
            expected_crosscheck_sha256=args.expected_crosscheck_sha256,
            require_write_access=bool(args.apply),
        )
        report = ctx["report"]
        if args.apply:
            expected = {
                "plan_sha256": args.expected_plan_sha256,
                "planned_rows": args.expected_write_rows,
                "blocked_rows": args.expected_blocked_rows,
                "planned_codes": args.expected_payload_codes,
            }
            actual = {key: report[key] for key in expected}
            if actual != expected:
                raise RuntimeError(f"fresh apply preflight differs from approved dry-run: expected={expected}, actual={actual}")
            if report["candidate_sha256"] != args.expected_candidate_sha256:
                raise RuntimeError("fresh candidate SHA-256 differs from explicit apply approval")
            if report["crosscheck_sha256"] != args.expected_crosscheck_sha256:
                raise RuntimeError("fresh independent cross-check SHA-256 differs from explicit apply approval")
            incomplete = []
            for path in args.bundle.glob("tdx_apply_journal_*.json"):
                try:
                    prior = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    incomplete.append(path.name)
                    continue
                if prior.get("status") not in {"completed", "rolled_back", "failed_before_writes"}:
                    incomplete.append(path.name)
            if incomplete:
                raise RuntimeError(f"unresolved prior apply journals require manual recovery: {incomplete}")
            run_id = datetime.now(BUSINESS_TZ).strftime("%Y%m%d_%H%M%S_%f")
            journal_path = args.bundle / f"tdx_apply_journal_{run_id}.json"
            report_path = args.bundle / f"tdx_apply_result_{run_id}.json"
            apply_result = _apply(client, ctx, args, args.plan_nonce, journal_path)
            result = apply_result
            report.update(
                {
                    "status": "completed",
                    "write_performed": True,
                    "journal_path": str(journal_path),
                    "apply_result_path": str(report_path),
                    "apply_result": result,
                }
            )
        else:
            run_id = datetime.now(BUSINESS_TZ).strftime("%Y%m%d_%H%M%S_%f")
            report_path = args.bundle / f"tdx_apply_preflight_{run_id}.json"
        report["report_path"] = str(report_path)
        _atomic_json(report_path, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, default=str))
        return 0
    except Exception as exc:
        journal_status = None
        if journal_path is not None and journal_path.is_file():
            try:
                journal_status = json.loads(journal_path.read_text(encoding="utf-8")).get("status")
            except Exception:
                journal_status = "unreadable"
        did_mutate = bool(apply_result) or journal_status in {
            "in_progress", "completed", "rolled_back", "rollback_failed", "rollback_incomplete"
        }
        blocked = {
            "status": (
                "apply_failed_rolled_back" if journal_status == "rolled_back"
                else "completed_reporting_failed" if journal_status == "completed"
                else "recovery_required" if journal_status in {"rollback_failed", "rollback_incomplete", "in_progress", "unreadable"}
                else "failed_closed"
            ),
            "write_performed": did_mutate,
            "database_final_state": (
                "completed" if journal_status == "completed"
                else "rolled_back" if journal_status == "rolled_back"
                else "unknown" if did_mutate else "unchanged"
            ),
            "journal_status": journal_status,
            "journal_path": str(journal_path) if journal_path else None,
            "plan_nonce": args.plan_nonce,
            "error": f"{type(exc).__name__}: {exc}",
            "failed_at": datetime.now(BUSINESS_TZ).isoformat(timespec="seconds"),
        }
        try:
            error_path = report_path or args.bundle / f"tdx_apply_preflight_failed_{datetime.now(BUSINESS_TZ).strftime('%Y%m%d_%H%M%S_%f')}.json"
            blocked["report_path"] = str(error_path)
            _atomic_json(error_path, blocked)
        except Exception:
            pass
        print(json.dumps(blocked, ensure_ascii=False, sort_keys=True, indent=2, default=str))
        return 2
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            if apply_lock is not None:
                apply_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

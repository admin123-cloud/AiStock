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
from datetime import date, datetime
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

from utils.clickhouse_schema import (
    KLINE_DAILY_ENGINE_FULL_PREFIX,
    KLINE_DAILY_PARTITION_KEY,
    KLINE_DAILY_REQUIRED_COLUMN_TYPES,
    KLINE_DAILY_SORTING_KEY,
    MARKET_TABLE_DDL,
    STARTUP_TABLE_CONTRACTS,
    _NEEQ_LEGACY_BJ_PATTERN,
    _NEEQ_SECURITY_CODE_PATTERN,
    _compact_sql_fragment,
)
from utils.daily_date_universe import (
    DATE_UNIVERSE_COMPLETE,
    DATE_UNIVERSE_STATES,
    normalize_code,
)
from utils.market_warehouse import clickhouse_client
from utils.security_market_policy import clickhouse_normalized_code_expression, is_neeq_security_code


DATABASE = "stock"
FORMAL_TABLE = "kline_daily"
REGISTRY_TABLE = "daily_date_universe_registry"
MEMBER_TABLE = "daily_date_universe_member"
READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 240, "max_threads": 2}
EXPECTED_PAYLOAD_FIELDS = {
    "registry": {
        "dataset_id", "code", "security_type", "identity_status", "source_kind",
        "source_ref", "source_sha256", "coverage_start", "coverage_end",
        "completeness_state", "date_set_sha256", "member_count", "published_at", "is_active",
    },
    "member": {
        "dataset_id", "code", "trade_date", "member_ordinal", "date_set_sha256", "published_at",
    },
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


def _parse_payload_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _strict_json_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _is_sha256_hex(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or not ("T" in value or " " in value):
        return False
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _payload_stats(path: Path, kind: str) -> dict[str, Any]:
    meta = _file_meta(path)
    result: dict[str, Any] = {
        "kind": kind,
        **meta,
        "row_count": 0,
        "parse_errors": 0,
        "missing_fields": 0,
        "empty_code_count": 0,
        "duplicate_code_count": 0,
        "duplicate_key_count": 0,
        "duplicate_date_count": 0,
        "invalid_trade_date_count": 0,
        "invalid_dates_container_count": 0,
        "empty_dates_list_count": 0,
        "date_list_order_violation_count": 0,
        "neeq_code_count": 0,
        "noncanonical_code_count": 0,
        "invalid_metadata_count": 0,
    }
    if not meta["exists"]:
        result["status"] = "missing"
        return result
    expected = EXPECTED_PAYLOAD_FIELDS[kind]
    code_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    seen_codes: set[str] = set()
    first_key: tuple[str, str] | None = None
    last_key: tuple[str, str] | None = None
    order_violations = 0
    adjacent_duplicates = 0
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
            if not isinstance(row, dict):
                result["parse_errors"] += 1
                continue
            result["row_count"] += 1
            if not expected.issubset(row):
                result["missing_fields"] += 1
            raw_code = row.get("code")
            code = str(raw_code or "").strip()
            if not code:
                result["empty_code_count"] += 1
            elif not isinstance(raw_code, str) or raw_code != normalize_code(raw_code):
                result["noncanonical_code_count"] += 1
            canonical_code = normalize_code(code) if code else ""
            if canonical_code and is_neeq_security_code(canonical_code):
                result["neeq_code_count"] += 1
            code_counts[code] += 1
            seen_codes.add(code)
            if kind == "registry":
                state = str(row.get("completeness_state") or "").strip().lower()
                member_count = _strict_json_int(row.get("member_count"))
                is_active = _strict_json_int(row.get("is_active"), minimum=0)
                coverage_start = row.get("coverage_start")
                coverage_end = row.get("coverage_end")
                coverage_start_date = _parse_payload_date(coverage_start) if coverage_start is not None else None
                coverage_end_date = _parse_payload_date(coverage_end) if coverage_end is not None else None
                if (
                    not str(row.get("dataset_id") or "").strip()
                    or not str(row.get("security_type") or "").strip()
                    or not str(row.get("identity_status") or "").strip()
                    or not str(row.get("source_kind") or "").strip()
                    or not str(row.get("source_ref") or "").strip()
                    or not _is_sha256_hex(row.get("source_sha256"))
                    or not _is_sha256_hex(row.get("date_set_sha256"))
                    or member_count is None
                    or is_active not in {0, 1}
                    or not _is_iso_datetime(row.get("published_at"))
                    or state not in DATE_UNIVERSE_STATES
                    or (state == DATE_UNIVERSE_COMPLETE and (member_count <= 0 or coverage_start_date is None or coverage_end_date is None))
                    or (state != DATE_UNIVERSE_COMPLETE and (member_count != 0 or coverage_start is not None or coverage_end is not None))
                ):
                    result["invalid_metadata_count"] += 1
            elif kind == "member":
                if (
                    not str(row.get("dataset_id") or "").strip()
                    or not _is_sha256_hex(row.get("date_set_sha256"))
                    or _strict_json_int(row.get("member_ordinal"), minimum=1) is None
                    or not _is_iso_datetime(row.get("published_at"))
                ):
                    result["invalid_metadata_count"] += 1
            row_key: tuple[str, str] | None = None
            if kind == "accepted":
                row_key = (code, "")
                dates = row.get("dates")
                if not isinstance(dates, list):
                    result["invalid_dates_container_count"] += 1
                    dates = []
                elif not dates:
                    result["empty_dates_list_count"] += 1
                total_dates += len(dates)
                previous_date: date | None = None
                seen_dates: set[date] = set()
                for raw_date in dates:
                    parsed_date = _parse_payload_date(raw_date)
                    if parsed_date is None:
                        result["invalid_trade_date_count"] += 1
                        continue
                    if parsed_date in seen_dates:
                        result["duplicate_date_count"] += 1
                    seen_dates.add(parsed_date)
                    if previous_date is not None and parsed_date < previous_date:
                        result["date_list_order_violation_count"] += 1
                    previous_date = parsed_date
            elif kind == "registry":
                status_counts[str(row.get("completeness_state") or "")] += 1
                row_key = (code, "")
            else:
                parsed_date = _parse_payload_date(row.get("trade_date"))
                if parsed_date is None:
                    result["invalid_trade_date_count"] += 1
                else:
                    row_key = (code, parsed_date.isoformat())
            if row_key is None:
                continue
            if first_key is None:
                first_key = row_key
            if last_key is not None:
                if kind == "member" and row_key == last_key:
                    adjacent_duplicates += 1
                if row_key < last_key:
                    order_violations += 1
            last_key = row_key
    duplicate_code_count = (
        sum(max(count - 1, 0) for count in code_counts.values())
        if kind in {"registry", "accepted"}
        else 0
    )
    result.update(
        {
            "code_count": len(seen_codes),
            "code_count_by_row": dict(sorted(code_counts.items())) if kind == "registry" else None,
            "completeness_state_counts": dict(sorted(status_counts.items())) if kind == "registry" else None,
            "total_dates": total_dates if kind == "accepted" else None,
            "first_key": first_key,
            "last_key": last_key,
            "adjacent_duplicate_count": adjacent_duplicates if kind == "member" else None,
            "duplicate_key_count": adjacent_duplicates if kind == "member" else 0,
            "duplicate_code_count": duplicate_code_count,
            "order_violation_count": order_violations,
            "status": "blocked"
            if (
                result["row_count"] == 0
                or result["parse_errors"]
                or result["missing_fields"]
                or result["empty_code_count"]
                or result["neeq_code_count"]
                or result["noncanonical_code_count"]
                or result["invalid_metadata_count"]
                or duplicate_code_count
                or adjacent_duplicates
                or order_violations
                or result["invalid_trade_date_count"]
                or result["invalid_dates_container_count"]
                or result["empty_dates_list_count"]
                or result["duplicate_date_count"]
                or result["date_list_order_violation_count"]
            )
            else "ready",
        }
    )
    return result


def _iter_payload_rows(path: Path):
    with _open_jsonl(path) as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                yield row


def _registry_member_payload_compatibility(
    *, registry_candidate: Path, member_candidate: Path, accepted_manifest: Path
) -> dict[str, Any]:
    registry_by_code: dict[str, dict[str, Any]] = {}
    duplicate_registry_codes = 0
    for row in _iter_payload_rows(registry_candidate):
        raw_code = row.get("code")
        code = normalize_code(raw_code) if raw_code else ""
        if not code:
            continue
        if code in registry_by_code:
            duplicate_registry_codes += 1
            continue
        registry_by_code[code] = {
            "dataset_id": str(row.get("dataset_id") or "").strip(),
            "date_set_sha256": str(row.get("date_set_sha256") or ""),
            "member_count": _strict_json_int(row.get("member_count")),
            "completeness_state": str(row.get("completeness_state") or "").strip().lower(),
            "is_active": _strict_json_int(row.get("is_active"), minimum=0),
            "published_at": row.get("published_at"),
            "coverage_start": _parse_payload_date(row.get("coverage_start")) if row.get("coverage_start") is not None else None,
            "coverage_end": _parse_payload_date(row.get("coverage_end")) if row.get("coverage_end") is not None else None,
            "observed_member_count": 0,
            "first_member_date": None,
            "last_member_date": None,
            "member_digest": hashlib.sha256(),
        }

    orphan_member_count = 0
    dataset_id_mismatch_count = 0
    digest_mismatch_count = 0
    published_at_mismatch_count = 0
    member_ordinal_mismatch_count = 0
    for row in _iter_payload_rows(member_candidate):
        raw_code = row.get("code")
        code = normalize_code(raw_code) if raw_code else ""
        registry_row = registry_by_code.get(code)
        if registry_row is None:
            orphan_member_count += 1
            continue
        if str(row.get("dataset_id") or "").strip() != registry_row["dataset_id"]:
            dataset_id_mismatch_count += 1
            continue
        if row.get("published_at") != registry_row["published_at"]:
            published_at_mismatch_count += 1
        if str(row.get("date_set_sha256") or "") != registry_row["date_set_sha256"]:
            digest_mismatch_count += 1
        trade_day = _parse_payload_date(row.get("trade_date"))
        if trade_day is None:
            continue
        expected_ordinal = registry_row["observed_member_count"] + 1
        registry_row["observed_member_count"] = expected_ordinal
        if _strict_json_int(row.get("member_ordinal"), minimum=1) != expected_ordinal:
            member_ordinal_mismatch_count += 1
        if registry_row["first_member_date"] is None:
            registry_row["first_member_date"] = trade_day
        registry_row["last_member_date"] = trade_day
        registry_row["member_digest"].update(f"{code}\t{trade_day.isoformat()}\n".encode("utf-8"))

    member_count_mismatch_count = 0
    coverage_mismatch_count = 0
    completeness_state_member_violation_count = 0
    for row in registry_by_code.values():
        expected_count = row["member_count"]
        observed_count = row["observed_member_count"]
        if expected_count is None or observed_count != expected_count:
            member_count_mismatch_count += 1
        if row["member_digest"].hexdigest() != row["date_set_sha256"]:
            digest_mismatch_count += 1
        if row["completeness_state"] == DATE_UNIVERSE_COMPLETE:
            if row["first_member_date"] != row["coverage_start"] or row["last_member_date"] != row["coverage_end"]:
                coverage_mismatch_count += 1
        elif observed_count:
            completeness_state_member_violation_count += 1

    def candidate_member_keys():
        for row in _iter_payload_rows(member_candidate):
            raw_code = row.get("code")
            code = normalize_code(raw_code) if raw_code else ""
            registry_row = registry_by_code.get(code)
            if (
                registry_row is None
                or registry_row["completeness_state"] != DATE_UNIVERSE_COMPLETE
                or registry_row["is_active"] != 1
                or str(row.get("dataset_id") or "").strip() != registry_row["dataset_id"]
            ):
                continue
            trade_day = _parse_payload_date(row.get("trade_date"))
            if trade_day is not None:
                yield code, trade_day.isoformat()

    candidate_iterator = iter(candidate_member_keys())
    current_candidate = next(candidate_iterator, None)
    accepted_keys_missing_from_candidate = 0
    keys_without_active_complete_registry = 0
    accepted_key_count = 0
    matched_accepted_key_count = 0
    for row in _iter_payload_rows(accepted_manifest):
        raw_code = row.get("code")
        code = normalize_code(raw_code) if raw_code else ""
        if not code or not isinstance(row.get("dates"), list):
            continue
        registry_row = registry_by_code.get(code)
        for raw_day in row["dates"]:
            trade_day = _parse_payload_date(raw_day)
            if trade_day is None:
                continue
            accepted_key_count += 1
            if (
                registry_row is None
                or registry_row["completeness_state"] != DATE_UNIVERSE_COMPLETE
                or registry_row["is_active"] != 1
            ):
                keys_without_active_complete_registry += 1
            accepted_key = (code, trade_day.isoformat())
            while current_candidate is not None and current_candidate < accepted_key:
                current_candidate = next(candidate_iterator, None)
            if current_candidate == accepted_key:
                matched_accepted_key_count += 1
                current_candidate = next(candidate_iterator, None)
            else:
                accepted_keys_missing_from_candidate += 1

    registry_member_blockers = []
    for name, count in (
        ("duplicate_registry_codes", duplicate_registry_codes),
        ("orphan_member_rows", orphan_member_count),
        ("dataset_id_mismatches", dataset_id_mismatch_count),
        ("digest_mismatches", digest_mismatch_count),
        ("published_at_mismatches", published_at_mismatch_count),
        ("member_count_mismatches", member_count_mismatch_count),
        ("member_ordinal_mismatches", member_ordinal_mismatch_count),
        ("coverage_mismatches", coverage_mismatch_count),
        ("members_on_incomplete_registry_rows", completeness_state_member_violation_count),
    ):
        if count:
            registry_member_blockers.append(name)
    accepted_blockers = []
    if accepted_keys_missing_from_candidate:
        accepted_blockers.append("accepted_keys_missing_from_candidate_members")
    if keys_without_active_complete_registry:
        accepted_blockers.append("accepted_keys_without_active_complete_registry")
    return {
        "registry_member_consistency": {
            "status": "blocked" if registry_member_blockers else "ready",
            "registry_code_count": len(registry_by_code),
            "duplicate_registry_code_count": duplicate_registry_codes,
            "orphan_member_count": orphan_member_count,
            "dataset_id_mismatch_count": dataset_id_mismatch_count,
            "digest_mismatch_count": digest_mismatch_count,
            "published_at_mismatch_count": published_at_mismatch_count,
            "member_count_mismatch_count": member_count_mismatch_count,
            "member_ordinal_mismatch_count": member_ordinal_mismatch_count,
            "coverage_mismatch_count": coverage_mismatch_count,
            "completeness_state_member_violation_count": completeness_state_member_violation_count,
            "blockers": registry_member_blockers,
        },
        "accepted_member_compatibility": {
            "status": "blocked" if accepted_blockers else "ready",
            "relationship": "accepted exact keys must be a subset of active complete candidate members; candidate-only keys remain unaccepted",
            "accepted_key_count": accepted_key_count,
            "matched_accepted_key_count": matched_accepted_key_count,
            "accepted_keys_missing_from_candidate": accepted_keys_missing_from_candidate,
            "keys_without_active_complete_registry": keys_without_active_complete_registry,
            "blockers": accepted_blockers,
        },
    }


def _ddl_for(name: str) -> str:
    for ddl in MARKET_TABLE_DDL:
        if re.search(rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(name)}\b", ddl, re.IGNORECASE):
            return " ".join(line.strip() for line in ddl.strip().splitlines())
    return ""


def _candidate_kline_column_types(live_columns: list[dict[str, Any]]) -> dict[str, str]:
    """Return the candidate types after the documented created_at normalization."""
    candidate: dict[str, str] = {}
    for row in live_columns:
        name = str(row.get("name") or "")
        type_name = str(row.get("type") or "")
        if not name or not type_name:
            continue
        candidate[name] = "DateTime" if name == "created_at" else type_name
    return candidate


def _source_date_constraint_compatibility(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    raw_first_date = aggregate.get("first_trade_date")
    if isinstance(raw_first_date, datetime):
        first_date = raw_first_date.date()
    elif isinstance(raw_first_date, date):
        first_date = raw_first_date
    else:
        try:
            first_date = date.fromisoformat(str(raw_first_date)[:10])
        except (TypeError, ValueError):
            first_date = None
    try:
        raw_row_count = aggregate.get("rows")
        row_count = int(raw_row_count) if raw_row_count is not None else None
    except (TypeError, ValueError):
        row_count = None
    floor = date(2000, 1, 1)
    if row_count is None or row_count <= 0:
        reason = "source_row_count_missing_or_empty"
    elif first_date is None:
        reason = "source_min_trade_date_missing_or_unparseable"
    elif first_date < floor:
        reason = "source_contains_rows_before_candidate_date_constraint"
    else:
        reason = None
    return {
        "status": "blocked" if reason else "ready",
        "first_trade_date": first_date.isoformat() if first_date else None,
        "minimum_allowed_trade_date": floor.isoformat(),
        "source_row_count": row_count,
        "blocker": reason,
    }


def _source_row_identity_compatibility(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    counts: dict[str, int | None] = {}
    for name in ("rows", "unique_keys", "unique_ids", "null_created_at", "nonnull_created_at"):
        raw_value = aggregate.get(name)
        try:
            counts[name] = int(raw_value) if raw_value is not None else None
        except (TypeError, ValueError):
            counts[name] = None
    rows = counts["rows"]
    unique_keys = counts["unique_keys"]
    unique_ids = counts["unique_ids"]
    null_created_at = counts["null_created_at"]
    nonnull_created_at = counts["nonnull_created_at"]
    blockers: list[str] = []
    if rows is None or rows <= 0:
        blockers.append("source_row_count_missing_or_empty")
    if rows is None or unique_keys is None:
        blockers.append("source_unique_key_count_missing")
    elif rows != unique_keys:
        blockers.append(
            "source_rows_exceed_unique_exact_keys" if rows > unique_keys else "source_unique_exact_keys_exceed_rows"
        )
    if rows is None or unique_ids is None:
        blockers.append("source_unique_id_count_missing")
    elif rows != unique_ids:
        blockers.append("source_rows_exceed_unique_ids" if rows > unique_ids else "source_unique_ids_exceed_rows")
    if (
        rows is None
        or null_created_at is None
        or nonnull_created_at is None
        or null_created_at + nonnull_created_at != rows
    ):
        blockers.append("source_created_at_counts_do_not_match_rows")
    return {
        "status": "blocked" if blockers else "ready",
        "counts": counts,
        "blockers": blockers,
    }


def _source_security_constraint_compatibility(aggregate: Mapping[str, Any]) -> dict[str, Any]:
    row_count = _strict_json_int(aggregate.get("rows"))
    rejected_rows = _strict_json_int(aggregate.get("target_neeq_rejected_rows"))
    blockers: list[str] = []
    if rejected_rows is None:
        blockers.append("source_neeq_constraint_count_missing_or_invalid")
    elif row_count is None or rejected_rows > row_count:
        blockers.append("source_neeq_constraint_count_incompatible_with_row_count")
    elif rejected_rows:
        blockers.append("source_rows_violate_target_neeq_constraints")
    return {
        "status": "blocked" if blockers else "ready",
        "forbidden_neeq_rows": rejected_rows,
        "source_row_count": row_count,
        "blockers": blockers,
    }


def _source_copy_compatibility(
    live_columns: list[dict[str, Any]], aggregate: Mapping[str, Any]
) -> dict[str, Any]:
    expected = dict(KLINE_DAILY_REQUIRED_COLUMN_TYPES)
    candidate = _candidate_kline_column_types(live_columns)
    source_types = {
        str(row.get("name") or ""): str(row.get("type") or "")
        for row in live_columns
        if row.get("name")
    }
    names = [str(row.get("name") or "") for row in live_columns if row.get("name")]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    missing = sorted(set(expected) - set(candidate))
    mismatches = {
        name: {"expected": expected[name], "candidate": candidate[name]}
        for name in sorted(set(expected) & set(candidate))
        if candidate[name] != expected[name]
    }
    unexpected = sorted(set(candidate) - set(expected))
    source_created_at_type = source_types.get("created_at")
    unsupported_created_at_source_type = (
        source_created_at_type not in {"DateTime", "Nullable(DateTime)"}
    )
    date_compatibility = _source_date_constraint_compatibility(aggregate)
    row_identity_compatibility = _source_row_identity_compatibility(aggregate)
    security_constraint_compatibility = _source_security_constraint_compatibility(aggregate)
    blockers = []
    if missing:
        blockers.append("source_missing_required_columns")
    if mismatches:
        blockers.append("source_column_types_do_not_match_target")
    if unexpected:
        blockers.append("source_has_unexpected_columns")
    if duplicates:
        blockers.append("source_has_duplicate_column_metadata")
    if unsupported_created_at_source_type:
        blockers.append("source_created_at_type_not_supported")
    if date_compatibility["status"] != "ready":
        blockers.append(str(date_compatibility["blocker"]))
    if row_identity_compatibility["status"] != "ready":
        blockers.extend(row_identity_compatibility["blockers"])
    if security_constraint_compatibility["status"] != "ready":
        blockers.extend(security_constraint_compatibility["blockers"])
    return {
        "status": "blocked" if blockers else "ready",
        "blockers": blockers,
        "expected_column_types": expected,
        "candidate_column_types": candidate,
        "missing_columns": missing,
        "type_mismatches": mismatches,
        "unexpected_columns": unexpected,
        "duplicate_columns": duplicates,
        "source_created_at_type": source_created_at_type,
        "source_created_at_type_supported": not unsupported_created_at_source_type,
        "date_constraint_compatibility": date_compatibility,
        "row_identity_compatibility": row_identity_compatibility,
        "security_constraint_compatibility": security_constraint_compatibility,
    }


def _rename_candidate_ddl(source_table: str) -> str:
    candidate_table = f"stock.{source_table}__migration_candidate"
    canonical_ddl = _ddl_for(source_table)
    if not canonical_ddl:
        raise RuntimeError(f"{source_table}:canonical_ddl_missing")
    source_prefix = f"CREATE TABLE IF NOT EXISTS {source_table}"
    if canonical_ddl.lower().count(source_prefix.lower()) != 1:
        raise RuntimeError(f"{source_table}:canonical_ddl_could_not_be_renamed")
    candidate_ddl = canonical_ddl.replace(
        source_prefix, f"CREATE TABLE {candidate_table}", 1
    )
    created_table = candidate_ddl.split("(", 1)[0].split()[-1]
    if created_table.replace("`", "").lower() != candidate_table.lower():
        raise RuntimeError(f"{source_table}:candidate_table_name_mismatch")
    return candidate_ddl


def _candidate_kline_ddl() -> str:
    """Build the migration target from the canonical bootstrap DDL."""
    return _rename_candidate_ddl(FORMAL_TABLE)


def _candidate_registry_ddl(table: str) -> str:
    if table not in {REGISTRY_TABLE, MEMBER_TABLE}:
        raise ValueError(f"not an allowed date-universe candidate table: {table}")
    return _rename_candidate_ddl(table)


def _ddl_table_body(ddl: str) -> str:
    create_index = ddl.lower().find("create table")
    if create_index < 0:
        raise ValueError("CREATE TABLE clause is missing")
    opening = ddl.find("(", create_index + len("create table"))
    if opening < 0:
        raise ValueError("CREATE TABLE column body is missing")
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(ddl):
        char = ddl[index]
        if quote is not None:
            if ord(char) == 92 and quote in {"'", '"'}:
                index += 2
                continue
            if char == quote:
                if index + 1 < len(ddl) and ddl[index + 1] == quote and quote != "`":
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return ddl[opening + 1:index]
        index += 1
    raise ValueError("CREATE TABLE column body has unbalanced parentheses")


def _split_ddl_top_level(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            if ord(char) == 92 and quote in {"'", '"'}:
                index += 2
                continue
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote and quote != "`":
                    index += 2
                    continue
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("DDL column list has unbalanced parentheses")
        elif char == "," and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
        index += 1
    if depth != 0 or quote is not None:
        raise ValueError("DDL column list has unbalanced parentheses or quotes")
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _ddl_type_token(value: str) -> str:
    upper_value = value.upper()
    type_end = len(value)
    for marker in (" DEFAULT ", " MATERIALIZED ", " ALIAS ", " CODEC ", " TTL "):
        position = upper_value.find(marker)
        if position >= 0:
            type_end = min(type_end, position)
    return value[:type_end].strip().rstrip(",")


def _candidate_ddl_column_types(ddl: str) -> tuple[dict[str, str], list[str]]:
    columns: dict[str, str] = {}
    errors: list[str] = []
    non_column_prefixes = ("CONSTRAINT", "INDEX", "PRIMARY", "UNIQUE", "PROJECTION")
    for clause in _split_ddl_top_level(_ddl_table_body(ddl)):
        if clause.upper().startswith(non_column_prefixes):
            continue
        pieces = clause.split(None, 1)
        if len(pieces) != 2:
            errors.append("unrecognized_table_clause")
            continue
        name = pieces[0].strip("`")
        if not name or not (name[0].isalpha() or name[0] == "_") or not all(
            character.isalnum() or character == "_" for character in name
        ):
            errors.append("unrecognized_table_clause")
            continue
        if name in columns:
            errors.append(f"duplicate_column:{name}")
        columns[name] = _ddl_type_token(pieces[1])
    return columns, errors


def _candidate_contract_errors(table_name: str, candidate_ddl: str) -> list[str]:
    contract = next((item for item in STARTUP_TABLE_CONTRACTS if item.name == table_name), None)
    if contract is None:
        return ["startup_contract_missing"]
    errors: list[str] = []
    expected_target = f"stock.{table_name}__migration_candidate"
    opening = candidate_ddl.find("(")
    ddl_prefix = candidate_ddl[:opening] if opening >= 0 else candidate_ddl
    prefix_parts = ddl_prefix.split()
    actual_target = prefix_parts[-1].replace("`", "") if prefix_parts else ""
    if actual_target.lower() != expected_target.lower():
        errors.append("candidate_table_name_mismatch")

    upper_ddl = candidate_ddl.upper()
    engine_position = upper_ddl.find("ENGINE")
    engine_tail = candidate_ddl[engine_position + len("ENGINE"):].strip() if engine_position >= 0 else ""
    if engine_tail.startswith("="):
        engine_tail = engine_tail[1:].strip()
    engine = engine_tail.split(None, 1)[0] if engine_tail else ""
    if not engine.lower().startswith(contract.engine_prefix.lower()):
        errors.append("engine_mismatch")
    if contract.engine_full_prefix and not engine.lower().startswith(contract.engine_full_prefix.lower()):
        errors.append("engine_full_prefix_mismatch")

    partition_position = upper_ddl.find("PARTITION BY")
    order_position = upper_ddl.find("ORDER BY", partition_position + 1) if partition_position >= 0 else -1
    if partition_position >= 0:
        partition_start = partition_position + len("PARTITION BY")
        partition_end = order_position if order_position >= 0 else len(candidate_ddl)
        actual_partition = candidate_ddl[partition_start:partition_end].strip()
    else:
        actual_partition = ""
    if _normalise(actual_partition) != _normalise(contract.partition_key):
        errors.append("partition_key_mismatch")

    order_position = upper_ddl.find("ORDER BY")
    actual_sorting = candidate_ddl[order_position + len("ORDER BY"):].strip() if order_position >= 0 else ""
    if _normalise(actual_sorting) != _normalise(contract.sorting_key):
        errors.append("sorting_key_mismatch")

    expected_types = dict(contract.required_column_types)
    if not set(contract.required_columns).issubset(expected_types):
        errors.append("startup_contract_column_types_incomplete")
    actual_types, parse_errors = _candidate_ddl_column_types(candidate_ddl)
    errors.extend(parse_errors)
    if set(actual_types) != set(expected_types):
        missing = sorted(set(expected_types) - set(actual_types))
        unexpected = sorted(set(actual_types) - set(expected_types))
        errors.append(f"column_set_mismatch:missing={missing},unexpected={unexpected}")
    for column, expected_type in expected_types.items():
        actual_type = actual_types.get(column)
        if actual_type != expected_type:
            errors.append(f"column_type_mismatch:{column}:{actual_type!r}!={expected_type!r}")

    compact_ddl = _compact_sql_fragment(candidate_ddl).lower()
    for constraint_name, expression in contract.required_check_constraints:
        expected_check = _compact_sql_fragment(f"CONSTRAINT {constraint_name} {expression}").lower()
        if expected_check not in compact_ddl:
            errors.append(f"check_constraint_mismatch:{constraint_name}")
    return errors


def _candidate_schema_proposals() -> tuple[dict[str, Any], dict[str, str | None]]:
    specs = (
        (FORMAL_TABLE, _candidate_kline_ddl),
        (REGISTRY_TABLE, lambda: _candidate_registry_ddl(REGISTRY_TABLE)),
        (MEMBER_TABLE, lambda: _candidate_registry_ddl(MEMBER_TABLE)),
    )
    proposals: dict[str, str | None] = {}
    table_status: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    for table_name, builder in specs:
        try:
            candidate_ddl = builder()
        except Exception as exc:
            candidate_ddl = None
            errors = [f"{table_name}:{type(exc).__name__}:{exc}"]
        else:
            errors = [f"{table_name}:{error}" for error in _candidate_contract_errors(table_name, candidate_ddl)]
        proposals[table_name] = candidate_ddl
        table_status[table_name] = {
            "status": "blocked" if errors else "ready",
            "target_table": f"stock.{table_name}__migration_candidate",
            "errors": errors,
        }
        blockers.extend(errors)
    return (
        {
            "status": "blocked" if blockers else "ready",
            "source": "canonical_bootstrap_ddl_validated_against_startup_contracts",
            "engine_full_prefix": KLINE_DAILY_ENGINE_FULL_PREFIX,
            "partition_key": KLINE_DAILY_PARTITION_KEY,
            "sorting_key": KLINE_DAILY_SORTING_KEY,
            "column_types": dict(KLINE_DAILY_REQUIRED_COLUMN_TYPES),
            "blockers": blockers,
            "tables": table_status,
        },
        proposals,
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
    normalized_code = clickhouse_normalized_code_expression("code")
    try:
        select_query_count += 1
        aggregate = _dict_rows(
            client.query(
                "SELECT count() AS rows,uniqExact((code,trade_date)) AS unique_keys,"
                "uniqExact(id) AS unique_ids,countIf(isNull(created_at)) AS null_created_at,"
                "countIf(isNotNull(created_at)) AS nonnull_created_at,"
                f"countIf(match({normalized_code},{_quote(_NEEQ_SECURITY_CODE_PATTERN)}) "
                f"OR match({normalized_code},{_quote(_NEEQ_LEGACY_BJ_PATTERN)})) AS target_neeq_rejected_rows,"
                "min(trade_date) AS first_trade_date,max(trade_date) AS last_trade_date FROM stock.kline_daily",
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
    engine = str(formal.get("engine") or "")
    engine_full = str(formal.get("engine_full") or "")
    version_match = re.search(r"ReplacingMergeTree\s*\(([^)]*)\)", engine_full, re.IGNORECASE)
    exact_present = all(name in by_table for name in names)
    schema_problems: list[str] = []
    if FORMAL_TABLE not in by_table:
        schema_problems.append("kline_daily:table_missing")
    else:
        if engine.lower() != "replacingmergetree":
            schema_problems.append("kline_daily:engine_incompatible")
        if _normalise(formal.get("partition_key")) != _normalise(KLINE_DAILY_PARTITION_KEY):
            schema_problems.append("kline_daily:partition_key_incompatible")
        if not version_match:
            schema_problems.append("kline_daily:replacing_version_expression_missing")
        elif _normalise(version_match.group(1)) != _normalise("created_at"):
            schema_problems.append("kline_daily:version_expression_incompatible")
        if _normalise(formal.get("sorting_key")) != _normalise(KLINE_DAILY_SORTING_KEY):
            schema_problems.append("kline_daily:sorting_key_incompatible")
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
            parsed_summary = json.loads(candidate_summary.read_text(encoding="utf-8"))
            if isinstance(parsed_summary, dict):
                summary["candidate_summary"] = parsed_summary
            else:
                summary["parse_error"] = "candidate summary must be a JSON object"
        except Exception as exc:
            summary["parse_error"] = f"{type(exc).__name__}:{exc}"
    summary_values = summary["candidate_summary"]
    expected_registry_rows = _strict_json_int(summary_values.get("candidate_code_count"))
    expected_member_rows = _strict_json_int(summary_values.get("candidate_member_total_across_source_backed_codes"))
    expected_accepted_codes = _strict_json_int(summary_values.get("accepted_code_count"))
    expected_accepted_members = _strict_json_int(summary_values.get("accepted_member_count"))
    comparisons = {
        "registry_row_count_matches_summary": expected_registry_rows is not None and registry["row_count"] == expected_registry_rows,
        "member_row_count_matches_summary": expected_member_rows is not None and member["row_count"] == expected_member_rows,
        "accepted_code_count_matches_summary": expected_accepted_codes is not None and accepted["code_count"] == expected_accepted_codes,
        "accepted_member_count_matches_summary": expected_accepted_members is not None and accepted["total_dates"] == expected_accepted_members,
        "accepted_manifest_is_present": accepted["exists"],
        "candidate_summary_is_present": summary_meta["exists"] and summary["parse_error"] is None,
    }
    cross_file = _registry_member_payload_compatibility(
        registry_candidate=registry_candidate,
        member_candidate=member_candidate,
        accepted_manifest=accepted_manifest,
    )
    status = (
        "ready"
        if all(comparisons.values())
        and registry["status"] == "ready"
        and member["status"] == "ready"
        and accepted["status"] == "ready"
        and cross_file["registry_member_consistency"]["status"] == "ready"
        and cross_file["accepted_member_compatibility"]["status"] == "ready"
        else "blocked"
    )
    return {
        "status": status,
        "registry_candidate": registry,
        "member_candidate": member,
        "candidate_summary": summary,
        "accepted_manifest": accepted,
        "comparisons": comparisons,
        **cross_file,
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
    source_copy_compatibility = _source_copy_compatibility(live_columns, aggregate)
    candidate_schema_validation, candidate_ddls = _candidate_schema_proposals()
    parts = schema.get("parts") or []
    live_table_row = next((row for row in schema.get("tables", []) if str(row.get("name")) == FORMAL_TABLE), {})
    expected = {
        "kline_daily_contract_ddl": candidate_ddls.get(FORMAL_TABLE),
        "registry_contract_ddl": candidate_ddls.get(REGISTRY_TABLE),
        "member_contract_ddl": candidate_ddls.get(MEMBER_TABLE),
    }
    source_column_names = {str(row.get("name")) for row in live_columns if row.get("name")}
    target_columns = [name for name, _ in KLINE_DAILY_REQUIRED_COLUMN_TYPES]
    copy_columns = [name for name in target_columns if name in source_column_names]
    plan_ready = (
        schema.get("status") == "ready"
        and payloads.get("status") == "ready"
        and source_copy_compatibility.get("status") == "ready"
        and candidate_schema_validation.get("status") == "ready"
    )
    copy_blockers = list(source_copy_compatibility.get("blockers") or [])
    if schema.get("status") != "ready":
        copy_blockers.append("live_schema_not_ready")
    if payloads.get("status") != "ready":
        copy_blockers.append("local_payload_checks_not_ready")
    if source_copy_compatibility.get("status") != "ready" and not copy_blockers:
        copy_blockers.append("source_copy_compatibility_not_ready")
    if candidate_schema_validation.get("status") != "ready":
        copy_blockers.extend(candidate_schema_validation.get("blockers") or ["candidate_schema_not_ready"])
    select_columns = ", ".join(
        "ifNull(created_at,toDateTime('1970-01-01 00:00:00')) AS created_at" if name == "created_at" else name
        for name in copy_columns
    )
    copy_plan = {
        "status": "ready" if plan_ready else "blocked",
        "blockers": list(dict.fromkeys(copy_blockers)),
        "source_table": "stock.kline_daily",
        "candidate_table": "stock.kline_daily__migration_candidate",
        "target_columns": target_columns,
        "source_columns_selected": copy_columns,
        "select_expression": (
            f"SELECT {select_columns} FROM stock.kline_daily ORDER BY toYYYYMM(trade_date), code, trade_date"
            if plan_ready
            else None
        ),
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
    blocked = not plan_ready
    return {
        "schema_version": 1,
        "readonly": True,
        "status": "blocked" if blocked else "ready_for_explicit_migration_review",
        "gate": "blocked_live_schema_payload_or_source_copy_compatibility" if blocked else "review_only_no_commands_issued",
        "live_schema_readback": schema,
        "candidate_schema_validation": candidate_schema_validation,
        "source_copy_compatibility": source_copy_compatibility,
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

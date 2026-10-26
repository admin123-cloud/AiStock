"""Classify index residuals and build an exact-key permit/write dry-run.

This script is deliberately read-only.  It consumes the local 9-code
reconciliation candidates, classifies residual OHLC/amount/volume differences,
reads live ClickHouse schema metadata and registry/member state, and records a
future raw/FINAL write-back contract.  It never creates tables, mutates rows,
or publishes a date-universe permit.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.daily_date_universe import build_exact_key_permit_from_rows, permit_sha256
from utils.market_warehouse import clickhouse_client


SOURCE_TABLE = "stock.wstock_daily_tdx_evidence_20261014_v2"
FORMAL_TABLE_NAME = "kline_daily"
FORMAL_TABLE = "stock.kline_daily"
REGISTRY_TABLE = "daily_date_universe_registry"
MEMBER_TABLE = "daily_date_universe_member"
STABLE_CODES = (
    "000001.SH",
    "000016.SH",
    "000300.SH",
    "000680.SH",
    "000688.SH",
    "000852.SH",
    "000905.SH",
    "399001.SZ",
    "399006.SZ",
)
FIELDS = ("open", "high", "low", "close", "volume", "amount")
PRICE_FIELDS = ("open", "high", "low", "close")
READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 180, "max_threads": 2}
PRICE_DISPLAY_TOLERANCE = 0.0050001
AMOUNT_RELATIVE_TOLERANCE = 1e-4
VOLUME_TOLERANCE = 0.01


def _open_jsonl(path: Path, mode: str = "rt"):
    return gzip.open(path, mode, encoding="utf-8") if path.suffix == ".gz" else path.open(mode, encoding="utf-8")


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with _open_jsonl(path) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dict_rows(result: Any) -> list[dict[str, Any]]:
    names = list(getattr(result, "column_names", ()) or ())
    return [dict(zip(names, row)) for row in getattr(result, "result_rows", ())]


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(values),
        "min": ordered[0],
        "median": ordered[len(ordered) // 2],
        "p95": ordered[int(0.95 * (len(ordered) - 1))],
        "max": ordered[-1],
    }


def classify_residual(row: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a candidate row without changing its values."""

    formal = row.get("formal_values") or {}
    candidate = row.get("candidate_values") or {}
    residual = tuple(str(item) for item in row.get("residual_substantive_fields") or ())
    residual_set = set(residual)
    price_deltas = [
        abs(float(candidate[field]) - float(formal[field]))
        for field in PRICE_FIELDS
        if field in residual_set and _float(candidate.get(field)) is not None and _float(formal.get(field)) is not None
    ]
    max_price_delta = max(price_deltas) if price_deltas else None
    if price_deltas:
        price_class = "price_display_rounding" if max_price_delta <= PRICE_DISPLAY_TOLERANCE else "price_material"
    else:
        price_class = "no_price_residual"

    amount_delta = None
    amount_relative_delta = None
    if "amount" in residual_set and _float(candidate.get("amount")) is not None and _float(formal.get("amount")) is not None:
        amount_delta = abs(float(candidate["amount"]) - float(formal["amount"]))
        amount_relative_delta = amount_delta / max(abs(float(formal["amount"])), 1.0)
        amount_class = "amount_near_equal" if amount_relative_delta <= AMOUNT_RELATIVE_TOLERANCE else "amount_material"
    else:
        amount_class = "no_amount_residual"

    volume_delta = None
    if "volume" in residual_set and _float(candidate.get("volume")) is not None and _float(formal.get("volume")) is not None:
        volume_delta = abs(float(candidate["volume"]) - float(formal["volume"]))
        volume_class = "volume_residual_after_div100"
    else:
        volume_class = "volume_aligned"

    flags = [
        flag
        for flag in (volume_class, price_class, amount_class)
        if flag not in {"volume_aligned", "no_price_residual", "no_amount_residual"}
    ]
    if not residual:
        residual_class = "none"
        value_gate = "eligible_volume_only"
    else:
        residual_class = "+".join(flags) or "residual_unclassified"
        value_gate = "blocked_residual_fields"
    return {
        "residual_fields": list(residual),
        "residual_class": residual_class,
        "value_gate": value_gate,
        "max_price_abs_delta": max_price_delta,
        "amount_abs_delta": amount_delta,
        "amount_relative_delta": amount_relative_delta,
        "volume_abs_delta_after_div100": volume_delta,
        "price_class": price_class,
        "amount_class": amount_class,
        "volume_class": volume_class,
    }


def load_and_classify_candidates(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    class_counts: Counter[str] = Counter()
    code_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    field_counts: Counter[str] = Counter()
    source_counts: Counter[tuple[str, str]] = Counter()
    price_deltas: list[float] = []
    amount_deltas: list[float] = []
    amount_relative: list[float] = []
    volume_deltas: list[float] = []
    lineage_missing = 0
    for candidate in _iter_jsonl(path):
        classification = classify_residual(candidate)
        row = {
            "code": candidate.get("code"),
            "trade_date": str(candidate.get("trade_date") or "")[:10],
            "source_table": candidate.get("source_table") or SOURCE_TABLE,
            "source_file_sha": candidate.get("source_file_sha"),
            "source_row": candidate.get("source_row"),
            **classification,
        }
        rows.append(row)
        class_counts[classification["residual_class"]] += 1
        code_counts[row["code"]][classification["residual_class"]] += 1
        field_counts.update(classification["residual_fields"])
        source_counts[(str(row["code"]), str(row["source_file_sha"] or ""))] += 1
        if row["source_file_sha"] in (None, "") or row["source_row"] is None:
            lineage_missing += 1
        if classification["max_price_abs_delta"] is not None:
            price_deltas.append(float(classification["max_price_abs_delta"]))
        if classification["amount_abs_delta"] is not None:
            amount_deltas.append(float(classification["amount_abs_delta"]))
        if classification["amount_relative_delta"] is not None:
            amount_relative.append(float(classification["amount_relative_delta"]))
        if classification["volume_abs_delta_after_div100"] is not None:
            volume_deltas.append(float(classification["volume_abs_delta_after_div100"]))
    summary = {
        "candidate_rows": len(rows),
        "residual_rows": sum(1 for row in rows if row["residual_class"] != "none"),
        "no_residual_rows": sum(1 for row in rows if row["residual_class"] == "none"),
        "residual_class_counts": dict(sorted(class_counts.items())),
        "field_counts": dict(sorted(field_counts.items())),
        "by_code": {code: dict(sorted(counter.items())) for code, counter in sorted(code_counts.items())},
        "source_file_sha_pairs": [
            {"code": code, "source_file_sha": file_sha, "rows": count}
            for (code, file_sha), count in sorted(source_counts.items())
        ],
        "lineage_missing_rows": lineage_missing,
        "delta_statistics": {
            "price_max_abs_delta": _stats(price_deltas),
            "amount_abs_delta": _stats(amount_deltas),
            "amount_relative_delta": _stats(amount_relative),
            "volume_abs_delta_after_div100": _stats(volume_deltas),
        },
        "classification_policy": {
            "price_display_rounding_abs_tolerance": PRICE_DISPLAY_TOLERANCE,
            "amount_near_equal_relative_tolerance": AMOUNT_RELATIVE_TOLERANCE,
            "volume_tolerance": VOLUME_TOLERANCE,
            "residual_rows_are_not_write_authorized": True,
        },
    }
    return rows, summary


def _normalise_key(value: Any) -> str:
    return re.sub(r"[\s`()]+", "", str(value or "")).lower()


def read_schema_preflight(client: Any) -> dict[str, Any]:
    """Read live table/column metadata and compare it to the write contract."""

    table_names = (FORMAL_TABLE_NAME, REGISTRY_TABLE, MEMBER_TABLE)
    names_sql = ",".join(_quote(name) for name in table_names)
    table_result = client.query(
        "SELECT name,engine,engine_full,partition_key,sorting_key,primary_key,create_table_query "
        f"FROM system.tables WHERE database='stock' AND name IN ({names_sql}) ORDER BY name",
        settings=READONLY_SETTINGS,
    )
    table_rows = _dict_rows(table_result)
    column_result = client.query(
        "SELECT table,name,type,default_kind,default_expression,is_in_partition_key,is_in_sorting_key,is_in_primary_key "
        f"FROM system.columns WHERE database='stock' AND table IN ({names_sql}) ORDER BY table,position",
        settings=READONLY_SETTINGS,
    )
    column_rows = _dict_rows(column_result)
    by_table = {str(row.get("name")): row for row in table_rows}
    columns_by_table: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in column_rows:
        columns_by_table[str(row.get("table"))].append(row)

    expected_columns = {
        FORMAL_TABLE_NAME: {"code", "trade_date", "open", "high", "low", "close", "volume", "amount", "created_at", "id"},
        REGISTRY_TABLE: {"dataset_id", "code", "completeness_state", "date_set_sha256", "is_active"},
        MEMBER_TABLE: {"dataset_id", "code", "trade_date", "date_set_sha256"},
    }
    problems: list[str] = []
    checks: dict[str, Any] = {}
    for table in table_names:
        live = by_table.get(table)
        actual_columns = {str(row.get("name")) for row in columns_by_table.get(table, [])}
        table_check = {
            "present": live is not None,
            "engine": live.get("engine") if live else None,
            "engine_full": live.get("engine_full") if live else None,
            "partition_key": live.get("partition_key") if live else None,
            "sorting_key": live.get("sorting_key") if live else None,
            "primary_key": live.get("primary_key") if live else None,
            "required_columns": sorted(expected_columns[table]),
            "actual_columns": sorted(actual_columns),
            "missing_columns": sorted(expected_columns[table] - actual_columns),
        }
        checks[table] = table_check
        if live is None:
            problems.append(f"{table}: table_missing")
            continue
        if table == FORMAL_TABLE_NAME:
            if not str(live.get("engine") or "").lower().startswith("replacingmergetree"):
                problems.append(f"{table}: engine_incompatible")
            if _normalise_key(live.get("partition_key")) != _normalise_key("toYYYYMM(trade_date)"):
                problems.append(f"{table}: partition_key_incompatible")
            if _normalise_key(live.get("sorting_key")) != _normalise_key("(code,trade_date)"):
                problems.append(f"{table}: sorting_key_incompatible")
            if table_check["missing_columns"]:
                problems.append(f"{table}: required_columns_missing")
            engine_full = str(live.get("engine_full") or "")
            version_match = re.search(r"ReplacingMergeTree\s*\(([^)]*)\)", engine_full, re.IGNORECASE)
            table_check["replacing_version_expression"] = version_match.group(1).strip() if version_match else None
            table_check["version_column_present"] = bool(table_check["replacing_version_expression"])
            if not table_check["version_column_present"]:
                problems.append(f"{table}: replacing_version_expression_missing")
    return {
        "status": "ready" if not problems else "blocked",
        "problems": problems,
        "tables": checks,
        "query_settings": READONLY_SETTINGS,
        "production_mutation": {"ddl": False, "dml": False, "mutation": False},
    }


def _read_formal_baseline(client: Any, keys: set[tuple[str, str]]) -> dict[str, Any]:
    if not keys:
        return {"status": "empty", "raw": {}, "final": {}}
    codes = sorted({code for code, _ in keys})
    code_sql = ",".join(_quote(code) for code in codes)
    columns = "code,trade_date,open,high,low,close,volume,amount,created_at,id"
    data: dict[str, dict[str, Any]] = {}
    for label, suffix in (("raw", ""), ("final", " FINAL")):
        result = client.query(
            f"SELECT {columns} FROM {FORMAL_TABLE}{suffix} WHERE code IN ({code_sql}) ORDER BY code,trade_date",
            settings=READONLY_SETTINGS,
        )
        rows = _dict_rows(result)
        hits = [row for row in rows if (str(row.get("code")), str(row.get("trade_date"))[:10]) in keys]
        by_key: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in hits:
            by_key[(str(row.get("code")), str(row.get("trade_date"))[:10])].append(row)
        duplicate_extra = sum(max(0, len(items) - 1) for items in by_key.values())
        snapshot_payload = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n"
            for key in sorted(by_key)
            for row in by_key[key]
        )
        data[label] = {
            "all_rows_read": len(rows),
            "candidate_rows_read": len(hits),
            "candidate_unique_keys": len(by_key),
            "candidate_missing_keys": len(keys - set(by_key)),
            "candidate_duplicate_extra_rows": duplicate_extra,
            "snapshot_sha256": hashlib.sha256(snapshot_payload.encode("utf-8")).hexdigest(),
            "sample_keys": [f"{code}@{trade_date}" for code, trade_date in sorted(keys)[:10]],
        }
    data["raw_final_key_set_equal"] = (
        data["raw"]["candidate_unique_keys"] == data["final"]["candidate_unique_keys"]
        and data["raw"]["candidate_missing_keys"] == data["final"]["candidate_missing_keys"]
    )
    data["status"] = "complete"
    return data


def _read_exact_key_permit_state(client: Any, keys: set[tuple[str, str]], schema: Mapping[str, Any]) -> dict[str, Any]:
    codes = sorted({code for code, _ in keys})
    code_sql = ",".join(_quote(code) for code in codes)
    tables = schema.get("tables") or {}
    registry = tables.get(REGISTRY_TABLE) or {}
    member = tables.get(MEMBER_TABLE) or {}
    requested_digest = permit_sha256(keys, ())
    state: dict[str, Any] = {
        "status": "blocked",
        "requested_key_count": len(keys),
        "requested_code_count": len(codes),
        "requested_key_digest": requested_digest,
        "registry_table": REGISTRY_TABLE,
        "member_table": MEMBER_TABLE,
        "registry_rows": [],
        "member_rows_read": 0,
        "permit_digest": None,
        "query_settings": READONLY_SETTINGS,
    }
    if not registry.get("present") or not member.get("present"):
        missing = [name for name, check in ((REGISTRY_TABLE, registry), (MEMBER_TABLE, member)) if not check.get("present")]
        state["block_reason"] = "registry_member_table_missing"
        state["missing_tables"] = missing
        state["per_key_state"] = "blocked_registry_member_schema_missing"
        return state
    try:
        registry_result = client.query(
            f"SELECT dataset_id,code,completeness_state,date_set_sha256,is_active FROM {REGISTRY_TABLE} FINAL WHERE code IN ({code_sql}) ORDER BY code",
            settings=READONLY_SETTINGS,
        )
        registry_rows = _dict_rows(registry_result)
        state["registry_rows"] = registry_rows
        member_rows: list[dict[str, Any]] = []
        key_list = sorted(keys)
        chunk_size = 500
        for start in range(0, len(key_list), chunk_size):
            chunk = key_list[start : start + chunk_size]
            key_sql = ",".join(f"({_quote(code)},toDate({_quote(day)}))" for code, day in chunk)
            member_result = client.query(
                f"SELECT dataset_id,code,trade_date,date_set_sha256 FROM {MEMBER_TABLE} WHERE (code,trade_date) IN ({key_sql})",
                settings=READONLY_SETTINGS,
            )
            member_rows.extend(_dict_rows(member_result))
        state["member_rows_read"] = len(member_rows)
        permit = build_exact_key_permit_from_rows(
            keys,
            registry_rows=registry_rows,
            member_rows=member_rows,
            source="index-volume-reconciliation-dry-run",
        )
        state.update({"status": "would_allow", "permit_digest": permit.permit_digest, "per_key_state": "would_allow"})
    except Exception as exc:
        state["block_reason"] = f"exact_key_readback_failed:{type(exc).__name__}"
        state["error"] = str(exc)
        state["per_key_state"] = "blocked_exact_key_readback"
    return state


def build_write_after_readback_plan(*, key_count: int, classification: Mapping[str, Any], schema: Mapping[str, Any], permit: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "blocked_before_write",
        "write_executed": False,
        "candidate_key_count": key_count,
        "value_gate": {
            "eligible_volume_only_rows": classification.get("no_residual_rows", 0),
            "residual_rows_blocked": classification.get("residual_rows", 0),
            "policy": "only rows with no residual fields could enter a future value permit; all residual classes remain review/blocked",
        },
        "schema_preflight": {
            "status": schema.get("status"),
            "problems": schema.get("problems", []),
            "required_before_write": [
                "kline_daily partition_key must be toYYYYMM(trade_date)",
                "kline_daily ReplacingMergeTree must expose an explicit version expression or an approved deterministic replacement contract",
                "daily_date_universe_registry and daily_date_universe_member must exist and be readable",
            ],
        },
        "exact_key_permit_dry_run": {
            "status": permit.get("status"),
            "requested_key_count": permit.get("requested_key_count"),
            "requested_key_digest": permit.get("requested_key_digest"),
            "block_reason": permit.get("block_reason"),
        },
        "pre_write_snapshot_contract": {
            "baseline": baseline,
            "chunk_size": 500,
            "exact_key_predicate_template": "(code,trade_date) IN ((<code_1>,toDate(<date_1>)),...)",
            "read_raw_exact_keys": "SELECT code,trade_date,open,high,low,close,volume,amount,created_at,id FROM stock.kline_daily WHERE (code,trade_date) IN (<chunk>) ORDER BY code,trade_date",
            "read_final_exact_keys": "SELECT code,trade_date,open,high,low,close,volume,amount,created_at,id FROM stock.kline_daily FINAL WHERE (code,trade_date) IN (<chunk>) ORDER BY code,trade_date",
            "aggregate_raw_exact_keys": "SELECT count() AS rows, uniqExact((code,trade_date)) AS unique_keys FROM stock.kline_daily WHERE (code,trade_date) IN (<chunk>)",
            "aggregate_final_exact_keys": "SELECT count() AS rows, uniqExact((code,trade_date)) AS unique_keys FROM stock.kline_daily FINAL WHERE (code,trade_date) IN (<chunk>)",
            "record_source_lineage": "source_table + source_file_sha + source_row + code + trade_date",
        },
        "future_write_contract_not_executed": {
            "scope": "exact candidate keys only, after schema and permit gates",
            "payload": "preserve formal OHLC/amount and replace volume only for value-gate eligible rows",
            "mutation": "no production mutation issued in this dry-run",
        },
        "post_write_raw_readback": {
            "query": "run read_raw_exact_keys and aggregate_raw_exact_keys for every same chunk",
            "checks": [
                "raw candidate row count equals expected keys",
                "uniqExact(code,trade_date) equals expected keys",
                "no out-of-scope key changed",
                "only approved volume values changed; all preserved columns match pre-write snapshot",
            ],
        },
        "post_write_final_readback": {
            "query": "run read_final_exact_keys and aggregate_final_exact_keys for every same chunk",
            "checks": [
                "FINAL candidate row count and key set equal expected keys",
                "FINAL volume equals candidate volume within 0.01",
                "FINAL preserved fields equal pre-write values",
                "raw and FINAL snapshots converge after replacement",
            ],
        },
        "rollback_contract": {
            "requires": "the immutable pre-write raw/FINAL snapshot and the same exact-key permit",
            "scope": "same code/trade_date keys only",
            "verification": "repeat raw and FINAL readback plus snapshot hash comparison",
        },
        "production_mutation": {"ddl": False, "dml": False, "mutation": False, "registry_publish": False},
    }


def run_dry_run(*, candidates: Path, output_details: Path, output_summary: Path, output_permit: Path, output_plan: Path) -> dict[str, Any]:
    rows, classification = load_and_classify_candidates(candidates)
    keys = {(str(row["code"]), str(row["trade_date"])[:10]) for row in rows}
    client = clickhouse_client()
    schema = read_schema_preflight(client)
    permit = _read_exact_key_permit_state(client, keys, schema)
    baseline = _read_formal_baseline(client, keys)

    output_details.parent.mkdir(parents=True, exist_ok=True)
    with _open_jsonl(output_details, "wt") as handle:
        for row in rows:
            out = dict(row)
            out.update(
                {
                    "schema_preflight_status": schema["status"],
                    "exact_key_permit_status": permit["status"],
                    "write_status": "not_authorized",
                    "production_mutation": {"ddl": False, "dml": False, "mutation": False},
                }
            )
            handle.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")

    summary = {
        "schema_version": 1,
        "readonly": True,
        "input_candidates": str(candidates),
        "output_details": str(output_details),
        "candidate_scope": {"codes": list(STABLE_CODES), "key_count": len(keys)},
        "classification": classification,
        "schema_preflight": schema,
        "exact_key_permit_dry_run": permit,
        "formal_before_write_baseline": baseline,
        "gate": "blocked_before_write" if schema["status"] != "ready" or permit["status"] != "would_allow" or classification["residual_rows"] else "ready_for_explicit_review",
        "write_executed": False,
        "production_mutation": {"ddl": False, "dml": False, "mutation": False, "registry_publish": False},
    }
    output_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    permit_summary = {
        "schema_version": 1,
        "readonly": True,
        "input_candidates": str(candidates),
        "requested_key_count": len(keys),
        "requested_key_digest": permit.get("requested_key_digest"),
        "exact_key_permit_dry_run": permit,
        "schema_preflight_status": schema["status"],
        "value_gate": {"eligible_volume_only_rows": classification["no_residual_rows"], "residual_rows_blocked": classification["residual_rows"]},
        "write_authorized": False,
        "production_mutation": {"ddl": False, "dml": False, "mutation": False, "registry_publish": False},
    }
    output_permit.write_text(json.dumps(permit_summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    output_plan.write_text(
        json.dumps(
            build_write_after_readback_plan(
                key_count=len(keys), classification=classification, schema=schema, permit=permit, baseline=baseline
            ),
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output-details", type=Path, required=True)
    parser.add_argument("--output-summary", type=Path, required=True)
    parser.add_argument("--output-permit", type=Path, required=True)
    parser.add_argument("--output-plan", type=Path, required=True)
    args = parser.parse_args()
    summary = run_dry_run(
        candidates=args.candidates,
        output_details=args.output_details,
        output_summary=args.output_summary,
        output_permit=args.output_permit,
        output_plan=args.output_plan,
    )
    print(json.dumps({"gate": summary["gate"], "candidate_scope": summary["candidate_scope"], "classification": summary["classification"], "schema_status": summary["schema_preflight"]["status"], "permit_status": summary["exact_key_permit_dry_run"]["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

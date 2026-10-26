"""Build a full-table, read-only daily closure snapshot in range batches.

The accepted THS date-membership manifest is used as local registry/member
evidence.  Each batch obtains an exact-key permit from that evidence, reads a
bounded code range from ClickHouse, and compares the formal values with the
native THS export.  The online registry/member tables are probed read-only;
when they are absent the report stays blocked even though the local permit is
valid.  No ClickHouse DDL, DML, mutation, or production registry write is
performed.
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
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_daily_closure_snapshot import (
    READONLY_SETTINGS,
    _load_accepted_manifest,
    _manifest_reader,
    _parse_ths_source_rows,
    _read_status_rows,
    _dict_rows,
)
from utils.daily_closure_snapshot import (
    CLOSURE_BLOCKED,
    CLOSURE_HEALTHY,
    CLOSURE_REVIEW,
    build_daily_closure_snapshot_from_reader,
    classify_daily_rows,
)
from utils.daily_date_universe import normalize_code, normalize_trade_date
from utils.market_warehouse import clickhouse_client


def _quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _read_formal_batch(
    client: Any,
    *,
    codes: Iterable[str],
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    code_list = sorted({normalize_code(code) for code in codes})
    if not code_list:
        return []
    code_sql = ",".join(_quote(code) for code in code_list)
    result = client.query(
        f"""
        SELECT code,trade_date,open,high,low,close,volume,amount
        FROM stock.kline_daily FINAL
        WHERE code IN ({code_sql})
          AND trade_date >= toDate({_quote(start_date.isoformat())})
          AND trade_date <= toDate({_quote(end_date.isoformat())})
        ORDER BY trade_date,code
        """,
        settings=READONLY_SETTINGS,
    )
    return _dict_rows(result)


def _read_global_formal_stats(
    client: Any,
    *,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    result = client.query(
        f"""
        SELECT
            count() AS formal_rows,
            uniqExact((code,trade_date)) AS formal_key_count,
            uniqExact(code) AS formal_code_count,
            min(trade_date) AS formal_first_trade_date,
            max(trade_date) AS formal_latest_trade_date
        FROM stock.kline_daily FINAL
        WHERE trade_date >= toDate({_quote(start_date.isoformat())})
          AND trade_date <= toDate({_quote(end_date.isoformat())})
        """,
        settings=READONLY_SETTINGS,
    )
    row = result.first_row
    return {
        "formal_rows": int(row[0] or 0),
        "formal_key_count": int(row[1] or 0),
        "formal_code_count": int(row[2] or 0),
        "formal_first_trade_date": str(row[3]) if row[3] else None,
        "formal_latest_trade_date": str(row[4]) if row[4] else None,
    }


def _read_anomaly_rows(
    client: Any,
    *,
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    result = client.query(
        f"""
        SELECT code,trade_date,open,high,low,close,volume,amount
        FROM stock.kline_daily FINAL
        WHERE trade_date >= toDate({_quote(start_date.isoformat())})
          AND trade_date <= toDate({_quote(end_date.isoformat())})
          AND (
               high < greatest(open, close)
            OR low > least(open, close)
            OR high < low
            OR (amount > 0 AND volume <= 0)
            OR (volume > 0 AND amount <= 0)
          )
        ORDER BY trade_date,code
        """,
        settings=READONLY_SETTINGS,
    )
    return _dict_rows(result)


def _read_tdx_anomaly_source_rows(client: Any, anomaly_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Read exact-code/date TDX evidence for current anomalies only."""

    rows = list(anomaly_rows)
    codes = sorted({normalize_code(row["code"]) for row in rows})
    if not codes:
        return []
    code_sql = ",".join(_quote(code) for code in codes)
    type_result = client.query(
        f"SELECT code,type FROM stock.stocks FINAL WHERE code IN ({code_sql})",
        settings=READONLY_SETTINGS,
    )
    type_by_code = {normalize_code(row[0]): str(row[1] or "").strip().lower() for row in type_result.result_rows}
    result = client.query(
        f"""
        SELECT code,trade_date,open,high,low,close,volume_shares,amount
        FROM stock.wstock_daily_tdx_evidence_20261014_v2
        WHERE code IN ({code_sql})
        """,
        settings=READONLY_SETTINGS,
    )
    output: list[dict[str, Any]] = []
    for code, trade_date, op, high, low, close, volume_shares, amount in result.result_rows:
        canonical_code = normalize_code(code)
        raw_volume = None if volume_shares is None else float(volume_shares)
        security_type = type_by_code.get(canonical_code, "")
        if raw_volume is None:
            mapped_volume = None
            volume_unit_mapping = "unknown_null"
        elif security_type == "index":
            mapped_volume = raw_volume
            volume_unit_mapping = "index_raw"
        else:
            mapped_volume = raw_volume / 100.0
            volume_unit_mapping = "stock_shares_div_100"
        output.append(
            {
                "code": canonical_code,
                "trade_date": trade_date,
                "open": op,
                "high": high,
                "low": low,
                "close": close,
                "volume": mapped_volume,
                "amount": amount,
                "source_kind": "wstock_tdx_evidence",
                "volume_unit_mapping": volume_unit_mapping,
            }
        )
    return output


def _online_registry_member_state(client: Any) -> dict[str, Any]:
    result = client.query(
        """
        SELECT name
        FROM system.tables
        WHERE database = 'stock'
          AND name IN ('daily_date_universe_registry','daily_date_universe_member')
        ORDER BY name
        """,
        settings={"readonly": 1, "max_execution_time": 60, "max_threads": 1},
    )
    names = [str(row[0]) for row in result.result_rows]
    return {
        "present_tables": names,
        "complete": set(names)
        == {"daily_date_universe_registry", "daily_date_universe_member"},
    }


def _status_rank(value: str) -> int:
    return {CLOSURE_HEALTHY: 0, CLOSURE_REVIEW: 1, CLOSURE_BLOCKED: 2}.get(value, 2)


def _worst_status(values: Iterable[str]) -> str:
    statuses = list(values)
    return max(statuses, key=_status_rank, default=CLOSURE_HEALTHY)


def _merge_source_gates(gates: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(gates)
    mismatched = sum(int(row.get("mismatched_rows") or 0) for row in rows)
    missing = sum(int(row.get("missing_source_keys") or 0) for row in rows)
    formal_compared = sum(int(row.get("formal_rows_compared") or 0) for row in rows)
    source_compared = sum(int(row.get("source_rows_compared") or 0) for row in rows)
    status = _worst_status(str(row.get("status") or CLOSURE_REVIEW) for row in rows)
    reason = "source_value_mismatch" if mismatched else "source_key_missing" if missing else "matched"
    return {
        "status": status,
        "reason": reason,
        "formal_rows_compared": formal_compared,
        "source_rows_compared": source_compared,
        "missing_source_keys": missing,
        "mismatched_rows": mismatched,
        "batch_count": len(rows),
        "independent_source": "accepted_ths_native_export",
        "unit_mapping": "THS shares / 100 -> kline_daily lots; amount unchanged",
        "semantic_limit": "accepted manifest certifies date membership; value/unit certification remains review-only",
    }


def _merge_registry_gate(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    online_state: Mapping[str, Any],
) -> dict[str, Any]:
    rows = list(snapshots)
    permit_digests = [
        str(row.get("permit_digest") or "")
        for row in rows
        if str(row.get("permit_digest") or "")
    ]
    local_status = _worst_status(
        str((row.get("gates", {}).get("registry_member_exact_key") or {}).get("status") or CLOSURE_BLOCKED)
        for row in rows
    )
    status = CLOSURE_HEALTHY if online_state.get("complete") else CLOSURE_BLOCKED
    if _status_rank(local_status) > _status_rank(status):
        status = local_status
    digest = hashlib.sha256("\n".join(sorted(permit_digests)).encode("utf-8")).hexdigest() if permit_digests else None
    return {
        "status": status,
        "reason": "registry_member_online_readback_verified" if online_state.get("complete") else "online_registry_member_tables_missing",
        "online_registry_member": dict(online_state),
        "local_accepted_registry_readback": "healthy" if rows else "missing",
        "batch_count": len(rows),
        "permit_digest": digest,
        "batch_permit_digest_count": len(permit_digests),
        "errors": [] if online_state.get("complete") else ["production_registry_member_not_deployed"],
    }


def _merge_coverage_gate(
    *,
    expected_key_count: int,
    formal_key_count: int,
    formal_present_count: int,
    expected_latest: date | None,
    formal_latest: date | None,
    duplicate_key_count: int = 0,
    missing_key_samples: Iterable[str] = (),
    outside_key_samples: Iterable[str] = (),
) -> dict[str, Any]:
    missing = max(0, expected_key_count - formal_present_count)
    outside = max(0, formal_key_count - formal_present_count)
    errors: list[str] = []
    if missing:
        errors.append("formal_keys_missing")
    if outside:
        errors.append("formal_keys_outside_expected_universe")
    if duplicate_key_count:
        errors.append("duplicate_formal_keys")
    if expected_latest is not None and (formal_latest is None or formal_latest < expected_latest):
        errors.append("latest_formal_trade_date_behind_expected")
    return {
        "status": CLOSURE_BLOCKED if errors else CLOSURE_HEALTHY,
        "reason": errors[0] if errors else "coverage_and_physical_checks_passed",
        "expected_key_count": expected_key_count,
        "formal_key_count": formal_key_count,
        "formal_present_expected_key_count": formal_present_count,
        "missing_key_count": missing,
        "missing_key_samples": list(missing_key_samples)[:20],
        "outside_key_count": outside,
        "outside_key_samples": list(outside_key_samples)[:20],
        "duplicate_key_count": duplicate_key_count,
        "expected_latest_trade_date": expected_latest.isoformat() if expected_latest else None,
        "formal_latest_trade_date": formal_latest.isoformat() if formal_latest else None,
        "evidence_scope": "full_formal_table",
    }


def _reconcile_anomalies_with_accepted_source(
    classified: Iterable[Mapping[str, Any]],
    records: Iterable[Mapping[str, Any]],
    external_source_rows: Iterable[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Attach an independent-source disposition to every current anomaly."""

    record_by_code = {normalize_code(row["code"]): row for row in records}
    source_by_key: dict[tuple[str, date], Mapping[str, Any]] = {}
    for code in sorted({normalize_code(row["code"]) for row in classified}):
        record = record_by_code.get(code)
        if record is None:
            continue
        for source_row in _parse_ths_source_rows(record):
            source_by_key[(code, normalize_trade_date(source_row["trade_date"]))] = source_row
    external_by_key = {
        (normalize_code(row["code"]), normalize_trade_date(row["trade_date"])): row
        for row in external_source_rows
    }

    output: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    fields = ("open", "high", "low", "close", "volume", "amount")
    for raw in classified:
        row = dict(raw)
        key = (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
        source_row = source_by_key.get(key)
        if source_row is None and key in external_by_key:
            source_row = external_by_key[key]
            source_kind = str(source_row.get("source_kind") or "independent_source")
        else:
            source_kind = "accepted_ths_manifest"
        if source_row is None and normalize_code(row["code"]) not in record_by_code:
            disposition = "independent_source_key_missing_unaccepted_registry_scope"
            source_detail = {"status": disposition, "source_code_scope": "unaccepted"}
        elif source_row is None:
            disposition = "accepted_source_key_missing"
            source_detail = {"status": disposition, "source_code_scope": "accepted_ths_manifest"}
        else:
            mismatch_fields = [
                field
                for field in fields
                if (row.get(field) is None) != (source_row.get(field) is None)
                or (
                    row.get(field) is not None
                    and source_row.get(field) is not None
                    and not math.isclose(
                        float(row[field]),
                        float(source_row[field]),
                        rel_tol=1e-9,
                        abs_tol=1e-9,
                    )
                )
            ]
            disposition_prefix = "accepted_source" if source_kind == "accepted_ths_manifest" else "independent_source"
            disposition = f"{disposition_prefix}_value_match" if not mismatch_fields else f"{disposition_prefix}_value_mismatch"
            source_detail = {
                "status": disposition,
                "source_code_scope": source_kind,
                "mismatch_fields": mismatch_fields,
                "source_values": {field: source_row.get(field) for field in fields},
            }
        counts[disposition] += 1
        row["independent_source_reconciliation"] = source_detail
        output.append(row)
    return output, counts


def _build_report(
    *,
    client: Any,
    manifest_path: Path,
    start_date: date,
    end_date: date,
    batch_size: int,
    output_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    records = _load_accepted_manifest(manifest_path)
    records_by_code = {normalize_code(row["code"]): row for row in records}
    records = [
        row
        for row in records
        if any(start_date <= normalize_trade_date(value) <= end_date for value in row.get("dates") or [])
    ]
    reader = _manifest_reader(records)
    status_rows = _read_status_rows(client)
    global_stats = _read_global_formal_stats(client, start_date=start_date, end_date=end_date)

    expected_key_count = 0
    expected_latest: date | None = None
    for row in records:
        dates = [normalize_trade_date(value) for value in row.get("dates") or []]
        dates = [value for value in dates if start_date <= value <= end_date]
        expected_key_count += len(dates)
        if dates:
            expected_latest = max(expected_latest or dates[0], max(dates))

    batch_snapshots: list[Mapping[str, Any]] = []
    source_gates: list[Mapping[str, Any]] = []
    formal_present_expected = 0
    missing_key_samples: list[str] = []
    outside_key_samples: list[str] = []
    for offset in range(0, len(records), max(1, batch_size)):
        batch = records[offset : offset + max(1, batch_size)]
        codes = [normalize_code(row["code"]) for row in batch]
        formal_rows = _read_formal_batch(
            client,
            codes=codes,
            start_date=start_date,
            end_date=end_date,
        )
        expected_keys = [
            (normalize_code(row["code"]), normalize_trade_date(trade_date))
            for row in batch
            for trade_date in row.get("dates") or []
            if start_date <= normalize_trade_date(trade_date) <= end_date
        ]
        formal_keys = {
            (normalize_code(row["code"]), normalize_trade_date(row["trade_date"]))
            for row in formal_rows
        }
        formal_present_expected += len(formal_keys.intersection(set(expected_keys)))
        expected_key_set = set(expected_keys)
        for code, trade_date in sorted(expected_key_set - formal_keys):
            if len(missing_key_samples) < 20:
                missing_key_samples.append(f"{code}@{trade_date.isoformat()}")
        for code, trade_date in sorted(formal_keys - expected_key_set):
            if len(outside_key_samples) < 20:
                outside_key_samples.append(f"{code}@{trade_date.isoformat()}")
        source_rows: list[dict[str, Any]] = []
        for row in batch:
            source_rows.extend(
                item
                for item in _parse_ths_source_rows(row)
                if start_date <= normalize_trade_date(item["trade_date"]) <= end_date
            )
        snapshot = build_daily_closure_snapshot_from_reader(
            reader=reader,
            expected_keys=expected_keys,
            formal_rows=formal_rows,
            source_rows=source_rows,
            status_rows=status_rows,
            source="accepted-local-registry-member-range",
            permit_error="online_registry_member_tables_missing; local_accepted_evidence_only",
            expected_latest_trade_date=max((normalize_trade_date(item[1]) for item in expected_keys), default=None),
        )
        batch_snapshots.append(snapshot.as_dict())
        source_gates.append(snapshot.gates["source_reconciliation"])

    anomaly_rows = _read_anomaly_rows(client, start_date=start_date, end_date=end_date)
    classified, anomaly_source_counts = _reconcile_anomalies_with_accepted_source(
        classify_daily_rows(anomaly_rows, status_rows=status_rows),
        records_by_code.values(),
        _read_tdx_anomaly_source_rows(client, anomaly_rows),
    )
    anomaly_counts = Counter(str(row["classification"]) for row in classified)
    samples: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in classified:
        if len(samples[row["classification"]]) < 5:
            samples[row["classification"]].append(row)

    online_state = _online_registry_member_state(client)
    registry_gate = _merge_registry_gate(batch_snapshots, online_state=online_state)
    coverage_gate = _merge_coverage_gate(
        expected_key_count=expected_key_count,
        formal_key_count=int(global_stats["formal_key_count"]),
        formal_present_count=formal_present_expected,
        expected_latest=expected_latest,
        formal_latest=normalize_trade_date(global_stats["formal_latest_trade_date"])
        if global_stats.get("formal_latest_trade_date")
        else None,
        missing_key_samples=missing_key_samples,
        outside_key_samples=outside_key_samples,
    )
    blocking = sum(1 for row in classified if row.get("severity") == CLOSURE_BLOCKED)
    reviews = len(classified) - blocking
    value_gate = {
        "status": CLOSURE_BLOCKED if blocking else CLOSURE_REVIEW if reviews else CLOSURE_HEALTHY,
        "reason": "blocking_value_domain_anomalies" if blocking else "review_value_domain_anomalies" if reviews else "value_domain_passed",
        "anomaly_rows": len(classified),
        "blocking_rows": blocking,
        "review_rows": reviews,
        "classification_complete": True,
        "evidence_scope": "full_formal_table_anomaly_scan",
    }
    source_gate = _merge_source_gates(source_gates)
    gates = {
        "registry_member_exact_key": registry_gate,
        "formal_coverage_and_physical": coverage_gate,
        "value_domain": value_gate,
        "source_reconciliation": source_gate,
    }
    status = _worst_status(str(gate.get("status") or CLOSURE_BLOCKED) for gate in gates.values())
    report = {
        "schema_version": 2,
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "readonly": True,
        "mode": "range",
        "evidence_scope": "full_formal_table",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "status": status,
        "expected_key_count": expected_key_count,
        "formal_key_count": int(global_stats["formal_key_count"]),
        "formal_rows_read": int(global_stats["formal_rows"]),
        "formal_code_count": int(global_stats["formal_code_count"]),
        "permit_source": str(manifest_path),
        "accepted_registry_code_count": len(records),
        "accepted_manifest_source_date_rows": expected_key_count,
        "batch_size": batch_size,
        "batch_count": len(batch_snapshots),
        "gates": gates,
        "anomaly_counts": dict(sorted(anomaly_counts.items())),
        "anomaly_samples": {name: values for name, values in sorted(samples.items())},
        "anomaly_source_reconciliation_counts": dict(sorted(anomaly_source_counts.items())),
        "anomaly_digest": hashlib.sha256(
            "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) for row in classified).encode("utf-8")
        ).hexdigest(),
        "global_formal_stats": global_stats,
        "source_policy": "THS native export is independent evidence for the accepted 1068-code subset; source manifest itself certifies membership only",
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
        "scope_limits": {
            "accepted_registry_member_scope": "1068 accepted THS stock date sets",
            "unaccepted_formal_rows_included": int(global_stats["formal_key_count"]) - formal_present_expected,
            "online_registry_member_tables": online_state,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "accepted_registry_evidence.json").write_text(
        json.dumps(
            {
                "readonly": True,
                "manifest": str(manifest_path),
                "code_count": len(records),
                "member_count": expected_key_count,
                "codes": [normalize_code(row["code"]) for row in records],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return report, classified


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted-manifest", type=Path, required=True)
    parser.add_argument("--start-date", default="1990-01-01")
    parser.add_argument("--end-date", default="2100-01-01")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    start_date = normalize_trade_date(args.start_date)
    end_date = normalize_trade_date(args.end_date)
    if end_date < start_date:
        raise SystemExit("end date must be on or after start date")
    report, classified = _build_report(
        client=clickhouse_client(),
        manifest_path=args.accepted_manifest,
        start_date=start_date,
        end_date=end_date,
        batch_size=args.batch_size,
        output_dir=args.output_dir,
    )
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "daily_closure_snapshot.json"
    anomaly_path = output_dir / "daily_closure_anomalies.jsonl.gz"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    with gzip.open(anomaly_path, "wt", encoding="utf-8") as handle:
        for row in classified:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    print(
        json.dumps(
            {
                "output": str(report_path),
                "anomalies_output": str(anomaly_path),
                "status": report["status"],
                "formal_rows_read": report["formal_rows_read"],
                "expected_key_count": report["expected_key_count"],
                "formal_key_count": report["formal_key_count"],
                "anomaly_counts": report["anomaly_counts"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == CLOSURE_HEALTHY else 2


if __name__ == "__main__":
    raise SystemExit(main())

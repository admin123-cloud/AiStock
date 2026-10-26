"""Close the remaining accepted keys that have only ``formal_bar_missing``.

This is a read-only evidence builder.  It reads the exact formal key, the
exact lifecycle/status rows, the independent TDX key, and the accepted THS
raw row for four keys.  It never inserts, updates, deletes, mutates, or
publishes registry/member data.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_daily_closure_snapshot import _parse_ths_source_rows
from utils.daily_date_universe import normalize_code, normalize_trade_date
from utils.market_warehouse import clickhouse_client


READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 120, "max_threads": 1}
TARGET_KEYS = (
    ("600601.SH", "1992-03-25"),
    ("600605.SH", "1992-04-29"),
    ("600605.SH", "1992-05-05"),
    ("600605.SH", "1992-05-18"),
)


def _quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _dict_rows(result: Any) -> list[dict[str, Any]]:
    names = list(getattr(result, "column_names", ()) or ())
    return [dict(zip(names, row)) for row in getattr(result, "result_rows", ())]


def load_manifest_records(path: Path, codes: set[str]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            code = normalize_code(row.get("code"))
            if code in codes:
                records[code] = row
    return records


def classify_key(
    *,
    accepted_member: bool,
    candidate_member: bool,
    formal_row: Mapping[str, Any] | None,
    tdx_row: Mapping[str, Any] | None,
    status_rows: list[Mapping[str, Any]],
    ths_row: Mapping[str, Any] | None,
) -> tuple[str, str]:
    """Return a deterministic disposition and next action for one key."""

    statuses = {str(row.get("status") or "").strip() for row in status_rows}
    if formal_row is not None:
        return "formal_row_present_unexpected", "reconcile_expected_key_inputs"
    if not accepted_member or not candidate_member:
        return "membership_evidence_missing", "blocked_until_registry_member_evidence"
    if "not_trading" in statuses or "suspended" in statuses:
        return "closed_by_lifecycle_status", "retain_missing_key_with_lifecycle_exception"
    if tdx_row is not None:
        return "independent_source_key_present_formal_missing", "blocked_until_formal_gap_review"
    if ths_row is not None and statuses == {"formal_bar_missing"}:
        return (
            "blocked_formal_bar_missing_lifecycle_unresolved",
            "requires_independent_lifecycle_or_exchange_calendar_evidence",
        )
    return "formal_bar_missing_evidence_incomplete", "blocked_until_source_evidence"


def build_report(
    *,
    client: Any,
    accepted_manifest: Path,
    output: Path,
) -> dict[str, Any]:
    keys = {(normalize_code(code), normalize_trade_date(day).isoformat()) for code, day in TARGET_KEYS}
    codes = {code for code, _ in keys}
    manifest = load_manifest_records(accepted_manifest, codes)
    code_sql = ",".join(_quote(code) for code in sorted(codes))
    date_sql = ",".join(f"toDate({_quote(day)})" for _, day in sorted(keys))

    formal_result = client.query(
        f"""
        SELECT code,trade_date,open,high,low,close,volume,amount
        FROM stock.kline_daily FINAL
        WHERE code IN ({code_sql}) AND trade_date IN ({date_sql})
        ORDER BY code,trade_date
        """,
        settings=READONLY_SETTINGS,
    )
    formal_rows = {
        (normalize_code(row["code"]), str(row["trade_date"])[:10]): row
        for row in _dict_rows(formal_result)
    }
    tdx_result = client.query(
        f"""
        SELECT code,trade_date,open,high,low,close,volume_shares,amount
        FROM stock.wstock_daily_tdx_evidence_20261014_v2
        WHERE code IN ({code_sql}) AND trade_date IN ({date_sql})
        ORDER BY code,trade_date
        """,
        settings=READONLY_SETTINGS,
    )
    tdx_rows = {
        (normalize_code(row["code"]), str(row["trade_date"])[:10]): row
        for row in _dict_rows(tdx_result)
    }
    status_result = client.query(
        f"""
        SELECT code,start_date,end_date,status,evidence_url,reason,observed_at,
               expected_resume_date,source,review_state,evidence_json
        FROM stock.kline_daily_market_status_audit FINAL
        WHERE code IN ({code_sql})
          AND start_date IN ({date_sql})
          AND end_date IN ({date_sql})
        ORDER BY code,start_date
        """,
        settings=READONLY_SETTINGS,
    )
    status_rows = _dict_rows(status_result)
    status_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in status_rows:
        key = (normalize_code(row["code"]), str(row["start_date"])[:10])
        status_by_key.setdefault(key, []).append(row)

    rows: list[dict[str, Any]] = []
    for code, day in sorted(keys):
        record = manifest.get(code)
        accepted_dates = set(str(value)[:10] for value in (record or {}).get("dates") or [])
        ths_rows = _parse_ths_source_rows(record) if record else []
        ths = next((row for row in ths_rows if row["trade_date"] == day), None)
        statuses = status_by_key.get((code, day), [])
        disposition, next_action = classify_key(
            accepted_member=day in accepted_dates,
            candidate_member=day in accepted_dates,
            formal_row=formal_rows.get((code, day)),
            tdx_row=tdx_rows.get((code, day)),
            status_rows=statuses,
            ths_row=ths,
        )
        rows.append(
            {
                "code": code,
                "trade_date": day,
                "accepted_member": day in accepted_dates,
                "candidate_member": day in accepted_dates,
                "candidate_status": "source_date_set_available" if record else "missing",
                "formal_row_present": (code, day) in formal_rows,
                "independent_source_key_present": (code, day) in tdx_rows,
                "status_rows": statuses,
                "lifecycle_statuses": sorted({str(row.get("status") or "") for row in statuses if row.get("status")}),
                "lifecycle_review_states": sorted({str(row.get("review_state") or "") for row in statuses if row.get("review_state")}),
                "accepted_source_policy": (record or {}).get("source_policy"),
                "accepted_source_file": ((record or {}).get("source_files") or {}).get("raw_gbk"),
                "accepted_ths_raw_row": ths,
                "independent_source_row": tdx_rows.get((code, day)),
                "disposition": disposition,
                "next_action": next_action,
            }
        )
    report = {
        "schema_version": 1,
        "readonly": True,
        "accepted_manifest": str(accepted_manifest),
        "accepted_manifest_sha256": hashlib.sha256(accepted_manifest.read_bytes()).hexdigest(),
        "target_key_count": len(rows),
        "rows": rows,
        "disposition_counts": {
            name: sum(1 for row in rows if row["disposition"] == name)
            for name in sorted({row["disposition"] for row in rows})
        },
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
        "gate": "blocked" if any(row["disposition"].startswith("blocked_") for row in rows) else "review",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(client=clickhouse_client(), accepted_manifest=args.accepted_manifest, output=args.output)
    print(json.dumps({"output": str(args.output), "disposition_counts": report["disposition_counts"], "gate": report["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

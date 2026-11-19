"""Read-only source-vs-formal daily coverage audit.

This command consumes an existing per-security source date manifest and the
live formal table through SELECT-only ClickHouse queries.  It writes a local
exception JSONL.GZ plus per-code summary; it does not create or mutate the
future ``daily_coverage_audit`` table.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path
import sys
from typing import Any, Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.daily_coverage_audit import (
    AUDIT_FORMAL_PRESENT,
    build_daily_coverage_audit,
    derive_formal_scope_sets,
)
from utils.daily_date_universe import date_set_sha256, normalize_code
from utils.market_status import StatusIndex
from utils.market_warehouse import clickhouse_client


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
CORE_INDEX_CODES = frozenset(
    {
        "000001.SH",
        "399001.SZ",
        "399006.SZ",
        "399106.SZ",
        "000985.SH",
        "000680.SH",
        "000300.SH",
        "000016.SH",
        "000905.SH",
        "000852.SH",
        "000688.SH",
    }
)


def _query(client: Any, sql: str):
    return client.query(
        sql,
        settings={"readonly": 1, "max_execution_time": 180, "max_threads": 2},
    ).result_rows


def _quote(value: object) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _load_candidates(path: Path) -> set[str]:
    output: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if str(row.get("classification") or "").strip().lower() == "stock":
                output.add(normalize_code(row["code"]))
    return output


def _status_index(client: Any) -> StatusIndex:
    rows = _query(
        client,
        "SELECT code,start_date,end_date,status,evidence_url,reason,review_state,evidence_json "
        "FROM stock.kline_daily_market_status_audit FINAL",
    )
    fields = (
        "code",
        "start_date",
        "end_date",
        "status",
        "evidence_url",
        "reason",
        "review_state",
        "evidence_json",
    )
    return StatusIndex(dict(zip(fields, row)) for row in rows)


def _formal_dates(client: Any, codes: Iterable[str]) -> dict[str, set[str]]:
    codes = sorted(set(codes))
    if not codes:
        return {}
    code_sql = ",".join(_quote(code) for code in codes)
    rows = _query(
        client,
        f"SELECT code,trade_date FROM stock.kline_daily FINAL WHERE code IN ({code_sql})",
    )
    output = {code: set() for code in codes}
    for code, trade_date in rows:
        output.setdefault(normalize_code(code), set()).add(str(trade_date)[:10])
    return output


def _choose_source(record: dict[str, Any]) -> tuple[str, list[str]] | None:
    sources = record.get("sources") or {}
    candidates = [
        (str(name), value)
        for name, value in sources.items()
        if isinstance(value, dict) and value.get("dates")
    ]
    if not candidates:
        return None
    # Prefer the widest declared source set for an audit candidate, while
    # preserving the source name in evidence. Promotion still requires the
    # independent source acceptance gate.
    name, payload = max(candidates, key=lambda item: int(item[1].get("date_count") or 0))
    return name, [str(item)[:10] for item in payload.get("dates") or []]


def audit_manifest(
    *,
    client: Any,
    manifest: Path,
    historical_candidates: set[str],
    as_of: str,
    output_dir: Path,
) -> dict[str, Any]:
    master_rows = [
        {
            "code": row[0],
            "type": row[1],
            "quit": row[2],
            "listing_status": row[3],
            "list_date": row[4],
            "delist_date": row[5],
        }
        for row in _query(client, "SELECT code,type,quit,listing_status,list_date,delist_date FROM stock.stocks FINAL")
    ]
    scope = derive_formal_scope_sets(
        master_rows,
        historical_candidate_codes=historical_candidates,
        core_index_codes=CORE_INDEX_CODES,
        as_of=as_of,
    )
    allowed = scope.allowed_codes
    status_index = _status_index(client)
    records: list[dict[str, Any]] = []
    with gzip.open(manifest, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            code = normalize_code(row.get("code"))
            if code in allowed:
                records.append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    exception_path = output_dir / "daily_coverage_exceptions_20261023.jsonl.gz"
    summary_path = output_dir / "daily_coverage_summary_20261023.json"
    summaries: list[dict[str, Any]] = []
    exception_count = 0
    source_available_count = 0
    batch_size = 40
    with gzip.open(exception_path, "wt", encoding="utf-8") as output:
        for offset in range(0, len(records), batch_size):
            batch = records[offset : offset + batch_size]
            formal = _formal_dates(client, [normalize_code(row["code"]) for row in batch])
            for record in batch:
                code = normalize_code(record["code"])
                selected = _choose_source(record)
                if selected is None:
                    summaries.append(
                        {"code": code, "status": record.get("status"), "source": None, "expected_count": None}
                    )
                    continue
                source_name, expected = selected
                expected_set = set(expected)
                date_digest = date_set_sha256(code, expected_set)
                rows = build_daily_coverage_audit(
                    code=code,
                    expected_dates=expected_set,
                    formal_dates=formal.get(code, set()),
                    date_universe_id=f"manifest:{source_name}:{code}",
                    date_set_sha256=date_digest,
                    status_index=status_index,
                )
                exceptions = [row.as_dict() for row in rows if row.audit_state != AUDIT_FORMAL_PRESENT]
                for item in exceptions:
                    output.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
                exception_count += len(exceptions)
                source_available_count += 1
                state_counts: dict[str, int] = {}
                for item in rows:
                    state_counts[item.audit_state] = state_counts.get(item.audit_state, 0) + 1
                summaries.append(
                    {
                        "code": code,
                        "status": record.get("status"),
                        "source": source_name,
                        "expected_count": len(expected_set),
                        "formal_count": len(formal.get(code, set())),
                        "coverage_start": min(expected_set) if expected_set else None,
                        "coverage_end": max(expected_set) if expected_set else None,
                        "state_counts": state_counts,
                    }
                )

    summary = {
        "generated_at": datetime.now(BUSINESS_TZ).isoformat(),
        "readonly": True,
        "manifest": str(manifest),
        "scope": {
            "allowed_codes": len(allowed),
            "manifest_records_in_scope": len(records),
            "source_date_available_records": source_available_count,
        },
        "exception_rows": exception_count,
        "exception_path": str(exception_path),
        "records": summaries,
        "next_gate": "review and publish accepted registry/member rows before online coverage audit DDL/DML",
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--historical-candidates", type=Path, required=True)
    parser.add_argument("--as-of", default=datetime.now(BUSINESS_TZ).date().isoformat())
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = audit_manifest(
        client=clickhouse_client(),
        manifest=args.manifest,
        historical_candidates=_load_candidates(args.historical_candidates),
        as_of=args.as_of,
        output_dir=args.output_dir,
    )
    print(json.dumps({
        "exception_rows": summary["exception_rows"],
        "manifest_records_in_scope": summary["scope"]["manifest_records_in_scope"],
        "source_date_available_records": summary["scope"]["source_date_available_records"],
        "summary_path": summary["exception_path"].replace("exceptions_20261023.jsonl.gz", "summary_20261023.json"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

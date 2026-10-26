"""Build exact-key /100 index-volume reconciliation candidates read-only.

The source mismatch ledger is the scope authority.  This builder keeps only
the nine index codes whose anomaly rows are consistently formal/source
``1/100``.  It reads the independent-source table and formal daily table for
lineage and value read-back, applies ``source.volume_shares / 100`` to the
volume field in memory, and writes JSONL candidates plus a summary.  It never
issues DDL, DML, or registry publication.
"""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.classify_daily_independent_source_differences import TOLERANCES, substantive_fields
from scripts.classify_index_volume_scale_anomalies import formal_to_source_factor
from utils.market_warehouse import clickhouse_client


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
SOURCE_TABLE = "stock.wstock_daily_tdx_evidence_20261014_v2"
FORMAL_TABLE = "stock.kline_daily"
FIELDS = ("open", "high", "low", "close", "volume", "amount")
READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 180, "max_threads": 2}


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


def load_stable_mismatch_rows(path: Path) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Load exact mismatch keys whose observed factor is consistently 1/100."""

    rows: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_keys: list[dict[str, Any]] = []
    factor_counts: Counter[str] = Counter()
    mapping_counts: Counter[str] = Counter()
    skipped_factor_counts: Counter[str] = Counter()
    for row in _iter_jsonl(path):
        code = str(row.get("code") or "").strip().upper()
        if code not in STABLE_CODES or "volume" not in (row.get("mismatch_fields") or ()):
            continue
        formal = row.get("formal_values") or {}
        source = row.get("source_values") or {}
        factor = formal_to_source_factor(formal.get("volume"), source.get("volume"))
        if factor != "1/100":
            skipped_factor_counts[factor] += 1
            continue
        trade_date = str(row.get("trade_date") or "")[:10]
        if not trade_date:
            continue
        key = (code, trade_date)
        if key in rows:
            duplicate_keys.append({"code": code, "trade_date": trade_date})
            continue
        rows[key] = row
        factor_counts[factor] += 1
        mapping_counts[str(row.get("source_volume_unit_mapping") or "unknown_null")] += 1
    return rows, {
        "candidate_mismatch_rows": len(rows),
        "duplicate_key_count": len(duplicate_keys),
        "duplicate_keys_sample": duplicate_keys[:20],
        "factor_counts": dict(sorted(factor_counts.items())),
        "mapping_counts": dict(sorted(mapping_counts.items())),
        "skipped_factor_counts": dict(sorted(skipped_factor_counts.items())),
    }


def _read_rows(client: Any, table: str, codes: Iterable[str], columns: str) -> list[dict[str, Any]]:
    code_sql = ",".join(_quote(code) for code in sorted(set(codes)))
    result = client.query(
        f"SELECT {columns} FROM {table} WHERE code IN ({code_sql}) ORDER BY trade_date,code",
        settings=READONLY_SETTINGS,
    )
    return _dict_rows(result)


def _source_lookup(client: Any) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    rows = _read_rows(
        client,
        SOURCE_TABLE,
        STABLE_CODES,
        "code,trade_date,open,high,low,close,volume_shares,amount,file_sha,source_row",
    )
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_keys: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("code") or "").strip().upper(), str(row.get("trade_date") or "")[:10])
        if key in lookup:
            duplicate_keys.append({"code": key[0], "trade_date": key[1]})
            continue
        lookup[key] = row
    return lookup, {"rows_read": len(rows), "unique_key_count": len(lookup), "duplicate_key_count": len(duplicate_keys), "duplicate_keys_sample": duplicate_keys[:20]}


def _formal_lookup(client: Any) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    rows = _read_rows(client, FORMAL_TABLE + " FINAL", STABLE_CODES, "code,trade_date,open,high,low,close,volume,amount")
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_keys: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("code") or "").strip().upper(), str(row.get("trade_date") or "")[:10])
        if key in lookup:
            duplicate_keys.append({"code": key[0], "trade_date": key[1]})
            continue
        lookup[key] = row
    return lookup, {"rows_read": len(rows), "unique_key_count": len(lookup), "duplicate_key_count": len(duplicate_keys), "duplicate_keys_sample": duplicate_keys[:20]}


def _residual_fields(formal: Mapping[str, Any], candidate: Mapping[str, Any]) -> tuple[str, ...]:
    residual: list[str] = []
    for field in FIELDS:
        left, right = _float(formal.get(field)), _float(candidate.get(field))
        if left is None or right is None or abs(left - right) > float(TOLERANCES[field]):
            residual.append(field)
    return tuple(residual)


def _candidate_row(mismatch: Mapping[str, Any], formal: Mapping[str, Any] | None, source: Mapping[str, Any] | None) -> dict[str, Any]:
    formal_values = dict(mismatch.get("formal_values") or {})
    ledger_source_values = dict(mismatch.get("source_values") or {})
    source_values = {
        "open": source.get("open") if source else ledger_source_values.get("open"),
        "high": source.get("high") if source else ledger_source_values.get("high"),
        "low": source.get("low") if source else ledger_source_values.get("low"),
        "close": source.get("close") if source else ledger_source_values.get("close"),
        "volume_shares": source.get("volume_shares") if source else ledger_source_values.get("volume"),
        "amount": source.get("amount") if source else ledger_source_values.get("amount"),
    }
    transformed = {
        "open": source_values["open"],
        "high": source_values["high"],
        "low": source_values["low"],
        "close": source_values["close"],
        "volume": (_float(source_values["volume_shares"]) / 100.0) if _float(source_values["volume_shares"]) is not None else None,
        "amount": source_values["amount"],
    }
    formal_for_compare = dict(formal or formal_values)
    residual = _residual_fields(formal_for_compare, transformed)
    volume_before = _float(formal_for_compare.get("volume"))
    volume_after = _float(transformed.get("volume"))
    volume_delta = (volume_after - volume_before) if volume_after is not None and volume_before is not None else None
    return {
        "code": str(mismatch.get("code") or "").strip().upper(),
        "trade_date": str(mismatch.get("trade_date") or "")[:10],
        "source_table": SOURCE_TABLE,
        "formal_table": FORMAL_TABLE,
        "source_file_sha": source.get("file_sha") if source else None,
        "source_row": source.get("source_row") if source else None,
        "source_volume_unit_mapping": mismatch.get("source_volume_unit_mapping"),
        "formal_values": formal_for_compare,
        "ledger_source_values": ledger_source_values,
        "source_values_raw": source_values,
        "candidate_transform": "volume = source.volume_shares / 100; all other fields unchanged",
        "candidate_values": transformed,
        "volume_delta_after_transform": volume_delta,
        "residual_substantive_fields": list(residual),
        "substantive_fields_before_transform": list(substantive_fields(mismatch)),
        "candidate_disposition": "review_only_volume_scale_candidate" if not residual else "review_only_with_residual_fields",
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
    }


def build_candidates(*, mismatch_ledger: Path, output_jsonl: Path, output_summary: Path) -> dict[str, Any]:
    mismatch_rows, ledger_summary = load_stable_mismatch_rows(mismatch_ledger)
    client = clickhouse_client()
    source_lookup, source_summary = _source_lookup(client)
    formal_lookup, formal_summary = _formal_lookup(client)
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    residual_counts: Counter[str] = Counter()
    missing_source: list[dict[str, str]] = []
    missing_formal: list[dict[str, str]] = []
    with _open_jsonl(output_jsonl, "wt") as handle:
        for key in sorted(mismatch_rows):
            mismatch = mismatch_rows[key]
            source = source_lookup.get(key)
            formal = formal_lookup.get(key)
            if source is None:
                missing_source.append({"code": key[0], "trade_date": key[1]})
            if formal is None:
                missing_formal.append({"code": key[0], "trade_date": key[1]})
            candidate = _candidate_row(mismatch, formal, source)
            counts[candidate["candidate_disposition"]] += 1
            residual_counts["+".join(candidate["residual_substantive_fields"]) or "none"] += 1
            handle.write(json.dumps(candidate, ensure_ascii=False, default=str) + "\n")
    summary = {
        "schema_version": 1,
        "readonly": True,
        "gate": "review_only",
        "stable_codes": list(STABLE_CODES),
        "input_mismatch_ledger": str(mismatch_ledger),
        "output_candidates": str(output_jsonl),
        "candidate_transform": "formal volume candidate = independent source volume_shares / 100; amount and prices unchanged",
        "rows": {"candidate_rows": len(mismatch_rows), "missing_source_rows": len(missing_source), "missing_formal_rows": len(missing_formal)},
        "candidate_dispositions": dict(sorted(counts.items())),
        "residual_substantive_field_combinations": dict(sorted(residual_counts.items())),
        "ledger_scope": ledger_summary,
        "source_readback": {"table": SOURCE_TABLE, **source_summary, "query_settings": READONLY_SETTINGS},
        "formal_readback": {"table": FORMAL_TABLE, **formal_summary, "query_settings": READONLY_SETTINGS},
        "missing_source_sample": missing_source[:20],
        "missing_formal_sample": missing_formal[:20],
        "policy": {
            "scope": "exact source_table + file_sha + code + trade_date key",
            "do_not_apply_global_factor": True,
            "399106_excluded": True,
            "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
        },
    }
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mismatch-ledger", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--output-summary", type=Path, required=True)
    args = parser.parse_args()
    summary = build_candidates(mismatch_ledger=args.mismatch_ledger, output_jsonl=args.output_jsonl, output_summary=args.output_summary)
    print(json.dumps({"output_jsonl": str(args.output_jsonl), "output_summary": str(args.output_summary), "rows": summary["rows"], "gate": summary["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

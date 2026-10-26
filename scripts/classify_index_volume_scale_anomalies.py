"""Break down index ``index_raw`` volume scale anomalies read-only.

The input is the strict independent-source mismatch ledger.  Rows are limited
to registry candidates classified as ``index`` and to volume ratios outside
the accepted near-one interval ``[0.5, 2)``.  The report keeps the exact
source table, code, dates, field combinations, formal/source ratio factors,
and a small set of raw value samples.  ClickHouse is queried only for
read-only identity enrichment with ``readonly=1``.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.classify_daily_independent_source_differences import (
    ratio_bucket,
    substantive_fields,
)
from utils.market_warehouse import clickhouse_client


FIELDS = ("open", "high", "low", "close", "volume", "amount")
SOURCE_TABLE = "stock.wstock_daily_tdx_evidence_20261014_v2"
READONLY_SETTINGS = {"readonly": 1, "max_execution_time": 120, "max_threads": 1}


def _open_jsonl(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open("r", encoding="utf-8")


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with _open_jsonl(path) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def formal_to_source_factor(formal: Any, source: Any) -> str:
    """Label a formal/source ratio by the nearest common scale factor."""

    left, right = _float(formal), _float(source)
    if left is None or right is None:
        return "missing_or_nonfinite"
    if left == 0 and right == 0:
        return "both_zero"
    if right == 0:
        return "source_zero"
    if left == 0:
        return "formal_zero"
    ratio = abs(left / right)
    factors = (
        (0.001, "1/1000"),
        (0.01, "1/100"),
        (0.1, "1/10"),
        (0.25, "1/4"),
        (0.5, "1/2"),
        (1.0, "1"),
        (2.0, "2"),
        (4.0, "4"),
        (10.0, "10"),
        (100.0, "100"),
        (1000.0, "1000"),
    )
    nearest, label = min(factors, key=lambda pair: abs(math.log10(ratio / pair[0])))
    if abs(math.log10(ratio / nearest)) <= math.log10(1.25):
        return label
    return "other_ratio"


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def load_index_codes(path: Path) -> set[str]:
    output: set[str] = set()
    for row in _iter_jsonl(path):
        if str(row.get("category") or "").strip().lower() == "index":
            code = str(row.get("code") or "").strip().upper()
            if code:
                output.add(code)
    return output


def _dict_rows(result: Any) -> list[dict[str, Any]]:
    names = list(getattr(result, "column_names", ()) or ())
    return [dict(zip(names, row)) for row in getattr(result, "result_rows", ())]


def read_identity(client: Any, codes: set[str]) -> tuple[dict[str, dict[str, Any]], str | None]:
    if not codes:
        return {}, None
    quoted = ",".join("'" + code.replace("'", "\\'") + "'" for code in sorted(codes))
    query = f"""
        SELECT code,name,market,type,list_date,listing_status,delist_date,quit,st
        FROM stock.stocks FINAL
        WHERE code IN ({quoted})
        ORDER BY code
    """
    try:
        result = client.query(query, settings=READONLY_SETTINGS)
    except Exception as exc:  # keep the analytical report useful if identity is unavailable
        return {}, f"{type(exc).__name__}: {exc}"
    return {
        str(row["code"]).strip().upper(): row
        for row in _dict_rows(result)
    }, None


def read_source_metadata(client: Any, codes: set[str]) -> tuple[dict[tuple[str, str], dict[str, Any]], str | None]:
    """Read source file lineage for affected index code/date keys."""

    if not codes:
        return {}, None
    quoted = ",".join("'" + code.replace("'", "\\'") + "'" for code in sorted(codes))
    query = f"""
        SELECT code,trade_date,file_sha,source_row
        FROM {SOURCE_TABLE}
        WHERE code IN ({quoted})
        ORDER BY code,trade_date
    """
    try:
        result = client.query(query, settings=READONLY_SETTINGS)
    except Exception as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    output: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _dict_rows(result):
        key = (str(row["code"]).strip().upper(), str(row["trade_date"])[:10])
        output[key] = {"file_sha": str(row.get("file_sha") or ""), "source_row": row.get("source_row")}
    return output, None


def _new_code_bucket() -> dict[str, Any]:
    return {
        "strict_anomaly_rows": 0,
        "substantive_anomaly_rows": 0,
        "precision_only_rows": 0,
        "trade_date_start": None,
        "trade_date_end": None,
        "ratio_values": [],
        "ratio_buckets": Counter(),
        "factor_labels": Counter(),
        "strict_field_combinations": Counter(),
        "substantive_field_combinations": Counter(),
        "substantive_field_counts": Counter(),
        "source_tables": Counter(),
        "source_file_sha_all_source_row_counts": Counter(),
        "source_rows": [],
        "factor_samples": {},
        "non_dominant_factor_rows": [],
        "samples": [],
    }


def _serialise_code_bucket(code: str, bucket: Mapping[str, Any], identity: Mapping[str, Any] | None) -> dict[str, Any]:
    ratios = list(bucket["ratio_values"])
    result = {
        "code": code,
        "security_identity": dict(identity or {}),
        "strict_anomaly_rows": bucket["strict_anomaly_rows"],
        "substantive_anomaly_rows": bucket["substantive_anomaly_rows"],
        "precision_only_rows": bucket["precision_only_rows"],
        "trade_date_start": bucket["trade_date_start"],
        "trade_date_end": bucket["trade_date_end"],
        "ratio_statistics_formal_to_source": {
            "min": min(ratios) if ratios else None,
            "p25": _percentile(ratios, 0.25),
            "median": statistics.median(ratios) if ratios else None,
            "p75": _percentile(ratios, 0.75),
            "max": max(ratios) if ratios else None,
        },
        "ratio_buckets": dict(sorted(bucket["ratio_buckets"].items())),
        "formal_to_source_factor_labels": dict(sorted(bucket["factor_labels"].items())),
        "strict_field_combinations": dict(sorted(bucket["strict_field_combinations"].items())),
        "substantive_field_combinations": dict(sorted(bucket["substantive_field_combinations"].items())),
        "substantive_field_counts": dict(sorted(bucket["substantive_field_counts"].items())),
        "source_tables": dict(sorted(bucket["source_tables"].items())),
        "source_file_sha_all_source_row_counts": dict(sorted(bucket["source_file_sha_all_source_row_counts"].items())),
        "source_row_min_all_source_rows": min(bucket["source_rows"]) if bucket["source_rows"] else None,
        "source_row_max_all_source_rows": max(bucket["source_rows"]) if bucket["source_rows"] else None,
        "factor_samples": dict(sorted(bucket["factor_samples"].items())),
        "non_dominant_factor_rows": list(bucket["non_dominant_factor_rows"]),
        "samples": list(bucket["samples"]),
    }
    return result


def build_report(
    *,
    mismatch_ledger: Path,
    registry_candidate: Path,
    output: Path,
    sample_limit: int = 8,
) -> dict[str, Any]:
    index_codes = load_index_codes(registry_candidate)
    by_code: defaultdict[str, dict[str, Any]] = defaultdict(_new_code_bucket)
    ratio_buckets: Counter[str] = Counter()
    factors: Counter[str] = Counter()
    strict_combinations: Counter[str] = Counter()
    substantive_combinations: Counter[str] = Counter()
    substantive_fields_count: Counter[str] = Counter()
    source_tables: Counter[str] = Counter()
    total = substantive_total = precision_total = 0

    for row in _iter_jsonl(mismatch_ledger):
        code = str(row.get("code") or "").strip().upper()
        if code not in index_codes:
            continue
        mismatch = set(str(field) for field in row.get("mismatch_fields") or ())
        if "volume" not in mismatch:
            continue
        formal_values = row.get("formal_values") or {}
        source_values = row.get("source_values") or {}
        ratio = _float(formal_values.get("volume"))
        source_volume = _float(source_values.get("volume"))
        if ratio is None or source_volume is None:
            bucket_name = "missing_or_nonfinite"
            ratio_value = None
        elif source_volume == 0:
            bucket_name = "source_zero" if ratio != 0 else "both_zero"
            ratio_value = None
        else:
            ratio_value = abs(ratio / source_volume)
            bucket_name = ratio_bucket(ratio, source_volume)
        if bucket_name == "0.5-2":
            continue
        total += 1
        trade_date = str(row.get("trade_date") or "")[:10]
        strict = tuple(field for field in FIELDS if field in mismatch)
        strict_combo = "+".join(strict)
        substantive = substantive_fields(row)
        substantive_combo = "+".join(substantive) if substantive else "precision_only"
        factor = formal_to_source_factor(ratio, source_volume)
        mapping = str(row.get("source_volume_unit_mapping") or "unknown_null")
        bucket = by_code[code]
        bucket["strict_anomaly_rows"] += 1
        bucket["trade_date_start"] = min(bucket["trade_date_start"] or trade_date, trade_date)
        bucket["trade_date_end"] = max(bucket["trade_date_end"] or trade_date, trade_date)
        bucket["ratio_buckets"][bucket_name] += 1
        bucket["factor_labels"][factor] += 1
        bucket["strict_field_combinations"][strict_combo] += 1
        bucket["source_tables"][SOURCE_TABLE] += 1
        ratio_buckets[bucket_name] += 1
        factors[factor] += 1
        strict_combinations[strict_combo] += 1
        source_tables[SOURCE_TABLE] += 1
        if ratio_value is not None:
            bucket["ratio_values"].append(ratio_value)
        bucket["factor_samples"].setdefault(
            factor,
            {
                "trade_date": trade_date,
                "source_table": SOURCE_TABLE,
                "source_volume_unit_mapping": mapping,
                "ratio_bucket": bucket_name,
                "formal_to_source_factor": factor,
                "formal_volume": formal_values.get("volume"),
                "source_volume": source_values.get("volume"),
                "strict_fields": list(strict),
                "substantive_fields": list(substantive),
                "formal_amount": formal_values.get("amount"),
                "source_amount": source_values.get("amount"),
            },
        )
        if factor != "1/100" and len(bucket["non_dominant_factor_rows"]) < 100:
            bucket["non_dominant_factor_rows"].append(
                {
                    "trade_date": trade_date,
                    "formal_to_source_factor": factor,
                    "ratio_bucket": bucket_name,
                    "formal_volume": formal_values.get("volume"),
                    "source_volume": source_values.get("volume"),
                    "strict_fields": list(strict),
                    "substantive_fields": list(substantive),
                    "formal_amount": formal_values.get("amount"),
                    "source_amount": source_values.get("amount"),
                }
            )
        if substantive:
            substantive_total += 1
            bucket["substantive_anomaly_rows"] += 1
            bucket["substantive_field_combinations"][substantive_combo] += 1
            bucket["substantive_field_counts"].update(substantive)
            substantive_combinations[substantive_combo] += 1
            substantive_fields_count.update(substantive)
        else:
            precision_total += 1
            bucket["precision_only_rows"] += 1
        if len(bucket["samples"]) < sample_limit:
            bucket["samples"].append(
                {
                    "trade_date": trade_date,
                    "source_table": SOURCE_TABLE,
                    "source_volume_unit_mapping": mapping,
                    "ratio_bucket": bucket_name,
                    "formal_to_source_factor": factor,
                    "formal_volume": formal_values.get("volume"),
                    "source_volume": source_values.get("volume"),
                    "strict_fields": list(strict),
                    "substantive_fields": list(substantive),
                    "formal_amount": formal_values.get("amount"),
                    "source_amount": source_values.get("amount"),
                }
            )

    client = clickhouse_client()
    identity, identity_error = read_identity(client, set(by_code))
    source_metadata, source_metadata_error = read_source_metadata(client, set(by_code))
    for code, bucket in by_code.items():
        for (metadata_code, _), metadata in source_metadata.items():
            if metadata_code != code:
                continue
            bucket["source_file_sha_all_source_row_counts"][metadata["file_sha"]] += 1
            if metadata["source_row"] is not None:
                bucket["source_rows"].append(int(metadata["source_row"]))
        for sample in list(bucket["samples"]) + list(bucket["factor_samples"].values()):
            metadata = source_metadata.get((code, sample["trade_date"]))
            if metadata:
                sample["source_file_sha"] = metadata["file_sha"]
                sample["source_row"] = metadata["source_row"]
    code_rows = [
        _serialise_code_bucket(code, bucket, identity.get(code))
        for code, bucket in sorted(by_code.items(), key=lambda item: (-item[1]["strict_anomaly_rows"], item[0]))
    ]
    stable_codes = [
        row["code"]
        for row in code_rows
        if set(row["formal_to_source_factor_labels"]) == {"1/100"}
    ]
    mixed_codes = [
        row["code"]
        for row in code_rows
        if "1/100" in row["formal_to_source_factor_labels"]
        and any(label != "1/100" for label in row["formal_to_source_factor_labels"])
    ]
    report = {
        "schema_version": 1,
        "readonly": True,
        "input": str(mismatch_ledger),
        "registry_candidate": str(registry_candidate),
        "independent_source_table": SOURCE_TABLE,
        "filter": "registry category=index AND strict mismatch_fields contains volume AND formal/source ratio outside [0.5,2)",
        "rows": {
            "index_volume_scale_anomaly_rows": total,
            "substantive_rows": substantive_total,
            "precision_only_rows": precision_total,
            "index_code_count": len(code_rows),
        },
        "global": {
            "ratio_buckets": dict(sorted(ratio_buckets.items())),
            "formal_to_source_factor_labels": dict(sorted(factors.items())),
            "strict_field_combinations": dict(sorted(strict_combinations.items())),
            "substantive_field_combinations": dict(sorted(substantive_combinations.items())),
            "substantive_field_counts": dict(sorted(substantive_fields_count.items())),
            "source_tables": dict(sorted(source_tables.items())),
        },
        "by_code": code_rows,
        "identity_readback": {
            "status": "complete" if not identity_error else "partial",
            "identity_rows": len(identity),
            "error": identity_error,
            "query_settings": READONLY_SETTINGS,
        },
        "source_metadata_readback": {
            "status": "complete" if not source_metadata_error else "partial",
            "source_key_rows": len(source_metadata),
            "error": source_metadata_error,
            "query_settings": READONLY_SETTINGS,
        },
        "treatment_plan": {
            "state": "review_only",
            "do_not_apply_global_factor": True,
            "candidate_stable_1_over_100_codes": stable_codes,
            "candidate_mixed_segment_codes": mixed_codes,
            "candidate_policy_scope": "source_table + file_sha + code + exact date; candidate only, not a production permit",
            "candidate_transform": "formal_volume = independent_source.volume_shares / 100 for stable 1/100 segments",
            "mixed_segment_action": "keep exact exception dates blocked until a second independent source confirms the four 399106.SZ rows",
            "amount_action": "do not rescale amount; compare separately because 399106.SZ exception rows also have amount differences",
            "stage_1": "按 code/source/date 保留原始 formal 与独立来源值，确认指数代码和来源字段语义",
            "stage_2": "仅对 ratio factor 稳定、字段组合一致且有第二独立来源确认的代码建立 exact-key candidate",
            "stage_3": "对未闭合代码维持 blocked；先完成独立来源对账，再考虑精确键修复",
            "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
        },
        "gate": "blocked" if total else "review",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mismatch-ledger", type=Path, required=True)
    parser.add_argument("--registry-candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-limit", type=int, default=8)
    args = parser.parse_args()
    report = build_report(
        mismatch_ledger=args.mismatch_ledger,
        registry_candidate=args.registry_candidate,
        output=args.output,
        sample_limit=max(0, args.sample_limit),
    )
    print(json.dumps({"output": str(args.output), "rows": report["rows"], "global": report["global"], "gate": report["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

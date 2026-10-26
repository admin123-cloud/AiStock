"""Classify the large independent-source mismatch ledger without writes.

The input ledger is the strict exact-key comparison produced by
``build_daily_registry_scope_explanations.py``.  This script applies the
documented absolute tolerances, then aggregates the substantive rows by
security type, field combination, source unit mapping, and formal/source
ratio buckets.  It never contacts ClickHouse and never mutates production.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIELDS = ("open", "high", "low", "close", "volume", "amount")
TOLERANCES = {
    "open": 0.001,
    "high": 0.001,
    "low": 0.001,
    "close": 0.001,
    "volume": 0.01,
    "amount": 0.5,
}


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
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def substantive_fields(
    row: Mapping[str, Any],
    *,
    tolerances: Mapping[str, float] = TOLERANCES,
) -> tuple[str, ...]:
    """Return strict mismatch fields that exceed the absolute tolerance."""

    formal = row.get("formal_values") or {}
    source = row.get("source_values") or {}
    strict = set(str(name) for name in row.get("mismatch_fields") or ())
    output: list[str] = []
    for field in FIELDS:
        if field not in strict:
            continue
        left, right = _float(formal.get(field)), _float(source.get(field))
        if left is None or right is None or abs(left - right) > float(tolerances[field]):
            output.append(field)
    return tuple(output)


def ratio_bucket(formal: Any, source: Any) -> str:
    """Bucket the stored formal/source ratio for unit diagnostics."""

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
    if ratio < 0.01:
        return "<0.01"
    if ratio < 0.1:
        return "0.01-0.1"
    if ratio < 0.5:
        return "0.1-0.5"
    if ratio < 2:
        return "0.5-2"
    if ratio < 10:
        return "2-10"
    if ratio < 100:
        return "10-100"
    return ">=100"


def security_type(code: str, registry_categories: Mapping[str, str]) -> str:
    return str(registry_categories.get(str(code).strip().upper(), "unknown_or_unmapped"))


def unit_layers(mapping: str, category: str) -> dict[str, str]:
    mapping = str(mapping or "unknown_null")
    if mapping == "stock_shares_div_100":
        volume = "stock_volume_shares_div_100_to_formal_lots"
    elif mapping == "index_raw":
        volume = "index_volume_native_raw"
    else:
        volume = "volume_unit_unconfirmed"
    return {
        "volume_unit": volume,
        "amount_unit": "amount_native_unconverted_unit_unverified",
        "price_unit": "price_native_direct_abs_tol_0.001",
        "mapping": mapping,
        "category": category,
    }


def load_registry_categories(path: Path) -> dict[str, str]:
    categories: dict[str, str] = {}
    for row in _iter_jsonl(path):
        code = str(row.get("code") or "").strip().upper()
        if code:
            categories[code] = str(row.get("category") or "unknown_or_unmapped")
    return categories


def _new_bucket() -> dict[str, Any]:
    return {
        "strict_rows": 0,
        "substantive_rows": 0,
        "precision_only_rows": 0,
        "strict_field_counts": Counter(),
        "substantive_field_counts": Counter(),
        "substantive_field_combinations": Counter(),
        "unit_counts": Counter(),
        "volume_ratio_buckets": Counter(),
        "amount_ratio_buckets": Counter(),
        "volume_unit_diagnostic_counts": Counter(),
        "amount_scale_diagnostic_counts": Counter(),
    }


def _serialise_bucket(bucket: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(bucket)
    for name in (
        "strict_field_counts",
        "substantive_field_counts",
        "substantive_field_combinations",
        "unit_counts",
        "volume_ratio_buckets",
        "amount_ratio_buckets",
        "volume_unit_diagnostic_counts",
        "amount_scale_diagnostic_counts",
    ):
        result[name] = dict(sorted(result[name].items()))
    return result


def classify(
    *,
    mismatch_ledger: Path,
    registry_candidate: Path,
    output: Path,
    sample_limit: int = 5,
) -> dict[str, Any]:
    categories = load_registry_categories(registry_candidate)
    total = substantive_count = precision_count = 0
    strict_fields: Counter[str] = Counter()
    substantive_fields_count: Counter[str] = Counter()
    combinations: Counter[str] = Counter()
    by_type: defaultdict[str, dict[str, Any]] = defaultdict(_new_bucket)
    by_mapping: defaultdict[str, dict[str, Any]] = defaultdict(_new_bucket)
    samples: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in _iter_jsonl(mismatch_ledger):
        total += 1
        code = str(row.get("code") or "").strip().upper()
        category = security_type(code, categories)
        mapping = str(row.get("source_volume_unit_mapping") or "unknown_null")
        strict = tuple(field for field in FIELDS if field in set(row.get("mismatch_fields") or ()))
        substantive = substantive_fields(row)
        formal_values = row.get("formal_values") or {}
        source_values = row.get("source_values") or {}
        volume_ratio = ratio_bucket(formal_values.get("volume"), source_values.get("volume"))
        amount_ratio = ratio_bucket(formal_values.get("amount"), source_values.get("amount"))
        bucket = by_type[category]
        unit_bucket = by_mapping[mapping]
        for target in (bucket, unit_bucket):
            target["strict_rows"] += 1
            target["strict_field_counts"].update(strict)
            target["unit_counts"][mapping] += 1
            target["volume_ratio_buckets"][volume_ratio] += 1
            target["amount_ratio_buckets"][amount_ratio] += 1
            target["volume_unit_diagnostic_counts"][
                "mapped_scale_near_1" if volume_ratio == "0.5-2" else "unit_or_source_scale_review"
            ] += 1
            target["amount_scale_diagnostic_counts"][
                "amount_scale_near_1" if amount_ratio == "0.5-2" else "amount_scale_review"
            ] += 1
        strict_fields.update(strict)
        if substantive:
            substantive_count += 1
            combo = "+".join(substantive)
            combinations[combo] += 1
            substantive_fields_count.update(substantive)
            for target in (bucket, unit_bucket):
                target["substantive_rows"] += 1
                target["substantive_field_counts"].update(substantive)
                target["substantive_field_combinations"][combo] += 1
            if len(samples[category]) < sample_limit:
                samples[category].append(
                    {
                        "code": code,
                        "trade_date": str(row.get("trade_date"))[:10],
                        "category": category,
                        "strict_fields": list(strict),
                        "substantive_fields": list(substantive),
                        "source_volume_unit_mapping": mapping,
                        "unit_layers": unit_layers(mapping, category),
                        "volume_ratio_bucket": volume_ratio,
                        "amount_ratio_bucket": amount_ratio,
                    }
                )
        else:
            precision_count += 1
            for target in (bucket, unit_bucket):
                target["precision_only_rows"] += 1

    for target in list(by_type.values()) + list(by_mapping.values()):
        # Keep the unit layer explicit even for categories that had no
        # substantive rows; the ratio buckets still describe the full strict
        # mismatch population.
        target["unit_layer_examples"] = sorted(
            {
                json.dumps(unit_layers(mapping, "unknown_or_unmapped"), ensure_ascii=False, sort_keys=True)
                for mapping in target["unit_counts"]
            }
        )
    report = {
        "schema_version": 1,
        "readonly": True,
        "input": str(mismatch_ledger),
        "registry_candidate": str(registry_candidate),
        "tolerances": TOLERANCES,
        "rows": {
            "strict_mismatch_rows": total,
            "substantive_difference_rows": substantive_count,
            "precision_only_rows": precision_count,
        },
        "global": {
            "strict_field_counts": dict(sorted(strict_fields.items())),
            "substantive_field_counts": dict(sorted(substantive_fields_count.items())),
            "substantive_field_combinations": dict(sorted(combinations.items())),
        },
        "by_security_type": {name: _serialise_bucket(bucket) for name, bucket in sorted(by_type.items())},
        "by_volume_unit_mapping": {name: _serialise_bucket(bucket) for name, bucket in sorted(by_mapping.items())},
        "samples_by_security_type": {name: values for name, values in sorted(samples.items())},
        "unit_contract": {
            "stock": unit_layers("stock_shares_div_100", "stock"),
            "index": unit_layers("index_raw", "index"),
            "amount": "amount is kept in source/formal native storage scale; source semantic unit remains unverified",
            "price": "open/high/low/close are compared directly with abs tolerance 0.001",
        },
        "gate": "blocked" if substantive_count else "review",
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mismatch-ledger", type=Path, required=True)
    parser.add_argument("--registry-candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-limit", type=int, default=5)
    args = parser.parse_args()
    report = classify(
        mismatch_ledger=args.mismatch_ledger,
        registry_candidate=args.registry_candidate,
        output=args.output,
        sample_limit=max(0, args.sample_limit),
    )
    print(json.dumps({"output": str(args.output), "rows": report["rows"], "global": report["global"], "gate": report["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Reconcile Baostock-missing index keys with existing Tencent JSON evidence.

The Tencent files are existing local artifacts.  This script parses them without
network access, binds every observation to its file SHA and JSON row, and
compares the provider values with the formal exact-key readback carried by the
Baostock reconciliation ledger.  Tencent's observed index payload uses
``volume / 100`` for formal lots and ``amount * 10000`` for native amount; both
transforms stay inside this review ledger and never mutate ClickHouse.
"""

from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

FIELDS = ("open", "high", "low", "close", "volume", "amount")
TARGET_CODES = ("000680.SH", "000688.SH", "000852.SH")
PRICE_TOLERANCE = 0.01
VOLUME_TOLERANCE = 0.01
AMOUNT_RELATIVE_TOLERANCE = 1e-6
AMOUNT_ABSOLUTE_TOLERANCE = 1.0

DEFAULT_INPUT = Path(
    r"H:\Codex\Outputs\AiStock\daily_registry_scope_explanations_20261024\index_second_source_baostock_reconciliation.jsonl.gz"
)
DEFAULT_OUTPUT = Path(
    r"H:\Codex\Outputs\AiStock\daily_registry_scope_explanations_20261024\index_missing_keys_tencent_reconciliation.jsonl.gz"
)
DEFAULT_SUMMARY = Path(
    r"H:\Codex\Outputs\AiStock\daily_registry_scope_explanations_20261024\index_missing_keys_tencent_reconciliation_summary.json"
)
DEFAULT_ARTIFACT_ROOT = Path(r"H:\Codex\Outputs\AiStock")


def _open_jsonl(path: Path, mode: str = "rt"):
    return gzip.open(path, mode, encoding="utf-8") if path.suffix == ".gz" else path.open(mode, encoding="utf-8")


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with _open_jsonl(path) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _amount_tolerance(value: float | None) -> float:
    return max(AMOUNT_ABSOLUTE_TOLERANCE, abs(value or 0.0) * AMOUNT_RELATIVE_TOLERANCE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _code_from_key(key: str) -> str | None:
    match = re.fullmatch(r"(sh|sz)(\d{6})", key.lower())
    if not match:
        return None
    market, digits = match.groups()
    return f"{digits}.{market.upper()}"


def _source_paths(artifact_root: Path) -> list[Path]:
    """Return known index JSON artifacts in deterministic order."""

    patterns = (
        "daily_full_coverage_acceptance_20261016_v1/index_history_reference/sh000680_*.txt",
        "daily_full_coverage_acceptance_20261016_v1/index_history_reference/sh000688_*.txt",
        "daily_full_coverage_acceptance_20261016_v1/index_history_reference/sh000852_*.txt",
        "daily_full_coverage_acceptance_20261016_v1/recent_reference/sh000688.txt",
        "daily_full_coverage_acceptance_20261016_v1/recent_reference/sh000852.txt",
        "daily_missing_append_20261016_v1/recent_tencent/sh000680.txt",
        "daily_missing_append_20261016_v1/recent_tencent/sh000688.txt",
        "daily_missing_append_20261016_v1/recent_tencent/sh000852.txt",
    )
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(sorted(artifact_root.glob(pattern)))
    return list(dict.fromkeys(paths))


def _parse_tencent_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    payload = text[text.find("=") + 1 :].strip() if "=" in text else text.strip()
    obj = json.loads(payload)
    data = obj.get("data") or {}
    keys = [key for key in data if _code_from_key(key) in TARGET_CODES]
    if len(keys) != 1:
        raise ValueError(f"expected exactly one target index key in {path}, got {keys}")
    key = keys[0]
    code = _code_from_key(key)
    days = (data[key] or {}).get("day") or []
    observations: dict[str, dict[str, Any]] = {}
    duplicate_dates: list[str] = []
    for row_number, row in enumerate(days, start=1):
        if not isinstance(row, list) or len(row) < 9:
            continue
        date = str(row[0])[:10]
        if not date:
            continue
        if date in observations:
            duplicate_dates.append(date)
            continue
        raw = {
            "date": date,
            "open": row[1],
            "close": row[2],
            "high": row[3],
            "low": row[4],
            "volume": row[5],
            "amount": row[8],
        }
        normalized = {
            "open": _number(row[1]),
            "high": _number(row[3]),
            "low": _number(row[4]),
            "close": _number(row[2]),
            "volume": (_number(row[5]) / 100.0) if _number(row[5]) is not None else None,
            "amount": (_number(row[8]) * 10000.0) if _number(row[8]) is not None else None,
        }
        observations[date] = {"row_number": row_number, "raw": raw, "normalized": normalized}
    dates = sorted(observations)
    return {
        "code": code,
        "path": str(path),
        "file_sha256": _sha256(path),
        "file_size_bytes": path.stat().st_size,
        "provider_key": key,
        "rows": len(observations),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "duplicate_dates": duplicate_dates,
        "observations": observations,
    }


def load_candidates(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in _iter_jsonl(path):
        code = str(row.get("code") or "").strip().upper()
        date = str(row.get("trade_date") or "")[:10]
        if row.get("disposition") != "baostock_missing_exact_key" or code not in TARGET_CODES or not date:
            continue
        key = (code, date)
        if key in seen:
            raise ValueError(f"duplicate missing exact key: {code}@{date}")
        seen.add(key)
        rows.append(row)
    return rows


def _compare(formal: Mapping[str, Any], provider: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    values = provider.get("normalized") if provider else {}
    for field in FIELDS:
        left, right = _number(formal.get(field)), _number(values.get(field))
        tolerance = PRICE_TOLERANCE if field in {"open", "high", "low", "close"} else VOLUME_TOLERANCE if field == "volume" else _amount_tolerance(left)
        result[field] = {
            "formal": left,
            "tencent_normalized": right,
            "delta_tencent_minus_formal": (right - left) if left is not None and right is not None else None,
            "tolerance": tolerance,
            "within_tolerance": left is not None and right is not None and abs(right - left) <= tolerance,
        }
    return result


def _source_inventory(paths: list[Path]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    by_code: dict[str, list[dict[str, Any]]] = {}
    inventory: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            inventory.append({"path": str(path), "exists": False})
            continue
        parsed = _parse_tencent_file(path)
        by_code.setdefault(parsed["code"], []).append(parsed)
        inventory.append({k: parsed[k] for k in ("code", "path", "file_sha256", "file_size_bytes", "provider_key", "rows", "first_date", "last_date", "duplicate_dates")})
    return by_code, inventory


def reconcile(*, input_path: Path = DEFAULT_INPUT, output_jsonl: Path = DEFAULT_OUTPUT, output_summary: Path = DEFAULT_SUMMARY, artifact_root: Path = DEFAULT_ARTIFACT_ROOT, source_paths: list[Path] | None = None) -> dict[str, Any]:
    candidates = load_candidates(input_path)
    paths = source_paths if source_paths is not None else _source_paths(artifact_root)
    by_code, inventory = _source_inventory(paths)
    counts: Counter[str] = Counter()
    by_code_counts: dict[str, Counter[str]] = {}
    missing: list[dict[str, str]] = []
    source_conflicts: list[dict[str, Any]] = []
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with _open_jsonl(output_jsonl, "wt") as handle:
        for candidate in sorted(candidates, key=lambda row: (row["code"], row["trade_date"])):
            code, date = candidate["code"], candidate["trade_date"]
            observations = [source["observations"][date] | {"source": source} for source in by_code.get(code, []) if date in source["observations"]]
            formal = candidate.get("candidate_formal_values") or candidate.get("formal_values") or {}
            if not observations:
                disposition = "tencent_source_missing"
                missing.append({"code": code, "trade_date": date})
                comparison = _compare(formal, None)
                selected = None
            else:
                comparisons = [_compare(formal, observation) for observation in observations]
                selected = observations[0]
                all_match = all(item["within_tolerance"] for item in comparisons[0].values())
                duplicate_conflict = len(comparisons) > 1 and any(
                    any(not item["within_tolerance"] for item in comparison.values())
                    for comparison in comparisons[1:]
                )
                if duplicate_conflict:
                    disposition = "tencent_source_conflict"
                    source_conflicts.append({"code": code, "trade_date": date, "source_count": len(observations)})
                elif all_match:
                    disposition = "tencent_exact_consensus"
                else:
                    disposition = "tencent_residual_review"
                comparison = comparisons[0]
            result = {
                "code": code,
                "trade_date": date,
                "baostock_status": candidate.get("disposition"),
                "formal_values": formal,
                "tencent_sources": [
                    {
                        "path": item["source"]["path"],
                        "file_sha256": item["source"]["file_sha256"],
                        "provider_key": item["source"]["provider_key"],
                        "source_row": item["row_number"],
                        "raw_values": item["raw"],
                        "normalized_values": item["normalized"],
                        "coverage": {"first_date": item["source"]["first_date"], "last_date": item["source"]["last_date"]},
                    }
                    for item in observations
                ],
                "field_comparison": comparison,
                "disposition": disposition,
                "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
            }
            counts[disposition] += 1
            by_code_counts.setdefault(code, Counter())[disposition] += 1
            handle.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
    summary = {
        "schema_version": 1,
        "readonly": True,
        "gate": "review_only",
        "input": str(input_path),
        "output_jsonl": str(output_jsonl),
        "candidate_rows": len(candidates),
        "source_inventory": inventory,
        "dispositions": dict(sorted(counts.items())),
        "by_code": {code: dict(sorted(counter.items())) for code, counter in sorted(by_code_counts.items())},
        "source_missing_count": len(missing),
        "source_conflict_count": len(source_conflicts),
        "source_missing_sample": missing[:20],
        "source_conflict_sample": source_conflicts[:20],
        "comparison_contract": {
            "exact_key": "code + trade_date",
            "tencent_volume": "raw row[5] / 100 -> formal lots (observed index scale)",
            "tencent_amount": "raw row[8] * 10000 -> native amount (observed 万元 scale)",
            "price_abs_tolerance": PRICE_TOLERANCE,
            "volume_abs_tolerance": VOLUME_TOLERANCE,
            "amount_abs_tolerance_floor": AMOUNT_ABSOLUTE_TOLERANCE,
            "amount_relative_tolerance": AMOUNT_RELATIVE_TOLERANCE,
        },
        "identity_policy": {
            "provider": "Tencent local JSON artifact",
            "provider_key_must_match_exchange": True,
            "file_sha_and_source_row_preserved": True,
            "formal_missing_rows_are_not_created": True,
            "source_evidence_is_review_only": True,
        },
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
    }
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-jsonl", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--output-summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    args = parser.parse_args()
    summary = reconcile(input_path=args.input, output_jsonl=args.output_jsonl, output_summary=args.output_summary, artifact_root=args.artifact_root)
    print(json.dumps({"candidate_rows": summary["candidate_rows"], "dispositions": summary["dispositions"], "gate": summary["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

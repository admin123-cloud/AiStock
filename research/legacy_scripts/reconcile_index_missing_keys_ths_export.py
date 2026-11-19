"""Reconcile exact index keys against a read-only Tonghuashun TXT export.

The native THS export is kept as source evidence.  This script parses the
export without changing it, records its SHA/row/coverage and compares the
six OHLCV+amount fields with the existing formal values.  It only emits a
review ledger; it never writes ClickHouse or publishes registry/member data.
"""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

FIELDS = ("open", "high", "low", "close", "volume", "amount")
TARGET_CODES = {"000680.SH", "000688.SH", "000852.SH"}
TARGET_CODE = "000852.SH"
INDEX_NAMES = {"000680.SH": "科创综指", "000688.SH": "科创50", "000852.SH": "中证1000"}
PRICE_TOLERANCE = 0.01
# The native export rounds the observed index volume after the /10000
# normalization; a 0.01 formal-lot difference is therefore expected.
VOLUME_TOLERANCE = 0.02
AMOUNT_ABSOLUTE_TOLERANCE = 100.0
AMOUNT_RELATIVE_TOLERANCE = 1e-6
MISSING = {"", "--", "-", "—"}
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}),")


def _open_jsonl(path: Path, mode: str = "rt"):
    return gzip.open(path, mode, encoding="utf-8") if path.suffix == ".gz" else path.open(mode, encoding="utf-8")


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with _open_jsonl(path) as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _decode(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030", "gbk"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("ths-export", raw, 0, len(raw), "unsupported encoding")


def _number(token: str) -> float | None:
    token = token.strip().replace(",", "")
    if token in MISSING:
        return None
    try:
        value = Decimal(token.replace("%", ""))
    except InvalidOperation as exc:
        raise ValueError(f"invalid numeric value: {token!r}") from exc
    if not value.is_finite():
        raise ValueError(f"non-finite numeric value: {token!r}")
    return float(value)


def _parse_ths_export(path: Path) -> dict[str, Any]:
    text, encoding = _decode(path)
    rows: dict[str, dict[str, Any]] = {}
    duplicate_dates: list[str] = []
    invalid_rows: list[dict[str, Any]] = []
    header: list[str] | None = None
    physical_lines = text.splitlines()
    for line_number, raw_line in enumerate(physical_lines, start=1):
        if not raw_line.strip():
            continue
        values = re.split(r"\t+", raw_line.strip())
        if header is None:
            header = values
            continue
        if len(values) < 10:
            invalid_rows.append({"line": line_number, "problem": "column_count", "values": values})
            continue
        match = DATE_RE.match(values[0])
        if not match:
            invalid_rows.append({"line": line_number, "problem": "date_syntax", "value": values[0]})
            continue
        trade_date = match.group(1)
        try:
            raw = {
                "open": _number(values[1]),
                "high": _number(values[2]),
                "low": _number(values[3]),
                "close": _number(values[4]),
                "volume": _number(values[8]),
                "amount": _number(values[9]),
            }
        except ValueError as exc:
            invalid_rows.append({"line": line_number, "problem": str(exc), "value": values})
            continue
        if trade_date in rows:
            duplicate_dates.append(trade_date)
            continue
        rows[trade_date] = {
            "source_row": line_number,
            "raw_values": raw,
            "normalized_values": {
                "open": raw["open"],
                "high": raw["high"],
                "low": raw["low"],
                "close": raw["close"],
                "volume": raw["volume"] / 10000.0 if raw["volume"] is not None else None,
                "amount": raw["amount"],
            },
        }
    if header is None:
        raise ValueError("empty THS export")
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        "encoding": encoding,
        "header": header,
        "rows": rows,
        "duplicate_dates": sorted(set(duplicate_dates)),
        "invalid_rows": invalid_rows,
        "first_date": min(rows) if rows else None,
        "last_date": max(rows) if rows else None,
    }


def load_candidates(path: Path, code_filter: str = TARGET_CODE) -> list[dict[str, Any]]:
    if code_filter not in TARGET_CODES:
        raise ValueError(f"unsupported THS index code: {code_filter}")
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in _iter_jsonl(path):
        row_code = str(row.get("code") or "").strip().upper()
        date = str(row.get("trade_date") or "")[:10]
        disposition = str(row.get("disposition") or "")
        if row_code != code_filter or disposition not in {
            "sina_source_missing",
            "sina_ohlcv_residual_review_amount_unavailable",
            "tencent_source_missing",
            "baostock_missing_exact_key",
        } or not date:
            continue
        key = (row_code, date)
        if key in seen:
            raise ValueError(f"duplicate missing exact key: {row_code}@{date}")
        seen.add(key)
        rows.append(row)
    return rows


def _amount_tolerance(value: float | None) -> float:
    return max(AMOUNT_ABSOLUTE_TOLERANCE, abs(value or 0.0) * AMOUNT_RELATIVE_TOLERANCE)


def compare_values(formal: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for field in FIELDS:
        formal_value = formal.get(field)
        source_value = source.get(field)
        formal_number = float(formal_value) if formal_value is not None else None
        source_number = float(source_value) if source_value is not None else None
        tolerance = PRICE_TOLERANCE if field in {"open", "high", "low", "close"} else VOLUME_TOLERANCE if field == "volume" else _amount_tolerance(formal_number)
        within = formal_number is not None and source_number is not None and abs(source_number - formal_number) <= tolerance
        result[field] = {
            "formal": formal_number,
            "ths_normalized": source_number,
            "delta_ths_minus_formal": source_number - formal_number if formal_number is not None and source_number is not None else None,
            "tolerance": tolerance,
            "within_tolerance": within,
        }
    return result


def reconcile(*, input_path: Path, ths_file: Path, output_jsonl: Path, output_summary: Path, code: str = TARGET_CODE) -> dict[str, Any]:
    candidates = load_candidates(input_path, code)
    source = _parse_ths_export(ths_file)
    counts: Counter[str] = Counter()
    by_code: dict[str, Counter[str]] = {}
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with _open_jsonl(output_jsonl, "wt") as handle:
        for candidate in sorted(candidates, key=lambda row: (row["code"], row["trade_date"])):
            code, trade_date = candidate["code"], candidate["trade_date"]
            source_row = source["rows"].get(trade_date)
            formal = candidate.get("formal_values") or {}
            if source_row is None:
                comparison = {field: {"formal": formal.get(field), "ths_normalized": None, "delta_ths_minus_formal": None, "tolerance": None, "within_tolerance": False} for field in FIELDS}
                disposition = "ths_export_source_missing"
            else:
                normalized = source_row["normalized_values"]
                comparison = compare_values(formal, normalized)
                disposition = "ths_export_two_source_consensus" if all(item["within_tolerance"] for item in comparison.values()) else "ths_export_residual_review"
            result = {
                "code": code,
                "trade_date": trade_date,
                "upstream_disposition": candidate.get("disposition"),
                "formal_values": formal,
                "ths_export_source": {
                    "identity": "SH_index_native_txt_export",
                    "path": source["path"],
                    "file_sha256": source["sha256"],
                    "encoding": source["encoding"],
                    "source_row": source_row["source_row"] if source_row else None,
                    "raw_values": source_row["raw_values"] if source_row else None,
                    "normalized_values": source_row["normalized_values"] if source_row else None,
                    "coverage": {"first_date": source["first_date"], "last_date": source["last_date"], "rows": len(source["rows"])},
                },
                "field_comparison": comparison,
                "disposition": disposition,
                "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
            }
            counts[disposition] += 1
            by_code.setdefault(code, Counter())[disposition] += 1
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    source_inventory = {key: value for key, value in source.items() if key != "rows"}
    source_inventory["rows"] = len(source["rows"])
    summary = {
        "schema_version": 1,
        "readonly": True,
        "gate": "review_only",
        "input": str(input_path),
        "ths_file": source_inventory,
        "candidate_rows": len(candidates),
        "dispositions": dict(sorted(counts.items())),
        "by_code": {code: dict(sorted(counter.items())) for code, counter in sorted(by_code.items())},
        "comparison_contract": {
            "exact_key": "code + trade_date",
            "ths_volume": "native 总手 / 10000 -> formal lots (observed index scale)",
            "ths_amount": "native 金额 value as exported",
            "price_abs_tolerance": PRICE_TOLERANCE,
            "volume_abs_tolerance": VOLUME_TOLERANCE,
            "amount_abs_tolerance_floor": AMOUNT_ABSOLUTE_TOLERANCE,
            "amount_relative_tolerance": AMOUNT_RELATIVE_TOLERANCE,
        },
        "identity": {"code": code, "market": "SH", "name": INDEX_NAMES[code], "period": "日线", "adjustment": "不复权"},
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
    }
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--ths-file", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--output-summary", type=Path, required=True)
    parser.add_argument("--code", choices=sorted(TARGET_CODES), default=TARGET_CODE)
    args = parser.parse_args()
    summary = reconcile(input_path=args.input, ths_file=args.ths_file, output_jsonl=args.output_jsonl, output_summary=args.output_summary, code=args.code)
    print(json.dumps({"candidate_rows": summary["candidate_rows"], "dispositions": summary["dispositions"], "gate": summary["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

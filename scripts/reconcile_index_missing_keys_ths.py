"""Reconcile Baostock-missing index keys against existing THS cache, read-only.

The THS ``.day`` cache stores SH indices under internal aliases such as
``1B0680.day``.  Numeric ``000680.day``/``000688.day``/``000852.day`` files
under ``sznse`` are stock files with the same digits and must never be used as
evidence for the ``.SH`` indices.  This script records both facts per exact
code/date and keeps any cache volume transform inside the comparison ledger.
It never starts THS, downloads data, or writes ClickHouse.
"""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIELDS = ("open", "high", "low", "close", "volume", "amount")
TARGET_CODES = ("000680.SH", "000688.SH", "000852.SH")
PRICE_TOLERANCE = 0.01
VOLUME_TOLERANCE = 0.01
AMOUNT_RELATIVE_TOLERANCE = 1e-6
AMOUNT_ABSOLUTE_TOLERANCE = 1.0
DEFAULT_HISTORY_ROOT = Path(r"D:\同花顺\history")
DEFAULT_READER = Path.home() / ".codex" / "skills" / "ths_daily_kline_export" / "scripts" / "ths_history_cache_reader.py"

# These are THS internal SH-index aliases observed in the existing cache
# inventory.  The aliases are intentionally explicit: stripping a prefix from
# an arbitrary cache filename is not an identity proof.
INDEX_CACHE_ALIASES: dict[str, str] = {
    "000680.SH": "1B0680",
    "000688.SH": "1B0688",
    "000852.SH": "1B0852",
}


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


def _load_reader(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"THS cache reader is unavailable: {path}")
    spec = importlib.util.spec_from_file_location("aistock_ths_cache_reader", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load THS cache reader: {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves postponed annotations through sys.modules while
    # the external module is being executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def _decimal_float(value: Decimal | None) -> float | None:
    return _number(value) if value is not None else None


def _bar_values(bar: Any) -> dict[str, float | None]:
    return {
        "open": _decimal_float(bar.open),
        "high": _decimal_float(bar.high),
        "low": _decimal_float(bar.low),
        "close": _decimal_float(bar.close),
        "volume": (_decimal_float(bar.volume) / 10000.0) if bar.volume is not None else None,
        "amount": _decimal_float(bar.amount),
    }


def _compare_values(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for field in FIELDS:
        left_number, right_number = _number(left.get(field)), _number(right.get(field))
        tolerance = PRICE_TOLERANCE if field in {"open", "high", "low", "close"} else VOLUME_TOLERANCE if field == "volume" else _amount_tolerance(right_number)
        result[field] = {
            "wstock_normalized": left_number,
            "ths_normalized": right_number,
            "delta_ths_minus_wstock": (right_number - left_number) if left_number is not None and right_number is not None else None,
            "tolerance": tolerance,
            "within_tolerance": left_number is not None and right_number is not None and abs(right_number - left_number) <= tolerance,
        }
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_cache_inventory(history_root: Path, reader_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    reader = _load_reader(reader_path)
    caches: dict[str, dict[str, Any]] = {}
    inventory: dict[str, Any] = {}
    for code, alias in INDEX_CACHE_ALIASES.items():
        alias_path = history_root / "shase" / "day" / f"{alias}.day"
        collision_path = history_root / "sznse" / "day" / f"{code[:6]}.day"
        entry: dict[str, Any] = {
            "expected_index": code,
            "expected_market": "SH",
            "internal_alias": alias,
            "alias_path": str(alias_path),
            "alias_exists": alias_path.exists(),
            "collision_path": str(collision_path),
            "collision_exists": collision_path.exists(),
        }
        if alias_path.exists():
            cache = reader.read_daily_cache(alias_path)
            bars = {bar.date: bar for bar in cache.bars}
            caches[code] = {"identity": "SH_index_internal_alias", "path": alias_path, "sha256": cache.sha256, "bars": bars, "rows": len(cache.bars), "first_date": cache.bars[0].date if cache.bars else None, "last_date": cache.bars[-1].date if cache.bars else None, "anomalies": list(cache.anomalies), "incomplete_rows": list(cache.incomplete_rows)}
            entry.update({"identity": "SH_index_internal_alias", "source_sha256": cache.sha256, "rows": len(cache.bars), "first_date": cache.bars[0].date if cache.bars else None, "last_date": cache.bars[-1].date if cache.bars else None, "ohlc_anomaly_count": len(cache.anomalies), "incomplete_row_count": len(cache.incomplete_rows)})
        elif collision_path.exists():
            cache = reader.read_daily_cache(collision_path)
            bars = {bar.date: bar for bar in cache.bars}
            caches[code] = {"identity": "SZ_stock_numeric_collision", "path": collision_path, "sha256": cache.sha256, "bars": bars, "rows": len(cache.bars), "first_date": cache.bars[0].date if cache.bars else None, "last_date": cache.bars[-1].date if cache.bars else None, "anomalies": list(cache.anomalies), "incomplete_rows": list(cache.incomplete_rows)}
            entry.update({"identity": "SZ_stock_numeric_collision", "source_sha256": cache.sha256, "rows": len(cache.bars), "first_date": cache.bars[0].date if cache.bars else None, "last_date": cache.bars[-1].date if cache.bars else None, "ohlc_anomaly_count": len(cache.anomalies), "incomplete_row_count": len(cache.incomplete_rows)})
        else:
            caches[code] = {"identity": "unavailable", "path": None, "sha256": None, "bars": {}, "rows": 0, "first_date": None, "last_date": None, "anomalies": [], "incomplete_rows": []}
            entry.update({"identity": "unavailable", "source_sha256": None, "rows": 0, "first_date": None, "last_date": None, "ohlc_anomaly_count": 0, "incomplete_row_count": 0})
        inventory[code] = entry
    return caches, inventory


def reconcile(*, input_path: Path, output_jsonl: Path, output_summary: Path, history_root: Path = DEFAULT_HISTORY_ROOT, reader_path: Path = DEFAULT_READER) -> dict[str, Any]:
    candidates = load_candidates(input_path)
    caches, inventory = load_cache_inventory(history_root, reader_path)
    counts: Counter[str] = Counter()
    by_code: dict[str, Counter[str]] = {}
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with _open_jsonl(output_jsonl, "wt") as handle:
        for candidate in sorted(candidates, key=lambda row: (row["code"], row["trade_date"])):
            code, date = candidate["code"], candidate["trade_date"]
            cache = caches[code]
            bar = cache["bars"].get(date)
            wstock = candidate.get("wstock_normalized") or {}
            ths_values = _bar_values(bar) if bar is not None else {field: None for field in FIELDS}
            comparison = _compare_values(wstock, ths_values) if bar is not None else {field: {"wstock_normalized": _number(wstock.get(field)), "ths_normalized": None, "delta_ths_minus_wstock": None, "tolerance": None, "within_tolerance": False} for field in FIELDS}
            if cache["identity"] == "SH_index_internal_alias":
                if bar is None:
                    disposition = "ths_index_alias_date_missing"
                elif all(item["within_tolerance"] for item in comparison.values()):
                    disposition = "ths_index_two_source_consensus"
                else:
                    disposition = "ths_index_residual_review"
            elif cache["identity"] == "SZ_stock_numeric_collision":
                disposition = "ths_numeric_collision_identity_mismatch"
            else:
                disposition = "ths_index_source_unavailable"
            result = {
                "code": code,
                "trade_date": date,
                "baostock_status": candidate.get("disposition"),
                "formal_values": candidate.get("candidate_formal_values") or {},
                "wstock_normalized": wstock,
                "ths_source": {
                    "identity": cache["identity"],
                    "path": str(cache["path"]) if cache["path"] else None,
                    "internal_alias": INDEX_CACHE_ALIASES[code],
                    "file_sha256": cache["sha256"],
                    "source_row": next((index + 1 for index, value in enumerate(sorted(cache["bars"])) if value == date), None) if bar is not None else None,
                    "raw_values": {field: (str(getattr(bar, field)) if bar is not None and getattr(bar, field) is not None else None) for field in ("open", "high", "low", "close", "volume", "amount")},
                    "normalized_values": ths_values,
                },
                "field_comparison": comparison,
                "disposition": disposition,
                "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
            }
            counts[disposition] += 1
            by_code.setdefault(code, Counter())[disposition] += 1
            handle.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
    summary = {
        "schema_version": 1,
        "readonly": True,
        "gate": "review_only",
        "input": str(input_path),
        "output_jsonl": str(output_jsonl),
        "candidate_rows": len(candidates),
        "ths_reader": {"path": str(reader_path), "sha256": _sha256(reader_path)},
        "source_inventory": inventory,
        "dispositions": dict(sorted(counts.items())),
        "by_code": {code: dict(sorted(counter.items())) for code, counter in sorted(by_code.items())},
        "comparison_contract": {
            "exact_key": "code + trade_date",
            "ths_volume": "cache volume / 10000 -> formal lots (observed index scale)",
            "price_abs_tolerance": PRICE_TOLERANCE,
            "volume_abs_tolerance": VOLUME_TOLERANCE,
            "amount_abs_tolerance_floor": AMOUNT_ABSOLUTE_TOLERANCE,
            "amount_relative_tolerance": AMOUNT_RELATIVE_TOLERANCE,
        },
        "identity_policy": {
            "index_aliases_are_explicit": True,
            "numeric_sz_stock_collision_is_not_index_evidence": True,
            "cache_values_are_review_only": True,
            "formal_missing_rows_are_not_created_by_this_script": True,
        },
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
    }
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--output-summary", type=Path, required=True)
    parser.add_argument("--history-root", type=Path, default=DEFAULT_HISTORY_ROOT)
    parser.add_argument("--reader", type=Path, default=DEFAULT_READER)
    args = parser.parse_args()
    summary = reconcile(input_path=args.input, output_jsonl=args.output_jsonl, output_summary=args.output_summary, history_root=args.history_root, reader_path=args.reader)
    print(json.dumps({"candidate_rows": summary["candidate_rows"], "dispositions": summary["dispositions"], "gate": summary["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

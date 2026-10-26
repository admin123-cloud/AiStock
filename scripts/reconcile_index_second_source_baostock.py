"""Reconcile index exact keys with Baostock as an independent read-only source.

The input is the review-only exact-key candidate ledger produced by
``build_index_volume_reconciliation_candidates.py``.  This script queries
Baostock daily data, records the provider payload and query ranges, then
compares the WStock/TDX evidence row with Baostock on the same code/date.
It never writes ClickHouse and it never publishes a registry/member scope.

Baostock returns index volume in a provider scale observed as 100 times the
WStock ``volume_shares`` field.  The comparison therefore uses
``baostock.volume / 10000`` versus ``wstock.volume_shares / 100`` for the
formal lots.  This transform is recorded per row; it is not applied to the
formal table.
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

from scripts.build_index_volume_reconciliation_candidates import STABLE_CODES

FIELDS = ("open", "high", "low", "close", "volume", "amount")
PRICE_TOLERANCE = 0.01
VOLUME_TOLERANCE = 0.01
AMOUNT_RELATIVE_TOLERANCE = 1e-6
AMOUNT_ABSOLUTE_TOLERANCE = 1.0
DEFAULT_BAOSTOCK_ROOT = Path(
    r"H:\Codex\Outputs\AiStock\priority26_tdx_wstock_scope_20261019_v1"
)


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


def _same(left: Any, right: Any, tolerance: float) -> bool:
    left_number, right_number = _number(left), _number(right)
    return left_number is not None and right_number is not None and abs(left_number - right_number) <= tolerance


def load_candidates(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _iter_jsonl(path):
        code = str(row.get("code") or "").strip().upper()
        date = str(row.get("trade_date") or "")[:10]
        if code in STABLE_CODES and date:
            key = (code, date)
            if key in rows:
                raise ValueError(f"duplicate candidate key: {code}@{date}")
            rows[key] = row
    return rows


def _provider_code(code: str) -> str:
    market = "sh" if code.endswith(".SH") else "sz"
    return f"{market}.{code[:6]}"


def _load_baostock_module(root: Path):
    vendor = root / "vendor"
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    import baostock as bs  # type: ignore

    return bs


def query_provider(
    candidates: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    baostock_root: Path,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    """Fetch all candidate date ranges once per code and retain raw rows."""

    bs = _load_baostock_module(baostock_root)
    login = bs.login()
    if str(getattr(login, "error_code", "")) != "0":
        return {}, {"provider": "Baostock", "login_error_code": login.error_code, "login_error_msg": login.error_msg, "rows": 0}
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    by_code: dict[str, list[str]] = {}
    for code, date in candidates:
        by_code.setdefault(code, []).append(date)
    receipts: list[dict[str, Any]] = []
    try:
        for code in STABLE_CODES:
            dates = sorted(by_code.get(code, []))
            if not dates:
                continue
            request = {
                "code": _provider_code(code),
                "fields": "date,code,open,high,low,close,volume,amount,adjustflag,tradestatus",
                "start_date": dates[0],
                "end_date": dates[-1],
                "frequency": "d",
                "adjustflag": "3",
            }
            result = bs.query_history_k_data_plus(**request)
            provider_rows = 0
            error_code = str(getattr(result, "error_code", ""))
            error_msg = str(getattr(result, "error_msg", ""))
            while error_code == "0" and result.next():
                values = result.get_row_data()
                provider_rows += 1
                if len(values) < 10:
                    continue
                row = {
                    "provider_code": values[1],
                    "trade_date": values[0],
                    "open": values[2],
                    "high": values[3],
                    "low": values[4],
                    "close": values[5],
                    "volume_raw": values[6],
                    "amount": values[7],
                    "adjustflag": values[8],
                    "tradestatus": values[9],
                }
                key = (code, values[0][:10])
                if key in candidates:
                    rows[key] = row
            receipts.append({"code": code, "provider_code": request["code"], "request": request, "error_code": error_code, "error_msg": error_msg, "provider_rows": provider_rows, "candidate_rows": sum(1 for date in dates if (code, date) in rows)})
        summary = {"provider": "Baostock", "login_error_code": "0", "login_error_msg": "success", "rows": len(rows), "receipts": receipts}
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    return rows, summary


def compare_row(candidate: Mapping[str, Any], provider: Mapping[str, Any] | None) -> dict[str, Any]:
    code = str(candidate.get("code") or "").upper()
    date = str(candidate.get("trade_date") or "")[:10]
    source = candidate.get("source_values_raw") or {}
    source_values = {
        "open": source.get("open"),
        "high": source.get("high"),
        "low": source.get("low"),
        "close": source.get("close"),
        "volume": (_number(source.get("volume_shares")) / 100.0) if _number(source.get("volume_shares")) is not None else None,
        "amount": source.get("amount"),
    }
    provider_values = {
        "open": provider.get("open") if provider else None,
        "high": provider.get("high") if provider else None,
        "low": provider.get("low") if provider else None,
        "close": provider.get("close") if provider else None,
        "volume": (_number(provider.get("volume_raw")) / 10000.0) if provider else None,
        "amount": provider.get("amount") if provider else None,
    }
    diffs: dict[str, dict[str, Any]] = {}
    for field in FIELDS:
        tolerance = PRICE_TOLERANCE if field in {"open", "high", "low", "close"} else VOLUME_TOLERANCE if field == "volume" else _amount_tolerance(_number(provider_values["amount"]))
        left, right = _number(source_values[field]), _number(provider_values[field])
        diffs[field] = {"source_normalized": left, "baostock_normalized": right, "delta": (right - left) if left is not None and right is not None else None, "tolerance": tolerance, "within_tolerance": _same(left, right, tolerance)}
    missing = provider is None
    disposition = "baostock_missing_exact_key" if missing else "two_source_consensus" if all(item["within_tolerance"] for item in diffs.values()) else "two_source_residual_review"
    return {
        "code": code,
        "trade_date": date,
        "source_table": candidate.get("source_table"),
        "source_file_sha": candidate.get("source_file_sha"),
        "source_row": candidate.get("source_row"),
        "candidate_formal_values": candidate.get("formal_values") or {},
        "wstock_normalized": source_values,
        "baostock_raw": dict(provider or {}),
        "baostock_normalized": provider_values,
        "baostock_volume_transform": "volume_raw / 10000 -> formal lots (observed provider scale)",
        "field_comparison": diffs,
        "disposition": disposition,
        "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
    }


def reconcile(*, candidates_path: Path, output_jsonl: Path, output_summary: Path, baostock_root: Path) -> dict[str, Any]:
    candidates = load_candidates(candidates_path)
    provider_rows, provider_summary = query_provider(candidates, baostock_root=baostock_root)
    counts: Counter[str] = Counter()
    by_code: dict[str, Counter[str]] = {}
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with _open_jsonl(output_jsonl, "wt") as handle:
        for key in sorted(candidates):
            row = compare_row(candidates[key], provider_rows.get(key))
            counts[row["disposition"]] += 1
            by_code.setdefault(row["code"], Counter())[row["disposition"]] += 1
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    summary = {
        "schema_version": 1,
        "readonly": True,
        "gate": "review_only",
        "input_candidates": str(candidates_path),
        "output_jsonl": str(output_jsonl),
        "candidate_rows": len(candidates),
        "formal_key_readback": {
            "rows_with_formal_values": sum(bool(row.get("formal_values")) for row in candidates.values()),
            "rows_without_formal_values": sum(not bool(row.get("formal_values")) for row in candidates.values()),
            "interpretation": "输入候选来自正式表 exact-key 对账；缺失正式键不会被本脚本补写。",
        },
        "provider_readback": provider_summary,
        "dispositions": dict(sorted(counts.items())),
        "by_code": {code: dict(sorted(value.items())) for code, value in sorted(by_code.items())},
        "comparison_contract": {
            "price_abs_tolerance": PRICE_TOLERANCE,
            "volume_abs_tolerance": VOLUME_TOLERANCE,
            "amount_abs_tolerance_floor": AMOUNT_ABSOLUTE_TOLERANCE,
            "amount_relative_tolerance": AMOUNT_RELATIVE_TOLERANCE,
            "wstock_volume": "volume_shares / 100",
            "baostock_volume": "volume_raw / 10000",
            "exact_key": "code + trade_date",
        },
        "policy": {
            "source_consensus_can_authorize_only_exact_key_follow_up": True,
            "does_not_authorize_global_unit_rewrite": True,
            "does_not_bypass_live_schema_gate": True,
            "production_mutation": {"ddl": False, "dml": False, "registry_publish": False},
        },
    }
    output_summary.parent.mkdir(parents=True, exist_ok=True)
    output_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output-jsonl", type=Path, required=True)
    parser.add_argument("--output-summary", type=Path, required=True)
    parser.add_argument("--baostock-root", type=Path, default=DEFAULT_BAOSTOCK_ROOT)
    args = parser.parse_args()
    summary = reconcile(candidates_path=args.candidates, output_jsonl=args.output_jsonl, output_summary=args.output_summary, baostock_root=args.baostock_root)
    print(json.dumps({"output_jsonl": str(args.output_jsonl), "output_summary": str(args.output_summary), "candidate_rows": summary["candidate_rows"], "dispositions": summary["dispositions"], "gate": summary["gate"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

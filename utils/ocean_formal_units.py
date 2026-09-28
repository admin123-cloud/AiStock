"""Conservative classification for Ocean-vs-formal quantity differences.

This module deliberately does not convert or reconcile values.  OHLC changes
are real value evidence; volume/amount-only changes remain unit-contract
uncertainty until the formal table's unit contract is independently proven.
"""

from __future__ import annotations

import math
from typing import Any, Mapping


OHLC_FIELDS = frozenset({"open", "high", "low", "close"})
QUANTITY_FIELDS = frozenset({"source_volume_lots", "source_amount_yuan"})


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _ratio(formal: Any, source: Any) -> float | None:
    formal_number = _number(formal)
    source_number = _number(source)
    if formal_number is None or source_number in (None, 0.0):
        return None
    return formal_number / source_number


def _scale_signal(ratio: float | None, scales: tuple[float, ...]) -> str | None:
    if ratio is None:
        return None
    for scale in scales:
        if abs(ratio - scale) <= max(abs(scale) * 0.01, 1e-9):
            return f"near_{scale:g}"
    return None


def classify_formal_conflict(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return an evidence-only class for one formal conflict row.

    The returned ``unit_status`` is intentionally ``unconfirmed`` for all
    quantity-only differences.  A ratio near a conventional scale is recorded
    as a signal, never treated as authorization to convert or overwrite.
    """

    if row.get("reason") == "formal_duplicate_key":
        return {
            "conflict_class": "formal_duplicate_key",
            "unit_status": "not_applicable",
        }
    fields = frozenset(str(value) for value in row.get("different_fields") or ())
    candidate = row.get("candidate") or {}
    formal = row.get("formal") or {}
    ohlc = fields & OHLC_FIELDS
    quantity = fields & QUANTITY_FIELDS
    if ohlc and quantity:
        conflict_class = "real_ohlc_and_unit_unconfirmed_quantity"
    elif ohlc:
        conflict_class = "real_ohlc_conflict"
    elif quantity == {"source_amount_yuan"}:
        conflict_class = "unit_unconfirmed_amount"
    elif quantity == {"source_volume_lots"}:
        conflict_class = "unit_unconfirmed_volume"
    elif quantity:
        conflict_class = "unit_unconfirmed_quantity_units"
    else:
        conflict_class = "formal_existing_value_conflict_unclassified"
    result: dict[str, Any] = {
        "conflict_class": conflict_class,
        "unit_status": "unconfirmed" if quantity else "not_applicable",
    }
    if "source_amount_yuan" in quantity:
        amount_ratio = _ratio(formal.get("source_amount_yuan"), candidate.get("source_amount_yuan"))
        result["formal_to_source_amount_ratio"] = amount_ratio
        result["amount_scale_signal"] = _scale_signal(amount_ratio, (10000.0, 0.0001))
    if "source_volume_lots" in quantity:
        volume_ratio = _ratio(formal.get("source_volume_lots"), candidate.get("source_volume_lots"))
        result["formal_to_source_volume_ratio"] = volume_ratio
        result["volume_scale_signal"] = _scale_signal(volume_ratio, (100.0, 0.01))
    return result


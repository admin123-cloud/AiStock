"""Shared tolerance rules for purchased 5m to daily reconciliation."""
from __future__ import annotations

from math import isfinite
from typing import Iterable


# A small source-rounding or aggregation difference is acceptable for price,
# but the rule must remain relative so the same absolute delta is not equally
# acceptable for a 2-yuan and a 200-yuan security.
PRICE_RELATIVE_TOLERANCE = 0.005
RELATIVE_TOLERANCE = 0.001
PRICE_REFERENCE_FLOOR = 1.0


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def price_reference(values: Iterable[object], *, floor: float = PRICE_REFERENCE_FLOOR) -> float | None:
    usable = [abs(number) for value in values if (number := optional_float(value)) is not None]
    if not usable:
        return None
    return max(max(usable), float(floor))


def max_price_abs_diff(minute_values: Iterable[object], daily_values: Iterable[object]) -> float | None:
    minute = [optional_float(value) for value in minute_values]
    daily = [optional_float(value) for value in daily_values]
    if len(minute) != len(daily) or not minute or any(value is None for value in minute + daily):
        return None
    return max(abs(float(left) - float(right)) for left, right in zip(minute, daily))


def price_relative_diff(price_abs_diff: object, daily_values: Iterable[object]) -> float | None:
    absolute = optional_float(price_abs_diff)
    reference = price_reference(daily_values)
    if absolute is None or reference is None:
        return None
    return absolute / reference


def within_relative_tolerance(value: object, reference: object, tolerance: float) -> bool:
    measured = optional_float(value)
    baseline = optional_float(reference)
    if measured is None or baseline is None:
        return False
    return abs(measured - baseline) / max(abs(baseline), 1.0) <= float(tolerance)

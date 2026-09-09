"""Domain transformations; no scheduler, notification or order side effects."""
import re
import numpy as np
import pandas as pd
from typing import Any,Optional

def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, float) and (np.isnan(value) or np.isinf(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, str) and value.strip() == "":
            return None
        val = float(value)
        if np.isnan(val) or np.isinf(val):
            return None
        return val
    except Exception:
        return None


def _normalize_stock_code6(value: Any) -> str:
    raw = str(value or "").strip().upper()
    match = re.search(r"(\d{6})", raw)
    if match:
        return match.group(1)
    digits = re.sub(r"\D+", "", raw)
    if digits and len(digits) <= 6:
        return digits.zfill(6)
    return ""


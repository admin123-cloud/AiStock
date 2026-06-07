from __future__ import annotations

from copy import deepcopy
from typing import Dict, Optional


COST_PROFILES: Dict[str, Dict[str, float]] = {
    "current": {
        "slippage_buy": 0.0020,
        "slippage_sell": 0.0020,
        "commission_buy": 0.0003,
        "commission_sell": 0.0003,
        "stamp_tax_sell": 0.0010,
    },
    "optimistic": {
        "slippage_buy": 0.0005,
        "slippage_sell": 0.0005,
        "commission_buy": 0.0002,
        "commission_sell": 0.0002,
        "stamp_tax_sell": 0.0010,
    },
    "neutral": {
        "slippage_buy": 0.0010,
        "slippage_sell": 0.0010,
        "commission_buy": 0.0003,
        "commission_sell": 0.0003,
        "stamp_tax_sell": 0.0010,
    },
    "conservative": {
        "slippage_buy": 0.0020,
        "slippage_sell": 0.0020,
        "commission_buy": 0.0005,
        "commission_sell": 0.0005,
        "stamp_tax_sell": 0.0010,
    },
}


PROFILE_NOTES: Dict[str, str] = {
    "current": "Current repo default: 20bps one-side slippage + 3bps commission + 10bps sell stamp tax.",
    "optimistic": "Tighter fill assumption for liquid A-share names, still includes sell stamp tax.",
    "neutral": "Balanced professional baseline for daily A-share backtests.",
    "conservative": "Stress profile for weaker liquidity or more adverse execution.",
}


def available_cost_profiles() -> list[str]:
    return list(COST_PROFILES.keys())


def resolve_cost_profile(
    profile_name: str = "current",
    overrides: Optional[Dict[str, Optional[float]]] = None,
) -> Dict[str, float]:
    key = str(profile_name or "current").strip().lower()
    if key not in COST_PROFILES:
        valid = ", ".join(available_cost_profiles())
        raise ValueError(f"Unknown cost profile: {profile_name}. Valid options: {valid}")
    result = deepcopy(COST_PROFILES[key])
    if overrides:
        for field, value in overrides.items():
            if value is None:
                continue
            result[field] = float(value)
    result["cost_profile_name"] = key
    return result


def cost_profile_note(profile_name: str) -> str:
    key = str(profile_name or "current").strip().lower()
    return PROFILE_NOTES.get(key, "")

"""Read and validate the canonical machine-readable strategy contracts."""

from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_G3_SCORE88_CONTRACT_PATH = PROJECT_ROOT / "config" / "strategy_contracts" / "g3_institutional_mainwave_score88_v1.json"


@lru_cache(maxsize=1)
def formal_g3_score88_contract() -> dict[str, Any]:
    raw = FORMAL_G3_SCORE88_CONTRACT_PATH.read_bytes()
    contract = json.loads(raw.decode("utf-8"))
    if contract.get("strategy_id") != "g3_institutional_mainwave_score88_v1":
        raise ValueError("Unexpected formal G3 contract strategy_id")
    if contract.get("trade_strategy") != "institutional_mainwave_score88":
        raise ValueError("Unexpected formal G3 contract trade_strategy")
    if contract.get("status") != "formal_shadow_only":
        raise ValueError("Formal G3 contract must remain shadow-only")
    if contract.get("guardrails", {}).get("auto_order_allowed") is not False:
        raise ValueError("Formal G3 contract must not allow automatic orders")
    return contract


def formal_g3_score88_contract_metadata() -> dict[str, Any]:
    raw = FORMAL_G3_SCORE88_CONTRACT_PATH.read_bytes()
    contract = formal_g3_score88_contract()
    return {
        "path": "config/strategy_contracts/g3_institutional_mainwave_score88_v1.json",
        "schema_version": contract["schema_version"],
        "strategy_id": contract["strategy_id"],
        "trade_strategy": contract["trade_strategy"],
        "sha256": sha256(raw).hexdigest(),
    }

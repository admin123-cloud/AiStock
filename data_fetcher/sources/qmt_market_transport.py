"""Select QMT market transport; trading clients are outside this module."""
from __future__ import annotations

import json
import os

from utils.paths import runtime_path


def market_transport() -> str:
    explicit = os.getenv("AISTOCK_QMT_MARKET_TRANSPORT", "").strip().lower()
    if explicit:
        if explicit not in {"builtin", "xtquant"}:
            raise ValueError("AISTOCK_QMT_MARKET_TRANSPORT must be builtin or xtquant")
        return explicit
    path = runtime_path("qmt_builtin_market", "active_transport.json")
    if not path.exists():
        return "xtquant"
    body = json.loads(path.read_text(encoding="utf-8"))
    if body.get("active_transport") != "builtin" or not body.get("verification_passed"):
        raise RuntimeError("Invalid QMT transport activation record")
    return "builtin"


def load_market_api():
    if market_transport() == "builtin":
        from .qmt_builtin import QmtBuiltinMarketApi
        return QmtBuiltinMarketApi()
    from xtquant import xtdata
    return xtdata

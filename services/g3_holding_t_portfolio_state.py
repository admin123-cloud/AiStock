"""Persistent, manual-only position state for the holding-T page."""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.paths import runtime_path

STATE_PATH = runtime_path("g3_holding_t_paper", "portfolio_state.json")
DEFAULT_CODES = ("300037.SZ", "300054.SZ", "603881.SH")

def default_state() -> dict[str, Any]:
    return {"schema_version": 1, "target_total_weight_pct": 100.0, "single_t_leg_weight_pct": 25.0,
            "positions": {code: {"target_weight_pct": 50.0, "current_weight_pct": 50.0} for code in DEFAULT_CODES}, "events": []}

def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try: state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): state = default_state()
    state.setdefault("positions", {})
    for code in DEFAULT_CODES: state["positions"].setdefault(code, {"target_weight_pct": 50.0, "current_weight_pct": 0.0})
    state.setdefault("events", [])
    state.setdefault("single_t_leg_weight_pct", 25.0)
    return state

def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> dict[str, Any]:
    # `_editing` is a front-end-only switch.  Never persist it, otherwise a
    # browser refresh could leave a record permanently in edit mode.
    state["events"] = [
        {key: value for key, value in event.items() if key != "_editing"}
        for event in state.get("events", [])
        if isinstance(event, dict)
    ][-200:]
    state["updated_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return state

def confirm_position_action(payload: dict[str, Any], path: Path = STATE_PATH) -> dict[str, Any]:
    state = load_state(path); code = str(payload.get("code") or "").upper(); side = str(payload.get("side") or "").lower()
    if code not in state["positions"] or side not in {"buy", "sell"}: raise ValueError("invalid code or side")
    try:
        price = float(payload.get("price"))
        shares_raw = float(payload.get("shares"))
    except (TypeError, ValueError):
        raise ValueError("positive price and whole shares are required") from None
    if not math.isfinite(price) or price <= 0 or not math.isfinite(shares_raw) or shares_raw <= 0 or not shares_raw.is_integer():
        raise ValueError("positive price and whole shares are required")
    shares = int(shares_raw)
    leg = float(payload.get("weight_pct") or state["single_t_leg_weight_pct"])
    pos = state["positions"][code]; before = float(pos.get("current_weight_pct") or 0); target = float(pos.get("target_weight_pct") or 50)
    after = min(target, before + leg) if side == "buy" else max(0.0, before - leg)
    pos["current_weight_pct"] = after
    event = {
        "at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "code": code,
        "side": side,
        "weight_pct": leg,
        "before_weight_pct": before,
        "after_weight_pct": after,
        "price": round(price, 4),
        "shares": shares,
        "notional": round(price * shares, 2),
        "source": "manual_page_confirmation",
    }
    state["events"] = (state["events"] + [event])[-200:]
    save_state(state, path); return {"state": state, "event": event}

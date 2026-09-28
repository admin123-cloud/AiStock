from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_fetcher.sources.qmtmini_client import QmtMiniTradingClient
from utils.paths import runtime_path


BROKER_STATE_PATH = runtime_path("gen3_state_alpha", "broker_state.json")


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _int(value: Any) -> int:
    number = _num(value)
    return int(number or 0)


def _code6(value: Any) -> str:
    text = str(value or "").strip().upper()
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def _read_state() -> dict[str, Any]:
    if not BROKER_STATE_PATH.exists():
        return {"holdings": [], "capital": {}, "broker_trades": []}
    try:
        return json.loads(BROKER_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"holdings": [], "capital": {}, "broker_trades": []}


def _write_state(state: dict[str, Any]) -> None:
    BROKER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BROKER_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _position_to_holding(row: dict[str, Any]) -> dict[str, Any]:
    code_raw = str(row.get("stock_code") or "").strip().upper()
    code = _code6(code_raw)
    shares = _int(row.get("volume"))
    cost_price = _num(row.get("avg_price")) or _num(row.get("cost_price")) or _num(row.get("open_price"))
    current_price = _num(row.get("last_price"))
    market_value = _num(row.get("market_value"))
    if current_price is None and market_value is not None and shares > 0:
        current_price = market_value / shares
    return {
        "code": code,
        "code_raw": code_raw,
        "name": row.get("stock_name") or code,
        "shares": shares,
        "available_shares": _int(row.get("can_use_volume")),
        "cost_price": cost_price,
        "current_price": current_price,
        "market_value": market_value,
        "position_cost": _num(row.get("position_cost")),
        "route": "broker_real_position",
        "route_label": "QMT Mini real position",
        "trade_status": "broker_open",
        "management_action": "check_exit_contract",
        "exit_contract": "QMT Mini read-only position snapshot; managed by the G3 exit contract.",
        "source": "qmtmini_readonly_snapshot",
    }


def sync_qmtmini_broker_snapshot() -> dict[str, Any]:
    client = QmtMiniTradingClient()
    try:
        connect = client.connect()
        if not connect.get("ok"):
            return {"ok": False, "mode": "qmtmini_broker_snapshot_sync", "connect": connect}
        snapshot = client.account_snapshot(include_sensitive=True)
    finally:
        client.close()

    asset = snapshot.get("asset") if isinstance(snapshot.get("asset"), dict) else {}
    positions = [item for item in snapshot.get("positions") or [] if isinstance(item, dict)]
    holdings = [
        item
        for item in (_position_to_holding(row) for row in positions)
        if item.get("code") and int(item.get("shares") or 0) > 0
    ]
    capital = {
        "fund_balance": _num(asset.get("total_asset")),
        "available_cash": _num(asset.get("cash")),
        "withdrawable_cash": _num(asset.get("cash")),
        "frozen_cash": _num(asset.get("frozen_cash")),
        "market_value": _num(asset.get("market_value")),
        "total_capital": _num(asset.get("total_asset")),
        "holding_market_value": _num(asset.get("market_value")),
        "holdings_stale": False,
        "available_cash_derived": False,
    }
    if capital["available_cash"] is not None and capital["market_value"] is not None:
        capital["available_with_holdings"] = capital["available_cash"] + capital["market_value"]
    elif capital["total_capital"] is not None:
        capital["available_with_holdings"] = capital["total_capital"]

    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    state = _read_state()
    state.update(
        {
            "holdings": holdings,
            "capital": capital,
            "updated_at": now,
            "holding_source": "qmtmini_readonly_snapshot",
            "broker_account_id": snapshot.get("account_id"),
            "qmtmini_snapshot": {
                "asset_available": snapshot.get("asset_available"),
                "positions_count": snapshot.get("positions_count"),
                "orders_count": snapshot.get("orders_count"),
                "trades_count": snapshot.get("trades_count"),
            },
            "fallback_cache": False,
            "sync_message": "QMT Mini read-only account snapshot synced from host.",
            "capital_only": False,
        }
    )
    _write_state(state)
    return {
        "ok": bool(snapshot.get("ok")),
        "mode": "qmtmini_broker_snapshot_sync",
        "updated_at": now,
        "account_id": snapshot.get("account_id"),
        "holdings_count": len(holdings),
        "positions_count": snapshot.get("positions_count"),
        "capital": capital,
        "broker_state_path": str(BROKER_STATE_PATH),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync QMT Mini read-only account snapshot into G3 broker_state.json.")
    parser.add_argument("--log-file", default="", help="Optional path to append the JSON result for scheduled runs.")
    args = parser.parse_args()
    result = sync_qmtmini_broker_snapshot()
    output = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(output)
            handle.write("\n")
    print(output)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

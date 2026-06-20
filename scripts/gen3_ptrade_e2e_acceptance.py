"""Run a real-bridge G3 -> PTrade dry-run end-to-end acceptance.

This writes two dry-run G3 orders (BUY and SELL) into the active PTrade bridge
directory and waits for the running bridge consumer to acknowledge them. It
never sends live orders because every order is dry_run=true and approved=false.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.gen3_state_alpha as gen3
from execution.ptrade_bridge import PTradeFileBridge
from scripts.ptrade_bridge_live_probe import wait_for_ack
from utils.paths import report_path, runtime_path


DEFAULT_BRIDGE_DIR = runtime_path("ptrade_bridge")
DEFAULT_REPORT = report_path("gen3_ptrade_e2e_acceptance") / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _check(name: str, ok: bool, detail: Any = None, required: bool = True) -> Dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        "required": bool(required),
        "detail": detail,
    }


def _sample_ticket(run_id: str, code: str) -> Dict[str, Any]:
    return {
        "ticket_key": f"gen3-ptrade-e2e-{run_id}",
        "candidate_key": f"gen3-ptrade-e2e-candidate-{run_id}",
        "trade_key": f"gen3-ptrade-e2e-trade-{run_id}",
        "entry_date": run_id,
        "decision_date": run_id[:10],
        "route": "institutional_mainwave",
        "route_label": "institutional_mainwave",
        "code": code,
        "name": "Gen3 PTrade E2E",
        "qualified_shadow_buy": True,
        "m30_confirmed": True,
        "reference_close": 10.0,
        "position_pct": 0.5,
        "structure_stop": 9.2,
        "hard_stop": 8.8,
        "take_profit_1": 11.2,
        "exit_contract": "12% hard stop, 12% half take-profit, previous-low protection.",
    }


def _patch_ticket(ticket: Dict[str, Any]):
    saved = gen3._find_current_ticket

    def find_current_ticket(payload: Dict[str, Any]) -> Dict[str, Any] | None:
        payload = payload if isinstance(payload, dict) else {}
        ticket_keys = {ticket["ticket_key"], ticket["candidate_key"], ticket["trade_key"]}
        if str(payload.get("ticket_key") or "") in ticket_keys:
            return ticket
        if str(payload.get("code") or "") in {ticket["code"], f"{ticket['code']}.SH", f"{ticket['code']}.SZ"}:
            return ticket
        return saved(payload)

    gen3._find_current_ticket = find_current_ticket
    return saved


def _restore_ticket(saved) -> None:
    gen3._find_current_ticket = saved


def _ack_ok(result: Dict[str, Any], side: str) -> bool:
    ack = result.get("ack") if isinstance(result, dict) else {}
    if not isinstance(ack, dict):
        return False
    return bool(result.get("ok") and ack.get("status") == "dry_run" and str(ack.get("side") or "").upper() == side)


def run_acceptance(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    code: str = "600001",
    timeout_seconds: float = 30.0,
    poll_seconds: float = 1.0,
    max_heartbeat_age_seconds: float = 15.0,
) -> Dict[str, Any]:
    run_id = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bridge = PTradeFileBridge(root=bridge_dir)
    gen3.PTRADE_BRIDGE = bridge
    initial_status = bridge.status()
    checks: List[Dict[str, Any]] = []
    heartbeat_age = initial_status.get("ptrade_heartbeat_age_seconds")
    checks.append(
        _check(
            "ptrade_heartbeat_recent",
            isinstance(heartbeat_age, (int, float)) and float(heartbeat_age) <= max_heartbeat_age_seconds,
            heartbeat_age,
        )
    )
    checks.append(_check("no_stale_processing", int(initial_status.get("processing_stale_count") or 0) == 0, initial_status.get("processing_stale_count")))

    ticket = _sample_ticket(run_id=run_id, code=code)
    saved_find = _patch_ticket(ticket)
    try:
        buy = gen3._submit_ptrade_buy_order(
            {
                "ticket_key": ticket["ticket_key"],
                "code": ticket["code"],
                "price": ticket["reference_close"],
                "quantity": 100,
                "dry_run": True,
                "require_approval": True,
                "approved": False,
                "reason": "G3 PTrade E2E acceptance BUY dry-run",
                "notes": "gen3 ptrade e2e acceptance",
            }
        )
        sell = gen3._submit_ptrade_sell_order(
            {
                "ticket_key": ticket["ticket_key"],
                "code": ticket["code"],
                "quantity": 100,
                "price": 10.5,
                "dry_run": True,
                "require_approval": True,
                "approved": False,
                "reason": "G3 PTrade E2E acceptance SELL dry-run",
                "notes": "gen3 ptrade e2e acceptance",
            }
        )
    finally:
        _restore_ticket(saved_find)

    buy_order = buy.get("order") if isinstance(buy.get("order"), dict) else {}
    sell_order = sell.get("order") if isinstance(sell.get("order"), dict) else {}
    checks.append(_check("g3_buy_signal_submitted", bool(buy.get("ok") and buy_order.get("order_id")), buy.get("message") or buy_order))
    checks.append(_check("g3_sell_signal_submitted", bool(sell.get("ok") and sell_order.get("order_id")), sell.get("message") or sell_order))

    buy_ack = wait_for_ack(bridge.paths.root, str(buy_order.get("order_id") or ""), timeout_seconds=timeout_seconds, poll_seconds=poll_seconds) if buy_order else {"ok": False}
    sell_ack = wait_for_ack(bridge.paths.root, str(sell_order.get("order_id") or ""), timeout_seconds=timeout_seconds, poll_seconds=poll_seconds) if sell_order else {"ok": False}
    checks.append(_check("buy_ack_dry_run", _ack_ok(buy_ack, "BUY"), buy_ack))
    checks.append(_check("sell_ack_dry_run", _ack_ok(sell_ack, "SELL"), sell_ack))

    final_status = bridge.status()
    checks.append(_check("pending_queue_empty", int(final_status.get("pending_count") or 0) == 0, final_status.get("pending_count")))
    checks.append(_check("processing_queue_empty", int(final_status.get("processing_count") or 0) == 0, final_status.get("processing_count")))

    failed = [item for item in checks if item.get("required") and not item.get("ok")]
    return {
        "ok": not failed,
        "started_at": run_id,
        "completed_at": _now_text(),
        "bridge_dir": str(bridge.paths.root),
        "checks": checks,
        "ticket": ticket,
        "buy_order_id": buy_order.get("order_id"),
        "sell_order_id": sell_order.get("order_id"),
        "buy_ack_status": (buy_ack.get("ack") or {}).get("status") if isinstance(buy_ack.get("ack"), dict) else None,
        "sell_ack_status": (sell_ack.get("ack") or {}).get("status") if isinstance(sell_ack.get("ack"), dict) else None,
        "initial_status": initial_status,
        "final_status": final_status,
        "note": "Writes real bridge dry-run G3 BUY/SELL orders only; no live order is sent.",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run real-bridge G3 PTrade dry-run end-to-end acceptance.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--code", default="600001")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-heartbeat-age-seconds", type=float, default=15.0)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    result = run_acceptance(
        bridge_dir=Path(args.bridge_dir),
        code=str(args.code),
        timeout_seconds=float(args.timeout_seconds),
        poll_seconds=float(args.poll_seconds),
        max_heartbeat_age_seconds=float(args.max_heartbeat_age_seconds),
    )
    _write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

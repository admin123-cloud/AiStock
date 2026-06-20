"""G3 -> PTrade bridge dry-run acceptance without touching the real queue.

The goal is to prove the G3 order path can produce PTrade-compatible BUY and
SELL intents, and that the local bridge consumer can acknowledge them. It uses
an isolated temporary bridge directory, never calls PTrade, and never sends live
orders.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import api.gen3_state_alpha as gen3
from execution.ptrade_bridge import PTradeFileBridge
from scripts.ptrade_file_bridge_api_runner import process_once, read_json


DEFAULT_REPORT = ROOT / "reports" / "gen3_ptrade_bridge_acceptance" / "latest.json"


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


def _sample_ticket() -> Dict[str, Any]:
    return {
        "ticket_key": "gen3-ptrade-acceptance-ticket",
        "candidate_key": "gen3-ptrade-acceptance-candidate",
        "trade_key": "gen3-ptrade-acceptance-trade",
        "entry_date": "2026-06-19",
        "decision_date": "2026-06-19",
        "route": "institutional_mainwave",
        "route_label": "institutional_mainwave",
        "code": "600000",
        "name": "Gen3 Acceptance Stock",
        "qualified_shadow_buy": True,
        "m30_confirmed": True,
        "reference_close": 10.0,
        "position_pct": 0.5,
        "structure_stop": 9.2,
        "hard_stop": 8.8,
        "take_profit_1": 11.2,
        "exit_contract": "12% hard stop, half take profit at 12%, protect remaining by previous low.",
    }


def _reset_bridge_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _patch_gen3_for_isolated_run(bridge: PTradeFileBridge, ticket: Dict[str, Any], audit_rows: List[Dict[str, Any]], events: List[Dict[str, Any]]):
    saved = {
        "bridge": gen3.PTRADE_BRIDGE,
        "find_current_ticket": gen3._find_current_ticket,
        "load_paper_executions": gen3._load_paper_executions,
        "save_paper_executions": gen3._save_paper_executions,
        "append_monitor_event": gen3._append_monitor_event,
    }

    def find_current_ticket(payload: Dict[str, Any]) -> Dict[str, Any] | None:
        payload = payload if isinstance(payload, dict) else {}
        ticket_key = str(payload.get("ticket_key") or "")
        code = str(payload.get("code") or "")
        if ticket_key in {ticket["ticket_key"], ticket["candidate_key"], ticket["trade_key"]}:
            return ticket
        if code in {ticket["code"], "600000"}:
            return ticket
        return None

    def save_rows(rows: List[Dict[str, Any]]) -> None:
        audit_rows[:] = list(rows)

    gen3.PTRADE_BRIDGE = bridge
    gen3._find_current_ticket = find_current_ticket
    gen3._load_paper_executions = lambda: list(audit_rows)
    gen3._save_paper_executions = save_rows
    gen3._append_monitor_event = lambda payload: events.append(dict(payload))
    return saved


def _restore_gen3(saved: Dict[str, Any]) -> None:
    gen3.PTRADE_BRIDGE = saved["bridge"]
    gen3._find_current_ticket = saved["find_current_ticket"]
    gen3._load_paper_executions = saved["load_paper_executions"]
    gen3._save_paper_executions = saved["save_paper_executions"]
    gen3._append_monitor_event = saved["append_monitor_event"]


def _ack_for(bridge_dir: Path, order_id: str) -> Dict[str, Any]:
    path = bridge_dir / "acks" / f"{order_id}.json"
    return read_json(path) if path.exists() else {}


def run_acceptance(bridge_dir: Optional[Path] = None, keep_bridge_dir: bool = False) -> Dict[str, Any]:
    started_at = _now_text()
    checks: List[Dict[str, Any]] = []
    audit_rows: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    ticket = _sample_ticket()

    temp_context = None
    if bridge_dir is None:
        temp_context = TemporaryDirectory(prefix="gen3-ptrade-acceptance-")
        run_dir = Path(temp_context.name)
    else:
        run_dir = Path(bridge_dir)
        _reset_bridge_dir(run_dir)

    bridge = PTradeFileBridge(root=run_dir)
    saved = _patch_gen3_for_isolated_run(bridge, ticket, audit_rows, events)
    try:
        buy = gen3._submit_ptrade_buy_order(
            {
                "ticket_key": ticket["ticket_key"],
                "code": ticket["code"],
                "price": ticket["reference_close"],
                "dry_run": True,
                "require_approval": True,
                "approved": False,
                "notes": "gen3 ptrade bridge acceptance buy",
            }
        )
        sell = gen3._submit_ptrade_sell_order(
            {
                "ticket_key": ticket["ticket_key"],
                "code": ticket["code"],
                "quantity": 500,
                "price": 10.5,
                "dry_run": True,
                "require_approval": True,
                "approved": False,
                "notes": "gen3 ptrade bridge acceptance sell",
            }
        )
        checks.append(_check("g3_buy_submit_ok", bool(buy.get("ok")), buy.get("message") or buy.get("order")))
        checks.append(_check("g3_sell_submit_ok", bool(sell.get("ok")), sell.get("message") or sell.get("order")))

        pending_before = sorted((run_dir / "pending").glob("*.json"))
        checks.append(_check("g3_pending_orders_written", len(pending_before) == 2, [item.name for item in pending_before]))
        checks.append(_check("g3_audit_records_written", len(audit_rows) == 2, [row.get("ptrade_order_id") for row in audit_rows]))
        checks.append(_check("g3_monitor_events_written", len(events) == 2, [event.get("type") for event in events]))

        process_result = process_once(run_dir, enable_live_order=False, api_dir=run_dir)
        buy_order = buy.get("order") if isinstance(buy.get("order"), dict) else {}
        sell_order = sell.get("order") if isinstance(sell.get("order"), dict) else {}
        buy_ack = _ack_for(run_dir, str(buy_order.get("order_id") or ""))
        sell_ack = _ack_for(run_dir, str(sell_order.get("order_id") or ""))

        checks.append(_check("local_runner_processed_both_orders", process_result.get("processed") == 2, process_result))
        checks.append(_check("buy_ack_dry_run", buy_ack.get("status") == "dry_run" and buy_ack.get("side") == "BUY", buy_ack))
        checks.append(_check("sell_ack_dry_run", sell_ack.get("status") == "dry_run" and sell_ack.get("side") == "SELL", sell_ack))
        checks.append(_check("pending_queue_empty_after_ack", not list((run_dir / "pending").glob("*.json")), "pending empty"))
        checks.append(_check("processing_queue_empty_after_ack", not list((run_dir / "processing").glob("*.json")), "processing empty"))

        status = bridge.status()
        failed = [item for item in checks if item.get("required") and not item.get("ok")]
        result: Dict[str, Any] = {
            "ok": not failed,
            "started_at": started_at,
            "completed_at": _now_text(),
            "bridge_dir": str(run_dir),
            "checks": checks,
            "buy_order_id": buy_order.get("order_id"),
            "sell_order_id": sell_order.get("order_id"),
            "process_result": process_result,
            "audit_record_count": len(audit_rows),
            "monitor_event_count": len(events),
            "final_status": status,
            "note": "G3 PTrade acceptance uses an isolated bridge directory, dry_run=true orders, and the local file bridge runner only.",
        }
    finally:
        _restore_gen3(saved)
        if temp_context is not None and not keep_bridge_dir:
            temp_context.cleanup()
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated G3 -> PTrade dry-run bridge acceptance.")
    parser.add_argument("--bridge-dir", default="", help="Optional isolated bridge directory. It will be cleared before use.")
    parser.add_argument("--keep-bridge-dir", action="store_true", help="Keep the temporary bridge directory after the run.")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    result = run_acceptance(
        bridge_dir=Path(args.bridge_dir) if args.bridge_dir else None,
        keep_bridge_dir=bool(args.keep_bridge_dir),
    )
    _write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

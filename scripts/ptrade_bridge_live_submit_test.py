"""Guarded small live-submit test for the AiStock -> PTrade bridge."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.ptrade_bridge import PTradeFileBridge
from scripts.ptrade_bridge_live_probe import wait_for_ack, write_json
from scripts.ptrade_bridge_readiness_audit import run_audit
from utils.paths import runtime_path


DEFAULT_BRIDGE_DIR = runtime_path("ptrade_bridge")
DEFAULT_REPORT = ROOT / "reports" / "ptrade_bridge_live_submit_test" / "latest.json"
LIVE_ACK_STATUSES = {
    "submitted",
    "accepted",
    "filled",
    "partial_filled",
    "submitted_live",
}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _check(name: str, ok: bool, required: bool = True, detail: Any = None, message: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        "required": bool(required),
        "detail": detail,
        "message": message,
    }


def _required_failed(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in checks if item.get("required") and not item.get("ok")]


def run_live_submit_test(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    approve_live_submit: bool = False,
    code: str = "600000",
    side: str = "BUY",
    price: float = 10.5,
    quantity: int = 100,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 1.0,
    max_submit_seconds: float = 0.5,
    max_order_value: float = 20000.0,
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = [
        _check(
            "explicit_live_submit_approval",
            bool(approve_live_submit),
            True,
            bool(approve_live_submit),
            "Pass --approve-live-submit only after manual broker/account confirmation.",
        )
    ]

    normalized_side = str(side or "").strip().upper()
    if normalized_side in {"B", "LONG"}:
        normalized_side = "BUY"
    elif normalized_side in {"S", "SHORT"}:
        normalized_side = "SELL"

    try:
        price_value = float(price)
    except Exception:
        price_value = 0.0
    try:
        quantity_value = int(float(quantity))
    except Exception:
        quantity_value = 0
    try:
        max_order_value_local = float(max_order_value)
    except Exception:
        max_order_value_local = 0.0
    order_value = price_value * quantity_value

    checks.extend(
        [
            _check("side_valid", normalized_side in {"BUY", "SELL"}, True, normalized_side),
            _check("price_positive", price_value > 0, True, price_value),
            _check("quantity_positive", quantity_value > 0, True, quantity_value),
            _check(
                "buy_quantity_multiple_100",
                normalized_side != "BUY" or quantity_value % 100 == 0,
                True,
                quantity_value,
            ),
            _check("max_order_value_positive", max_order_value_local > 0, True, max_order_value_local),
            _check(
                "order_value_within_cli_cap",
                max_order_value_local > 0 and order_value <= max_order_value_local,
                True,
                {"order_value": order_value, "max_order_value": max_order_value_local},
            ),
        ]
    )

    readiness_audit: Optional[Dict[str, Any]] = None
    submit_result: Optional[Dict[str, Any]] = None
    ack_result: Optional[Dict[str, Any]] = None
    final_status: Optional[Dict[str, Any]] = None
    bridge_dir = Path(bridge_dir)

    if not _required_failed(checks):
        readiness_audit = run_audit(bridge_dir=bridge_dir, require_empty_queue_for_live=True)
        gates = readiness_audit.get("gates", {}) if isinstance(readiness_audit, dict) else {}
        config = readiness_audit.get("config", {}) if isinstance(readiness_audit.get("config"), dict) else {}
        try:
            ptrade_cap = float(config.get("max_order_value", 20000))
        except Exception:
            ptrade_cap = 0.0
        checks.extend(
            [
                _check("readiness_live_submit_ready", bool(gates.get("live_submit_ready")), True, gates),
                _check("order_value_within_ptrade_cap", ptrade_cap > 0 and order_value <= ptrade_cap, True, {"order_value": order_value, "max_order_value": ptrade_cap}),
            ]
        )

    if not _required_failed(checks):
        bridge = PTradeFileBridge(root=bridge_dir)
        started = time.perf_counter()
        order = bridge.submit_order(
            {
                "code": str(code),
                "side": normalized_side,
                "quantity": quantity_value,
                "price": price_value,
                "dry_run": False,
                "require_approval": True,
                "approved": True,
                "source": "ptrade_bridge_live_submit_test",
                "strategy": "manual_cloud_sim_live_submit_test",
                "reason": "Manual approved small live-submit test for Xiangcai PTrade cloud simulation.",
                "risk": {
                    "max_order_value": max_order_value_local,
                    "order_value": order_value,
                    "max_submit_seconds": max_submit_seconds,
                },
            }
        )
        submit_elapsed = time.perf_counter() - started
        submit_result = {
            "order_id": order.get("order_id"),
            "pending_file": str(bridge.paths.pending / f"{order['order_id']}.json"),
            "dry_run": bool(order.get("dry_run")),
            "approved": bool(order.get("approved")),
            "submit_elapsed_seconds": round(submit_elapsed, 6),
        }
        checks.append(
            _check(
                "submit_latency_within_budget",
                submit_elapsed <= float(max_submit_seconds),
                True,
                f"{submit_elapsed:.6f}s <= {float(max_submit_seconds):.6f}s",
            )
        )
        ack_result = wait_for_ack(
            bridge_dir=bridge.paths.root,
            order_id=str(order["order_id"]),
            timeout_seconds=max(0.1, float(timeout_seconds)),
            poll_seconds=max(0.05, float(poll_seconds)),
        )
        ack_status = (ack_result.get("ack") or {}).get("status") if isinstance(ack_result, dict) else None
        checks.append(
            _check(
                "live_submit_ack_received",
                bool(ack_result and ack_result.get("ok") and str(ack_status) in LIVE_ACK_STATUSES),
                True,
                str(ack_status),
                "The PTrade side should write a submitted/accepted/fill acknowledgement.",
            )
        )
        final_status = bridge.status()
    else:
        bridge = PTradeFileBridge(root=bridge_dir)
        final_status = bridge.status()

    result: Dict[str, Any] = {
        "ok": len(_required_failed(checks)) == 0,
        "started_at": _now_text(),
        "bridge_dir": str(bridge_dir),
        "checks": checks,
        "readiness_audit": readiness_audit,
        "submit_result": submit_result,
        "ack_result": ack_result,
        "final_status": final_status,
        "note": "This script writes dry_run=false only when --approve-live-submit is present and readiness gates pass.",
    }
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded small live-submit test for the AiStock PTrade bridge.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--approve-live-submit", action="store_true")
    parser.add_argument("--code", default="600000")
    parser.add_argument("--side", default="BUY")
    parser.add_argument("--price", type=float, default=10.5)
    parser.add_argument("--quantity", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-submit-seconds", type=float, default=0.5)
    parser.add_argument("--max-order-value", type=float, default=20000.0)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    result = run_live_submit_test(
        bridge_dir=Path(args.bridge_dir),
        approve_live_submit=bool(args.approve_live_submit),
        code=str(args.code),
        side=str(args.side),
        price=float(args.price),
        quantity=int(args.quantity),
        timeout_seconds=max(0.1, float(args.timeout_seconds)),
        poll_seconds=max(0.05, float(args.poll_seconds)),
        max_submit_seconds=max(0.01, float(args.max_submit_seconds)),
        max_order_value=max(0.0, float(args.max_order_value)),
    )
    write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

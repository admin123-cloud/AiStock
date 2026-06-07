"""Probe the real AiStock -> PTrade file bridge without sending live orders."""

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


DEFAULT_BRIDGE_DIR = ROOT / "data" / "runtime" / "ptrade_bridge"
DEFAULT_REPORT = ROOT / "reports" / "ptrade_bridge_live_probe" / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            data = json.loads(path.read_text(encoding=encoding))
            return data if isinstance(data, dict) else {}
        except Exception:
            continue
    return {}


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def wait_for_ack(
    bridge_dir: Path,
    order_id: str,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 1.0,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    poll = max(0.05, float(poll_seconds))
    ack_file = Path(bridge_dir) / "acks" / f"{order_id}.json"
    pending_file = Path(bridge_dir) / "pending" / f"{order_id}.json"
    processing_file = Path(bridge_dir) / "processing" / f"{order_id}.json"

    while time.monotonic() <= deadline:
        ack = read_json(ack_file)
        if ack:
            return {
                "ok": True,
                "order_id": order_id,
                "ack": ack,
                "ack_file": str(ack_file),
                "pending_exists": pending_file.exists(),
                "processing_exists": processing_file.exists(),
            }
        time.sleep(poll)

    return {
        "ok": False,
        "order_id": order_id,
        "ack": None,
        "ack_file": str(ack_file),
        "pending_exists": pending_file.exists(),
        "processing_exists": processing_file.exists(),
        "message": "timeout waiting for PTrade acknowledgement",
    }


def _heartbeat_ok(status: Dict[str, Any], max_age_seconds: float) -> bool:
    age = status.get("ptrade_heartbeat_age_seconds")
    return isinstance(age, (int, float)) and float(age) <= max_age_seconds


def run_probe(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    submit_dry_run: bool = False,
    require_heartbeat: bool = False,
    max_heartbeat_age_seconds: float = 15.0,
    code: str = "600000",
    price: float = 10.5,
    quantity: int = 100,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 1.0,
    max_submit_seconds: float = 0.5,
) -> Dict[str, Any]:
    bridge = PTradeFileBridge(root=bridge_dir)
    initial_status = bridge.status()
    checks: List[Dict[str, Any]] = []

    heartbeat_ok = _heartbeat_ok(initial_status, max_heartbeat_age_seconds)
    checks.append(
        {
            "name": "ptrade_heartbeat_recent",
            "ok": bool(heartbeat_ok),
            "required": bool(require_heartbeat),
            "detail": str(initial_status.get("ptrade_heartbeat_age_seconds")),
        }
    )
    checks.append(
        {
            "name": "no_stale_processing",
            "ok": int(initial_status.get("processing_stale_count") or 0) == 0,
            "required": True,
            "detail": str(initial_status.get("processing_stale_count")),
        }
    )

    submit_result: Optional[Dict[str, Any]] = None
    ack_result: Optional[Dict[str, Any]] = None
    submit_elapsed: Optional[float] = None
    if submit_dry_run:
        started = time.perf_counter()
        order = bridge.submit_order(
            {
                "code": code,
                "side": "BUY",
                "quantity": quantity,
                "price": price,
                "dry_run": True,
                "require_approval": True,
                "approved": False,
                "source": "ptrade_bridge_live_probe",
                "strategy": "live_probe",
                "reason": "live probe dry-run only; never send live order",
            }
        )
        submit_elapsed = time.perf_counter() - started
        submit_result = {
            "order_id": order.get("order_id"),
            "pending_file": str(bridge.paths.pending / f"{order['order_id']}.json"),
            "submit_elapsed_seconds": round(submit_elapsed, 6),
            "dry_run": True,
        }
        checks.append(
            {
                "name": "submit_latency_within_budget",
                "ok": submit_elapsed <= max_submit_seconds,
                "required": True,
                "detail": f"{submit_elapsed:.6f}s <= {max_submit_seconds:.6f}s",
            }
        )
        ack_result = wait_for_ack(
            bridge_dir=bridge.paths.root,
            order_id=str(order["order_id"]),
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
        ack_status = (ack_result.get("ack") or {}).get("status") if ack_result else None
        checks.append(
            {
                "name": "dry_run_ack_received",
                "ok": bool(ack_result and ack_result.get("ok") and ack_status in {"dry_run", "waiting_approval"}),
                "required": True,
                "detail": str(ack_status),
            }
        )

    final_status = bridge.status()
    required_failed = [
        item
        for item in checks
        if item.get("required") and not item.get("ok")
    ]
    if require_heartbeat and not heartbeat_ok:
        required_failed.append({"name": "required_heartbeat_missing", "ok": False})

    result: Dict[str, Any] = {
        "ok": len(required_failed) == 0,
        "started_at": _now_text(),
        "bridge_dir": str(bridge.paths.root),
        "submit_dry_run": bool(submit_dry_run),
        "require_heartbeat": bool(require_heartbeat),
        "max_heartbeat_age_seconds": max_heartbeat_age_seconds,
        "checks": checks,
        "initial_status": initial_status,
        "final_status": final_status,
        "submit_result": submit_result,
        "ack_result": ack_result,
        "note": "This probe never submits live orders. It writes only a dry-run order when --submit-dry-run is set.",
    }
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the real AiStock PTrade bridge.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--submit-dry-run", action="store_true", help="Write one dry-run order and wait for PTrade ack.")
    parser.add_argument("--require-heartbeat", action="store_true", help="Fail if PTrade heartbeat is missing or stale.")
    parser.add_argument("--max-heartbeat-age-seconds", type=float, default=15.0)
    parser.add_argument("--code", default="600000")
    parser.add_argument("--price", type=float, default=10.5)
    parser.add_argument("--quantity", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-submit-seconds", type=float, default=0.5)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    result = run_probe(
        bridge_dir=Path(args.bridge_dir),
        submit_dry_run=bool(args.submit_dry_run),
        require_heartbeat=bool(args.require_heartbeat),
        max_heartbeat_age_seconds=max(1.0, float(args.max_heartbeat_age_seconds)),
        code=str(args.code),
        price=float(args.price),
        quantity=int(args.quantity),
        timeout_seconds=max(0.1, float(args.timeout_seconds)),
        poll_seconds=max(0.05, float(args.poll_seconds)),
        max_submit_seconds=max(0.01, float(args.max_submit_seconds)),
    )
    write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

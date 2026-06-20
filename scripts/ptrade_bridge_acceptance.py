"""Bounded acceptance gate for the AiStock -> PTrade bridge.

This script is intended to run after the PTrade internal strategy is started.
It never submits live orders: it waits for a heartbeat, optionally writes one
dry-run probe order, then runs the read-only readiness audit.
"""

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
from scripts.ptrade_bridge_live_probe import run_probe
from scripts.ptrade_bridge_preflight import DEFAULT_STRATEGY_FILE, run_preflight
from scripts.ptrade_bridge_readiness_audit import run_audit, write_json
from utils.paths import runtime_path


DEFAULT_BRIDGE_DIR = runtime_path("ptrade_bridge")
DEFAULT_REPORT = ROOT / "reports" / "ptrade_bridge_acceptance" / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _check(name: str, ok: bool, required: bool, detail: Any = None, message: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        "required": bool(required),
        "detail": detail,
        "message": message,
    }


def _heartbeat_recent(status: Dict[str, Any], max_age_seconds: float) -> bool:
    age = status.get("ptrade_heartbeat_age_seconds")
    return isinstance(age, (int, float)) and float(age) <= float(max_age_seconds)


def wait_for_heartbeat(
    bridge_dir: Path,
    timeout_seconds: float = 120.0,
    poll_seconds: float = 1.0,
    max_heartbeat_age_seconds: float = 30.0,
) -> Dict[str, Any]:
    bridge = PTradeFileBridge(root=bridge_dir)
    deadline = time.monotonic() + max(0.1, float(timeout_seconds))
    poll = max(0.05, float(poll_seconds))
    last_status = bridge.status()
    while time.monotonic() <= deadline:
        last_status = bridge.status()
        if _heartbeat_recent(last_status, max_heartbeat_age_seconds):
            return {
                "ok": True,
                "status": last_status,
                "heartbeat_age_seconds": last_status.get("ptrade_heartbeat_age_seconds"),
                "message": "PTrade heartbeat is recent.",
            }
        time.sleep(poll)
    return {
        "ok": False,
        "status": last_status,
        "heartbeat_age_seconds": last_status.get("ptrade_heartbeat_age_seconds"),
        "message": "timeout waiting for PTrade heartbeat",
    }


def _next_actions(result: Dict[str, Any]) -> List[str]:
    if not result.get("preflight", {}).get("ok"):
        return ["Fix local preflight failures before starting the PTrade strategy."]
    if not result.get("heartbeat", {}).get("ok"):
        return ["Start or repair the PTrade internal strategy until status/latest.json heartbeat is recent."]
    probe = result.get("dry_run_probe") if isinstance(result.get("dry_run_probe"), dict) else {}
    if probe and not probe.get("ok"):
        return ["Keep PTrade strategy running and rerun the dry-run acceptance probe; do not submit live orders yet."]
    audit = result.get("readiness_audit") if isinstance(result.get("readiness_audit"), dict) else {}
    if audit.get("gates", {}).get("live_submit_ready"):
        return ["Ready for one manually approved small cloud-simulation live-submit test."]
    actions = audit.get("next_actions")
    if isinstance(actions, list) and actions:
        return [str(item) for item in actions]
    return ["Review acceptance report before any approved live-submit test."]


def run_acceptance(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    strategy_file: Path = DEFAULT_STRATEGY_FILE,
    heartbeat_timeout_seconds: float = 120.0,
    poll_seconds: float = 1.0,
    max_heartbeat_age_seconds: float = 30.0,
    submit_dry_run: bool = True,
    dry_run_timeout_seconds: float = 30.0,
    code: str = "600000",
    price: float = 10.5,
    quantity: int = 100,
) -> Dict[str, Any]:
    started_at = _now_text()
    preflight = run_preflight(bridge_dir=bridge_dir, strategy_file=strategy_file, require_live_disabled=False)
    heartbeat: Dict[str, Any] = {"ok": False, "message": "preflight failed; heartbeat wait skipped"}
    dry_run_probe: Optional[Dict[str, Any]] = None

    checks: List[Dict[str, Any]] = [
        _check("local_preflight_ok", bool(preflight.get("ok")), True, preflight.get("bridge_dir"), "Local preflight must pass."),
    ]

    if preflight.get("ok"):
        heartbeat = wait_for_heartbeat(
            bridge_dir=bridge_dir,
            timeout_seconds=heartbeat_timeout_seconds,
            poll_seconds=poll_seconds,
            max_heartbeat_age_seconds=max_heartbeat_age_seconds,
        )
    checks.append(
        _check(
            "ptrade_heartbeat_recent",
            bool(heartbeat.get("ok")),
            True,
            heartbeat.get("heartbeat_age_seconds"),
            "PTrade internal strategy heartbeat must be recent.",
        )
    )

    if submit_dry_run and heartbeat.get("ok"):
        dry_run_probe = run_probe(
            bridge_dir=bridge_dir,
            submit_dry_run=True,
            require_heartbeat=True,
            max_heartbeat_age_seconds=max_heartbeat_age_seconds,
            code=code,
            price=float(price),
            quantity=int(quantity),
            timeout_seconds=dry_run_timeout_seconds,
            poll_seconds=poll_seconds,
        )
        ack_result = dry_run_probe.get("ack_result") if isinstance(dry_run_probe, dict) else {}
        ack_payload = ack_result.get("ack") if isinstance(ack_result, dict) else {}
        if not isinstance(ack_payload, dict):
            ack_payload = {}
        ack_status = ack_payload.get("status")
        checks.append(
            _check(
                "dry_run_probe_ack_received",
                bool(dry_run_probe.get("ok")) if isinstance(dry_run_probe, dict) else False,
                True,
                ack_status,
                "A dry-run probe ack must be received before live-submit testing.",
            )
        )
    elif submit_dry_run:
        checks.append(
            _check(
                "dry_run_probe_ack_received",
                False,
                True,
                None,
                "Dry-run probe skipped because heartbeat is missing.",
            )
        )

    readiness_audit = run_audit(bridge_dir=bridge_dir)
    checks.append(
        _check(
            "live_submit_gate_ready",
            bool(readiness_audit.get("gates", {}).get("live_submit_ready")),
            True,
            readiness_audit.get("gates", {}),
            "All local gates must pass before a small approved live-submit test.",
        )
    )

    failed_required = [item for item in checks if item.get("required") and not item.get("ok")]
    result: Dict[str, Any] = {
        "ok": len(failed_required) == 0,
        "started_at": started_at,
        "completed_at": _now_text(),
        "bridge_dir": str(Path(bridge_dir)),
        "strategy_file": str(Path(strategy_file)),
        "checks": checks,
        "preflight": preflight,
        "heartbeat": heartbeat,
        "dry_run_probe": dry_run_probe,
        "readiness_audit": readiness_audit,
        "next_actions": [],
        "note": "Acceptance gate never submits live orders. It writes one dry-run order only when heartbeat is recent.",
    }
    result["next_actions"] = _next_actions(result)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded AiStock PTrade bridge acceptance gate.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--strategy-file", default=str(DEFAULT_STRATEGY_FILE))
    parser.add_argument("--heartbeat-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-heartbeat-age-seconds", type=float, default=30.0)
    parser.add_argument("--skip-dry-run", action="store_true", help="Do not write a dry-run probe order.")
    parser.add_argument("--dry-run-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--code", default="600000")
    parser.add_argument("--price", type=float, default=10.5)
    parser.add_argument("--quantity", type=int, default=100)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    quantity = max(100, int(args.quantity))
    quantity = max(100, (quantity // 100) * 100)
    result = run_acceptance(
        bridge_dir=Path(args.bridge_dir),
        strategy_file=Path(args.strategy_file),
        heartbeat_timeout_seconds=max(0.1, float(args.heartbeat_timeout_seconds)),
        poll_seconds=max(0.05, float(args.poll_seconds)),
        max_heartbeat_age_seconds=max(1.0, float(args.max_heartbeat_age_seconds)),
        submit_dry_run=not bool(args.skip_dry_run),
        dry_run_timeout_seconds=max(0.1, float(args.dry_run_timeout_seconds)),
        code=str(args.code),
        price=float(args.price),
        quantity=quantity,
    )
    write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

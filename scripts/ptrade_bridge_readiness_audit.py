"""Read-only readiness audit for the AiStock -> PTrade bridge."""

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

from execution.ptrade_bridge import PTradeFileBridge
from utils.paths import runtime_path


DEFAULT_BRIDGE_DIR = runtime_path("ptrade_bridge")
DEFAULT_REPORT = ROOT / "reports" / "ptrade_bridge_readiness_audit" / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _check(name: str, ok: bool, required_for: List[str], detail: Any = None, message: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        "required_for": list(required_for),
        "detail": detail,
        "message": message,
    }


def _failed(checks: List[Dict[str, Any]], gate: str) -> List[Dict[str, Any]]:
    return [
        item
        for item in checks
        if gate in (item.get("required_for") or []) and not item.get("ok")
    ]


def _next_actions(checks: List[Dict[str, Any]], status: Dict[str, Any]) -> List[str]:
    names = {str(item.get("name")): item for item in checks if not item.get("ok")}
    actions: List[str] = []
    if "ptrade_heartbeat_recent" in names:
        actions.append("Start the PTrade internal strategy and wait for status/latest.json heartbeat.")
    if "dry_run_probe_ack_recent" in names:
        actions.append("Run one dry-run probe after heartbeat is recent: ptrade_bridge_live_probe.py --submit-dry-run --require-heartbeat.")
    if "ptrade_live_order_enabled" in names and "ptrade_heartbeat_recent" not in names:
        actions.append("After dry-run ack is proven, switch PTrade strategy ENABLE_LIVE_ORDER = True for the small approved live-submit test.")
    if "no_stale_processing" in names:
        actions.append("Manually reconcile stale processing files in PTrade, then recover them to errors.")
    if "empty_queue_before_live_test" in names:
        actions.append("Wait for pending/processing files to clear before a small approved live-submit test.")
    if not actions and status.get("ready_for_live_order"):
        actions.append("Ready for a small approved live-submit test, subject to manual broker/account confirmation.")
    if not actions:
        actions.append("Review readiness checks before submitting any approved live order.")
    return actions


def run_audit(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    require_empty_queue_for_live: bool = True,
) -> Dict[str, Any]:
    bridge = PTradeFileBridge(root=bridge_dir)
    status = bridge.status()
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    pending_count = int(status.get("pending_count") or 0)
    processing_count = int(status.get("processing_count") or 0)
    stale_count = int(status.get("processing_stale_count") or 0)
    try:
        config = json.loads(bridge.paths.config.read_text(encoding="utf-8-sig")) if bridge.paths.config.exists() else {}
    except Exception:
        config = {}

    empty_queue = pending_count == 0 and processing_count == 0
    max_order_value = config.get("max_order_value", 20000)
    try:
        max_order_value_ok = float(max_order_value) > 0
    except Exception:
        max_order_value_ok = False

    checks = [
        _check(
            "local_submit_ready",
            bool(readiness.get("local_submit_ready")),
            ["local_submit", "dry_run_probe", "live_submit"],
            readiness.get("local_submit_ready"),
            "AiStock can write local pending order files.",
        ),
        _check(
            "ptrade_heartbeat_recent",
            bool(readiness.get("ptrade_heartbeat_recent")),
            ["dry_run_probe", "live_submit"],
            status.get("ptrade_heartbeat_age_seconds"),
            "PTrade strategy heartbeat must be recent.",
        ),
        _check(
            "dry_run_probe_ack_recent",
            bool(readiness.get("dry_run_probe_ack_recent")),
            ["live_submit"],
            (status.get("latest_probe_ack") or {}).get("order_id") if isinstance(status.get("latest_probe_ack"), dict) else None,
            "A recent ptrade_bridge_live_probe dry-run ack must exist.",
        ),
        _check(
            "ptrade_live_order_enabled",
            bool(readiness.get("ptrade_live_order_enabled")),
            ["live_submit"],
            readiness.get("ptrade_live_order_enabled"),
            "PTrade strategy heartbeat must report enable_live_order=true before a live-submit test.",
        ),
        _check(
            "no_stale_processing",
            stale_count == 0,
            ["dry_run_probe", "live_submit"],
            stale_count,
            "No stale claimed order files should remain in processing.",
        ),
        _check(
            "empty_queue_before_live_test",
            empty_queue,
            ["live_submit"] if require_empty_queue_for_live else [],
            {"pending_count": pending_count, "processing_count": processing_count},
            "Small live-submit test should start from an empty bridge queue.",
        ),
        _check(
            "max_order_value_positive",
            max_order_value_ok,
            ["live_submit"],
            max_order_value,
            "PTrade-side max_order_value must be positive.",
        ),
    ]

    gates = {
        "local_submit_ready": len(_failed(checks, "local_submit")) == 0,
        "dry_run_probe_ready": len(_failed(checks, "dry_run_probe")) == 0,
        "live_submit_ready": len(_failed(checks, "live_submit")) == 0,
    }
    result = {
        "ok": bool(gates["local_submit_ready"]),
        "audited_at": _now_text(),
        "bridge_dir": str(bridge.paths.root),
        "checks": checks,
        "gates": gates,
        "status": status,
        "config": config,
        "next_actions": _next_actions(checks, status),
        "note": "Read-only audit. It never submits orders and never calls PTrade.",
    }
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only AiStock PTrade bridge readiness audit.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--allow-nonempty-queue-for-live", action="store_true")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    result = run_audit(
        bridge_dir=Path(args.bridge_dir),
        require_empty_queue_for_live=not bool(args.allow_nonempty_queue_for_live),
    )
    write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("gates", {}).get("live_submit_ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())

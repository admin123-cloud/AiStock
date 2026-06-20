"""Verify G3 dry-run BUY/SELL through the PTrade internal strategy.

This gate is stricter than the generic G3 PTrade E2E acceptance: it requires
the active bridge heartbeat to come from ``ptrade_file_bridge_strategy`` before
writing any G3 dry-run orders. It never sends live orders.
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
from scripts.gen3_ptrade_e2e_acceptance import run_acceptance
from utils.paths import report_path, runtime_path


DEFAULT_BRIDGE_DIR = runtime_path("ptrade_bridge")
DEFAULT_REPORT = report_path("gen3_ptrade_internal_strategy_acceptance") / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _check(name: str, ok: bool, detail: Any = None, required: bool = True) -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "required": bool(required), "detail": detail}


def _wait_for_internal_strategy(
    bridge: PTradeFileBridge,
    wait_seconds: float,
    poll_seconds: float,
) -> Dict[str, Any]:
    deadline = time.time() + max(0.0, float(wait_seconds))
    status = bridge.status()
    while time.time() <= deadline:
        status = bridge.status()
        if status.get("ptrade_internal_strategy_running"):
            break
        if wait_seconds <= 0:
            break
        time.sleep(max(0.2, float(poll_seconds)))
    return status


def run_internal_strategy_acceptance(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    code: str = "600001",
    wait_seconds: float = 0.0,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 1.0,
    max_heartbeat_age_seconds: float = 15.0,
) -> Dict[str, Any]:
    started_at = _now_text()
    bridge = PTradeFileBridge(root=bridge_dir)
    status = _wait_for_internal_strategy(bridge, wait_seconds=wait_seconds, poll_seconds=poll_seconds)
    checks: List[Dict[str, Any]] = [
        _check(
            "consumer_is_ptrade_internal_strategy",
            bool(status.get("ptrade_internal_strategy_running")),
            {
                "consumer_type": status.get("ptrade_consumer_type"),
                "heartbeat_source": status.get("ptrade_heartbeat_source"),
                "heartbeat_age_seconds": status.get("ptrade_heartbeat_age_seconds"),
            },
        ),
        _check("no_stale_processing", int(status.get("processing_stale_count") or 0) == 0, status.get("processing_stale_count")),
    ]
    failed = [item for item in checks if item.get("required") and not item.get("ok")]
    if failed:
        return {
            "ok": False,
            "started_at": started_at,
            "completed_at": _now_text(),
            "bridge_dir": str(bridge.paths.root),
            "checks": checks,
            "initial_status": status,
            "acceptance": {},
            "message": "PTrade 内部策略未接管桥接队列；未写入 G3 dry-run 订单。",
            "note": "Start ptrade_file_bridge_strategy inside the PTrade trading terminal, then rerun this gate.",
        }

    acceptance = run_acceptance(
        bridge_dir=bridge.paths.root,
        code=code,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
        max_heartbeat_age_seconds=max_heartbeat_age_seconds,
    )
    for item in acceptance.get("checks") or []:
        if isinstance(item, dict):
            checks.append(item)
    failed = [item for item in checks if item.get("required", True) and not item.get("ok")]
    return {
        "ok": not failed,
        "started_at": started_at,
        "completed_at": _now_text(),
        "bridge_dir": str(bridge.paths.root),
        "checks": checks,
        "initial_status": status,
        "final_status": acceptance.get("final_status") if isinstance(acceptance, dict) else bridge.status(),
        "acceptance": acceptance,
        "buy_ack_status": acceptance.get("buy_ack_status") if isinstance(acceptance, dict) else None,
        "sell_ack_status": acceptance.get("sell_ack_status") if isinstance(acceptance, dict) else None,
        "message": "PTrade 内部策略 G3 dry-run 买卖验收通过。" if not failed else "PTrade 内部策略 G3 dry-run 买卖验收未通过。",
        "note": "Requires ptrade_file_bridge_strategy heartbeat; all submitted orders are dry_run=true.",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify G3 PTrade dry-run through the PTrade internal strategy.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--code", default="600001")
    parser.add_argument("--wait-seconds", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-heartbeat-age-seconds", type=float, default=15.0)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    result = run_internal_strategy_acceptance(
        bridge_dir=Path(args.bridge_dir),
        code=str(args.code),
        wait_seconds=float(args.wait_seconds),
        timeout_seconds=float(args.timeout_seconds),
        poll_seconds=float(args.poll_seconds),
        max_heartbeat_age_seconds=float(args.max_heartbeat_age_seconds),
    )
    _write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

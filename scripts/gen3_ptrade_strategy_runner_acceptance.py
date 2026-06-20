"""Verify G3 dry-run BUY/SELL through the PTrade strategy bridge script.

This gate accepts either a hosted PTrade strategy task or the PTrade bundled
Python script runner, but it rejects the local API runner. It never sends live
orders.
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

from execution.ptrade_bridge import PTradeFileBridge
from scripts.gen3_ptrade_e2e_acceptance import run_acceptance
from utils.paths import report_path, runtime_path


DEFAULT_BRIDGE_DIR = runtime_path("ptrade_bridge")
DEFAULT_REPORT = report_path("gen3_ptrade_strategy_runner_acceptance") / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _check(name: str, ok: bool, detail: Any = None, required: bool = True) -> Dict[str, Any]:
    return {"name": name, "ok": bool(ok), "required": bool(required), "detail": detail}


def run_strategy_runner_acceptance(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    code: str = "600001",
    timeout_seconds: float = 30.0,
    poll_seconds: float = 1.0,
    max_heartbeat_age_seconds: float = 15.0,
) -> Dict[str, Any]:
    started_at = _now_text()
    bridge = PTradeFileBridge(root=bridge_dir)
    status = bridge.status()
    source = status.get("ptrade_heartbeat_source")
    consumer_type = status.get("ptrade_consumer_type")
    checks: List[Dict[str, Any]] = [
        _check(
            "consumer_is_ptrade_strategy_bridge",
            source == "ptrade_file_bridge_strategy"
            and consumer_type in {"ptrade_internal_strategy", "ptrade_python_script_runner"},
            {
                "consumer_type": consumer_type,
                "heartbeat_source": source,
                "execution_mode": status.get("ptrade_heartbeat_execution_mode"),
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
            "message": "PTrade 策略桥接脚本未接管队列；未写入 G3 dry-run 订单。",
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
        "message": "PTrade 策略桥接脚本 G3 dry-run 买卖验收通过。" if not failed else "PTrade 策略桥接脚本 G3 dry-run 买卖验收未通过。",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify G3 PTrade dry-run through the PTrade strategy bridge script.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--code", default="600001")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-heartbeat-age-seconds", type=float, default=15.0)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)
    result = run_strategy_runner_acceptance(
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

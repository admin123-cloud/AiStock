"""Wait for PTrade heartbeat, then run the bounded dry-run acceptance gate."""

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

from scripts.ptrade_bridge_acceptance import (
    DEFAULT_BRIDGE_DIR,
    DEFAULT_STRATEGY_FILE,
    run_acceptance,
    wait_for_heartbeat,
)
from scripts.ptrade_bridge_readiness_audit import run_audit, write_json


DEFAULT_REPORT = ROOT / "reports" / "ptrade_bridge_watch_acceptance" / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_watch_acceptance(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    strategy_file: Path = DEFAULT_STRATEGY_FILE,
    watch_timeout_seconds: float = 600.0,
    poll_seconds: float = 1.0,
    max_heartbeat_age_seconds: float = 30.0,
    dry_run_timeout_seconds: float = 30.0,
    code: str = "600000",
    price: float = 10.5,
    quantity: int = 100,
) -> Dict[str, Any]:
    started_at = _now_text()
    bridge_dir = Path(bridge_dir)
    strategy_file = Path(strategy_file)
    heartbeat = wait_for_heartbeat(
        bridge_dir=bridge_dir,
        timeout_seconds=max(0.1, float(watch_timeout_seconds)),
        poll_seconds=max(0.05, float(poll_seconds)),
        max_heartbeat_age_seconds=max(1.0, float(max_heartbeat_age_seconds)),
    )
    acceptance: Optional[Dict[str, Any]] = None
    if heartbeat.get("ok"):
        acceptance = run_acceptance(
            bridge_dir=bridge_dir,
            strategy_file=strategy_file,
            heartbeat_timeout_seconds=1.0,
            poll_seconds=max(0.05, float(poll_seconds)),
            max_heartbeat_age_seconds=max(1.0, float(max_heartbeat_age_seconds)),
            submit_dry_run=True,
            dry_run_timeout_seconds=max(0.1, float(dry_run_timeout_seconds)),
            code=str(code),
            price=float(price),
            quantity=int(quantity),
        )
    audit = run_audit(bridge_dir=bridge_dir)
    result: Dict[str, Any] = {
        "ok": bool(acceptance and acceptance.get("ok")),
        "started_at": started_at,
        "completed_at": _now_text(),
        "bridge_dir": str(bridge_dir),
        "strategy_file": str(strategy_file),
        "watch_timeout_seconds": float(watch_timeout_seconds),
        "heartbeat": heartbeat,
        "acceptance": acceptance,
        "readiness_audit": audit,
        "next_actions": [],
        "note": "Bounded watcher. It waits for heartbeat and runs dry-run acceptance only; it never submits live orders.",
    }
    if not heartbeat.get("ok"):
        result["next_actions"] = ["Start the PTrade internal strategy and wait for status/latest.json heartbeat."]
    elif acceptance and acceptance.get("ok"):
        result["next_actions"] = ["Dry-run acceptance passed. Switch ENABLE_LIVE_ORDER=True in PTrade only for the final small approved live-submit test."]
    elif acceptance and isinstance(acceptance.get("next_actions"), list):
        result["next_actions"] = [str(item) for item in acceptance.get("next_actions") if item]
    else:
        result["next_actions"] = ["Review watch acceptance report before any live-submit test."]
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for PTrade heartbeat and run dry-run acceptance.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--strategy-file", default=str(DEFAULT_STRATEGY_FILE))
    parser.add_argument("--watch-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-heartbeat-age-seconds", type=float, default=30.0)
    parser.add_argument("--dry-run-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--code", default="600000")
    parser.add_argument("--price", type=float, default=10.5)
    parser.add_argument("--quantity", type=int, default=100)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    quantity = max(100, int(args.quantity))
    quantity = max(100, (quantity // 100) * 100)
    result = run_watch_acceptance(
        bridge_dir=Path(args.bridge_dir),
        strategy_file=Path(args.strategy_file),
        watch_timeout_seconds=max(0.1, float(args.watch_timeout_seconds)),
        poll_seconds=max(0.05, float(args.poll_seconds)),
        max_heartbeat_age_seconds=max(1.0, float(args.max_heartbeat_age_seconds)),
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

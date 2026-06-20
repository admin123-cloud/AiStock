"""Smoke test the AiStock -> PTrade file bridge without sending live orders."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.ptrade_bridge import PTradeFileBridge
from scripts.ptrade_file_bridge_api_runner import process_once, read_json
from utils.paths import runtime_path


DEFAULT_SMOKE_ROOT = runtime_path("ptrade_bridge_smoke")
DEFAULT_REPORT = ROOT / "reports" / "ptrade_bridge_smoke" / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _assert_step(ok: bool, checks: List[Dict[str, Any]], name: str, detail: str = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})


def _fresh_run_dir(base: Path) -> Path:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = base / run_id
    target.mkdir(parents=True, exist_ok=False)
    return target


def run_smoke(
    bridge_dir: Optional[Path] = None,
    max_submit_seconds: float = 0.5,
    include_real_status: bool = False,
) -> Dict[str, Any]:
    run_dir = Path(bridge_dir) if bridge_dir else _fresh_run_dir(DEFAULT_SMOKE_ROOT)
    if bridge_dir:
        run_dir.mkdir(parents=True, exist_ok=True)
        for child in run_dir.iterdir():
            if child.is_dir() and child.name in {"pending", "processing", "acks", "fills", "positions", "orders", "status", "canceled", "errors"}:
                shutil.rmtree(child)
            elif child.is_file() and child.name == "config.json":
                child.unlink()

    checks: List[Dict[str, Any]] = []
    bridge = PTradeFileBridge(root=run_dir)

    started = time.perf_counter()
    order = bridge.submit_order(
        {
            "code": "600000",
            "side": "BUY",
            "quantity": 100,
            "price": 10.5,
            "dry_run": True,
            "require_approval": True,
            "source": "ptrade_bridge_smoke",
        }
    )
    submit_elapsed = time.perf_counter() - started
    pending_file = run_dir / "pending" / f"{order['order_id']}.json"
    _assert_step(pending_file.exists(), checks, "dry_run_pending_written", str(pending_file))
    _assert_step(
        submit_elapsed <= max_submit_seconds,
        checks,
        "submit_latency_within_budget",
        f"{submit_elapsed:.6f}s <= {max_submit_seconds:.6f}s",
    )

    dry_run_result = process_once(run_dir, enable_live_order=False, api_dir=run_dir)
    ack_file = run_dir / "acks" / f"{order['order_id']}.json"
    dry_ack = read_json(ack_file) if ack_file.exists() else {}
    _assert_step(dry_run_result.get("processed") == 1, checks, "dry_run_processed_once", str(dry_run_result))
    _assert_step(dry_ack.get("status") == "dry_run", checks, "dry_run_ack_status", str(dry_ack.get("status")))
    _assert_step(not pending_file.exists(), checks, "dry_run_pending_cleared", str(pending_file))
    _assert_step(not (run_dir / "processing" / pending_file.name).exists(), checks, "dry_run_processing_cleared", pending_file.name)

    approval_order = bridge.submit_order(
        {
            "code": "600000",
            "side": "BUY",
            "quantity": 100,
            "price": 10.5,
            "dry_run": False,
            "require_approval": True,
            "approved": False,
            "source": "ptrade_bridge_smoke",
        }
    )
    approval_result = process_once(run_dir, enable_live_order=True, api_dir=run_dir)
    approval_processing = run_dir / "processing" / f"{approval_order['order_id']}.json"
    approval_ack = read_json(run_dir / "acks" / f"{approval_order['order_id']}.json")
    _assert_step(approval_result.get("processed") == 1, checks, "waiting_approval_processed_once", str(approval_result))
    _assert_step(approval_ack.get("status") == "waiting_approval", checks, "waiting_approval_ack_status", str(approval_ack.get("status")))
    _assert_step(not approval_processing.exists(), checks, "waiting_approval_processing_cleared", str(approval_processing))

    stale_order = bridge.submit_order(
        {
            "code": "600000",
            "side": "BUY",
            "quantity": 100,
            "price": 10.5,
            "dry_run": True,
            "source": "ptrade_bridge_smoke",
        }
    )
    stale_pending = run_dir / "pending" / f"{stale_order['order_id']}.json"
    stale_processing = run_dir / "processing" / stale_pending.name
    stale_pending.replace(stale_processing)
    old_ts = time.time() - 600
    os.utime(stale_processing, (old_ts, old_ts))
    stale_status = bridge.status()
    _assert_step(stale_status.get("processing_stale_count") == 1, checks, "stale_processing_visible", str(stale_status))
    recovered = bridge.mark_stale_processing(max_age_seconds=300, reason="smoke stale processing recovery")
    _assert_step(recovered.get("moved_count") == 1, checks, "stale_processing_moved_to_errors", str(recovered))
    _assert_step(not stale_processing.exists(), checks, "stale_processing_cleared", str(stale_processing))

    final_status = bridge.status()
    ok = all(item.get("ok") for item in checks)
    result: Dict[str, Any] = {
        "ok": ok,
        "started_at": _now_text(),
        "bridge_dir": str(run_dir),
        "submit_elapsed_seconds": round(submit_elapsed, 6),
        "max_submit_seconds": max_submit_seconds,
        "checks": checks,
        "final_status": final_status,
        "note": "No live order was sent; this smoke test uses isolated dry-run files.",
    }
    if include_real_status:
        result["real_bridge_status"] = PTradeFileBridge().status()
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test AiStock PTrade bridge non-blocking behavior.")
    parser.add_argument("--bridge-dir", default="", help="Optional isolated smoke bridge directory. It will be cleared.")
    parser.add_argument("--max-submit-seconds", type=float, default=0.5)
    parser.add_argument("--include-real-status", action="store_true")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    result = run_smoke(
        bridge_dir=Path(args.bridge_dir) if args.bridge_dir else None,
        max_submit_seconds=max(0.01, float(args.max_submit_seconds)),
        include_real_status=bool(args.include_real_status),
    )
    _write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

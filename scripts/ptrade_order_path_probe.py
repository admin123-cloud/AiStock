"""Probe the G2 -> PTrade order path without touching the real bridge queue."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.ptrade_bridge import PTradeFileBridge


DEFAULT_PROBE_ROOT = ROOT / "data" / "runtime" / "ptrade_order_path_probe"
DEFAULT_REPORT = ROOT / "reports" / "ptrade_order_path_probe" / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _fresh_probe_dir(base: Path) -> Path:
    if "ptrade_order_path_probe" not in {part.lower() for part in base.resolve().parts}:
        raise ValueError("Refusing to clear a directory outside ptrade_order_path_probe")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True, exist_ok=True)
    return base


def run_probe(
    bridge_dir: Path = DEFAULT_PROBE_ROOT,
    max_submit_seconds: float = 0.5,
) -> Dict[str, Any]:
    import api.trading as trading

    run_dir = _fresh_probe_dir(Path(bridge_dir))
    bridge = PTradeFileBridge(root=run_dir)
    calls: List[str] = []
    checks: List[Dict[str, Any]] = []
    result: Dict[str, Any] = {}
    missing_candidate_result: Dict[str, Any] = {}
    missing_gate_result: Dict[str, Any] = {}
    elapsed = 0.0

    original_bridge = trading.PTRADE_BRIDGE
    originals: Dict[str, Callable[..., Any]] = {
        "_build_gen2_selection_pool": trading._build_gen2_selection_pool,
        "get_v4_market_gate": trading.get_v4_market_gate,
        "clickhouse_query_df": trading.clickhouse_query_df,
        "clickhouse_table_exists": trading.clickhouse_table_exists,
    }

    def blocked(name: str) -> Callable[..., Any]:
        def _inner(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise RuntimeError(f"{name} should not run on the G2 order fast path")

        return _inner

    try:
        trading.PTRADE_BRIDGE = bridge
        for name in originals:
            setattr(trading, name, blocked(name))

        payload = {
            "code": "600000",
            "signal_date": "2026-06-04",
            "candidate": {
                "code": "600000",
                "entry_date": "2026-06-04",
                "name": "order path probe",
                "buyable": True,
                "is_suspended": False,
                "entry_price": 10.5,
                "stage_label": "probe",
            },
            "market_gate": {
                "trade_date": "2026-06-04",
                "available": True,
                "can_open": True,
                "message": "probe gate pass",
            },
            "dry_run": True,
            "require_approval": True,
            "approved": False,
        }

        started = time.perf_counter()
        result = trading.submit_gen2_paper_order(payload)
        elapsed = time.perf_counter() - started

        missing_candidate_result = trading.submit_gen2_paper_order(
            {
                "code": "600000",
                "signal_date": "2026-06-04",
                "market_gate": payload["market_gate"],
                "dry_run": True,
                "require_approval": True,
                "approved": False,
            }
        )
        missing_gate_result = trading.submit_gen2_paper_order(
            {
                "code": "600000",
                "signal_date": "2026-06-04",
                "candidate": payload["candidate"],
                "dry_run": True,
                "require_approval": True,
                "approved": False,
            }
        )
    finally:
        trading.PTRADE_BRIDGE = original_bridge
        for name, func in originals.items():
            setattr(trading, name, func)

    order_id = ((result or {}).get("order") or {}).get("order_id") if isinstance(result, dict) else None
    pending_file = run_dir / "pending" / f"{order_id}.json" if order_id else None
    status = bridge.status()

    checks.append({"name": "submit_ok", "ok": bool(isinstance(result, dict) and result.get("ok"))})
    checks.append({"name": "candidate_snapshot_used", "ok": result.get("candidate_lookup_mode") == "request_snapshot"})
    checks.append({"name": "market_gate_snapshot_used", "ok": result.get("market_gate_lookup_mode") == "request_snapshot"})
    checks.append(
        {
            "name": "missing_candidate_snapshot_fast_reject",
            "ok": bool(
                isinstance(missing_candidate_result, dict)
                and not missing_candidate_result.get("ok")
                and missing_candidate_result.get("candidate_lookup_mode") == "request_snapshot_missing"
            ),
        }
    )
    checks.append(
        {
            "name": "missing_market_gate_snapshot_fast_reject",
            "ok": bool(
                isinstance(missing_gate_result, dict)
                and not missing_gate_result.get("ok")
                and missing_gate_result.get("market_gate_lookup_mode") == "request_snapshot_missing"
            ),
        }
    )
    checks.append({"name": "no_heavy_lookup_called", "ok": not calls, "detail": calls})
    checks.append({"name": "pending_written", "ok": bool(pending_file and pending_file.exists()), "detail": str(pending_file) if pending_file else ""})
    checks.append(
        {
            "name": "submit_latency_within_budget",
            "ok": elapsed <= max_submit_seconds,
            "detail": f"{elapsed:.6f}s <= {max_submit_seconds:.6f}s",
        }
    )

    return {
        "ok": all(item.get("ok") for item in checks),
        "started_at": _now_text(),
        "bridge_dir": str(run_dir),
        "submit_elapsed_seconds": round(elapsed, 6),
        "max_submit_seconds": max_submit_seconds,
        "checks": checks,
        "result": result,
        "missing_candidate_result": missing_candidate_result,
        "missing_gate_result": missing_gate_result,
        "final_status": status,
        "note": "Isolated probe only. It blocks selection-pool, market-gate, and ClickHouse calls to prove the order path writes pending with request snapshots and quickly rejects missing snapshots without heavy synchronous data work.",
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Probe G2 paper-order non-blocking fast path.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_PROBE_ROOT))
    parser.add_argument("--max-submit-seconds", type=float, default=0.5)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    result = run_probe(
        bridge_dir=Path(args.bridge_dir),
        max_submit_seconds=max(0.01, float(args.max_submit_seconds)),
    )
    _write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

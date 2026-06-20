"""Read-only diagnosis for missing PTrade bridge heartbeat."""

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
from scripts.ptrade_bridge_preflight import (
    DEFAULT_STRATEGY_FILE,
    _extract_bool_literal,
    _extract_strategy_bridge_dir,
    _same_path,
)
from scripts.ptrade_bridge_readiness_audit import write_json
from utils.paths import runtime_path


DEFAULT_BRIDGE_DIR = runtime_path("ptrade_bridge")
DEFAULT_REPORT = ROOT / "reports" / "ptrade_bridge_heartbeat_diagnose" / "latest.json"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _check(name: str, ok: bool, detail: Any = None, message: str = "") -> Dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        "detail": detail,
        "message": message,
    }


def _recent_files(path: Path, limit: int = 5) -> List[Dict[str, Any]]:
    if not path.exists() or not path.is_dir():
        return []
    rows = []
    for item in sorted(path.glob("*.json"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:limit]:
        try:
            mtime = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            mtime = None
        rows.append({"path": str(item), "mtime": mtime})
    return rows


def _read_strategy(strategy_file: Path) -> Dict[str, Any]:
    if not strategy_file.exists():
        return {"exists": False, "readable": False, "text": "", "error": "strategy file missing"}
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return {"exists": True, "readable": True, "text": strategy_file.read_text(encoding=encoding), "encoding": encoding}
        except Exception as exc:
            last_error = str(exc)
    return {"exists": True, "readable": False, "text": "", "error": last_error}


def run_diagnosis(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    strategy_file: Path = DEFAULT_STRATEGY_FILE,
) -> Dict[str, Any]:
    bridge = PTradeFileBridge(root=Path(bridge_dir))
    strategy_file = Path(strategy_file)
    status = bridge.status()
    readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
    strategy = _read_strategy(strategy_file)
    strategy_text = str(strategy.get("text") or "")
    strategy_bridge_dir = _extract_strategy_bridge_dir(strategy_text) if strategy_text else None
    strategy_path_matches = bool(strategy_bridge_dir and _same_path(Path(strategy_bridge_dir), bridge.paths.root))
    status_file = bridge.paths.status_dir / "latest.json"
    status_file_exists = status_file.exists()
    heartbeat_recent = bool(readiness.get("ptrade_heartbeat_recent"))

    checks = [
        _check("bridge_root_exists", bridge.paths.root.exists() and bridge.paths.root.is_dir(), str(bridge.paths.root), "Bridge root must exist."),
        _check("status_dir_exists", bridge.paths.status_dir.exists() and bridge.paths.status_dir.is_dir(), str(bridge.paths.status_dir), "status directory must exist."),
        _check("strategy_file_readable", bool(strategy.get("readable")), {"path": str(strategy_file), "error": strategy.get("error")}, "Strategy file must be readable."),
        _check(
            "strategy_bridge_dir_matches",
            strategy_path_matches,
            {"expected": str(bridge.paths.root), "actual": strategy_bridge_dir},
            "PTrade strategy BRIDGE_DIR must point to this bridge directory.",
        ),
        _check(
            "strategy_live_disabled_for_first_smoke",
            _extract_bool_literal(strategy_text, "ENABLE_LIVE_ORDER") is False if strategy_text else False,
            {"value": _extract_bool_literal(strategy_text, "ENABLE_LIVE_ORDER") if strategy_text else None},
            "First smoke should run with ENABLE_LIVE_ORDER=False.",
        ),
        _check("heartbeat_file_exists", status_file_exists, str(status_file), "PTrade strategy should write status/latest.json."),
        _check(
            "heartbeat_recent",
            heartbeat_recent,
            {"age_seconds": status.get("ptrade_heartbeat_age_seconds"), "file": status.get("ptrade_heartbeat_file")},
            "PTrade strategy heartbeat should be recent.",
        ),
        _check(
            "queue_not_blocking_heartbeat",
            int(status.get("pending_count") or 0) == 0 and int(status.get("processing_count") or 0) == 0,
            {"pending_count": status.get("pending_count"), "processing_count": status.get("processing_count")},
            "Non-empty queues do not explain a missing heartbeat, but they matter before live-submit.",
        ),
    ]

    if heartbeat_recent:
        stage = "heartbeat_ok"
        reason = "PTrade 心跳已出现且仍然新鲜。"
        next_action = "运行 dry-run 探针，验证 PTrade 是否消费 pending 并写回 ack。"
    elif not strategy.get("readable"):
        stage = "strategy_file_unreadable"
        reason = "本地策略模板不可读，无法确认 PTrade 端粘贴的 BRIDGE_DIR 和安全开关。"
        next_action = "重新生成部署包，并复制可读的 ptrade_file_bridge_strategy.py 到 PTrade。"
    elif not strategy_path_matches:
        stage = "strategy_bridge_dir_mismatch"
        reason = "策略文件里的 BRIDGE_DIR 与当前桥接目录不一致，PTrade 即使运行也可能写到别的目录。"
        next_action = "重新生成部署包，或在 PTrade 策略顶部把 BRIDGE_DIR 改为当前 bridge_dir。"
    elif not status_file_exists:
        stage = "heartbeat_file_missing"
        reason = "status/latest.json 不存在，通常表示 PTrade 内部桥接策略未启动、未运行到轮询，或 PTrade 端使用了不同的 BRIDGE_DIR。"
        next_action = "在 PTrade 云仿真交易端启动/重启桥接策略，确认策略顶部 BRIDGE_DIR，然后观察 status/latest.json。"
    else:
        stage = "heartbeat_stale"
        reason = "status/latest.json 存在但不新鲜，通常表示 PTrade 策略曾运行后停止、异常退出，或轮询被 PTrade 端阻塞。"
        next_action = "查看 PTrade 策略日志和本地 errors 目录，重启策略后确认 updated_at 持续刷新。"

    result = {
        "ok": heartbeat_recent,
        "diagnosed_at": _now_text(),
        "bridge_dir": str(bridge.paths.root),
        "strategy_file": str(strategy_file),
        "stage": stage,
        "reason": reason,
        "next_action": next_action,
        "checks": checks,
        "status": status,
        "recent_errors": _recent_files(bridge.paths.errors),
        "recent_status_files": _recent_files(bridge.paths.status_dir),
        "note": "Read-only diagnosis. It never submits orders, writes pending files, calls PTrade, or queries ClickHouse.",
    }
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose missing AiStock PTrade bridge heartbeat.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--strategy-file", default=str(DEFAULT_STRATEGY_FILE))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args(argv)

    result = run_diagnosis(bridge_dir=Path(args.bridge_dir), strategy_file=Path(args.strategy_file))
    write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

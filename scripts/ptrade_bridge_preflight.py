"""Local preflight checks before running the AiStock PTrade bridge strategy."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ptrade_bridge_readiness_audit import run_audit, write_json


DEFAULT_BRIDGE_DIR = ROOT / "data" / "runtime" / "ptrade_bridge"
DEFAULT_STRATEGY_FILE = ROOT / "scripts" / "ptrade_file_bridge_strategy.py"
DEFAULT_REPORT = ROOT / "reports" / "ptrade_bridge_preflight" / "latest.json"
REQUIRED_DIRS = [
    "pending",
    "processing",
    "acks",
    "fills",
    "positions",
    "orders",
    "status",
    "canceled",
    "cancel_requests",
    "cancel_acks",
    "errors",
]


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


def _read_json_file(path: Path) -> Dict[str, Any]:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            data = json.loads(path.read_text(encoding=encoding))
            return data if isinstance(data, dict) else {}
        except Exception:
            continue
    raise ValueError(f"invalid json: {path}")


def _extract_strategy_value(text: str, name: str) -> Optional[str]:
    match = re.search(rf"^\s*{re.escape(name)}\s*=\s*(.+?)\s*$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _extract_strategy_bridge_dir(text: str) -> Optional[str]:
    raw = _extract_strategy_value(text, "BRIDGE_DIR")
    if not raw:
        return None
    match = re.match(r"""[rRuUbBfF]*(['"])(.*)\1""", raw)
    return match.group(2) if match else raw


def _extract_bool_literal(text: str, name: str) -> Optional[bool]:
    raw = _extract_strategy_value(text, name)
    if raw is None:
        return None
    value = raw.split("#", 1)[0].strip()
    if value == "True":
        return True
    if value == "False":
        return False
    return None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except Exception:
        return str(left).lower() == str(right).lower()


def _next_actions(checks: List[Dict[str, Any]], audit: Dict[str, Any]) -> List[str]:
    failed_required = [item["name"] for item in checks if item.get("required") and not item.get("ok")]
    actions: List[str] = []
    if "bridge_dir_writable" in failed_required:
        actions.append("Fix filesystem permissions for the bridge directory before starting PTrade.")
    if "strategy_bridge_dir_matches" in failed_required:
        actions.append("Regenerate the deploy package or edit BRIDGE_DIR in the PTrade strategy before starting it.")
    if "strategy_live_disabled" in failed_required:
        actions.append("Keep ENABLE_LIVE_ORDER = False for the first PTrade dry-run smoke test.")
    if "config_max_order_value_positive" in failed_required:
        actions.append("Fix config.json max_order_value before any live-submit test.")
    if not failed_required:
        actions.append("Start the PTrade internal strategy and wait for status/latest.json heartbeat.")
        actions.extend(str(item) for item in audit.get("next_actions", []) if item)
    return list(dict.fromkeys(actions))


def run_preflight(
    bridge_dir: Path = DEFAULT_BRIDGE_DIR,
    strategy_file: Path = DEFAULT_STRATEGY_FILE,
    create_dirs: bool = True,
    strict_strategy_path: bool = True,
    require_live_disabled: bool = True,
) -> Dict[str, Any]:
    bridge_dir = Path(bridge_dir)
    strategy_file = Path(strategy_file)
    checks: List[Dict[str, Any]] = []

    root_created = False
    try:
        if create_dirs:
            bridge_dir.mkdir(parents=True, exist_ok=True)
        root_created = bridge_dir.exists() and bridge_dir.is_dir()
    except Exception as exc:
        checks.append(_check("bridge_root_exists", False, True, str(exc), "Bridge root must exist or be creatable."))
    else:
        checks.append(_check("bridge_root_exists", root_created, True, str(bridge_dir), "Bridge root exists."))

    dir_details: Dict[str, Any] = {}
    dirs_ok = True
    for name in REQUIRED_DIRS:
        path = bridge_dir / name
        try:
            if create_dirs:
                path.mkdir(parents=True, exist_ok=True)
            ok = path.exists() and path.is_dir()
            dirs_ok = dirs_ok and ok
            dir_details[name] = {"ok": ok, "path": str(path)}
        except Exception as exc:
            dirs_ok = False
            dir_details[name] = {"ok": False, "path": str(path), "error": str(exc)}
    checks.append(_check("required_dirs_exist", dirs_ok, True, dir_details, "Required bridge subdirectories exist."))

    canary_path = bridge_dir / "status" / f"preflight-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.json"
    try:
        payload = {"ok": True, "source": "ptrade_bridge_preflight", "written_at": _now_text()}
        write_json(canary_path, payload)
        read_back = _read_json_file(canary_path)
        writable = read_back.get("source") == "ptrade_bridge_preflight"
    except Exception as exc:
        writable = False
        canary_detail: Any = {"path": str(canary_path), "error": str(exc)}
    else:
        canary_detail = {"path": str(canary_path)}
    finally:
        try:
            if canary_path.exists():
                canary_path.unlink()
        except Exception:
            pass
    checks.append(_check("bridge_dir_writable", writable, True, canary_detail, "Bridge status directory is writable."))

    config_path = bridge_dir / "config.json"
    config: Dict[str, Any] = {}
    config_valid = True
    max_order_value_ok = True
    if config_path.exists():
        try:
            config = _read_json_file(config_path)
        except Exception as exc:
            config_valid = False
            config = {"_error": str(exc)}
        raw_max = config.get("max_order_value", 20000)
        try:
            max_order_value_ok = float(raw_max) > 0
        except Exception:
            max_order_value_ok = False
    checks.append(_check("config_json_valid", config_valid, True, str(config_path) if config_path.exists() else "missing; defaults apply", "config.json is valid JSON when present."))
    checks.append(_check("config_max_order_value_positive", max_order_value_ok, True, config.get("max_order_value", 20000), "max_order_value must be positive."))

    strategy_text = ""
    strategy_exists = strategy_file.exists()
    checks.append(_check("strategy_file_exists", strategy_exists, True, str(strategy_file), "PTrade strategy file exists."))
    if strategy_exists:
        try:
            strategy_text = strategy_file.read_text(encoding="utf-8-sig")
        except Exception as exc:
            checks.append(_check("strategy_file_readable", False, True, str(exc), "PTrade strategy file must be readable as UTF-8."))
        else:
            checks.append(_check("strategy_file_readable", True, True, str(strategy_file), "PTrade strategy file is readable as UTF-8."))

    extracted_bridge_dir = _extract_strategy_bridge_dir(strategy_text) if strategy_text else None
    if extracted_bridge_dir:
        strategy_path_matches = _same_path(Path(extracted_bridge_dir), bridge_dir)
    else:
        strategy_path_matches = False
    checks.append(
        _check(
            "strategy_bridge_dir_matches",
            strategy_path_matches,
            bool(strict_strategy_path),
            {"expected": str(bridge_dir), "actual": extracted_bridge_dir},
            "Strategy BRIDGE_DIR points to the same bridge directory.",
        )
    )

    live_disabled = _extract_bool_literal(strategy_text, "ENABLE_LIVE_ORDER") is False if strategy_text else False
    checks.append(_check("strategy_live_disabled", live_disabled, bool(require_live_disabled), _extract_strategy_value(strategy_text, "ENABLE_LIVE_ORDER") if strategy_text else None, "ENABLE_LIVE_ORDER must be False for the first dry-run smoke test."))

    pending_count = len(list((bridge_dir / "pending").glob("*.json"))) if (bridge_dir / "pending").exists() else 0
    processing_count = len(list((bridge_dir / "processing").glob("*.json"))) if (bridge_dir / "processing").exists() else 0
    checks.append(
        _check(
            "queue_state_visible",
            True,
            False,
            {"pending_count": pending_count, "processing_count": processing_count},
            "Queue state is visible; non-empty queues are reported but not a preflight failure.",
        )
    )

    audit = run_audit(bridge_dir=bridge_dir)
    failed_required = [item for item in checks if item.get("required") and not item.get("ok")]
    result = {
        "ok": len(failed_required) == 0,
        "checked_at": _now_text(),
        "bridge_dir": str(bridge_dir),
        "strategy_file": str(strategy_file),
        "checks": checks,
        "audit": audit,
        "next_actions": _next_actions(checks, audit),
        "note": "Local preflight only. It writes and removes one canary file; it never submits orders and never calls PTrade.",
    }
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Local preflight for the AiStock PTrade bridge.")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--strategy-file", default=str(DEFAULT_STRATEGY_FILE))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--no-create", action="store_true", help="Do not create missing bridge directories.")
    parser.add_argument("--allow-strategy-path-mismatch", action="store_true")
    parser.add_argument("--allow-live-enabled", action="store_true", help="Do not fail only because ENABLE_LIVE_ORDER is True.")
    args = parser.parse_args(argv)

    result = run_preflight(
        bridge_dir=Path(args.bridge_dir),
        strategy_file=Path(args.strategy_file),
        create_dirs=not bool(args.no_create),
        strict_strategy_path=not bool(args.allow_strategy_path_mismatch),
        require_live_disabled=not bool(args.allow_live_enabled),
    )
    write_json(Path(args.report), result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

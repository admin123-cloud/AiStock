"""
Run AiStock -> PTrade file bridge through PTradeQuantAPI.

Default mode is dry-run. It consumes pending JSON orders, writes acknowledgements,
and never sends live orders unless both are true:
- this runner is started with --enable-live-order
- the order JSON contains approved=true and dry_run=false
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRIDGE_DIR = ROOT / "data" / "runtime" / "ptrade_bridge"
DEFAULT_PTRADE_API_DIR = Path(r"D:\PTrade\ptrade\Libs\Python\api")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path: Path) -> Dict[str, Any]:
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


def ensure_dirs(root: Path) -> None:
    for name in ("pending", "processing", "acks", "fills", "positions", "orders", "status", "canceled", "cancel_requests", "cancel_acks", "errors"):
        (root / name).mkdir(parents=True, exist_ok=True)


def unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def iter_pending(root: Path) -> Iterable[Path]:
    pending = root / "pending"
    if not pending.exists():
        return []
    return sorted(pending.glob("*.json"), key=lambda item: item.stat().st_mtime)


def iter_cancel_requests(root: Path) -> Iterable[Path]:
    path = root / "cancel_requests"
    if not path.exists():
        return []
    return sorted(path.glob("*.json"), key=lambda item: item.stat().st_mtime)


def json_file_count(root: Path, folder: str) -> int:
    path = root / folder
    if not path.exists():
        return 0
    return len(list(path.glob("*.json")))


def oldest_json_age_seconds(root: Path, folder: str) -> Optional[float]:
    path = root / folder
    if not path.exists():
        return None
    ages: List[float] = []
    now = time.time()
    for item in path.glob("*.json"):
        try:
            ages.append(max(0.0, now - item.stat().st_mtime))
        except Exception:
            continue
    return round(max(ages), 3) if ages else None


def queue_status(root: Path) -> Dict[str, Any]:
    return {
        "pending_count": json_file_count(root, "pending"),
        "processing_count": json_file_count(root, "processing"),
        "cancel_request_count": json_file_count(root, "cancel_requests"),
        "oldest_pending_age_seconds": oldest_json_age_seconds(root, "pending"),
        "oldest_processing_age_seconds": oldest_json_age_seconds(root, "processing"),
        "oldest_cancel_request_age_seconds": oldest_json_age_seconds(root, "cancel_requests"),
    }


def claim_pending(root: Path, source: Path) -> Optional[Path]:
    target = root / "processing" / source.name
    try:
        source.replace(target)
        return target
    except FileNotFoundError:
        return None


def write_cancel_ack(root: Path, cancel_data: Dict[str, Any], status: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
    payload = dict(cancel_data)
    payload["status"] = status
    payload["message"] = message
    payload["updated_at"] = now_text()
    if extra:
        payload.update(extra)
    order_id = str(payload.get("order_id") or f"unknown-{int(time.time())}")
    write_json(root / "cancel_acks" / f"{order_id}.json", payload)


def execute_cancel(root: Path, source: Path, cancel_data: Dict[str, Any], enable_live_order: bool, api_dir: Path) -> None:
    order_id = str(cancel_data.get("order_id") or source.stem)
    known_order = cancel_data.get("known_order") if isinstance(cancel_data.get("known_order"), dict) else {}
    ptrade_order_id = str(cancel_data.get("ptrade_order_id") or known_order.get("ptrade_order_id") or "").strip()
    if not enable_live_order:
        write_cancel_ack(root, cancel_data, "cancel_dry_run", "Cancel request recorded; live cancel is disabled.")
        unlink_if_exists(source)
        return
    if not ptrade_order_id:
        write_cancel_ack(root, cancel_data, "cancel_unavailable", "No ptrade_order_id is available for broker cancel.", {"order_id": order_id})
        unlink_if_exists(source)
        return
    pt = import_ptrade_api(api_dir)
    cancel_fn = getattr(pt, "cancel_order", None) or getattr(pt, "order_cancel", None) or getattr(pt, "cancel", None)
    if cancel_fn is None:
        raise RuntimeError("PTrade cancel function is unavailable")
    result = cancel_fn(ptrade_order_id)
    write_cancel_ack(root, cancel_data, "cancel_submitted", "Cancel submitted to PTrade.", {"ptrade_order_id": ptrade_order_id, "ptrade_result": str(result)})
    unlink_if_exists(source)


def ptrade_symbol(symbol: str) -> str:
    text = str(symbol or "").strip().upper()
    code = "".join(ch for ch in text if ch.isdigit())[:6]
    if not code:
        return text
    if text.endswith(".XSHG") or text.endswith(".XSHE"):
        return text
    if text.endswith(".SH") or code.startswith(("60", "68", "51", "58")):
        return f"{code}.XSHG"
    return f"{code}.XSHE"


def to_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return default
    return bool(value)


def load_config(root: Path) -> Dict[str, Any]:
    data = read_json(root / "config.json")
    return data if isinstance(data, dict) else {}


def max_live_order_value(root: Path) -> float:
    cfg = load_config(root)
    try:
        return float(cfg.get("max_order_value") or 20000.0)
    except Exception:
        return 20000.0


def validate_live_order(root: Path, order_id: str, side: str, quantity: int, price: Any) -> float:
    if price in (None, ""):
        raise ValueError("PTrade limit order requires price; market order is not enabled")
    limit_price = float(price)
    if limit_price <= 0:
        raise ValueError("PTrade live order price must be positive")
    if quantity <= 0:
        raise ValueError(f"invalid live order quantity: order_id={order_id}")
    if side == "BUY" and quantity % 100 != 0:
        raise ValueError("A-share live buy quantity must be a multiple of 100")
    order_value = abs(float(quantity) * limit_price)
    max_value = max_live_order_value(root)
    if max_value > 0 and order_value > max_value:
        raise ValueError(f"live order value {order_value:.2f} exceeds max_order_value {max_value:.2f}")
    return limit_price


def write_status(root: Path, extra: Optional[Dict[str, Any]] = None) -> None:
    payload = {
        "ok": True,
        "source": "ptrade_file_bridge_api_runner",
        "bridge_dir": str(root),
        "updated_at": now_text(),
    }
    payload.update(queue_status(root))
    if extra:
        payload.update(extra)
    write_json(root / "status" / "latest.json", payload)


def ack(root: Path, order_data: Dict[str, Any], status: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
    payload = dict(order_data)
    payload["status"] = status
    payload["message"] = message
    payload["updated_at"] = now_text()
    if extra:
        payload.update(extra)
    order_id = str(payload.get("order_id") or f"unknown-{int(time.time())}")
    write_json(root / "acks" / f"{order_id}.json", payload)


def mark_error(root: Path, source: Path, order_data: Dict[str, Any], message: str) -> None:
    payload = dict(order_data)
    payload["status"] = "error"
    payload["message"] = message
    payload["updated_at"] = now_text()
    payload["traceback"] = traceback.format_exc(limit=8)
    write_json(root / "errors" / source.name, payload)
    unlink_if_exists(source)


def import_ptrade_api(api_dir: Path):
    api_path = str(api_dir)
    if api_path not in sys.path:
        sys.path.insert(0, api_path)
    import PTradeQuantApi as pt  # type: ignore

    return pt


def execute_order(root: Path, source: Path, order_data: Dict[str, Any], enable_live_order: bool, api_dir: Path) -> None:
    order_id = str(order_data.get("order_id") or source.stem)
    symbol = ptrade_symbol(str(order_data.get("ptrade_symbol") or order_data.get("code") or ""))
    side = str(order_data.get("side") or "").strip().upper()
    quantity = int(float(order_data.get("quantity") or 0))
    price = order_data.get("price")
    dry_run = to_bool(order_data.get("dry_run"), default=True)
    require_approval = to_bool(order_data.get("require_approval"), default=True)
    approved = to_bool(order_data.get("approved"), default=False)

    if not symbol or quantity <= 0 or side not in ("BUY", "SELL"):
        raise ValueError(f"invalid order fields: order_id={order_id}")

    signed_amount = quantity if side == "BUY" else -abs(quantity)
    if dry_run or not enable_live_order:
        ack(
            root,
            order_data,
            "dry_run",
            "Dry run only; no live order was sent.",
            {"ptrade_symbol": symbol, "signed_amount": signed_amount},
        )
        unlink_if_exists(source)
        return

    if require_approval and not approved:
        ack(root, order_data, "waiting_approval", "Order requires approved=true before live execution.")
        unlink_if_exists(source)
        return

    limit_price = validate_live_order(root, order_id, side, quantity, price)

    pt = import_ptrade_api(api_dir)
    ptrade_order_id = pt.order(symbol, signed_amount, limit_price)
    ack(
        root,
        order_data,
        "submitted",
        "Live order submitted to PTrade.",
        {"ptrade_symbol": symbol, "signed_amount": signed_amount, "ptrade_order_id": str(ptrade_order_id)},
    )
    unlink_if_exists(source)


def process_once(root: Path, enable_live_order: bool, api_dir: Path) -> Dict[str, Any]:
    ensure_dirs(root)
    write_status(root, {"event": "process_once_start", "enable_live_order": bool(enable_live_order)})
    processed = 0
    cancel_processed = 0
    errors = 0
    for path in iter_pending(root):
        claimed = claim_pending(root, path)
        if claimed is None:
            continue
        data = read_json(claimed)
        try:
            execute_order(root, claimed, data, enable_live_order=enable_live_order, api_dir=api_dir)
            processed += 1
        except Exception as exc:
            mark_error(root, claimed, data, str(exc))
            errors += 1
    for path in iter_cancel_requests(root):
        data = read_json(path)
        try:
            execute_cancel(root, path, data, enable_live_order=enable_live_order, api_dir=api_dir)
            cancel_processed += 1
        except Exception as exc:
            mark_error(root, path, data, str(exc))
            errors += 1
    result = {"processed": processed, "cancel_processed": cancel_processed, "errors": errors, "updated_at": now_text()}
    write_status(root, {"event": "process_once_done", "enable_live_order": bool(enable_live_order), **result})
    return result


def probe_api(api_dir: Path) -> Dict[str, Any]:
    pt = import_ptrade_api(api_dir)
    portfolio = pt.get_portfolio()
    safe = {
        "ok": True,
        "portfolio_type": type(portfolio).__name__,
        "has_cash": hasattr(portfolio, "cash"),
        "updated_at": now_text(),
    }
    return safe


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="AiStock PTrade file bridge API runner")
    parser.add_argument("--bridge-dir", default=str(DEFAULT_BRIDGE_DIR))
    parser.add_argument("--ptrade-api-dir", default=str(DEFAULT_PTRADE_API_DIR))
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--enable-live-order", action="store_true")
    parser.add_argument("--probe", action="store_true", help="Probe PTrade API connection without printing account data")
    args = parser.parse_args(argv)

    root = Path(args.bridge_dir)
    api_dir = Path(args.ptrade_api_dir)
    ensure_dirs(root)

    if args.probe:
        print(json.dumps(probe_api(api_dir), ensure_ascii=False, indent=2))
        return 0

    if args.loop:
        print(
            json.dumps(
                {
                    "ok": True,
                    "bridge_dir": str(root),
                    "ptrade_api_dir": str(api_dir),
                    "enable_live_order": bool(args.enable_live_order),
                    "pid": os.getpid(),
                    "started_at": now_text(),
                },
                ensure_ascii=False,
            )
        )
        while True:
            result = process_once(root, enable_live_order=bool(args.enable_live_order), api_dir=api_dir)
            if result["processed"] or result["errors"]:
                print(json.dumps(result, ensure_ascii=False))
            time.sleep(max(0.5, float(args.poll_seconds)))

    result = process_once(root, enable_live_order=bool(args.enable_live_order), api_dir=api_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
PTrade strategy template for AiStock file bridge.

Copy this file into PTrade's strategy editor. Keep ENABLE_LIVE_ORDER = False for
the first smoke test, then switch it to True only after the broker login and a
small test order workflow are confirmed.
"""

import json
import time
from pathlib import Path

try:
    import PTradeQuantApi as pt
except Exception:
    pt = None

BRIDGE_DIR = r"F:\Stock\AiStock\data\runtime\ptrade_bridge"
ENABLE_LIVE_ORDER = False
POLL_SECONDS = 3
POSITION_SNAPSHOT_SECONDS = 300
ORDER_SNAPSHOT_SECONDS = 300
ENABLE_POSITION_SNAPSHOT = False
ENABLE_ORDER_SNAPSHOT = False
MAX_LIVE_ORDER_VALUE = 20000.0


def _bridge_path(*parts):
    return Path(BRIDGE_DIR).joinpath(*parts)


def _now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _ensure_dirs():
    for name in ["pending", "processing", "acks", "fills", "positions", "orders", "status", "canceled", "cancel_requests", "cancel_acks", "errors"]:
        path = _bridge_path(name)
        if not path.exists():
            path.mkdir(parents=True)


def _json_file_count(folder):
    path = _bridge_path(folder)
    if not path.exists():
        return 0
    return len([item for item in path.glob("*.json")])


def _oldest_json_age_seconds(folder):
    path = _bridge_path(folder)
    if not path.exists():
        return None
    oldest = None
    for item in path.glob("*.json"):
        try:
            age = max(0.0, time.time() - item.stat().st_mtime)
        except Exception:
            continue
        oldest = age if oldest is None else max(oldest, age)
    return round(oldest, 3) if oldest is not None else None


def _queue_status():
    return {
        "pending_count": _json_file_count("pending"),
        "processing_count": _json_file_count("processing"),
        "cancel_request_count": _json_file_count("cancel_requests"),
        "oldest_pending_age_seconds": _oldest_json_age_seconds("pending"),
        "oldest_processing_age_seconds": _oldest_json_age_seconds("processing"),
        "oldest_cancel_request_age_seconds": _oldest_json_age_seconds("cancel_requests"),
    }


def _g_get(name, default=None):
    holder = globals().get("g")
    if holder is None:
        return default
    return getattr(holder, name, default)


def _g_set(name, value):
    holder = globals().get("g")
    if holder is not None:
        setattr(holder, name, value)


def _remember_error(message):
    _g_set("last_bridge_error", str(message))
    _g_set("last_bridge_error_at", _now_text())


def _read_json(path):
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            with open(path, "r", encoding=encoding) as f:
                return json.load(f)
        except Exception:
            pass
    raise ValueError("invalid json: %s" % path)


def _read_config():
    path = _bridge_path("config.json")
    if not path.exists():
        return {}
    try:
        data = _read_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _to_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "y", "on"):
            return True
        if text in ("0", "false", "no", "n", "off"):
            return False
        return default
    return bool(value)


def _max_live_order_value():
    cfg = _read_config()
    try:
        return float(cfg.get("max_order_value") or MAX_LIVE_ORDER_VALUE)
    except Exception:
        return MAX_LIVE_ORDER_VALUE


def _write_json(folder, name, payload):
    _ensure_dirs()
    path = _bridge_path(folder, name)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _safe_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _safe_value(v) for k, v in value.items()}
    out = {}
    for key in dir(value):
        if key.startswith("_"):
            continue
        try:
            item = getattr(value, key)
        except Exception:
            continue
        if callable(item):
            continue
        out[key] = _safe_value(item)
    return out if out else str(value)


def _as_rows(value):
    safe = _safe_value(value)
    if isinstance(safe, list):
        return safe
    if isinstance(safe, dict):
        for key in ["positions", "orders", "rows", "data", "result"]:
            item = safe.get(key)
            if isinstance(item, list):
                return item
        return [safe]
    if safe in (None, ""):
        return []
    return [{"value": safe}]


def _write_status(extra=None):
    payload = {
        "ok": True,
        "source": "ptrade_internal_strategy",
        "bridge_dir": BRIDGE_DIR,
        "enable_live_order": bool(ENABLE_LIVE_ORDER),
        "enable_position_snapshot": bool(ENABLE_POSITION_SNAPSHOT),
        "enable_order_snapshot": bool(ENABLE_ORDER_SNAPSHOT),
        "max_live_order_value": _max_live_order_value(),
        "poll_seconds": POLL_SECONDS,
        "total_order_processed": int(_g_get("total_order_processed", 0) or 0),
        "total_cancel_processed": int(_g_get("total_cancel_processed", 0) or 0),
        "total_bridge_errors": int(_g_get("total_bridge_errors", 0) or 0),
        "last_bridge_error": _g_get("last_bridge_error", ""),
        "last_bridge_error_at": _g_get("last_bridge_error_at", ""),
        "updated_at": _now_text(),
    }
    payload.update(_queue_status())
    if extra:
        payload.update(extra)
    _write_json("status", "latest.json", payload)


def _ack(order_data, status, message, extra=None):
    payload = dict(order_data)
    payload["status"] = status
    payload["message"] = message
    payload["updated_at"] = _now_text()
    if extra:
        payload.update(extra)
    _write_json("acks", "%s.json" % order_data.get("order_id", "unknown"), payload)


def _cancel_ack(cancel_data, status, message, extra=None):
    payload = dict(cancel_data)
    payload["status"] = status
    payload["message"] = message
    payload["updated_at"] = _now_text()
    if extra:
        payload.update(extra)
    _write_json("cancel_acks", "%s.json" % payload.get("order_id", "unknown"), payload)


def _move_to_error(path, order_data, message):
    path = Path(path)
    order_data["status"] = "error"
    order_data["message"] = message
    order_data["updated_at"] = _now_text()
    _write_json("errors", path.name, order_data)
    try:
        path.unlink()
    except Exception:
        pass


def _claim_pending(path):
    path = Path(path)
    processing = _bridge_path("processing")
    if not processing.exists():
        processing.mkdir(parents=True)
    target = processing / path.name
    try:
        path.replace(target)
        return target
    except Exception:
        return None


def _ptrade_order(code, qty, price):
    if price in (None, ""):
        raise ValueError("PTrade live order requires limit price")
    limit_price = float(price)

    global_order = globals().get("order")
    if callable(global_order):
        try:
            return global_order(code, qty, limit_price)
        except TypeError:
            return global_order(code, qty)

    if pt is None:
        raise RuntimeError("PTradeQuantApi is unavailable and no global order() exists")
    return pt.order(code, qty, limit_price)


def _ptrade_cancel(ptrade_order_id):
    for name in ("cancel_order", "order_cancel", "cancel"):
        fn = globals().get(name)
        if callable(fn):
            return fn(ptrade_order_id)
        if pt is not None and hasattr(pt, name):
            return getattr(pt, name)(ptrade_order_id)
    raise RuntimeError("PTrade cancel function is unavailable")


def _validate_live_order(order_data, code, side, qty, price):
    if not code:
        raise ValueError("PTrade live order requires code")
    if side not in ("BUY", "SELL"):
        raise ValueError("PTrade live order side must be BUY or SELL")
    abs_qty = abs(int(qty or 0))
    if abs_qty <= 0:
        raise ValueError("PTrade live order quantity must be positive")
    if side == "BUY" and abs_qty % 100 != 0:
        raise ValueError("A-share live buy quantity must be a multiple of 100")
    if price in (None, ""):
        raise ValueError("PTrade live order requires limit price")
    limit_price = float(price)
    if limit_price <= 0:
        raise ValueError("PTrade live order price must be positive")
    order_value = abs_qty * limit_price
    max_value = _max_live_order_value()
    if max_value > 0 and order_value > max_value:
        raise ValueError("live order value %.2f exceeds max_order_value %.2f" % (order_value, max_value))
    return limit_price


def _call_ptrade(name):
    fn = globals().get(name)
    if callable(fn):
        return fn()
    if pt is not None and hasattr(pt, name):
        return getattr(pt, name)()
    raise RuntimeError("%s unavailable" % name)


def _place_order(order_data):
    code = order_data.get("ptrade_symbol") or order_data.get("code")
    side = str(order_data.get("side") or "").upper()
    qty = int(order_data.get("quantity") or 0)
    price = order_data.get("price")
    if side == "SELL":
        qty = -abs(qty)

    if _to_bool(order_data.get("dry_run"), True) or not ENABLE_LIVE_ORDER:
        _ack(order_data, "dry_run", "Dry run only; no live order was sent.")
        return

    if _to_bool(order_data.get("require_approval"), True) and not _to_bool(order_data.get("approved"), False):
        _ack(order_data, "waiting_approval", "Order requires approved=true before live execution.")
        return

    limit_price = _validate_live_order(order_data, code, side, qty, price)
    result = _ptrade_order(code, qty, limit_price)
    _ack(order_data, "submitted", "Live order submitted to PTrade.", {"ptrade_result": str(result)})


def _poll_pending():
    pending = _bridge_path("pending")
    result = {"processed": 0, "errors": 0}
    if not pending.exists():
        return result
    for path in sorted(pending.glob("*.json")):
        name = path.name
        claimed = _claim_pending(path)
        if not claimed:
            continue
        try:
            data = _read_json(claimed)
            _place_order(data)
            Path(claimed).unlink()
            result["processed"] += 1
        except Exception as exc:
            try:
                data = _read_json(claimed)
            except Exception:
                data = {"order_id": name.replace(".json", "")}
            _remember_error(str(exc))
            _move_to_error(claimed, data, str(exc))
            result["errors"] += 1
    return result


def _poll_cancel_requests():
    request_dir = _bridge_path("cancel_requests")
    result = {"cancel_processed": 0, "errors": 0}
    if not request_dir.exists():
        return result
    for path in sorted(request_dir.glob("*.json")):
        name = path.name
        try:
            data = _read_json(path)
            known_order = data.get("known_order") if isinstance(data.get("known_order"), dict) else {}
            ptrade_order_id = str(data.get("ptrade_order_id") or known_order.get("ptrade_order_id") or "").strip()
            if not ENABLE_LIVE_ORDER:
                _cancel_ack(data, "cancel_dry_run", "Cancel request recorded; live cancel is disabled.")
                path.unlink()
                result["cancel_processed"] += 1
                continue
            if not ptrade_order_id:
                _cancel_ack(data, "cancel_unavailable", "No ptrade_order_id is available for broker cancel.")
                path.unlink()
                result["cancel_processed"] += 1
                continue
            cancel_result = _ptrade_cancel(ptrade_order_id)
            _cancel_ack(data, "cancel_submitted", "Cancel submitted to PTrade.", {"ptrade_order_id": ptrade_order_id, "ptrade_result": str(cancel_result)})
            path.unlink()
            result["cancel_processed"] += 1
        except Exception as exc:
            try:
                data = _read_json(path)
            except Exception:
                data = {"order_id": name.replace(".json", "")}
            _remember_error(str(exc))
            _move_to_error(path, data, str(exc))
            result["errors"] += 1
    return result


def _snapshot_positions():
    try:
        raw = _call_ptrade("query_stock_positions")
        rows = _as_rows(raw)
        _write_json(
            "positions",
            "latest.json",
            {
                "ok": True,
                "source": "ptrade_internal_strategy",
                "snapshot_time": _now_text(),
                "positions": rows,
            },
        )
        return {"ok": True, "position_count": len(rows)}
    except Exception as exc:
        _write_json(
            "status",
            "position_snapshot_error.json",
            {"ok": False, "source": "ptrade_internal_strategy", "updated_at": _now_text(), "message": str(exc)},
        )
        return {"ok": False, "message": str(exc)}


def _snapshot_orders():
    try:
        raw = _call_ptrade("query_stock_orders")
        rows = _as_rows(raw)
        _write_json(
            "orders",
            "latest.json",
            {
                "ok": True,
                "source": "ptrade_internal_strategy",
                "snapshot_time": _now_text(),
                "orders": rows,
            },
        )
        return {"ok": True, "order_count": len(rows)}
    except Exception as exc:
        _write_json(
            "status",
            "order_snapshot_error.json",
            {"ok": False, "source": "ptrade_internal_strategy", "updated_at": _now_text(), "message": str(exc)},
        )
        return {"ok": False, "message": str(exc)}


def initialize(context):
    _ensure_dirs()
    g.last_poll_ts = 0
    g.last_position_snapshot_ts = 0
    g.last_order_snapshot_ts = 0
    g.total_order_processed = 0
    g.total_cancel_processed = 0
    g.total_bridge_errors = 0
    g.last_bridge_error = ""
    g.last_bridge_error_at = ""
    _write_status({"event": "initialize"})
    log.info("AiStock PTrade file bridge initialized: %s" % BRIDGE_DIR)


def handle_data(context, data):
    now = time.time()
    if now - getattr(g, "last_poll_ts", 0) < POLL_SECONDS:
        return
    g.last_poll_ts = now
    order_result = _poll_pending()
    cancel_result = _poll_cancel_requests()
    g.total_order_processed = int(getattr(g, "total_order_processed", 0) or 0) + int(order_result.get("processed") or 0)
    g.total_cancel_processed = int(getattr(g, "total_cancel_processed", 0) or 0) + int(cancel_result.get("cancel_processed") or 0)
    g.total_bridge_errors = int(getattr(g, "total_bridge_errors", 0) or 0) + int(order_result.get("errors") or 0) + int(cancel_result.get("errors") or 0)
    status_extra = {"event": "poll", "last_order_poll": order_result, "last_cancel_poll": cancel_result}
    if ENABLE_POSITION_SNAPSHOT and now - getattr(g, "last_position_snapshot_ts", 0) >= POSITION_SNAPSHOT_SECONDS:
        g.last_position_snapshot_ts = now
        status_extra["position_snapshot"] = _snapshot_positions()
    if ENABLE_ORDER_SNAPSHOT and now - getattr(g, "last_order_snapshot_ts", 0) >= ORDER_SNAPSHOT_SECONDS:
        g.last_order_snapshot_ts = now
        status_extra["order_snapshot"] = _snapshot_orders()
    _write_status(status_extra)

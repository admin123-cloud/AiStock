"""
File based bridge between AiStock and a PTrade strategy.

AiStock writes order intents to ``pending``. A PTrade strategy polls those files,
places orders inside the broker-authorized terminal, then writes acknowledgements
to ``acks`` and fills/position snapshots to their matching folders.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_json_load(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        for encoding in ("utf-8-sig", "utf-8"):
            try:
                return json.loads(path.read_text(encoding=encoding))
            except Exception:
                continue
        return default
    except Exception:
        return default


def _safe_json_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _normalize_code(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"(\d{6})", text)
    return match.group(1) if match else ""


def _ptrade_symbol(code6: str) -> str:
    if code6.startswith(("60", "68", "51", "58")):
        return f"{code6}.SH"
    return f"{code6}.SZ"


def _iter_json_files(path: Path) -> Iterable[Path]:
    if not path.exists():
        return []
    return sorted(path.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)


def _file_age_seconds(path: Path, now: Optional[datetime] = None) -> float:
    try:
        current = now or datetime.now()
        return max(0.0, current.timestamp() - path.stat().st_mtime)
    except Exception:
        return 0.0


def _latest_ack_matching(path: Path, source: str = "") -> Optional[Dict[str, Any]]:
    for item in _iter_json_files(path):
        data = _safe_json_load(item, {})
        if not isinstance(data, dict):
            continue
        if source:
            raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
            candidates = {
                str(data.get("source") or ""),
                str(raw.get("source") or ""),
            }
            if source not in candidates:
                continue
        data["_file"] = str(item)
        data["_age_seconds"] = round(_file_age_seconds(item), 3)
        return data
    return None


@dataclass(frozen=True)
class PTradeBridgePaths:
    root: Path

    @property
    def pending(self) -> Path:
        return self.root / "pending"

    @property
    def acks(self) -> Path:
        return self.root / "acks"

    @property
    def fills(self) -> Path:
        return self.root / "fills"

    @property
    def positions(self) -> Path:
        return self.root / "positions"

    @property
    def status_dir(self) -> Path:
        return self.root / "status"

    @property
    def processing(self) -> Path:
        return self.root / "processing"

    @property
    def canceled(self) -> Path:
        return self.root / "canceled"

    @property
    def cancel_requests(self) -> Path:
        return self.root / "cancel_requests"

    @property
    def cancel_acks(self) -> Path:
        return self.root / "cancel_acks"

    @property
    def errors(self) -> Path:
        return self.root / "errors"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    def ensure(self) -> None:
        for path in [
            self.pending,
            self.processing,
            self.acks,
            self.fills,
            self.positions,
            self.status_dir,
            self.canceled,
            self.cancel_requests,
            self.cancel_acks,
            self.errors,
        ]:
            path.mkdir(parents=True, exist_ok=True)


class PTradeFileBridge:
    def __init__(self, root: Optional[Path | str] = None) -> None:
        default_root = Path(__file__).resolve().parents[1] / "data" / "runtime" / "ptrade_bridge"
        self.paths = PTradeBridgePaths(Path(root or os.getenv("AISTOCK_PTRADE_BRIDGE_DIR") or default_root))
        self.paths.ensure()

    def status(self) -> Dict[str, Any]:
        cfg = _safe_json_load(self.paths.config, {})
        if not isinstance(cfg, dict):
            cfg = {}
        stale_seconds = max(30, int(float(cfg.get("processing_stale_seconds", 300) or 300)))
        processing_files = list(_iter_json_files(self.paths.processing))
        stale_processing = [path for path in processing_files if _file_age_seconds(path) >= stale_seconds]
        last_ack = next(iter(_iter_json_files(self.paths.acks)), None)
        last_fill = next(iter(_iter_json_files(self.paths.fills)), None)
        last_position = next(iter(_iter_json_files(self.paths.positions)), None)
        last_processing = next(iter(processing_files), None)
        heartbeat_file = self.paths.status_dir / "latest.json"
        heartbeat = _safe_json_load(heartbeat_file, {})
        if not isinstance(heartbeat, dict):
            heartbeat = {}
        heartbeat_age = _file_age_seconds(heartbeat_file) if heartbeat_file.exists() else None
        heartbeat_recent_seconds = max(5, int(float(cfg.get("heartbeat_recent_seconds", 30) or 30)))
        dry_run_ack_recent_seconds = max(30, int(float(cfg.get("dry_run_ack_recent_seconds", 600) or 600)))
        latest_probe_ack = _latest_ack_matching(self.paths.acks, source="ptrade_bridge_live_probe")
        latest_probe_ack_age = latest_probe_ack.get("_age_seconds") if latest_probe_ack else None
        latest_probe_ack_status = str(latest_probe_ack.get("status") or "") if latest_probe_ack else ""
        heartbeat_recent = heartbeat_age is not None and heartbeat_age <= heartbeat_recent_seconds
        ptrade_live_order_enabled = bool(heartbeat.get("enable_live_order"))
        dry_run_probe_recent = (
            latest_probe_ack is not None
            and latest_probe_ack_status in {"dry_run", "waiting_approval"}
            and isinstance(latest_probe_ack_age, (int, float))
            and latest_probe_ack_age <= dry_run_ack_recent_seconds
        )
        strategy_queue = {
            "pending_count": heartbeat.get("pending_count"),
            "processing_count": heartbeat.get("processing_count"),
            "cancel_request_count": heartbeat.get("cancel_request_count"),
            "oldest_pending_age_seconds": heartbeat.get("oldest_pending_age_seconds"),
            "oldest_processing_age_seconds": heartbeat.get("oldest_processing_age_seconds"),
            "oldest_cancel_request_age_seconds": heartbeat.get("oldest_cancel_request_age_seconds"),
            "total_order_processed": heartbeat.get("total_order_processed"),
            "total_cancel_processed": heartbeat.get("total_cancel_processed"),
            "total_bridge_errors": heartbeat.get("total_bridge_errors"),
            "last_bridge_error": heartbeat.get("last_bridge_error"),
            "last_bridge_error_at": heartbeat.get("last_bridge_error_at"),
            "last_order_poll": heartbeat.get("last_order_poll"),
            "last_cancel_poll": heartbeat.get("last_cancel_poll"),
        }
        ready_for_live = bool(heartbeat_recent and dry_run_probe_recent and ptrade_live_order_enabled and not stale_processing)
        return {
            "ok": True,
            "bridge_dir": str(self.paths.root),
            "pending_count": len(list(_iter_json_files(self.paths.pending))),
            "processing_count": len(processing_files),
            "processing_stale_seconds": stale_seconds,
            "processing_stale_count": len(stale_processing),
            "ack_count": len(list(_iter_json_files(self.paths.acks))),
            "fill_count": len(list(_iter_json_files(self.paths.fills))),
            "canceled_count": len(list(_iter_json_files(self.paths.canceled))),
            "cancel_request_count": len(list(_iter_json_files(self.paths.cancel_requests))),
            "cancel_ack_count": len(list(_iter_json_files(self.paths.cancel_acks))),
            "dry_run_default": bool(cfg.get("dry_run_default", True)),
            "require_approval_default": bool(cfg.get("require_approval_default", True)),
            "last_processing_file": str(last_processing) if last_processing else None,
            "last_processing_age_seconds": round(_file_age_seconds(last_processing), 3) if last_processing else None,
            "ptrade_heartbeat": heartbeat or None,
            "ptrade_heartbeat_file": str(heartbeat_file) if heartbeat_file.exists() else None,
            "ptrade_heartbeat_age_seconds": round(heartbeat_age, 3) if heartbeat_age is not None else None,
            "ptrade_strategy_queue": strategy_queue if heartbeat else None,
            "heartbeat_recent_seconds": heartbeat_recent_seconds,
            "dry_run_ack_recent_seconds": dry_run_ack_recent_seconds,
            "latest_probe_ack": latest_probe_ack,
            "ready_for_live_order": ready_for_live,
            "readiness": {
                "local_submit_ready": True,
                "ptrade_heartbeat_recent": bool(heartbeat_recent),
                "dry_run_probe_ack_recent": bool(dry_run_probe_recent),
                "ptrade_live_order_enabled": bool(ptrade_live_order_enabled),
                "no_stale_processing": len(stale_processing) == 0,
                "ready_for_live_order": ready_for_live,
                "message": "PTrade bridge is ready for approved live order." if ready_for_live else "Local submit is available, but PTrade consumption is not fully proven yet.",
            },
            "last_ack_file": str(last_ack) if last_ack else None,
            "last_fill_file": str(last_fill) if last_fill else None,
            "last_position_file": str(last_position) if last_position else None,
            "updated_at": _now_text(),
        }

    def save_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        current = _safe_json_load(self.paths.config, {})
        if not isinstance(current, dict):
            current = {}
        for key in [
            "dry_run_default",
            "require_approval_default",
            "max_order_value",
            "processing_stale_seconds",
            "heartbeat_recent_seconds",
            "dry_run_ack_recent_seconds",
        ]:
            if key in payload:
                current[key] = payload.get(key)
        current["updated_at"] = _now_text()
        _safe_json_write(self.paths.config, current)
        return current

    def submit_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")

        code = _normalize_code(payload.get("code"))
        if not code:
            raise ValueError("code must be a 6 digit stock code")

        side = str(payload.get("side") or payload.get("action") or "").strip().upper()
        if side in {"BUY", "B", "LONG"}:
            side = "BUY"
        elif side in {"SELL", "S", "SHORT"}:
            side = "SELL"
        else:
            raise ValueError("side must be BUY or SELL")

        quantity = int(float(payload.get("quantity") or payload.get("shares") or 0))
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if side == "BUY" and quantity % 100 != 0:
            raise ValueError("A-share buy quantity must be a multiple of 100")

        price = payload.get("price")
        price_type = str(payload.get("price_type") or ("limit" if price else "market")).strip().lower()
        cfg = _safe_json_load(self.paths.config, {})
        dry_run = bool(payload.get("dry_run", cfg.get("dry_run_default", True)))
        require_approval = bool(payload.get("require_approval", cfg.get("require_approval_default", True)))

        order_id = str(payload.get("order_id") or f"pt-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}")
        order = {
            "schema": "aistock.ptrade.order.v1",
            "order_id": order_id,
            "created_at": _now_text(),
            "status": "pending",
            "source": str(payload.get("source") or "aistock"),
            "strategy": str(payload.get("strategy") or "manual"),
            "code": code,
            "ptrade_symbol": str(payload.get("ptrade_symbol") or _ptrade_symbol(code)),
            "name": str(payload.get("name") or ""),
            "side": side,
            "quantity": quantity,
            "price": float(price) if price not in (None, "") else None,
            "price_type": price_type,
            "dry_run": dry_run,
            "require_approval": require_approval,
            "approved": bool(payload.get("approved", False)),
            "reason": str(payload.get("reason") or ""),
            "risk": payload.get("risk") if isinstance(payload.get("risk"), dict) else {},
            "raw": payload,
        }
        _safe_json_write(self.paths.pending / f"{order_id}.json", order)
        return order

    def list_orders(self, limit: int = 100) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        for folder, status in [
            (self.paths.pending, "pending"),
            (self.paths.processing, "processing"),
            (self.paths.acks, "ack"),
            (self.paths.fills, "fill"),
            (self.paths.canceled, "canceled"),
            (self.paths.cancel_requests, "cancel_requested"),
            (self.paths.cancel_acks, "cancel_ack"),
            (self.paths.errors, "error"),
        ]:
            for path in _iter_json_files(folder):
                data = _safe_json_load(path, {})
                if isinstance(data, dict):
                    data["_bridge_status"] = status
                    data["_file"] = str(path)
                    if status == "processing":
                        data["_processing_age_seconds"] = round(_file_age_seconds(path), 3)
                    rows.append(data)
        rows.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
        return {"rows": rows[: max(1, int(limit))], "updated_at": _now_text()}

    def mark_stale_processing(self, max_age_seconds: int = 300, reason: str = "") -> Dict[str, Any]:
        threshold = max(30, int(max_age_seconds or 300))
        moved: List[Dict[str, Any]] = []
        for source in _iter_json_files(self.paths.processing):
            age = _file_age_seconds(source)
            if age < threshold:
                continue
            data = _safe_json_load(source, {})
            if not isinstance(data, dict):
                data = {"order_id": source.stem}
            data["status"] = "stale_processing"
            data["message"] = reason or "Order stayed in processing too long; verify PTrade terminal before resubmitting."
            data["processing_age_seconds"] = round(age, 3)
            data["updated_at"] = _now_text()
            target = self.paths.errors / source.name
            _safe_json_write(target, data)
            source.unlink(missing_ok=True)
            moved.append({"order_id": data.get("order_id") or source.stem, "age_seconds": round(age, 3), "file": str(target)})
        return {"ok": True, "moved": moved, "moved_count": len(moved), "updated_at": _now_text()}

    def list_fills(self, limit: int = 100) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        for path in _iter_json_files(self.paths.fills):
            data = _safe_json_load(path, {})
            if isinstance(data, dict):
                data["_bridge_status"] = "fill"
                data["_file"] = str(path)
                rows.append(data)
        rows.sort(key=lambda item: str(item.get("filled_at") or item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return {"rows": rows[: max(1, int(limit))], "updated_at": _now_text()}

    def list_position_snapshots(self, limit: int = 20) -> Dict[str, Any]:
        rows: List[Dict[str, Any]] = []
        for path in _iter_json_files(self.paths.positions):
            data = _safe_json_load(path, {})
            if isinstance(data, dict):
                data["_file"] = str(path)
                rows.append(data)
        rows.sort(key=lambda item: str(item.get("snapshot_time") or item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return {"rows": rows[: max(1, int(limit))], "updated_at": _now_text()}

    def latest_positions(self) -> Dict[str, Any]:
        latest = next(iter(_iter_json_files(self.paths.positions)), None)
        if latest is None:
            return {"ok": False, "positions": [], "message": "no position snapshot", "updated_at": _now_text()}
        data = _safe_json_load(latest, {})
        if not isinstance(data, dict):
            return {"ok": False, "positions": [], "message": "invalid position snapshot", "updated_at": _now_text()}
        positions = data.get("positions")
        if not isinstance(positions, list):
            positions = []
        return {
            "ok": True,
            "positions": positions,
            "snapshot_time": data.get("snapshot_time") or data.get("updated_at") or data.get("created_at"),
            "account": data.get("account"),
            "source": data.get("source") or "ptrade",
            "file": str(latest),
            "updated_at": _now_text(),
        }

    def cancel_order(self, order_id: str, reason: str = "") -> Dict[str, Any]:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "", str(order_id or ""))
        if not safe_id:
            raise ValueError("order_id is required")
        source = self.paths.pending / f"{safe_id}.json"
        data = _safe_json_load(source, {}) if source.exists() else {}
        if not isinstance(data, dict):
            data = {}
        if source.exists():
            data = data or {"order_id": safe_id}
            data["status"] = "canceled"
            data["cancel_reason"] = reason
            data["updated_at"] = _now_text()
            _safe_json_write(self.paths.canceled / f"{safe_id}.json", data)
            source.unlink(missing_ok=True)
            return {"ok": True, "order_id": safe_id, "status": "canceled", "mode": "local_pending_removed"}

        ack_data = _safe_json_load(self.paths.acks / f"{safe_id}.json", {})
        if not isinstance(ack_data, dict):
            ack_data = {}
        request = {
            "schema": "aistock.ptrade.cancel.v1",
            "order_id": safe_id,
            "status": "cancel_requested",
            "created_at": _now_text(),
            "reason": reason,
            "source": "aistock",
            "known_order": ack_data,
        }
        _safe_json_write(self.paths.cancel_requests / f"{safe_id}.json", request)
        return {"ok": True, "order_id": safe_id, "status": "cancel_requested", "mode": "async_ptrade_cancel_request"}

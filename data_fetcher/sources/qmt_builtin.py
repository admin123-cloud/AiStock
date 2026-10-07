"""File transport to the market-only model running inside Big QMT.

SDK methods execute on QMT's main model thread. Nothing imports xtquant here.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from utils.paths import runtime_path


class QmtBuiltinError(RuntimeError):
    pass


class QmtBuiltinCapabilityError(QmtBuiltinError):
    pass


def bridge_root() -> Path:
    configured = os.getenv("AISTOCK_QMT_BUILTIN_ROOT", "").strip()
    return Path(configured) if configured else runtime_path("qmt_builtin_market")


def _write_json(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(body, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    for attempt in range(10):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.01)


def _read_json(path: Path) -> dict[str, Any]:
    for attempt in range(10):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


class QmtBuiltinMarketApi:
    transport = "builtin"
    protocol = 1

    def __init__(self, root: Path | None = None, timeout: float | None = None):
        self.root = root or bridge_root()
        self.timeout = timeout if timeout is not None else float(os.getenv("AISTOCK_QMT_BUILTIN_TIMEOUT", "20"))
        self.session: str | None = None
        self._streams: dict[int, dict[str, Any]] = {}
        self._stream_lock = threading.Lock()

    def health(self) -> dict[str, Any]:
        try:
            body = _read_json(self.root / "heartbeat.json")
        except (OSError, ValueError) as exc:
            raise QmtBuiltinError("Big QMT model is not running: missing/invalid heartbeat") from exc
        age = time.time() - float(body.get("updated_at_epoch", 0))
        if body.get("protocol") != self.protocol or body.get("source") != "qmt_builtin_contextinfo":
            raise QmtBuiltinError("Big QMT bridge protocol/source mismatch")
        if body.get("status") != "ready" or not -5 <= age <= 30 or not body.get("market_only"):
            raise QmtBuiltinError(f"Big QMT bridge unavailable: status={body.get('status')}, heartbeat_age={age:.1f}s")
        if self.session is not None and body.get("session") != self.session:
            raise QmtBuiltinError("Big QMT model restarted; reconnect explicitly")
        return body

    def connect(self, *_unused: Any) -> dict[str, Any]:
        if not self._streams:
            self.session = None
        body = self.health()
        self.session = body["session"]
        return {"ok": True, "transport": self.transport, "source": body["source"],
                "bridge_root": str(self.root), "session": self.session,
                "capabilities": body.get("capabilities", [])}

    def _request(self, operation: str, arguments: dict[str, Any], timeout: float | None = None) -> Any:
        body = self.health()
        if self.session is None:
            self.session = body["session"]
        if operation not in body.get("capabilities", []):
            raise QmtBuiltinCapabilityError(f"Big QMT model does not expose {operation}")
        request_id = uuid.uuid4().hex
        duration = self.timeout if timeout is None else timeout
        deadline = time.time() + duration
        input_path = self.root / "requests" / f"{request_id}.json"
        output_path = self.root / "responses" / f"{request_id}.json"
        _write_json(input_path, {
            "protocol": self.protocol, "session": self.session, "id": request_id,
            "operation": operation, "arguments": arguments, "deadline_epoch": deadline})
        try:
            while time.time() < deadline:
                if output_path.exists():
                    result = _read_json(output_path)
                    if (result.get("id") != request_id or result.get("session") != self.session
                            or result.get("protocol") != self.protocol):
                        raise QmtBuiltinError("Big QMT response identity mismatch")
                    if not result.get("ok"):
                        raise QmtBuiltinError(f"{operation}: {result.get('error_type')}: {result.get('error')}")
                    return result.get("result")
                time.sleep(0.1)
            raise QmtBuiltinError(f"Big QMT {operation} timed out; execution/delivery is unconfirmed")
        finally:
            # Only own exact request/response files; claimed work is never killed.
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    def get_full_tick(self, stock_codes: list[str]) -> dict[str, Any]:
        self.check_stream_health()
        if not stock_codes:
            return {}
        return self._request("get_full_tick", {"codes": stock_codes}) or {}

    def download_history_data(self, stock_code: str, period: str, start_time: str = "", end_time: str = "") -> Any:
        return self._request("download_history_data", {
            "code": stock_code, "period": period, "start_time": start_time, "end_time": end_time},
            timeout=float(os.getenv("AISTOCK_QMT_BUILTIN_DOWNLOAD_TIMEOUT", "120")))

    def download_history_data2(self, stock_list: list[str], period: str, start_time: str = "",
                               end_time: str = "", callback=None, incrementally=None) -> None:
        if incrementally is not None:
            raise QmtBuiltinCapabilityError("Native downloader has no confirmed incrementally parameter")
        if "download_history_data2" in self.health().get("capabilities", []):
            finished = 0
            for offset in range(0, len(stock_list), 16):
                batch = stock_list[offset:offset + 16]
                result = self._request("download_history_data2", {
                    "codes": batch, "period": period, "start_time": start_time, "end_time": end_time},
                    timeout=float(os.getenv("AISTOCK_QMT_BUILTIN_DOWNLOAD_TIMEOUT", "120")))
                if (set(result.get("completed", [])) | {item["code"] for item in result.get("failures", [])}) != set(batch):
                    raise QmtBuiltinError("Native download batch completion identity mismatch")
                for code in result["completed"]:
                    finished += 1
                    if callback is not None:
                        callback({"finished": finished, "total": len(stock_list), "stockcode": code})
                if result.get("failures"):
                    raise QmtBuiltinError("Native download batch has failed codes: " + json.dumps(result, ensure_ascii=False))
            return
        for offset, code in enumerate(stock_list, 1):
            self.download_history_data(code, period, start_time, end_time)
            if callback is not None:
                callback({"finished": offset, "total": len(stock_list), "stockcode": code})

    def get_market_data_ex(self, field_list=None, stock_list=None, period="1d", start_time="", end_time="",
                           count=-1, dividend_type="none", fill_data=False) -> dict[str, pd.DataFrame]:
        frames = {}
        codes = stock_list or []
        for offset in range(0, len(codes), 32):
            data = self._request("get_market_data_ex", {
                "fields": field_list or [], "codes": codes[offset:offset + 32], "period": period,
                "start_time": start_time, "end_time": end_time, "count": count,
                "dividend_type": dividend_type, "fill_data": fill_data})
            for code, packet in (data or {}).items():
                frame = pd.DataFrame(packet["data"], index=packet["index"], columns=packet["columns"])
                frame.attrs["qmt_market_transport"] = "builtin"
                # No dedupe, filling, scaling, or timezone conversion in transport.
                if frame.index.duplicated().any():
                    raise QmtBuiltinError(f"{code}: duplicate native bar timestamps")
                frames[code] = frame
        return frames

    def get_market_data(self, field_list=None, stock_list=None, **kwargs) -> dict[str, pd.DataFrame]:
        frames = self.get_market_data_ex(field_list, stock_list, **kwargs)
        fields = field_list or sorted({field for frame in frames.values() for field in frame.columns})
        result = {field: pd.DataFrame({code: frame[field] for code, frame in frames.items()
                                       if field in frame.columns}).T for field in fields}
        for frame in result.values():
            frame.attrs["qmt_market_transport"] = "builtin"
        return result

    def get_stock_list_in_sector(self, sector_name: str, real_timetag: Any = -1) -> list[str]:
        return self._request("get_stock_list_in_sector", {"sector": sector_name, "real_timetag": real_timetag}) or []

    def get_sector_list(self) -> list[str]:
        return self._request("get_sector_list", {}) or []

    def download_sector_data(self) -> Any:
        return self._request("download_sector_data", {})

    def get_instrument_detail(self, stock_code: str, iscomplete: bool = False) -> dict[str, Any]:
        if iscomplete:
            raise QmtBuiltinCapabilityError("Complete/expired instrument metadata is not confirmed on native API")
        return self._request("get_instrument_detail", {"code": stock_code}) or {}

    def get_instrument_detail_list(self, stock_list: list[str], iscomplete: bool = False) -> dict[str, Any]:
        if iscomplete:
            raise QmtBuiltinCapabilityError("Complete/expired instrument metadata is not confirmed on native API")
        if "get_instrument_detail_list" in self.health().get("capabilities", []):
            details = {}
            for offset in range(0, len(stock_list), 32):
                batch = stock_list[offset:offset + 32]
                result = self._request("get_instrument_detail_list", {"codes": batch}) or {}
                if set(result) != set(batch):
                    raise QmtBuiltinError("Native instrument detail batch identity mismatch")
                details.update(result)
            return details
        return {code: self.get_instrument_detail(code, iscomplete) for code in stock_list}

    def download_history_contracts(self, incrementally: bool = True) -> Any:
        # Explicit failure avoids treating an unrefreshed delisted cache as current.
        raise QmtBuiltinCapabilityError("Expired-contract refresh has no confirmed Big QMT equivalent")

    def get_trading_dates(self, market: str, start_time: str = "", end_time: str = "", count: int = -1) -> list[Any]:
        benchmark = {"SH": "000001.SH", "SZ": "399001.SZ"}.get(market)
        if benchmark is None:
            raise QmtBuiltinCapabilityError(f"No confirmed native calendar benchmark for {market}")
        if count <= 0:
            start = datetime.strptime((start_time or "19900101")[:8], "%Y%m%d")
            end = datetime.strptime(end_time[:8], "%Y%m%d") if end_time else datetime.now()
            count = max(1, (end - start).days + 1)
        return self._request("get_trading_dates", {
            "code": benchmark, "start_time": start_time, "end_time": end_time, "count": count}) or []

    def get_market_last_trade_date(self, market="SH") -> Any:
        values = self.get_trading_dates(market, count=1)
        if not values:
            raise QmtBuiltinError("Native calendar returned no last trading date")
        return values[-1]

    def subscribe_whole_quote(self, codes: list[str], callback) -> int:
        stream_id = uuid.uuid4().hex
        response = self._request("subscribe_whole_quote", {"codes": codes, "stream_id": stream_id})
        native_id = int(response["native_id"])
        record = {"stream_id": stream_id, "stop": threading.Event(), "sequence": 0, "error": None}
        with self._stream_lock:
            self._streams[native_id] = record

        def receive():
            try:
                while not record["stop"].is_set():
                    body = self.health()
                    native = body.get("streams", {}).get(stream_id, {})
                    if native.get("error"):
                        raise QmtBuiltinError(str(native["error"]))
                    next_sequence = record["sequence"] + 1
                    path = self.root / "streams" / f"{stream_id}_{next_sequence:09d}.json"
                    if path.exists():
                        packet = _read_json(path)
                        if packet.get("session") != self.session or packet.get("sequence") != next_sequence:
                            raise QmtBuiltinError("Native quote stream identity/sequence mismatch")
                        for batch in packet["batches"]:
                            callback(batch)
                        record["sequence"] = next_sequence
                    _write_json(self.root / "streams" / f"{stream_id}.ack.json", {
                        "session": self.session, "sequence": record["sequence"], "updated_at_epoch": time.time()})
                    record["stop"].wait(0.5)
            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"

        record["thread"] = threading.Thread(target=receive, daemon=True, name="qmt-native-quote-reader")
        record["thread"].start()
        return native_id

    def check_stream_health(self) -> None:
        with self._stream_lock:
            errors = [r["error"] for r in self._streams.values() if r["error"]]
        if errors:
            raise QmtBuiltinError("Native quote delivery failed: " + "; ".join(errors))

    def unsubscribe_quote(self, native_id: int) -> Any:
        with self._stream_lock:
            record = self._streams.pop(native_id, None)
        if record is None:
            return None
        record["stop"].set()
        record["thread"].join(timeout=2)
        return self._request("unsubscribe_quote", {"stream_id": record["stream_id"]})

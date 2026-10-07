# -*- coding: utf-8 -*-
"""Big QMT Python 3.6 model: allowlisted market operations on its main thread.

Run this file as a Python model in Big QMT. No accounts or order APIs.
Knowledge: F:/AiDevelop/ai-workspace/projects/AiStock
"""
import datetime
import json
import math
import os
import re
import time
import uuid

BRIDGE_ROOT = os.environ.get(
    "AISTOCK_QMT_BUILTIN_ROOT", r"F:\AiStock\data\data\runtime\qmt_builtin_market")
PROTOCOL = 1
ALLOWED = frozenset([
    "get_full_tick", "get_market_data_ex", "download_history_data",
    "get_stock_list_in_sector", "get_instrument_detail", "get_trading_dates",
    "get_sector_list", "download_sector_data", "download_history_contracts",
    "download_history_data2", "get_instrument_detail_list",
    "subscribe_whole_quote", "unsubscribe_quote"])
_state = {"ready": False, "session": None, "streams": {}, "busy": False}


def _path(*parts):
    return os.path.join(BRIDGE_ROOT, *parts)


def _safe(value):
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        return _safe(value.item())
    return str(value)


def _write(path, body):
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(_safe(body), stream, ensure_ascii=False, allow_nan=False)
    for attempt in range(10):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.01)


def _read(path):
    for attempt in range(10):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                return json.load(stream)
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.01)


def _capabilities(C):
    mapping = {
        "get_full_tick": "get_full_tick", "get_market_data_ex": "get_market_data_ex",
        "get_stock_list_in_sector": "get_stock_list_in_sector",
        "get_instrument_detail": "get_instrument_detail", "get_trading_dates": "get_trading_dates",
        "subscribe_whole_quote": "subscribe_whole_quote", "unsubscribe_quote": "unsubscribe_quote",
        "get_sector_list": "get_sector_list"}
    available = [op for op, name in mapping.items() if callable(getattr(C, name, None))]
    for name in ["download_history_data", "download_sector_data", "download_history_contracts"]:
        if callable(globals().get(name)):
            available.append(name)
    if "download_history_data" in available:
        available.append("download_history_data2")
    if "get_instrument_detail" in available:
        available.append("get_instrument_detail_list")
    return sorted(available)


def _heartbeat(C, status="ready"):
    _write(_path("heartbeat.json"), {
        "protocol": PROTOCOL, "source": "qmt_builtin_contextinfo",
        "session": _state["session"], "updated_at_epoch": time.time(),
        "status": status, "capabilities": _capabilities(C),
        "streams": {key: {"sequence": value["sequence"], "error": value["error"]}
                    for key, value in _state["streams"].items()},
        "market_only": True})


def _codes(values):
    if not isinstance(values, list) or not values or len(values) > 8000:
        raise ValueError("expected 1..8000 security codes")
    if any(not isinstance(v, str) or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", v) for v in values):
        raise ValueError("invalid security code")
    return values


def _dispatch(C, operation, args):
    if operation not in ALLOWED or operation not in _capabilities(C):
        raise ValueError("unsupported market operation: " + str(operation))
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if operation == "get_full_tick":
        return C.get_full_tick(_codes(args["codes"]))
    if operation == "get_market_data_ex":
        codes = _codes(args["codes"])
        fields = args.get("fields") or ["open", "high", "low", "close", "volume", "amount"]
        if any(f not in ["time", "open", "high", "low", "close", "volume", "amount", "preClose", "suspendFlag"] for f in fields):
            raise ValueError("unsupported bar field")
        period = args.get("period", "1d")
        if period not in ["1d", "5m", "15m", "30m", "60m", "1h"]:
            raise ValueError("unsupported period")
        dividend = args.get("dividend_type", "none")
        if dividend not in ["none", "front", "back", "front_ratio", "back_ratio"]:
            raise ValueError("unsupported dividend type")
        if args.get("fill_data", False):
            raise ValueError("fill_data=True is forbidden for the canonical pipeline")
        result = C.get_market_data_ex(
            fields, codes, period="1h" if period == "60m" else period,
            start_time=args.get("start_time", ""), end_time=args.get("end_time", ""),
            count=int(args.get("count", -1)), dividend_type=dividend,
            fill_data=False, subscribe=False)
        return {code: {"index": [str(i) for i in frame.index],
                       "columns": list(frame.columns), "data": frame.values.tolist()}
                for code, frame in (result or {}).items()}
    if operation in ["download_history_data", "download_history_data2"]:
        codes = _codes(args["codes"] if operation == "download_history_data2" else [args["code"]])
        if len(codes) > 16:
            raise ValueError("native download batch must contain at most 16 codes")
        period = args["period"]
        if period not in ["1d", "5m", "15m", "30m", "60m", "1h"]:
            raise ValueError("unsupported download period")
        period = "5m" if period in ["15m", "30m", "60m", "1h"] else period
        completed, failures = [], []
        for code in codes:
            try:
                download_history_data(code, period, args.get("start_time", ""), args.get("end_time", ""))
                completed.append(code)
            except Exception as exc:
                failures.append({"code": code, "error_type": type(exc).__name__, "error": str(exc)})
            _heartbeat(C)
        if operation == "download_history_data" and failures:
            raise RuntimeError(failures[0]["error"])
        return {"request_completed": not failures, "base_period": period,
                "completed": completed, "failures": failures, "data_delivery_verified": False}
    if operation == "get_stock_list_in_sector":
        return C.get_stock_list_in_sector(args["sector"], args.get("real_timetag", -1))
    if operation == "get_instrument_detail":
        return C.get_instrument_detail(_codes([args["code"]])[0])
    if operation == "get_instrument_detail_list":
        codes = _codes(args["codes"])
        if len(codes) > 32:
            raise ValueError("native detail batch must contain at most 32 codes")
        return {code: C.get_instrument_detail(code) for code in codes}
    if operation == "get_trading_dates":
        # Big QMT requires a security code and a positive count, not a market and -1.
        return C.get_trading_dates(_codes([args["code"]])[0], args.get("start_time", ""),
                                   args.get("end_time", ""), max(1, int(args["count"])), "1d")
    if operation == "get_sector_list":
        return C.get_sector_list()
    if operation in ["download_sector_data", "download_history_contracts"]:
        return globals()[operation]()
    if operation == "subscribe_whole_quote":
        stream_id = args["stream_id"]
        if not re.fullmatch(r"[0-9a-f]{32}", stream_id):
            raise ValueError("invalid stream id")
        values = args["codes"]
        if values != ["SH", "SZ"] and values != ["SH"] and values != ["SZ"]:
            _codes(values)
        record = {"pending": [], "sequence": 0, "error": None, "last_seen": time.time()}
        def on_quote(data):
            if len(record["pending"]) >= 2000:
                record["error"] = "subscription buffer overflow; delivery incomplete"
                return
            record["pending"].append(_safe(data))
        sub_id = C.subscribe_whole_quote(values, callback=on_quote)
        if sub_id is None or int(sub_id) <= 0:
            raise RuntimeError("native subscription rejected")
        record["native_id"] = int(sub_id)
        _state["streams"][stream_id] = record
        return {"native_id": int(sub_id), "stream_id": stream_id}
    if operation == "unsubscribe_quote":
        stream_id = args["stream_id"]
        record = _state["streams"].pop(stream_id, None)
        if record is not None and not record.get("closed"):
            C.unsubscribe_quote(record["native_id"])
        return True
    raise ValueError("unsupported operation")


def _flush_streams(C):
    for stream_id, record in list(_state["streams"].items()):
        ack_path = _path("streams", stream_id + ".ack.json")
        acknowledged = 0
        if os.path.exists(ack_path):
            ack = _read(ack_path)
            if ack.get("session") == _state["session"]:
                acknowledged = min(record["sequence"], max(0, int(ack.get("sequence", 0))))
                record["last_seen"] = float(ack.get("updated_at_epoch", record["last_seen"]))
        # A dead external consumer must not leave a permanent native subscription.
        if time.time() - record["last_seen"] > 120:
            if not record.get("closed"):
                C.unsubscribe_quote(record["native_id"])
                record["closed"] = True
            record["error"] = "consumer lease expired"
            record["pending"] = []
            continue
        for sequence in range(record.get("cleaned", 0) + 1, acknowledged + 1):
            path = _path("streams", "{0}_{1:09d}.json".format(stream_id, sequence))
            if os.path.exists(path):
                os.remove(path)
        record["cleaned"] = acknowledged
        if record["sequence"] - acknowledged >= 120:
            record["error"] = "subscription backlog overflow; delivery incomplete"
        if record["error"] or not record["pending"]:
            continue
        pending, record["pending"] = record["pending"], []
        record["sequence"] += 1
        _write(_path("streams", "{0}_{1:09d}.json".format(stream_id, record["sequence"])), {
            "protocol": PROTOCOL, "session": _state["session"], "sequence": record["sequence"],
            "batches": pending, "collected_at_epoch": time.time()})


def aistock_market_pump(C):
    if not _state["ready"] or _state["busy"]:
        return
    _state["busy"] = True
    try:
        _flush_streams(C)
        _heartbeat(C)
        pending = []
        for name in os.listdir(_path("requests")):
            if not re.fullmatch(r"[0-9a-f]{32}\.json", name):
                continue
            try:
                pending.append((os.stat(_path("requests", name)).st_mtime_ns, name))
            except FileNotFoundError:
                # A caller can cancel an unclaimed request while we scan.
                continue
        pending.sort()
        # One request per timer invocation; serialized on QMT's model thread.
        if not pending:
            return
        name = pending[0][1]
        request_path = _path("requests", name)
        claimed = _path("processing", name)
        try:
            os.replace(request_path, claimed)
        except FileNotFoundError:
            # The external caller may have cancelled an unclaimed timed-out request.
            return
        response = {"protocol": PROTOCOL, "session": _state["session"], "id": name[:-5]}
        try:
            request = _read(claimed)
            if request.get("id") != name[:-5] or request.get("protocol") != PROTOCOL:
                raise ValueError("request identity or protocol mismatch")
            if request.get("session") != _state["session"]:
                raise ValueError("model restarted; stale request")
            if float(request.get("deadline_epoch", 0)) < time.time():
                raise ValueError("request expired before execution")
            response.update(ok=True, result=_dispatch(C, request["operation"], request.get("arguments", {})))
        except Exception as exc:
            response.update(ok=False, error_type=type(exc).__name__, error=str(exc))
        response["completed_at_epoch"] = time.time()
        _write(_path("responses", name), response)
        os.remove(claimed)
        _heartbeat(C)
    finally:
        _state["busy"] = False


def init(C):
    for folder in ["requests", "processing", "responses", "streams"]:
        os.makedirs(_path(folder), exist_ok=True)
    _state.update(ready=True, session=uuid.uuid4().hex, streams={}, busy=False)
    _heartbeat(C)
    C.run_time("aistock_market_pump", "1nSecond", "2020-01-01 00:00:00")
    print("AiStock market-only bridge initialized: " + BRIDGE_ROOT)


def after_init(C):
    aistock_market_pump(C)


def handlebar(C):
    if C.is_last_bar():
        aistock_market_pump(C)


def stop(C):
    _state["ready"] = False
    _heartbeat(C, "stopped")

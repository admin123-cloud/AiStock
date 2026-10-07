import ast
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from data_fetcher.sources.qmt_builtin import QmtBuiltinCapabilityError, QmtBuiltinError, QmtBuiltinMarketApi, _write_json
from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
from scripts import qmt_builtin_market_model as model


def test_native_index_units_survive_both_client_shapes_and_daily_writer(native):
    from utils.kline_units import normalize_qmt_daily_units
    from scripts.qmtmini_daily_backfill_validate import rows_from_qmt
    api = native[0]
    api.connect()
    code = "000001.SH"
    fields = ["open", "high", "low", "close", "volume", "amount"]
    frame = api.get_market_data_ex(fields, [code])[code]
    assert normalize_qmt_daily_units(frame, instrument_type="index").volume.iloc[0] == 123
    raw = api.get_market_data(fields, [code])
    rows, _ = rows_from_qmt(raw, [code], datetime(2026, 10, 7, 18),
                            "2026-09-30", "2026-09-30", index_codes={code})
    assert rows[0][6] == 123
    legacy = frame.copy()
    legacy.attrs.clear()
    assert normalize_qmt_daily_units(legacy, instrument_type="index").volume.iloc[0] == 1.23


@pytest.mark.parametrize("code,pvolume", [("600000.SH", 12300), ("899050.BJ", 123)])
def test_native_tick_aggregation_uses_volume_lots_independent_of_pvolume(code, pvolume):
    from types import SimpleNamespace
    from scripts.qmt_fullpush_intraday_aggregator import FullPushAggregator, minute_rows
    aggregator = FullPushAggregator(SimpleNamespace())
    aggregator.market_transport = "builtin"
    aggregator.on_data({code: {"time": "20260930093400", "lastPrice": 10,
                               "volume": 123, "pvolume": pvolume, "amount": 123000}})
    assert aggregator.daily_rows()[0][6] == 123
    assert minute_rows(list(aggregator.current_bars.values()), "5m")[0][6] == 123
    assert aggregator.volume_field == "volume"


def test_source_history_preserves_native_units_through_higher_period_reshape(native):
    from data_fetcher.sources.qmtmini import QmtMiniDataSource
    api = native[0]
    client = QmtMiniMarketClient()
    client._xtdata, client._connected = api, True
    source = QmtMiniDataSource()
    source.client, source._connected = client, True
    frame = source.get_stock_history("000001.SH", "2026-09-30", "2026-09-30", "5m")
    assert frame.attrs["qmt_market_transport"] == "builtin"
    assert frame.volume.iloc[0] == 123


class Context:
    def __init__(self):
        self.calls = []
        self.subscribed = {}

    def run_time(self, *args):
        self.timer = args

    def get_full_tick(self, codes):
        self.calls.append(("tick", threading.get_ident()))
        return {code: {"time": 1790751600000, "lastPrice": 10, "volume": 123} for code in codes}

    def get_market_data_ex(self, fields, codes, **kwargs):
        self.calls.append(("bars", threading.get_ident()))
        assert kwargs["fill_data"] is False and kwargs["subscribe"] is False
        if kwargs["period"] == "1d":
            index = ["20260930"]
        else:
            index = ["20260930093500", "20260930094000"]
        return {code: pd.DataFrame({f: [123.0] * len(index) if f == "volume" else [10.0] * len(index)
                                   for f in fields}, index=index) for code in codes}

    def get_stock_list_in_sector(self, sector, real_timetag=-1):
        return ["600000.SH", "000001.SZ"]

    def get_instrument_detail(self, code):
        return {"InstrumentName": "test", "OpenDate": "19990101"}

    def get_trading_dates(self, code, start, end, count, period):
        assert code == "000001.SH" and count > 0 and period == "1d"
        return ["20260929", "20260930"][-count:]

    def subscribe_whole_quote(self, codes, callback):
        identifier = len(self.subscribed) + 1
        self.subscribed[identifier] = callback
        callback({"600000.SH": {"lastPrice": 10, "time": 1790751600000}})
        callback({"600000.SH": {"lastPrice": 11, "time": 1790751601000}})
        return identifier

    def unsubscribe_quote(self, identifier):
        self.subscribed.pop(identifier, None)

    def is_last_bar(self):
        return True


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "BRIDGE_ROOT", str(tmp_path))
    context = Context()
    downloads = []
    monkeypatch.setattr(model, "download_history_data", lambda *args: downloads.append(args), raising=False)
    model.init(context)
    stop = threading.Event()
    failures = []
    def pump():
        while not stop.is_set():
            try:
                model.aistock_market_pump(context)
            except Exception as exc:
                failures.append(exc)
                return
            stop.wait(0.01)
    worker = threading.Thread(target=pump)
    worker.start()
    api = QmtBuiltinMarketApi(tmp_path, timeout=2)
    try:
        yield api, context, downloads, worker
    finally:
        for identifier in list(api._streams):
            api.unsubscribe_quote(identifier)
        stop.set()
        worker.join(timeout=3)
        assert not failures


def test_native_model_is_python36_and_does_not_import_sdk_or_trading():
    source = Path(model.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 6))
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom)) and "xtquant" in ast.unparse(node)
                   for node in ast.walk(tree))
    assert not {"passorder", "order_stock", "query_stock_asset", "get_trade_detail_data"} & set(model.ALLOWED)


def test_native_roundtrip_preserves_quantities_and_legacy_shapes(native, monkeypatch):
    api, context, downloads, worker = native
    monkeypatch.setattr("data_fetcher.sources.qmtmini_client._load_xtdata", lambda: api)
    client = QmtMiniMarketClient()
    connection = client.connect()
    assert connection["transport"] == "builtin" and "userdata_dir" not in connection
    client.download_history_data("600000.SH", "30m", "20260929", "20260930")
    assert downloads == [("600000.SH", "5m", "20260929", "20260930")]
    frame = client.get_market_data(["close", "volume"], ["600000.SH"], period="5m")
    assert list(frame["close"].columns) == ["20260930093500", "20260930094000"]
    assert frame["volume"].loc["600000.SH", "20260930093500"] == 123.0
    shaped = client.get_market_data_tdx_shape(["Close", "Volume"], ["600000.SH"], period="5m")
    assert shaped["Close"].loc["20260930093500", "600000.SH"] == 10.0
    assert client.get_full_tick(["600000.SH"])["600000.SH"]["volume"] == 123
    assert all(thread_id == worker.ident for _, thread_id in context.calls)


def test_native_calendar_adapts_market_and_unlimited_count(native):
    api, _, _, _ = native
    assert api.get_trading_dates("SH", "19900101", "20260930", -1) == ["20260929", "20260930"]
    assert api.get_market_last_trade_date("SH") == "20260930"
    with pytest.raises(QmtBuiltinCapabilityError):
        api.get_trading_dates("BJ")


def test_unsupported_refresh_never_claims_success(native):
    api, _, _, _ = native
    with pytest.raises(QmtBuiltinCapabilityError, match="get_sector_list"):
        api.get_sector_list()
    with pytest.raises(QmtBuiltinCapabilityError, match="Expired-contract"):
        api.download_history_contracts()
    with pytest.raises(QmtBuiltinCapabilityError):
        api.get_instrument_detail("600000.SH", True)


def test_native_subscription_keeps_event_order_and_cancels(native):
    api, context, _, _ = native
    received = []
    identifier = api.subscribe_whole_quote(["600000.SH"], received.append)
    deadline = time.monotonic() + 2
    while len(received) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [batch["600000.SH"]["lastPrice"] for batch in received] == [10, 11]
    api.unsubscribe_quote(identifier)
    assert not context.subscribed and not model._state["streams"]


def test_expired_and_trading_requests_never_execute(native):
    api, context, downloads, _ = native
    api.connect()
    request_id = "e" * 32
    _write_json(api.root / "requests" / f"{request_id}.json", {
        "protocol": 1, "id": request_id, "session": api.session,
        "operation": "download_history_data", "arguments": {}, "deadline_epoch": time.time() - 1})
    response = api.root / "responses" / f"{request_id}.json"
    deadline = time.monotonic() + 2
    while not response.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "expired" in json.loads(response.read_text())["error"] and not downloads
    with pytest.raises(ValueError, match="unsupported"):
        model._dispatch(context, "passorder", {})


def test_unavailable_and_stale_model_are_rejected(tmp_path):
    api = QmtBuiltinMarketApi(tmp_path, timeout=0.1)
    with pytest.raises(QmtBuiltinError, match="not running"):
        api.connect()
    _write_json(tmp_path / "heartbeat.json", {"protocol": 1, "source": "qmt_builtin_contextinfo",
        "status": "ready", "market_only": True, "session": "x", "updated_at_epoch": time.time() - 60})
    with pytest.raises(QmtBuiltinError, match="unavailable"):
        api.connect()


def test_active_selection_requires_verification_and_does_not_fallback(monkeypatch, tmp_path):
    from data_fetcher.sources import qmt_market_transport as selector
    monkeypatch.delenv("AISTOCK_QMT_MARKET_TRANSPORT", raising=False)
    monkeypatch.setattr(selector, "runtime_path", lambda *parts: tmp_path / parts[-1])
    assert selector.market_transport() == "xtquant"
    _write_json(tmp_path / "active_transport.json", {"active_transport": "builtin", "verification_passed": False})
    with pytest.raises(RuntimeError, match="Invalid"):
        selector.market_transport()
    monkeypatch.setenv("AISTOCK_QMT_MARKET_TRANSPORT", "builtin")
    assert selector.load_market_api().transport == "builtin"


def test_parallel_clients_still_execute_sdk_on_one_model_thread(native):
    from concurrent.futures import ThreadPoolExecutor
    api, context, _, worker = native
    def fetch(code):
        other = QmtBuiltinMarketApi(api.root, timeout=2)
        return other.get_full_tick([code])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, ["600000.SH", "000001.SZ", "600519.SH", "000001.SH"]))
    assert len(results) == 4
    assert all(thread_id == worker.ident for _, thread_id in context.calls)


def test_timeout_is_unconfirmed_and_cleans_only_own_pending_request(tmp_path):
    _write_json(tmp_path / "heartbeat.json", {"protocol": 1, "source": "qmt_builtin_contextinfo",
        "status": "ready", "market_only": True, "session": "x", "updated_at_epoch": time.time(),
        "capabilities": ["get_full_tick"]})
    api = QmtBuiltinMarketApi(tmp_path, timeout=0.05)
    with pytest.raises(QmtBuiltinError, match="unconfirmed"):
        api.get_full_tick(["600000.SH"])
    assert not list((tmp_path / "requests").glob("*.json"))


def test_duplicate_timestamps_are_rejected_instead_of_silently_deduplicated(native, monkeypatch):
    api, context, _, _ = native
    original = context.get_market_data_ex
    def duplicates(*args, **kwargs):
        result = original(*args, **kwargs)
        for frame in result.values():
            frame.index = [frame.index[0]] * len(frame)
        return result
    monkeypatch.setattr(context, "get_market_data_ex", duplicates)
    with pytest.raises(QmtBuiltinError, match="duplicate"):
        api.get_market_data(["close"], ["600000.SH"], period="5m")


def test_windows_atomic_write_retries_transient_file_lock(tmp_path, monkeypatch):
    original = model.os.replace
    attempts = []
    def locked_once(source, destination):
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError("reader briefly holds Windows file handle")
        return original(source, destination)
    monkeypatch.setattr(model.os, "replace", locked_once)
    model._write(str(tmp_path / "heartbeat.json"), {"ok": True})
    assert len(attempts) == 2
    assert json.loads((tmp_path / "heartbeat.json").read_text()) == {"ok": True}

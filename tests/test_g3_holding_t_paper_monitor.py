import json
from datetime import datetime

import pandas as pd
import pytest

from services.g3_holding_t_paper_monitor import (
    _notification_body,
    _notification_subject,
    _suggested_price_range,
    _request_intraday_minute_repair,
    _update_shadow_attribution,
    _warehouse_minute_metrics,
    run_once,
)


class FakeMarket:
    def get_full_tick(self, stock_codes):
        now_ms = int(datetime.now().timestamp() * 1000)
        return {code: {"lastPrice": 21.2, "time": now_ms, "bidVol": [1200], "askVol": [800]} for code in stock_codes}

    def get_market_data(self, **kwargs):
        code = kwargs["stock_list"][0]
        now_ms = int(datetime.now().timestamp() * 1000)
        return {
            "time": pd.DataFrame([[now_ms - 30_000 + i * 5_000 for i in range(7)]], index=[code]),
            "close": pd.DataFrame([[20.8, 20.9, 21.0, 21.1, 21.0, 21.1, 21.2]], index=[code]),
            "volume": pd.DataFrame([[100] * 7], index=[code]),
            "amount": pd.DataFrame([[2100] * 7], index=[code]),
        }


class UnprofitableBuybackMarket(FakeMarket):
    def get_full_tick(self, stock_codes):
        now_ms = int(datetime.now().timestamp() * 1000)
        return {code: {"lastPrice": 20.8, "time": now_ms, "bidVol": [800], "askVol": [1200]} for code in stock_codes}

    def get_market_data(self, **kwargs):
        code = kwargs["stock_list"][0]
        now_ms = int(datetime.now().timestamp() * 1000)
        return {
            "time": pd.DataFrame([[now_ms - 30_000 + i * 5_000 for i in range(7)]], index=[code]),
            "close": pd.DataFrame([[21.2, 21.1, 21.0, 20.9, 20.8, 20.7, 20.6]], index=[code]),
            "volume": pd.DataFrame([[100] * 7], index=[code]),
            "amount": pd.DataFrame([[2100] * 7], index=[code]),
        }


class StaleMinuteMarket(FakeMarket):
    def get_market_data(self, **kwargs):
        code = kwargs["stock_list"][0]
        stale_ms = int(datetime.now().timestamp() * 1000) - 9 * 60 * 1000
        return {
            "time": pd.DataFrame([[stale_ms] * 7], index=[code]),
            "close": pd.DataFrame([[20.8, 20.9, 21.0, 21.1, 21.0, 21.1, 21.2]], index=[code]),
            "volume": pd.DataFrame([[100] * 7], index=[code]),
            "amount": pd.DataFrame([[2100] * 7], index=[code]),
        }


class BrokenMinuteMarket(FakeMarket):
    def get_market_data(self, **kwargs):
        raise RuntimeError("5m endpoint unavailable")


class PanicInitialBuyMarket(FakeMarket):
    def get_full_tick(self, stock_codes):
        now_ms = int(datetime.now().timestamp() * 1000)
        return {code: {"lastPrice": 64.27, "time": now_ms, "bidVol": [9, 20, 14, 35, 2], "askVol": [477, 66, 53, 71, 68]} for code in stock_codes}

    def get_market_data(self, **kwargs):
        code = kwargs["stock_list"][0]
        now_ms = int(datetime.now().timestamp() * 1000)
        return {
            "time": pd.DataFrame([[now_ms - 30_000 + i * 5_000 for i in range(7)]], index=[code]),
            "close": pd.DataFrame([[65.2, 65.1, 65.0, 64.9, 64.8, 64.7]], index=[code]),
            "volume": pd.DataFrame([[100] * 6], index=[code]),
            "amount": pd.DataFrame([[6507.266970275037] * 6], index=[code]),
        }


class BrokenQmtMarket:
    def get_full_tick(self, stock_codes):
        raise RuntimeError("QMT bridge offline")

    def get_market_data(self, **kwargs):
        raise AssertionError("get_market_data should not run after tick failure")


def test_invalid_snapshot_is_recorded_as_block(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    state.write_text('{"broken":\n', encoding="utf-8")
    result = run_once(broker_state_path=state, ledger_path=ledger, checked_at=datetime(2026, 7, 14, 10, 30))
    assert result["reason"] == "broker_snapshot_invalid"
    assert result["order_path_enabled"] is False
    assert json.loads(ledger.read_text(encoding="utf-8").strip())["reason"] == "broker_snapshot_invalid"


def test_non_trading_day_exits_before_market_data_or_email(tmp_path, monkeypatch):
    import services.g3_holding_t_paper_monitor as monitor

    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    calls = []
    sent = []

    class NoMarketAccess:
        def get_full_tick(self, stock_codes):
            calls.append("tick")
            raise AssertionError("market data must not be requested on a non-trading day")

        def get_market_data(self, **kwargs):
            calls.append("minute")
            raise AssertionError("minute data must not be requested on a non-trading day")

    monkeypatch.setattr(monitor, "_is_trading_day", lambda now: False)
    result = run_once(
        broker_state_path=state,
        ledger_path=ledger,
        notification_state_path=notification_state,
        watchlist_path=tmp_path / "missing_watchlist.json",
        shadow_attribution_state_path=tmp_path / "shadow.json",
        market_client=NoMarketAccess(),
        email_sender=lambda subject, body: sent.append(subject) or {"sent": True},
        checked_at=datetime(2026, 8, 1, 10, 30),
        prefer_warehouse_minute=False,
    )

    assert result["reason"] == "non_trading_day"
    assert result["trading_day"] is False
    assert result["evaluated_count"] == 0
    assert result["plans"] == []
    assert result["notifications"] == []
    assert calls == []
    assert sent == []
    saved = json.loads(ledger.read_text(encoding="utf-8").strip())
    assert saved["reason"] == "non_trading_day"


def test_shadow_attribution_records_forward_tick_path(tmp_path):
    path = tmp_path / "shadow.json"
    _update_shadow_attribution(
        [{"code": "300054.SZ", "action": "watch_sell_t_signal", "market_price": 21.2, "rsi6": 60}],
        {"300054.SZ": {"last_price": 21.2, "tick_fresh": True}},
        now=datetime(2026, 7, 14, 10, 30), state_path=path,
    )
    rows = _update_shadow_attribution(
        [], {"300054.SZ": {"last_price": 20.8, "tick_fresh": True}},
        now=datetime(2026, 7, 14, 10, 45), state_path=path,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    cycle = saved["open_cycles"]["2026-07-14:300054.SZ"]
    assert rows[0]["last_relative_pct"] < 0
    assert cycle["checkpoints"]["m15"]["relative_pct"] < 0


def test_stale_minute_data_sends_one_exception_notice(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    sent = []
    kwargs = {
        "broker_state_path": state, "ledger_path": ledger, "notification_state_path": notification_state,
        "watchlist_path": tmp_path / "missing_watchlist.json", "shadow_attribution_state_path": tmp_path / "shadow.json",
        "market_client": StaleMinuteMarket(), "email_sender": lambda subject, body: sent.append(subject) or {"sent": True},
        "checked_at": datetime(2026, 7, 14, 10, 30), "prefer_warehouse_minute": False,
    }
    first = run_once(**kwargs)
    second = run_once(**kwargs)
    third = run_once(**kwargs)
    fourth = run_once(**kwargs)
    fifth = run_once(**kwargs)
    sixth = run_once(**kwargs)
    assert first["plans"][0]["action"] == "blocked"
    assert second["notifications"] == []
    assert third["notifications"] == []
    assert fourth["notifications"] == []
    assert fifth["notifications"] == [{"code": "300054.SZ", "action": "minute_data_exception", "sent": True}]
    assert sixth["notifications"] == []
    assert len(sent) == 1


def test_transient_stale_minute_data_does_not_send_exception_notice(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    sent = []
    kwargs = {
        "broker_state_path": state, "ledger_path": ledger, "notification_state_path": notification_state,
        "watchlist_path": tmp_path / "missing_watchlist.json", "shadow_attribution_state_path": tmp_path / "shadow.json",
        "market_client": StaleMinuteMarket(), "email_sender": lambda subject, body: sent.append(subject) or {"sent": True},
        "checked_at": datetime(2026, 7, 14, 10, 30), "prefer_warehouse_minute": False,
    }
    for _ in range(4):
        result = run_once(**kwargs)
        assert result["notifications"] == []
    fresh = run_once(**{**kwargs, "market_client": FakeMarket()})
    assert all(row.get("action") != "minute_data_exception" for row in fresh["notifications"])
    saved = json.loads(notification_state.read_text(encoding="utf-8"))
    assert "minute_stale_state" not in saved


def test_empty_filtered_warehouse_rows_are_skipped(monkeypatch):
    import utils.market_warehouse as market_warehouse

    frame = pd.DataFrame({
        "code": ["300054.SZ"],
        "datetime": ["2026-07-14 10:30:00"],
        "close": [20.0],
        "volume": [100],
        "amount": [210000.0],
    })
    monkeypatch.setattr(market_warehouse, "clickhouse_table_exists", lambda table: True)
    monkeypatch.setattr(market_warehouse, "clickhouse_query_df", lambda query: frame)
    assert _warehouse_minute_metrics(["300054.SZ"]) == {}


def test_warehouse_minute_rows_with_narrow_100x_unit_mismatch_are_normalized(monkeypatch):
    import utils.market_warehouse as market_warehouse

    now = datetime.now().replace(microsecond=0)
    frame = pd.DataFrame({
        "code": ["300054.SZ"],
        "datetime": [now],
        "close": [20.0],
        "volume": [100],
        "amount": [200000.0],
    })
    monkeypatch.setattr(market_warehouse, "clickhouse_table_exists", lambda table: True)
    monkeypatch.setattr(market_warehouse, "clickhouse_query_df", lambda query: frame)

    result = _warehouse_minute_metrics(["300054.SZ"])

    assert result["300054.SZ"]["vwap_5m"] == 20.0
    assert result["300054.SZ"]["minute_fresh"] is True


def test_qmt_market_data_unavailable_sends_one_exception_notice(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    sent = []
    kwargs = {
        "broker_state_path": state, "ledger_path": ledger, "notification_state_path": notification_state,
        "watchlist_path": tmp_path / "missing_watchlist.json", "shadow_attribution_state_path": tmp_path / "shadow.json",
        "market_client": BrokenQmtMarket(), "email_sender": lambda subject, body: sent.append((subject, body)) or {"sent": True},
        "checked_at": datetime(2026, 7, 14, 10, 30), "prefer_warehouse_minute": False,
    }
    first, second = run_once(**kwargs), run_once(**kwargs)
    assert first["reason"] == "qmt_market_data_unavailable"
    assert first["notifications"] == [{"code": "300054.SZ", "action": "qmt_market_data_exception", "sent": True}]
    assert second["notifications"] == []
    assert len(sent) == 1
    assert "QMT" in sent[0][0]
    assert "QMT bridge offline" in sent[0][1]


def test_stale_minute_data_recovery_sends_recovery_notice(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    sent = []
    kwargs = {
        "broker_state_path": state, "ledger_path": ledger, "notification_state_path": notification_state,
        "watchlist_path": tmp_path / "missing_watchlist.json", "shadow_attribution_state_path": tmp_path / "shadow.json",
        "market_client": StaleMinuteMarket(), "email_sender": lambda subject, body: sent.append((subject, body)) or {"sent": True},
        "checked_at": datetime(2026, 7, 14, 10, 30), "prefer_warehouse_minute": False,
    }
    for _ in range(5):
        run_once(**kwargs)
    fresh = run_once(**{**kwargs, "market_client": FakeMarket()})
    assert any(row.get("action") == "minute_data_recovered" and row.get("sent") for row in fresh["notifications"])
    saved = json.loads(notification_state.read_text(encoding="utf-8"))
    assert "minute_stale_state" not in saved
    assert sum(1 for subject, _ in sent if "分钟数据异常" in subject) == 1
    assert sum(1 for subject, _ in sent if "分钟数据恢复" in subject) == 1


def test_legacy_stale_notice_state_still_sends_recovery_notice(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    notification_state.write_text(json.dumps({
        "trade_date": "2026-07-14",
        "exception_keys": ["minute_data_stale:2026-07-14:300054.SZ"],
        "minute_stale_state": {
            "codes": ["300054.SZ"],
            "first_seen_at": "2026-07-14 10:20:00",
            "last_seen_at": "2026-07-14 10:29:00",
            "consecutive_count": 5,
        },
    }), encoding="utf-8")
    sent = []
    kwargs = {
        "broker_state_path": state, "ledger_path": ledger, "notification_state_path": notification_state,
        "watchlist_path": tmp_path / "missing_watchlist.json", "shadow_attribution_state_path": tmp_path / "shadow.json",
        "email_sender": lambda subject, body: sent.append((subject, body)) or {"sent": True},
        "checked_at": datetime(2026, 7, 14, 10, 30), "prefer_warehouse_minute": False,
    }
    stale = run_once(**{**kwargs, "market_client": StaleMinuteMarket()})
    assert stale["notifications"] == []
    fresh = run_once(**{**kwargs, "market_client": FakeMarket()})
    assert any(row.get("action") == "minute_data_recovered" and row.get("sent") for row in fresh["notifications"])
    assert sum(1 for subject, _ in sent if "minute" not in subject.lower() and "恢复" in subject) == 1


def test_legacy_exception_key_sends_recovery_after_old_state_was_cleared(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    notification_state.write_text(json.dumps({
        "trade_date": "2026-07-14",
        "exception_keys": ["minute_data_stale:2026-07-14:300054.SZ"],
    }), encoding="utf-8")
    sent = []
    result = run_once(
        broker_state_path=state, ledger_path=ledger, notification_state_path=notification_state,
        watchlist_path=tmp_path / "missing_watchlist.json", shadow_attribution_state_path=tmp_path / "shadow.json",
        market_client=FakeMarket(), email_sender=lambda subject, body: sent.append((subject, body)) or {"sent": True},
        checked_at=datetime(2026, 7, 14, 10, 30), prefer_warehouse_minute=False,
    )
    assert any(row.get("action") == "minute_data_recovered" and row.get("sent") for row in result["notifications"])
    saved = json.loads(notification_state.read_text(encoding="utf-8"))
    assert "minute_data_recovered:2026-07-14:300054.SZ" in saved["exception_keys"]


def test_qmt_market_data_recovery_sends_recovery_notice(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    sent = []
    kwargs = {
        "broker_state_path": state, "ledger_path": ledger, "notification_state_path": notification_state,
        "watchlist_path": tmp_path / "missing_watchlist.json", "shadow_attribution_state_path": tmp_path / "shadow.json",
        "market_client": BrokenQmtMarket(), "email_sender": lambda subject, body: sent.append((subject, body)) or {"sent": True},
        "checked_at": datetime(2026, 7, 14, 10, 30), "prefer_warehouse_minute": False,
    }
    run_once(**kwargs)
    fresh = run_once(**{**kwargs, "market_client": FakeMarket()})
    assert any(row.get("action") == "qmt_market_data_recovered" and row.get("sent") for row in fresh["notifications"])
    saved = json.loads(notification_state.read_text(encoding="utf-8"))
    assert "qmt_market_data_state" not in saved
    assert sum(1 for subject, _ in sent if "QMT行情异常" in subject) == 1
    assert sum(1 for subject, _ in sent if "QMT行情恢复" in subject) == 1


def test_stale_minute_data_does_not_send_a_lunch_break_alert(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    sent = []
    result = run_once(
        broker_state_path=state, ledger_path=ledger, notification_state_path=notification_state,
        watchlist_path=tmp_path / "missing_watchlist.json", shadow_attribution_state_path=tmp_path / "shadow.json",
        market_client=StaleMinuteMarket(), email_sender=lambda subject, body: sent.append(subject) or {"sent": True},
        checked_at=datetime(2026, 7, 14, 11, 38), prefer_warehouse_minute=False,
    )
    assert result["plans"][0]["action"] == "blocked"
    assert result["notifications"] == []
    assert sent == []


def test_blocked_minute_data_starts_targeted_same_day_repair(tmp_path):
    state_path = tmp_path / "minute_repair_state.json"
    report_path = tmp_path / "minute_repair_report.json"
    calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    result = _request_intraday_minute_repair(
        ["300054.SZ", "603881.SH"],
        {
            "300054.SZ": {"minute_fresh": False},
            "603881.SH": {"minute_fresh": True, "vwap_5m": 20.0, "rsi6": 50.0},
        },
        now=datetime(2026, 7, 31, 10, 30),
        state_path=state_path,
        report_path=report_path,
        popen=fake_popen,
    )

    assert result["status"] == "started"
    assert result["blocked_codes"] == ["300054.SZ"]
    assert result["notify_allowed"] is False
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert "--periods" in args and args[args.index("--periods") + 1] == "5m"
    assert "--codes" in args and args[args.index("--codes") + 1] == "300054.SZ"
    assert kwargs["creationflags"] == getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0)
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["attempt_count"] == 1
    assert saved["pid"] == 4321


def test_minute_repair_cooldown_avoids_duplicate_processes(tmp_path):
    state_path = tmp_path / "minute_repair_state.json"
    report_path = tmp_path / "minute_repair_report.json"
    calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(args, **kwargs):
        calls.append(args)
        return FakeProcess()

    first = _request_intraday_minute_repair(
        ["300054.SZ"],
        {"300054.SZ": {"minute_fresh": False}},
        now=datetime(2026, 7, 31, 10, 30),
        state_path=state_path,
        report_path=report_path,
        popen=fake_popen,
    )
    second = _request_intraday_minute_repair(
        ["300054.SZ"],
        {"300054.SZ": {"minute_fresh": False}},
        now=datetime(2026, 7, 31, 10, 31),
        state_path=state_path,
        report_path=report_path,
        popen=fake_popen,
    )

    assert first["status"] == "started"
    assert second["status"] == "cooldown"
    assert second["notify_allowed"] is False
    assert len(calls) == 1


def test_minute_repair_exhaustion_allows_exception_notification(tmp_path):
    state_path = tmp_path / "minute_repair_state.json"
    report_path = tmp_path / "minute_repair_report.json"
    calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(args, **kwargs):
        calls.append(args)
        return FakeProcess()

    for repair_time in (datetime(2026, 7, 31, 10, 30), datetime(2026, 7, 31, 10, 33), datetime(2026, 7, 31, 10, 36)):
        result = _request_intraday_minute_repair(
            ["300054.SZ"],
            {"300054.SZ": {"minute_fresh": False}},
            now=repair_time,
            state_path=state_path,
            report_path=report_path,
            popen=fake_popen,
        )
        assert result["status"] == "started"

    exhausted = _request_intraday_minute_repair(
        ["300054.SZ"],
        {"300054.SZ": {"minute_fresh": False}},
        now=datetime(2026, 7, 31, 10, 39),
        state_path=state_path,
        report_path=report_path,
        popen=fake_popen,
    )

    assert exhausted["status"] == "exhausted"
    assert exhausted["attempt_count"] == 3
    assert exhausted["notify_allowed"] is True
    assert len(calls) == 3


def test_run_once_suppresses_stale_exception_while_repair_is_running(tmp_path, monkeypatch):
    import services.g3_holding_t_paper_monitor as monitor

    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    monkeypatch.setattr(
        monitor,
        "_request_intraday_minute_repair",
        lambda codes, minute, *, now: {
            "status": "started",
            "blocked_codes": list(codes),
            "notify_allowed": False,
        },
    )
    monkeypatch.setattr(monitor, "_is_trading_day", lambda now: True)
    sent = []
    now = datetime.now().replace(hour=10, minute=30, second=0, microsecond=0)
    result = run_once(
        broker_state_path=state,
        ledger_path=ledger,
        notification_state_path=notification_state,
        watchlist_path=tmp_path / "missing_watchlist.json",
        shadow_attribution_state_path=tmp_path / "shadow.json",
        market_client=StaleMinuteMarket(),
        email_sender=lambda subject, body: sent.append(subject) or {"sent": True},
        checked_at=now,
        prefer_warehouse_minute=False,
    )

    assert result["minute_repair"]["status"] == "started"
    assert result["plans"][0]["action"] == "blocked"
    assert result["notifications"] == []
    assert sent == []


def test_minute_endpoint_failure_enters_repair_path(tmp_path, monkeypatch):
    import services.g3_holding_t_paper_monitor as monitor

    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    notification_state = tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    monkeypatch.setattr(
        monitor,
        "_request_intraday_minute_repair",
        lambda codes, minute, *, now: {
            "status": "started",
            "blocked_codes": list(codes),
            "notify_allowed": False,
        },
    )
    monkeypatch.setattr(monitor, "_is_trading_day", lambda now: True)
    now = datetime.now().replace(hour=10, minute=30, second=0, microsecond=0)
    result = run_once(
        broker_state_path=state,
        ledger_path=ledger,
        notification_state_path=notification_state,
        watchlist_path=tmp_path / "missing_watchlist.json",
        shadow_attribution_state_path=tmp_path / "shadow.json",
        market_client=BrokenMinuteMarket(),
        email_sender=lambda *_: {"sent": True},
        checked_at=now,
        prefer_warehouse_minute=False,
    )

    assert result["minute_repair"]["status"] == "started"
    assert result["minute_data_error"] == "RuntimeError: 5m endpoint unavailable"
    assert result["plans"][0]["action"] == "blocked"
    assert result["notifications"] == []


def test_sell_email_uses_sell_title_and_small_price_range():
    plan = {
        "action": "watch_sell_t_signal",
        "code": "300054.SZ",
        "market_price": 71.0,
        "checked_at": "2026-07-31 10:57:03",
        "price_vs_vwap_pct": 7.163,
        "rsi6": 71.81,
        "order_book_imbalance": 0.33,
        "reasons": ["测试卖出提醒"],
        "signal_quantity_required": True,
    }
    plan.update(_suggested_price_range(plan) or {})

    body = _notification_body(plan)

    assert body.startswith("卖出 T 仓提醒")
    assert "建议卖出价格小区间：" in body
    assert "71.00" in body
    assert plan["price_range_low"] < plan["price_range_high"]


def test_buyback_email_shows_range_below_break_even_ceiling():
    plan = {
        "action": "watch_buyback_t_signal",
        "code": "300054.SZ",
        "market_price": 70.80,
        "break_even_buyback_price": 70.922,
        "checked_at": "2026-07-31 11:05:03",
        "price_vs_vwap_pct": -0.4,
        "rsi6": 31.0,
        "order_book_imbalance": -0.2,
        "reasons": ["测试买回提醒"],
    }
    plan.update(_suggested_price_range(plan) or {})

    body = _notification_body(plan, conditional_buyback=True)

    assert body.startswith("买回 T 仓提醒")
    assert "建议买回价格小区间：" in body
    assert "硬上限 70.92 元" in body
    assert plan["price_range_high"] < plan["break_even_buyback_price"]


@pytest.mark.parametrize(
    ("action", "subject_label", "body_title", "direction"),
    [
        ("paper_sell_t_leg", "卖出T仓", "卖出 T 仓提醒", "卖出 T 仓"),
        ("watch_sell_t_signal", "卖出T仓", "卖出 T 仓提醒", "卖出 T 仓"),
        ("paper_buyback_t_leg", "买回T仓", "买回 T 仓提醒", "买回 T 仓"),
        ("watch_buyback_t_signal", "买回T仓", "买回 T 仓提醒", "买回 T 仓"),
        ("paper_initial_buy", "买入建仓", "买入建仓提醒", "买入建仓"),
        ("watch_initial_buy_signal", "买入建仓", "买入建仓提醒", "买入建仓"),
        ("paper_add_to_target", "买入补仓", "买入补仓提醒", "买入补仓"),
        ("watch_add_to_target_signal", "买入补仓", "买入补仓提醒", "买入补仓"),
    ],
)
def test_all_notification_actions_keep_subject_and_body_direction_in_sync(
    action, subject_label, body_title, direction
):
    plan = {
        "action": action,
        "code": "603881.SH",
        "market_price": 20.0,
        "break_even_buyback_price": 19.95,
        "checked_at": "2026-07-31 10:57:03",
        "price_vs_vwap_pct": 1.2,
        "rsi6": 61.0,
        "order_book_imbalance": 0.2,
        "reasons": ["测试动作语义一致性"],
    }
    plan.update(_suggested_price_range(plan) or {})

    subject = _notification_subject(plan)
    body = _notification_body(plan)

    assert subject == f"[AiStock 做T] 603881.SH {subject_label}提醒"
    header = "\n".join(body.splitlines()[:2])
    assert body.splitlines()[:2] == [body_title, f"操作方向：{direction}"]
    for opposite_title in {"卖出 T 仓提醒", "买回 T 仓提醒", "买入建仓提醒", "买入补仓提醒"} - {body_title}:
        assert opposite_title not in header


def test_notification_uses_stock_name_and_six_digit_code_when_available():
    plan = {
        "action": "watch_initial_buy_signal",
        "code": "300054.SZ",
        "stock_name": "鼎龙股份",
        "market_price": 20.0,
        "checked_at": "2026-08-11 10:00:00",
        "reasons": ["测试名称展示"],
    }

    subject = _notification_subject(plan)
    body = _notification_body(plan)

    assert subject == "[AiStock 做T] 鼎龙股份（300054） 买入建仓提醒"
    assert "标的：鼎龙股份（300054）" in body


def test_unknown_notification_action_is_rejected_instead_of_defaulting_to_buyback():
    plan = {"action": "future_action", "code": "603881.SH", "market_price": 20.0}

    with pytest.raises(ValueError, match="Unsupported T notification action"):
        _notification_body(plan)
    with pytest.raises(ValueError, match="Unsupported T notification action"):
        _suggested_price_range(plan)


def test_run_once_blocks_unknown_notification_action_without_sending_email(tmp_path, monkeypatch):
    import services.g3_holding_t_paper_monitor as monitor

    state = tmp_path / "state.json"
    state.write_text(json.dumps({"holdings": [{"code": "603881", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    sent = []

    monkeypatch.setattr(
        monitor,
        "evaluate_portfolio_t",
        lambda *args, **kwargs: {
            "mode": "paper_only_holding_t",
            "order_path_enabled": False,
            "checked_at": "2026-07-14 10:30:00",
            "evaluated_count": 1,
            "plans": [{"code": "603881.SH", "action": "future_action", "market_price": 20.0}],
        },
    )
    result = monitor.run_once(
        broker_state_path=state,
        ledger_path=tmp_path / "plans.jsonl",
        notification_state_path=tmp_path / "notification.json",
        shadow_attribution_state_path=tmp_path / "shadow.json",
        watchlist_path=tmp_path / "missing_watchlist.json",
        market_client=FakeMarket(),
        email_sender=lambda subject, body: sent.append((subject, body)) or {"sent": True},
        checked_at=datetime(2026, 7, 14, 10, 30),
        prefer_warehouse_minute=False,
    )

    assert sent == []
    assert result["notifications"] == [{
        "code": "603881.SH",
        "action": "future_action",
        "sent": False,
        "reason": "unsupported_notification_action",
    }]


def test_valid_snapshot_evaluates_every_holding_without_order_path(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    state.write_text(json.dumps({"holdings": [{"code": "002245", "shares": 2200, "available_shares": 2200}]}), encoding="utf-8")
    result = run_once(
        broker_state_path=state, ledger_path=ledger, watchlist_path=tmp_path / "absent_watchlist.json",
        notification_state_path=tmp_path / "notification.json",
        shadow_attribution_state_path=tmp_path / "shadow.json",
        market_client=FakeMarket(), email_sender=lambda *_: {"sent": False}, prefer_warehouse_minute=False,
        checked_at=datetime(2026, 7, 14, 10, 30),
    )
    assert result["ok"] is True
    assert result["evaluated_count"] == 1
    assert result["order_path_enabled"] is False
    snapshots = list((tmp_path / "tick_snapshots").glob("*.jsonl"))
    assert len(snapshots) == 1
    saved = json.loads(snapshots[0].read_text(encoding="utf-8").strip())
    assert saved["tick"]["bid_volume"] == [1200]
    assert saved["decision"]["code"] == "002245.SZ"


def test_sell_t_notice_is_deduplicated_and_enables_conditional_buyback(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    watchlist, notification_state = tmp_path / "watchlist.json", tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": [{"code": "300054", "shares": 1000, "available_shares": 1000}]}), encoding="utf-8")
    watchlist.write_text(json.dumps({"codes": ["300054.SZ"]}), encoding="utf-8")
    sent = []

    def sender(subject, body):
        sent.append((subject, body))
        return {"sent": True}

    first = run_once(
        broker_state_path=state, ledger_path=ledger, watchlist_path=watchlist,
        shadow_attribution_state_path=tmp_path / "shadow.json",
        notification_state_path=notification_state, market_client=FakeMarket(), email_sender=sender, prefer_warehouse_minute=False,
        checked_at=datetime(2026, 7, 14, 10, 30),
    )
    second = run_once(
        broker_state_path=state, ledger_path=ledger, watchlist_path=watchlist,
        shadow_attribution_state_path=tmp_path / "shadow.json",
        notification_state_path=notification_state, market_client=FakeMarket(), email_sender=sender, prefer_warehouse_minute=False,
        checked_at=datetime(2026, 7, 14, 10, 35),
    )

    assert first["plans"][0]["action"] == "paper_sell_t_leg"
    assert first["notifications"] == [{"code": "300054.SZ", "action": "paper_sell_t_leg", "sent": True}]
    assert second["plans"][0]["action"] == "observe"
    assert second["notifications"] == []
    assert len(sent) == 1
    assert sent[0][0] == "[AiStock 做T] 鼎龙股份（300054） 卖出T仓提醒"
    assert sent[0][1].splitlines()[:2] == ["卖出 T 仓提醒", "操作方向：卖出 T 仓"]
    assert "不会提交任何委托" in sent[0][1]


def test_buyback_is_suppressed_when_it_cannot_beat_holding(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    watchlist, notification_state = tmp_path / "watchlist.json", tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": []}), encoding="utf-8")
    watchlist.write_text(json.dumps({"codes": ["300037.SZ"]}), encoding="utf-8")
    notification_state.write_text(json.dumps({
        "trade_date": "2026-07-14",
        "action_notice_count": 1,
        "cycles": {"300037.SZ": {"sell_notice_shares": 100, "sell_signal_price": 20.0}},
    }), encoding="utf-8")
    result = run_once(
        broker_state_path=state, ledger_path=ledger, watchlist_path=watchlist,
        shadow_attribution_state_path=tmp_path / "shadow.json",
        notification_state_path=notification_state, market_client=UnprofitableBuybackMarket(),
        email_sender=lambda *_: {"sent": True}, checked_at=datetime(2026, 7, 14, 10, 30), prefer_warehouse_minute=False,
    )
    assert result["plans"][0]["action"] == "observe"
    assert result["notifications"] == []


def test_panic_add_to_target_can_trigger_when_rsi_is_temporarily_missing(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    watchlist, notification_state = tmp_path / "watchlist.json", tmp_path / "notification_state.json"
    portfolio_state = tmp_path / "portfolio_state.json"
    state.write_text(json.dumps({"holdings": []}), encoding="utf-8")
    watchlist.write_text(json.dumps({"codes": ["300054.SZ"]}), encoding="utf-8")
    portfolio_state.write_text(json.dumps({
        "schema_version": 1,
        "single_t_leg_weight_pct": 25,
        "positions": {"300054.SZ": {"current_weight_pct": 25, "target_weight_pct": 50}},
    }), encoding="utf-8")
    sent = []
    result = run_once(
        broker_state_path=state, ledger_path=ledger, watchlist_path=watchlist,
        shadow_attribution_state_path=tmp_path / "shadow.json",
        notification_state_path=notification_state, portfolio_state_path=portfolio_state,
        market_client=PanicInitialBuyMarket(), email_sender=lambda subject, body: sent.append((subject, body)) or {"sent": True},
        checked_at=datetime(2026, 7, 14, 11, 29), prefer_warehouse_minute=False,
    )
    plan = result["plans"][0]
    assert plan["action"] == "watch_add_to_target_signal"
    assert plan["rsi6"] is None
    assert plan["price_vs_vwap_pct"] <= -0.8
    assert plan["order_book_imbalance"] <= -0.35
    assert plan["current_weight_pct"] == 25
    assert plan["target_weight_pct"] == 50
    assert plan["reasons"][0].startswith("急跌补仓：当前总资金25% / 目标50%")
    assert result["notifications"] == [{"code": "300054.SZ", "action": "watch_add_to_target_signal", "sent": True}]
    assert len(sent) == 1
    assert sent[0][0] == "[AiStock 做T] 鼎龙股份（300054） 买入补仓提醒"
    assert sent[0][1].splitlines()[:2] == ["买入补仓提醒", "操作方向：买入补仓"]


def test_zero_weight_uses_initial_buy_instead_of_add_to_target(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    watchlist, notification_state = tmp_path / "watchlist.json", tmp_path / "notification_state.json"
    portfolio_state = tmp_path / "portfolio_state.json"
    state.write_text(json.dumps({"holdings": []}), encoding="utf-8")
    watchlist.write_text(json.dumps({"codes": ["300054.SZ"]}), encoding="utf-8")
    portfolio_state.write_text(json.dumps({
        "schema_version": 1,
        "single_t_leg_weight_pct": 25,
        "positions": {"300054.SZ": {"current_weight_pct": 0, "target_weight_pct": 50}},
    }), encoding="utf-8")
    sent = []
    result = run_once(
        broker_state_path=state, ledger_path=ledger, watchlist_path=watchlist,
        shadow_attribution_state_path=tmp_path / "shadow.json",
        notification_state_path=notification_state, portfolio_state_path=portfolio_state,
        market_client=PanicInitialBuyMarket(), email_sender=lambda subject, body: sent.append((subject, body)) or {"sent": True},
        checked_at=datetime(2026, 7, 14, 11, 29), prefer_warehouse_minute=False,
    )

    plan = result["plans"][0]
    assert plan["action"] == "watch_initial_buy_signal"
    assert plan["reasons"][0].startswith("急跌建仓")
    assert result["notifications"] == [{"code": "300054.SZ", "action": "watch_initial_buy_signal", "sent": True}]
    assert sent[0][0] == "[AiStock 做T] 鼎龙股份（300054） 买入建仓提醒"


def test_watchlist_can_start_without_a_qmt_account_position(tmp_path):
    state, ledger = tmp_path / "state.json", tmp_path / "plans.jsonl"
    watchlist, notification_state = tmp_path / "watchlist.json", tmp_path / "notification_state.json"
    state.write_text(json.dumps({"holdings": []}), encoding="utf-8")
    watchlist.write_text(json.dumps({"codes": ["300037.SZ"]}), encoding="utf-8")
    sent = []
    result = run_once(
        broker_state_path=state, ledger_path=ledger, watchlist_path=watchlist,
        shadow_attribution_state_path=tmp_path / "shadow.json",
        notification_state_path=notification_state, market_client=FakeMarket(),
        email_sender=lambda subject, body: sent.append((subject, body)) or {"sent": True}, prefer_warehouse_minute=False,
        checked_at=datetime(2026, 7, 14, 10, 30),
    )
    plan = result["plans"][0]
    assert plan["action"] == "watch_sell_t_signal"
    assert plan["position_source"] == "watchlist_no_qmt_account"
    assert plan["proposed_shares"] is None
    assert len(sent) == 1
    assert "实际持仓和可卖老仓" in sent[0][1]

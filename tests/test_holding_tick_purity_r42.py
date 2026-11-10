# -*- coding: utf-8 -*-
"""R42：GET /holding-tick-analysis 净化为纯读的行为测试。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261110_purify_holding_tick_analysis_r42.md

契约：
  - GET 调用链（_read_holding_tick_analysis）零 SMTP 外呼、零文件写：
    成功路径不调用 _mark_holding_tick_qmt_available；
    失败路径 alert 字段同 schema（sent/reason[/checked_at|last_sent_at]），
    但绝不发信、绝不写 holding_tick_alert_state / shadow_monitor_events。
  - 外呼能力移入通知调度通道：_notify_holding_tick_qmt_unavailable /
    _mark_holding_tick_qmt_available 语义原样保留，由
    _run_holding_tick_qmt_alert_once（pipeline_scheduler 任务调用）执行；
    dry_run=True 只读评估零副作用。
  - _load_monitor_event_rows_readonly：损坏文件内存恢复，绝不写 .corrupt-* 备份；
    _load_monitor_event_rows_with_repair（写通道）仍写备份。
  - 响应 schema 与净化前完全一致（前端零改动）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 隔离数据根：必须在导入 api.gen3_state_alpha 之前设置（与
# test_operations_preview_safety._ISOLATION_ROOT_ENV 同一语义）。
_ISOLATION_ENV = {
    "AISTOCK_DATA_ROOT": "isolated-data-root",
    "AISTOCK_REPORTS_ROOT": "isolated-reports-root",
    "AISTOCK_ARTIFACTS_ROOT": "isolated-artifacts-root",
    "AISTOCK_LOGS_ROOT": "isolated-logs-root",
}
_SAVED_ENV = {name: os.environ.get(name) for name in _ISOLATION_ENV}
for name, subdir in _ISOLATION_ENV.items():
    os.environ[name] = str(Path(__file__).resolve().parent / subdir)

try:
    import api.gen3_state_alpha as g3
finally:
    for name, prev in _SAVED_ENV.items():
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev

_CODES = ["300054.SZ", "300037.SZ", "603881.SH"]


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def isolated_paths(tmp_path, monkeypatch):
    """把 gen3 模块的运行时工件路径全部指到 tmp，并恢复原值。"""
    paths = {
        "MONITOR_STATE_PATH": tmp_path / "shadow_monitor_state.json",
        "MONITOR_EVENTS_PATH": tmp_path / "shadow_monitor_events.json",
        "HOLDING_TICK_ALERT_STATE_PATH": tmp_path / "holding_tick_alert_state.json",
        "HOLDING_TICK_WATCHLIST_PATH": tmp_path / "holding_tick_watchlist.json",
        "BROKER_STATE_PATH": tmp_path / "broker_state.json",
    }
    originals = {}
    for attr, path in paths.items():
        originals[attr] = getattr(g3, attr)
        monkeypatch.setattr(g3, attr, path)
    _write(paths["MONITOR_STATE_PATH"], {"enabled": True, "email_enabled": True, "trading_hours_only": True})
    yield paths
    for attr, value in originals.items():
        monkeypatch.setattr(g3, attr, value)


@pytest.fixture()
def in_trading_window(monkeypatch):
    monkeypatch.setattr(g3, "_in_trading_window", lambda now: True)


@pytest.fixture()
def holdings_snapshot(monkeypatch):
    monkeypatch.setattr(
        g3,
        "_broker_snapshot",
        lambda: {
            "updated_at": "2026-11-10 12:00:00",
            "holdings": [{"code": code, "shares": 100, "cost_price": 10.0} for code in _CODES],
        },
    )


@pytest.fixture()
def smtp_boobytrap(monkeypatch):
    """任何 SMTP 触碰都直接让测试失败（GET 链零外呼的结构化证明）。"""
    def _bomb(*args, **kwargs):
        raise AssertionError("R42 违约：holding-tick GET 链试图建立 SMTP 连接")
    monkeypatch.setattr(g3.smtplib, "SMTP", _bomb, raising=True)
    monkeypatch.setattr(g3.smtplib, "SMTP_SSL", _bomb, raising=True)
    yield


def _freeze_collaborators(monkeypatch, ticks):
    """冻结 GET 链的只读协作者，聚焦纯净性与 schema。"""
    monkeypatch.setattr(g3, "_read_qmt_ticks", lambda codes, source: ticks)
    monkeypatch.setattr(g3, "_load_stock_names", lambda codes: {})
    monkeypatch.setattr(g3, "_load_holding_tick_sector_members", lambda codes: {})
    monkeypatch.setattr(g3, "_load_holding_tick_minute_metrics", lambda codes: ({}, {c: {"rsi_5m_14": None, "rsi_15m_14": None} for c in codes}))
    monkeypatch.setattr(g3, "_load_daily_trend_bars", lambda codes, days: {})


def _snapshot_files(paths):
    return {name: (path.read_bytes() if path.exists() else None) for name, path in paths.items()}


class TestGetChainPurity:
    def test_success_path_schema_unchanged_and_zero_writes(self, isolated_paths, in_trading_window, holdings_snapshot, smtp_boobytrap, monkeypatch):
        raw = {
            "lastPrice": 61.77, "open": 60.0, "high": 62.0, "low": 59.9, "lastClose": 60.5,
            "bidPrice": [61.76], "askPrice": [61.78], "bidVol": [100, 0, 0, 0, 0], "askVol": [120, 0, 0, 0, 0],
            "time": datetime.now().strftime("%Y%m%d%H%M%S"), "speed1Min": 0.1, "speed5Min": 0.2,
        }
        _freeze_collaborators(monkeypatch, {code: dict(raw) for code in _CODES})
        mark_calls = []
        monkeypatch.setattr(g3, "_mark_holding_tick_qmt_available",
                            lambda *a, **k: mark_calls.append(a) or (_ for _ in ()).throw(AssertionError("R42 违约：GET 链调用恢复通知")))
        before = _snapshot_files(isolated_paths)
        payload = g3._read_holding_tick_analysis(",".join(_CODES))
        assert not mark_calls
        assert _snapshot_files(isolated_paths) == before, "GET 成功路径零文件写"
        assert payload["ok"] is True
        assert payload["mode"] == "read_only_holding_tick_analysis"
        assert payload["order_path_enabled"] is False
        assert payload["requested_codes"] == _CODES
        assert payload["broker_updated_at"] == "2026-11-10 12:00:00"
        assert isinstance(payload["warnings"], list) and payload["warnings"] == []
        assert set(payload["source"]) >= {"provider", "host", "port"}
        assert len(payload["items"]) == 3
        item = payload["items"][0]
        assert set(item) == {
            "code", "name", "available", "tick_time", "tick_age_seconds", "tick_fresh",
            "last_price", "open", "high", "low", "previous_close", "change_pct", "vwap_5m",
            "last_5m_at", "rsi_5m_14", "rsi_15m_14", "bid1", "ask1", "bid5_volume", "ask5_volume",
            "order_book_imbalance", "speed_1m", "speed_5m", "daily_trend", "sector_name",
            "sector_code", "peer_sample_size", "sector_follow_state", "sector_follow_label",
            "peer_changes", "shares", "cost_price", "diagnosis", "level", "anomalies",
        }
        assert item["code"] == _CODES[0] and item["available"] is True
        assert item["diagnosis"] and item["level"] in {"info", "success", "warning", "danger"}

    def test_failure_path_alert_schema_and_zero_side_effects(self, isolated_paths, in_trading_window, holdings_snapshot, smtp_boobytrap, monkeypatch):
        def _fail(codes, source):
            raise RuntimeError("bridge down")
        monkeypatch.setattr(g3, "_read_qmt_ticks", _fail)
        monkeypatch.setattr(g3, "_load_stock_names", lambda codes: {})
        monkeypatch.setattr(g3, "_load_holding_tick_sector_members", lambda codes: {})
        before = _snapshot_files(isolated_paths)
        payload = g3._read_holding_tick_analysis(",".join(_CODES))
        assert _snapshot_files(isolated_paths) == before, "GET 失败路径零文件写（alert 状态/事件）"
        assert payload["ok"] is False
        assert payload["warnings"] and "QMT Tick 不可用" in payload["warnings"][0]
        alert = payload["alert"]
        assert alert["sent"] is False
        assert alert["reason"] == "deferred_to_notification_channel"
        assert alert["checked_at"]

    def test_failure_path_cooldown_is_readonly(self, isolated_paths, in_trading_window, holdings_snapshot, smtp_boobytrap, monkeypatch):
        monkeypatch.setattr(g3, "_read_qmt_ticks", lambda codes, source: (_ for _ in ()).throw(RuntimeError("bridge down")))
        now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
        _write(isolated_paths["HOLDING_TICK_ALERT_STATE_PATH"],
               {"status": "unavailable", "last_unavailable_alert_at": now_text, "last_codes": _CODES})
        payload = g3._read_holding_tick_analysis(",".join(_CODES))
        assert payload["alert"] == {"sent": False, "reason": "cooldown", "last_sent_at": now_text}

    def test_failure_path_not_trading_window(self, isolated_paths, monkeypatch, holdings_snapshot):
        monkeypatch.setattr(g3, "_in_trading_window", lambda now: False)
        monkeypatch.setattr(g3, "_read_qmt_ticks", lambda codes, source: (_ for _ in ()).throw(RuntimeError("bridge down")))
        payload = g3._read_holding_tick_analysis(",".join(_CODES))
        assert payload["alert"]["reason"] == "not_trading_window"
        assert payload["alert"]["sent"] is False
        assert payload["alert"]["checked_at"]


class TestNotificationChannel:
    def test_notify_sends_and_writes_state_and_event(self, isolated_paths, in_trading_window, monkeypatch):
        sent = []
        monkeypatch.setattr(g3, "_send_shadow_email", lambda subject, body, rec=None: sent.append(subject) or {"sent": True, "to": ["x"], "subject": subject, "mode": "ssl", "port": 465})
        result = g3._notify_holding_tick_qmt_unavailable(_CODES, {"provider": "test"}, RuntimeError("boom"))
        assert result["sent"] is True and sent
        state = json.loads(isolated_paths["HOLDING_TICK_ALERT_STATE_PATH"].read_text(encoding="utf-8"))
        assert state["status"] == "unavailable" and state["last_unavailable_alert_at"]
        events = json.loads(isolated_paths["MONITOR_EVENTS_PATH"].read_text(encoding="utf-8"))["events"]
        assert events and events[-1]["type"] == "qmt_tick_unavailable"

    def test_notify_cooldown_suppresses_second_send(self, isolated_paths, in_trading_window, monkeypatch):
        sent = []
        monkeypatch.setattr(g3, "_send_shadow_email", lambda subject, body, rec=None: sent.append(subject) or {"sent": True, "to": ["x"], "subject": subject})
        g3._notify_holding_tick_qmt_unavailable(_CODES, {"provider": "test"}, RuntimeError("boom"))
        n = len(sent)
        result = g3._notify_holding_tick_qmt_unavailable(_CODES, {"provider": "test"}, RuntimeError("boom"))
        assert result == {"sent": False, "reason": "cooldown", "last_sent_at": json.loads(isolated_paths["HOLDING_TICK_ALERT_STATE_PATH"].read_text(encoding="utf-8"))["last_unavailable_alert_at"]}
        assert len(sent) == n

    def test_mark_available_closes_alert_loop(self, isolated_paths, in_trading_window, monkeypatch):
        now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
        _write(isolated_paths["HOLDING_TICK_ALERT_STATE_PATH"],
               {"status": "unavailable", "last_unavailable_alert_at": now_text, "last_codes": _CODES})
        sent = []
        monkeypatch.setattr(g3, "_send_shadow_email", lambda subject, body, rec=None: sent.append(subject) or {"sent": True, "to": ["x"], "subject": subject})
        g3._mark_holding_tick_qmt_available(_CODES, {"provider": "test"})
        assert sent, "恢复邮件在通知通道仍发送"
        state = json.loads(isolated_paths["HOLDING_TICK_ALERT_STATE_PATH"].read_text(encoding="utf-8"))
        assert state["status"] == "available" and state["last_recovered_at"]

    def test_run_once_dry_run_zero_side_effects_on_failure(self, isolated_paths, in_trading_window, holdings_snapshot, smtp_boobytrap, monkeypatch):
        monkeypatch.setattr(g3, "_read_qmt_ticks", lambda codes, source: (_ for _ in ()).throw(RuntimeError("bridge down")))
        before = _snapshot_files(isolated_paths)
        result = g3._run_holding_tick_qmt_alert_once(source="manual", dry_run=True)
        assert _snapshot_files(isolated_paths) == before
        assert result["ok"] is False and result["status"] == "unavailable" and result["dry_run"] is True
        assert result["gate"]["reason"] in {"would_send", "cooldown", "email_disabled", "not_trading_window"}

    def test_run_once_real_path_notifies_then_recovers(self, isolated_paths, in_trading_window, holdings_snapshot, monkeypatch):
        calls = {"fail": True}
        def _maybe_fail(codes, source):
            if calls["fail"]:
                raise RuntimeError("bridge down")
            return {code: {"lastPrice": 10.0} for code in codes}
        monkeypatch.setattr(g3, "_read_qmt_ticks", _maybe_fail)
        sent = []
        monkeypatch.setattr(g3, "_send_shadow_email", lambda subject, body, rec=None: sent.append(subject) or {"sent": True, "to": ["x"], "subject": subject})
        first = g3._run_holding_tick_qmt_alert_once(source="scheduler")
        assert first["ok"] is False and first["alert"]["sent"] is True and sent
        calls["fail"] = False
        second = g3._run_holding_tick_qmt_alert_once(source="scheduler")
        assert second["ok"] is True and second["status"] == "available"
        state = json.loads(isolated_paths["HOLDING_TICK_ALERT_STATE_PATH"].read_text(encoding="utf-8"))
        assert state["status"] == "available"
        assert len(sent) == 2, "告警邮件+恢复邮件各一封"


class TestMonitorEventRowsReadonly:
    def _corrupt(self, path):
        doc = json.dumps({"events": [{"type": "a"}, {"type": "b"}]}, ensure_ascii=False)
        _write(path, doc + doc)

    def test_readonly_recovers_without_backup(self, isolated_paths):
        self._corrupt(isolated_paths["MONITOR_EVENTS_PATH"])
        raw_before = isolated_paths["MONITOR_EVENTS_PATH"].read_bytes()
        rows = g3._load_monitor_event_rows_readonly()
        assert [row["type"] for row in rows] == ["a", "b", "a", "b"]
        assert isolated_paths["MONITOR_EVENTS_PATH"].read_bytes() == raw_before
        assert not list(isolated_paths["MONITOR_EVENTS_PATH"].parent.glob("*.corrupt-*")), "只读路径绝不写备份"

    def test_with_repair_still_writes_backup(self, isolated_paths):
        self._corrupt(isolated_paths["MONITOR_EVENTS_PATH"])
        rows = g3._load_monitor_event_rows_with_repair()
        assert len(rows) == 4
        assert list(isolated_paths["MONITOR_EVENTS_PATH"].parent.glob("*.corrupt-*")), "写通道保留审计备份语义"

    def test_recent_monitor_events_uses_readonly(self, isolated_paths):
        self._corrupt(isolated_paths["MONITOR_EVENTS_PATH"])
        rows = g3._recent_monitor_events(limit=3)
        assert [row["type"] for row in rows] == ["b", "a", "b"]
        assert not list(isolated_paths["MONITOR_EVENTS_PATH"].parent.glob("*.corrupt-*"))

    def test_readonly_healthy_file_identical(self, isolated_paths):
        _write(isolated_paths["MONITOR_EVENTS_PATH"], {"events": [{"type": "x"}] * 5})
        assert g3._load_monitor_event_rows_readonly() == [{"type": "x"}] * 5

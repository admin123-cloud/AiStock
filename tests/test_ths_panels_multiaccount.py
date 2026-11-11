# -*- coding: utf-8 -*-
"""R47 多账号面板层单测：面板枚举 / 分把窗口锁 / 面板注册表绑定。

不依赖 pywinauto/win32（枚举器注入合成面板）；锁走真实文件锁（tmp_path）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import ths_account_bridge as bridge  # noqa: E402


# ── 面板枚举（注入合成枚举器）──────────────────────────────────

def test_enumerate_panels_filters_trade_windows(monkeypatch):
    panels = [
        {"pid": 101, "process": "xiadan.exe", "title": "网上股票交易系统5.0", "hwnd": "0x1", "rect": [0, 0, 900, 700]},
        {"pid": 202, "process": "xiadan.exe", "title": "网上股票交易系统6.0", "hwnd": "0x2", "rect": [0, 0, 900, 700]},
        {"pid": 303, "process": "xiadan-plus.exe", "title": "NVOpenGLPbuffer", "hwnd": "0x3", "rect": [0, 0, 10, 10]},
        {"pid": 404, "process": "hexin.exe", "title": "同花顺(9.60.80) - 自选股", "hwnd": "0x4", "rect": [0, 0, 1800, 1000]},
    ]
    out = bridge.filter_trade_panels(panels)
    assert [p["pid"] for p in out] == [101, 202]


def test_enumerate_panels_keeps_unexpected_broker_process(monkeypatch):
    # 第二券商面板进程名不同（如 gf.exe）：仍靠标题令牌识别
    panels = [
        {"pid": 101, "process": "xiadan.exe", "title": "网上股票交易系统5.0", "hwnd": "0x1", "rect": [0, 0, 900, 700]},
        {"pid": 555, "process": "otherbroker.exe", "title": "XX证券网上交易系统", "hwnd": "0x5", "rect": [0, 0, 900, 700]},
    ]
    out = bridge.filter_trade_panels(panels)
    assert {p["pid"] for p in out} == {101, 555}


# ── 分把窗口锁 ────────────────────────────────────────────────

def test_panel_lock_per_panel_files(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_runtime_export_dir", lambda: tmp_path)
    # acct1 → legacy 锁名（与旧代码互斥）；acct2 → 分把锁
    assert bridge._panel_lock_name(None) == "ths_window.lock"
    assert bridge._panel_lock_name("acct1") == "ths_window.lock"
    assert bridge._panel_lock_name("acct2") == "ths_window.acct2.lock"
    with bridge._ths_window_operation_lock(panel="acct2", timeout_seconds=2):
        assert (tmp_path / "ths_window.acct2.lock").exists()
        # acct1 锁与 acct2 锁互不阻塞（分把防互踩）
        with bridge._ths_window_operation_lock(panel="acct1", timeout_seconds=2):
            pass


def test_panel_lock_same_panel_serializes(tmp_path, monkeypatch):
    import threading
    monkeypatch.setattr(bridge, "_runtime_export_dir", lambda: tmp_path)
    order = []
    entered = threading.Event()

    def worker():
        with bridge._ths_window_operation_lock(panel="acct2", timeout_seconds=5):
            order.append("enter")
            entered.set()
            import time as _t
            _t.sleep(1.2)  # 持锁须超过主线程的 timeout 窗口
            order.append("exit")

    t = threading.Thread(target=worker)
    t.start()
    entered.wait(timeout=5)  # 确认 worker 已持锁再竞争
    got = []
    try:
        with bridge._ths_window_operation_lock(panel="acct2", timeout_seconds=1):
            got.append("second-acquired")
    except TimeoutError:
        got.append("second-timeout")
    t.join()
    assert got == ["second-timeout"]
    assert order == ["enter", "exit"]


def test_panel_scope_switches_window_finder(monkeypatch):
    calls = {}

    def fake_find_by_panel(panel):
        calls["panel"] = panel
        return "WINDOW_OF_PANEL"

    monkeypatch.setattr(bridge, "_find_window_for_panel", fake_find_by_panel)
    with bridge.panel_scope({"panel_id": "acct2", "pid": 999, "process": "xiadan.exe"}):
        assert bridge._find_xiadan_window() == "WINDOW_OF_PANEL"
        assert calls["panel"]["panel_id"] == "acct2"
    # 出了 scope 恢复默认（走原 connect path 逻辑，不注入 panel）
    assert bridge._active_panel() is None


# ── 面板注册表（指纹→账号绑定）────────────────────────────────

def test_registry_bind_and_unbound(tmp_path):
    reg_file = tmp_path / "ths_panels.json"
    reg = bridge.PanelRegistry(reg_file)
    reg.bind(rule={"process": "xiadan.exe", "title_contains": "网上股票交易系统6.0"},
             account="acct2", broker_hint="XX证券")
    live = [
        {"pid": 101, "process": "xiadan.exe", "title": "网上股票交易系统5.0", "hwnd": "0x1", "rect": [0, 0, 900, 700]},
        {"pid": 202, "process": "xiadan.exe", "title": "网上股票交易系统6.0", "hwnd": "0x2", "rect": [0, 0, 900, 700]},
    ]
    resolved = reg.resolve(live)
    # acct1 默认规则（标题 5.0 + xiadan.exe）内置绑定
    assert resolved.by_account["acct1"]["pid"] == 101
    assert resolved.by_account["acct2"]["pid"] == 202
    assert resolved.unbound == []
    # 持久化往返
    reg2 = bridge.PanelRegistry(reg_file)
    resolved2 = reg2.resolve(live)
    assert resolved2.by_account["acct2"]["pid"] == 202


def test_registry_unbound_panel_never_mapped(tmp_path):
    reg = bridge.PanelRegistry(tmp_path / "ths_panels.json")
    live = [
        {"pid": 101, "process": "xiadan.exe", "title": "网上股票交易系统5.0", "hwnd": "0x1", "rect": [0, 0, 900, 700]},
        {"pid": 777, "process": "unknown.exe", "title": "某券商交易系统", "hwnd": "0x7", "rect": [0, 0, 900, 700]},
    ]
    resolved = reg.resolve(live)
    assert resolved.by_account["acct1"]["pid"] == 101
    assert [p["pid"] for p in resolved.unbound] == [777]  # 只上报，不映射


def test_registry_ambiguous_panels_fail_closed(tmp_path):
    # 两个窗口同标题都命中 acct2 规则 → acct2 判 ambiguous 不绑定；
    # acct1 默认规则只命中 5.0 窗口 → 正常绑定
    reg = bridge.PanelRegistry(tmp_path / "ths_panels.json")
    reg.bind(rule={"process": "xxbroker.exe", "title_contains": "XX证券网上交易系统"},
             account="acct2", broker_hint="XX")
    live = [
        {"pid": 101, "process": "xiadan.exe", "title": "网上股票交易系统5.0", "hwnd": "0x1", "rect": [0, 0, 900, 700]},
        {"pid": 202, "process": "xxbroker.exe", "title": "XX证券网上交易系统", "hwnd": "0x2", "rect": [0, 0, 900, 700]},
        {"pid": 203, "process": "xxbroker.exe", "title": "XX证券网上交易系统", "hwnd": "0x3", "rect": [0, 0, 900, 700]},
    ]
    resolved = reg.resolve(live)
    assert "acct2" not in resolved.by_account
    assert resolved.ambiguous_accounts == ["acct2"]
    assert resolved.by_account["acct1"]["pid"] == 101
    assert [p["pid"] for p in resolved.unbound] == [202, 203]

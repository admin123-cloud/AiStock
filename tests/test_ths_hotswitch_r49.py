# -*- coding: utf-8 -*-
"""R49 acct2 绑定 + Alt 热切换双账号读取单测。

覆盖任务书第 4 项：热切换状态机各分支（前台/切换失败/身份不符/切回失败）、
双账号巡读调度（home 在前、末位切回）、acct2 绑定幂等、
broker_state 首次接入当日无成交仍落基线。

全部合成 fixture（fake window / monkeypatch OCR 与热键发送），零 THS 依赖。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services import ths_account_bridge as bridge  # noqa: E402
from services import ths_holdings_poll as poll  # noqa: E402
from services import ths_hotswitch as hs  # noqa: E402

DAY = date(2026, 11, 11)


# ── fixture 工具 ────────────────────────────────────────────────────

class FakeWindow:
    def __init__(self, handle: int = 0x1234):
        self.handle = handle

    def is_minimized(self) -> bool:
        return False


def _cfg(**over):
    """acct1(曾,Alt+1 home) / acct2(王,Alt+2) 共享面板的热切换配置。"""
    cfg = {
        "target_hint": "曾", "target_hotkey": "1",
        "known_hints": [("曾", "acct1"), ("王", "acct2")],
        "home_hint": "曾", "home_hotkey": "1",
        "restore_after": False,
        "lock_name": "ths_window.lock",
    }
    cfg.update(over)
    return cfg


# ── 1) 状态机纯逻辑（ensure/restore，注入身份读取与热键层）──────────

def test_ensure_noop_when_already_on_target(monkeypatch):
    calls = []
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-曾*", None))
    monkeypatch.setattr(hs, "_switch_and_verify",
                        lambda w, hotkey, hint: calls.append(hotkey) or {"ok": True})
    assert hs.ensure_target_account(FakeWindow(), _cfg()) is None
    assert calls == []  # 已在目标账号：零切换零按键


def test_ensure_switches_when_other_known_account(monkeypatch):
    calls = []
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-王*美", None))
    monkeypatch.setattr(hs, "_switch_and_verify",
                        lambda w, hotkey, hint: calls.append((hotkey, hint))
                        or {"ok": True, "identity_label": "广发证券-曾*"})
    assert hs.ensure_target_account(FakeWindow(), _cfg()) is None
    assert calls == [("1", "曾")]


def test_ensure_failclosed_unknown_identity(monkeypatch):
    sent = []
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-李四", None))
    monkeypatch.setattr(hs, "_switch_and_verify",
                        lambda w, hotkey, hint: sent.append(hotkey) or {"ok": True})
    out = hs.ensure_target_account(FakeWindow(), _cfg())
    assert out["status"] == "identity_mismatch"
    assert sent == []  # 未知身份：绝不按键


def test_ensure_switch_failed_failclosed(monkeypatch):
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-王*美", None))
    monkeypatch.setattr(hs, "_switch_and_verify",
                        lambda w, hotkey, hint: {"ok": False,
                                                 "identity_label": "广发证券-王*美",
                                                 "attempts": 12})
    out = hs.ensure_target_account(FakeWindow(), _cfg())
    assert out["status"] == "switch_failed"
    assert out["identity_label"] == "广发证券-王*美"


def test_restore_home_ok_and_failed(monkeypatch):
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-王*美", None))
    monkeypatch.setattr(hs, "_switch_and_verify",
                        lambda w, hotkey, hint: {"ok": True,
                                                 "identity_label": "广发证券-曾*"})
    assert hs.restore_home_account(FakeWindow(), _cfg()) is None

    monkeypatch.setattr(hs, "_switch_and_verify",
                        lambda w, hotkey, hint: {"ok": False, "attempts": 12})
    out = hs.restore_home_account(FakeWindow(), _cfg())
    assert out["status"] == "switchback_failed"


def test_restore_home_noop_when_already_home(monkeypatch):
    sent = []
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-曾*", None))
    monkeypatch.setattr(hs, "_switch_and_verify",
                        lambda w, hotkey, hint: sent.append(hotkey) or {"ok": True})
    assert hs.restore_home_account(FakeWindow(), _cfg()) is None
    assert sent == []


# ── 2) capture_panel_zero_touch 集成分支 ────────────────────────────

class _FakeImg:
    def save(self, path):
        Path(str(path)).write_bytes(b"PNG")


def _patch_capture_env(monkeypatch, *, label, holdings=None, switch_ok=True,
                       restore_ok=True, log):
    """注入：面板查找/前台/锁/PrintWindow/OCR/解析/UIA 资金 + 热键层。"""
    panel = {"pid": 101, "process": "xiadan.exe", "title": "网上股票交易系统5.0"}
    monkeypatch.setattr(bridge, "_find_window_for_panel", lambda p: FakeWindow())
    monkeypatch.setattr(poll, "_is_foreground", lambda hwnd: False)
    monkeypatch.setattr(bridge, "_ths_window_operation_lock",
                        lambda *a, **k: _null_cm())
    monkeypatch.setattr(poll, "print_window_image", lambda hwnd: _FakeImg())
    state = {"label": label}

    def fake_ocr(png):
        lines = [state["label"], "股票余额 参考市值"] if label else ["股票余额"]
        items = [{"x": 1, "y": 1, "text": t, "score": 1.0} for t in lines]
        return items, lines

    monkeypatch.setattr(poll, "_ocr_image", fake_ocr)
    monkeypatch.setattr(bridge, "_parse_holdings_from_ocr_items",
                        lambda items, capital=None: holdings or
                        [{"code": "603881", "name": "数据港", "shares": 600,
                          "available_shares": 600, "cost_price": 33.993,
                          "current_price": 20.6}])
    monkeypatch.setattr(bridge, "_read_from_ths_uia_snapshot",
                        lambda: {"capital": {"available_cash": 4076.82}})
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: (state["label"], None))

    def fake_switch(w, hotkey, hint):
        log.append(("switch", hotkey, hint))
        if switch_ok:
            state["label"] = f"广发证券-{'曾*' if hint == '曾' else '王*美'}"
            return {"ok": True, "identity_label": state["label"]}
        return {"ok": False, "identity_label": state["label"], "attempts": 12}

    monkeypatch.setattr(hs, "_switch_and_verify", fake_switch)

    def fake_restore(w, cfg):
        log.append(("restore", cfg["home_hotkey"]))
        if restore_ok:
            state["label"] = f"广发证券-{'曾*' if cfg['home_hint'] == '曾' else '王*美'}"
            return None
        return {"status": "switchback_failed", "attempts": 12}

    monkeypatch.setattr(hs, "restore_home_account", fake_restore)
    return panel


class _NullCM:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _null_cm():
    return _NullCM()


def test_capture_foreground_skips_without_switch(monkeypatch):
    log = []
    panel = _patch_capture_env(monkeypatch, label="广发证券-王*美", log=log)
    monkeypatch.setattr(poll, "_is_foreground", lambda hwnd: True)
    cap = poll.capture_panel_zero_touch(
        "acct1", panel, identity_hint="曾", hotswitch=_cfg())
    assert cap["status"] == "skipped_foreground"
    assert log == []  # 前台绝不抢焦点、绝不切账号


def test_capture_switch_failed_branch(monkeypatch):
    log = []
    panel = _patch_capture_env(monkeypatch, label="广发证券-王*美",
                               switch_ok=False, log=log)
    cap = poll.capture_panel_zero_touch(
        "acct1", panel, identity_hint="曾", hotswitch=_cfg())
    assert cap["status"] == "switch_failed"
    assert cap["status"] in poll.SKIP_STATUSES  # 记 skipped 非 error
    assert log == [("switch", "1", "曾")]
    assert cap.get("holdings") is None  # 绝不带出他账号数据


def test_capture_identity_mismatch_unknown_panel(monkeypatch):
    log = []
    panel = _patch_capture_env(monkeypatch, label="华泰证券-张三", log=log)
    cap = poll.capture_panel_zero_touch(
        "acct1", panel, identity_hint="曾", hotswitch=_cfg())
    assert cap["status"] == "identity_mismatch"
    assert log == []  # 未知券商/身份：零按键零猜


def test_capture_switch_read_ok_then_restore(monkeypatch, tmp_path):
    log = []
    # 面板当前在曾（常态位）→ 读 acct2 须切王、读毕切回曾
    panel = _patch_capture_env(monkeypatch, label="广发证券-曾*", log=log)
    cap = poll.capture_panel_zero_touch(
        "acct2", panel, identity_hint="王",
        evidence_dir=tmp_path, hotswitch=_cfg(
            target_hint="王", target_hotkey="2", restore_after=True))
    assert cap["status"] == "ok"
    assert cap["holdings"][0]["code"] == "603881"
    assert cap["identity_label"] == "广发证券-王*美"
    assert ("switch", "2", "王") in log
    assert ("restore", "1") in log  # 读毕切回曾（常态位）
    assert cap.get("switchback_failed") is None


def test_capture_switchback_failed_flag(monkeypatch, tmp_path):
    log = []
    panel = _patch_capture_env(monkeypatch, label="广发证券-王*美",
                               restore_ok=False, log=log)
    cap = poll.capture_panel_zero_touch(
        "acct2", panel, identity_hint="王",
        evidence_dir=tmp_path, hotswitch=_cfg(
            target_hint="王", target_hotkey="2", restore_after=True))
    assert cap["status"] == "ok"              # 数据有效已读
    assert cap.get("switchback_failed") is True  # 但常态位未恢复 → 留痕


def test_capture_no_hotswitch_keeps_legacy_path(monkeypatch, tmp_path):
    log = []
    panel = _patch_capture_env(monkeypatch, label="广发证券-曾*", log=log)
    cap = poll.capture_panel_zero_touch(
        "acct1", panel, identity_hint="曾", evidence_dir=tmp_path)
    assert cap["status"] == "ok"
    assert log == []  # 无热切换配置：R48 路径原样


# ── 3) 双账号巡读调度（run_poll_round + 计划构建）───────────────────

def _live_panels():
    return [{"pid": 101, "process": "xiadan.exe",
             "title": "网上股票交易系统5.0", "hwnd": "0x1",
             "rect": [0, 0, 900, 700]}]


def _bind_registry(tmp_path):
    reg = bridge.PanelRegistry(tmp_path / "ths_panels.json")
    reg.bind(account="acct1", broker_hint="广发证券",
             rule={"process": "xiadan.exe",
                   "title_contains": "网上股票交易系统"},
             identity_hint="曾", hotkey="1")
    reg.bind(account="acct2", broker_hint="广发证券",
             rule={"process": "xiadan.exe",
                   "title_contains": "网上股票交易系统"},
             identity_hint="王", hotkey="2")
    return reg


def test_registry_hotswitch_shared_panel_and_idempotent_bind(tmp_path):
    reg = _bind_registry(tmp_path)
    # 幂等：重复绑定同账号 → 规则唯一（不堆积）
    reg.bind(account="acct2", broker_hint="广发证券",
             rule={"process": "xiadan.exe",
                   "title_contains": "网上股票交易系统"},
             identity_hint="王", hotkey="2")
    data = json.loads((tmp_path / "ths_panels.json").read_text(encoding="utf-8"))
    assert [r["account"] for r in data["rules"]] == ["acct1", "acct2"]

    resolved = reg.resolve(_live_panels())
    # 单面板共享：两账号映射到同一物理窗口（热切换组）
    assert resolved.by_account["acct1"]["pid"] == 101
    assert resolved.by_account["acct2"]["pid"] == 101
    assert resolved.unbound == []
    assert resolved.hotswitch_groups == {101: ["acct1", "acct2"]}
    # 绑定字段持久化（身份令牌 + 热键）
    rules = {r["account"]: r for r in resolved.rules}
    assert rules["acct1"]["identity_hint"] == "曾"
    assert rules["acct1"]["hotkey"] == "1"
    assert rules["acct2"]["identity_hint"] == "王"
    assert rules["acct2"]["hotkey"] == "2"
    assert reg.home_account() == "acct1"


def test_registry_legacy_rules_unaffected(tmp_path):
    # R47 语义回归：不带 hotkey 的规则仍是独占分把绑定
    reg = bridge.PanelRegistry(tmp_path / "ths_panels.json")
    reg.bind(account="acct2", broker_hint="XX证券",
             rule={"process": "xxbroker.exe", "title_contains": "XX证券网上交易系统"})
    live = [
        {"pid": 101, "process": "xiadan.exe", "title": "网上股票交易系统5.0",
         "hwnd": "0x1", "rect": [0, 0, 900, 700]},
        {"pid": 202, "process": "xxbroker.exe", "title": "XX证券网上交易系统",
         "hwnd": "0x2", "rect": [0, 0, 900, 700]},
    ]
    resolved = reg.resolve(live)
    assert resolved.by_account["acct1"]["pid"] == 101
    assert resolved.by_account["acct2"]["pid"] == 202
    assert resolved.hotswitch_groups == {}


def test_minimized_panel_resolves_for_hotswitch(tmp_path):
    """最小化面板（rect=-32000 哨兵）：默认枚举滤除；include_minimized=True
    时可解析（fills 导出/绑定需见窗，巡读 capture 仍 skipped_minimized）。"""
    minimized = {"pid": 101, "process": "xiadan.exe",
                 "title": "网上股票交易系统5.0", "hwnd": "0x1", "iconic": True,
                 "rect": [-32000, -32000, -31100, -31850]}
    assert bridge.filter_trade_panels([minimized]) == []
    assert [p["pid"] for p in
            bridge.filter_trade_panels([minimized], include_minimized=True)] == [101]
    reg = _bind_registry(tmp_path)
    resolved = reg.resolve([minimized])
    # R49 实况：午后面板常最小化——热切换组仍应解析到同一物理窗口
    assert resolved.by_account["acct1"]["pid"] == 101
    assert resolved.by_account["acct2"]["pid"] == 101
    assert resolved.hotswitch_groups == {101: ["acct1", "acct2"]}


def test_plan_builder_orders_home_first_with_restore_on_last():
    from scripts.ths_holdings_poll import build_hotswitch_plan

    reg = _type_stub_registry()
    plan = build_hotswitch_plan(reg, _live_panels())
    assert list(plan) == ["acct1", "acct2"]  # 曾（常态位）在前
    assert plan["acct1"]["identity_hint"] == "曾"
    assert plan["acct2"]["identity_hint"] == "王"
    assert plan["acct1"]["hotswitch"]["restore_after"] is False
    assert plan["acct2"]["hotswitch"]["restore_after"] is True  # 末位读毕切回
    assert plan["acct1"]["hotswitch"]["lock_name"] == "ths_window.lock"


def _type_stub_registry():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    return _bind_registry(tmp)


def test_run_poll_round_dual_accounts(monkeypatch, tmp_path):
    """双账号各读一遍：acct1 002015 / acct2 603881；恢复仅在末位。"""
    order = []
    restores = []

    def make_capture(label_by_acct):
        def capture(account, panel, hint=None):
            order.append(account)
            cfg_hint = label_by_acct[account]
            cap = {"status": "ok",
                   "identity_label": f"广发证券-{cfg_hint}",
                   "holdings": [{"code": "603881" if account == "acct2" else "002015",
                                 "name": "n", "shares": 600 if account == "acct2" else 4000,
                                 "available_shares": 600 if account == "acct2" else 4000,
                                 "current_price": 20.6, "cost_price": 33.993}]}
            if account == "acct2":
                cap["switchback_failed"] = None
            return cap
        return capture

    plan = {
        "acct1": {"panel": {"pid": 101}, "identity_hint": "曾",
                  "hotswitch": _cfg(restore_after=False)},
        "acct2": {"panel": {"pid": 101}, "identity_hint": "王",
                  "hotswitch": _cfg(target_hint="王", target_hotkey="2",
                                    restore_after=True)},
    }
    report = poll.run_poll_round(
        root=tmp_path, now=datetime(2026, 11, 11, 15, 40, 0), plan=plan,
        capture=make_capture({"acct1": "曾*", "acct2": "王*美"}))
    assert order == ["acct1", "acct2"]
    acc = report["accounts"]
    assert acc["acct1"]["status"] == "ok" and acc["acct2"]["status"] == "ok"
    # 快照分账号落盘，inferred 台账行带 account 键
    snap1 = tmp_path / "holdings_snapshots" / "20261111" / "154000_acct1.json"
    snap2 = tmp_path / "holdings_snapshots" / "20261111" / "154000_acct2.json"
    assert snap1.is_file() and snap2.is_file()
    rows = poll.read_jsonl(tmp_path / "trades_inferred" / "20261111.jsonl")
    # 首轮=各账号基线（对空 diff 不记买入），零 inferred 行
    assert rows == []


# ── 4) broker_state 首次接入：当日无成交也落基线 ─────────────────────

def test_broker_state_sync_init_base_zero_pending_writes_baseline(tmp_path):
    from services import broker_state_autosync as bsa

    state_file = tmp_path / "broker_state.json"
    state_file.write_text(json.dumps({
        "holdings": [{"code": "002015", "name": "协鑫能科", "shares": 4000,
                      "cost_price": 16.447, "current_price": 13.64}],
        "capital": {"available_cash": 54892.09},
        "updated_at": "2026-11-11 15:15:01",
    }), encoding="utf-8")
    fills_file = tmp_path / "fills_20261111_acct2.json"
    fills_file.write_text(json.dumps({
        "schema": "ths_fills_export/1", "status": "ok", "account": "acct2",
        "trade_date": "2026-11-11", "fills": [],
    }), encoding="utf-8")
    init_base = {
        "base_as_of": "2026-11-11",
        "base": {"603881": {"shares": 600, "cost_price": 33.993}},
        "base_cash_available": 4076.82,
        "base_meta": {"603881": {"price": 20.6, "name": "数据港"}},
        "provenance": {"source": "ths_ocr_first_read"},
    }
    result = bsa.run_sync(
        state_file=state_file, fills_file=fills_file,
        alerts_file=tmp_path / "alerts.jsonl",
        report_file=tmp_path / "report.json",
        history_file=tmp_path / "history.jsonl",
        for_date=DAY, now=datetime(2026, 11, 11, 15, 45, 0),
        init_base=init_base, account="acct2")
    assert result["rc"] == 0
    assert result["action"] == "write"
    full = json.loads(state_file.read_text(encoding="utf-8"))
    acct2 = full["accounts"]["acct2"]
    codes = {h["code"]: h for h in acct2["holdings"]}
    assert codes["603881"]["shares"] == 600
    assert codes["603881"]["cost_price"] == 33.993
    # acct1 顶层镜像不受影响
    assert full["holdings"][0]["code"] == "002015"

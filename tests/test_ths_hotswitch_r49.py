# -*- coding: utf-8 -*-
"""R49/R49c acct2 绑定 + 双账号热切换读取单测。

R49c 机制修正（2026-11-11 深夜微切片）：Alt+N 判死（12 轮自证恒另一
账号）→ 账号切换器 ComboBox(aid=2322) SelectionItem 选择路径。覆盖：
切换状态机各分支（已在目标/身份不符/选择失败各 reason/自证失败/切回
失败）、下拉文本匹配纯函数（找不到项/文本不匹配/歧义）、双账号巡读
调度（home 在前、末位切回）、acct2 绑定幂等、broker_state 首次接入
当日无成交仍落基线。

全部合成 fixture（fake window / monkeypatch OCR 与 UIA 选择层），
零 THS 依赖、零 comtypes 依赖（真机 UIA 路径由实机验证）。
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
    """acct1(曾,home常态位) / acct2(王) 共享面板的热切换配置（R49c：
    ComboBox aid=2322 SelectionItem 选择，无热键字段）。"""
    cfg = {
        "target_hint": "曾",
        "known_hints": [("曾", "acct1"), ("王", "acct2")],
        "home_hint": "曾",
        "restore_after": False,
        "switch_mode": "combobox_selection",
        "lock_name": "ths_window.lock",
    }
    cfg.update(over)
    return cfg


# ── 1) 状态机纯逻辑（ensure/restore，注入身份读取与 UIA 选择层）────

def test_ensure_noop_when_already_on_target(monkeypatch):
    calls = []
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-曾*", None))
    monkeypatch.setattr(hs, "_select_account_and_verify",
                        lambda w, hint: calls.append(hint) or {"ok": True})
    assert hs.ensure_target_account(FakeWindow(), _cfg()) is None
    assert calls == []  # 已在目标账号：零切换零交互


def test_ensure_switches_when_other_known_account(monkeypatch):
    calls = []
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-王*美", None))
    monkeypatch.setattr(hs, "_select_account_and_verify",
                        lambda w, hint: calls.append(hint)
                        or {"ok": True, "identity_label": "广发证券-曾*"})
    assert hs.ensure_target_account(FakeWindow(), _cfg()) is None
    assert calls == ["曾"]  # 经账号切换器选曾（无热键）


def test_ensure_failclosed_unknown_identity(monkeypatch):
    sent = []
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-李四", None))
    monkeypatch.setattr(hs, "_select_account_and_verify",
                        lambda w, hint: sent.append(hint) or {"ok": True})
    out = hs.ensure_target_account(FakeWindow(), _cfg())
    assert out["status"] == "identity_mismatch"
    assert sent == []  # 未知身份：绝不触切换器


@pytest.mark.parametrize("reason", [
    "combobox_absent",    # aid=2322 不在树中（结构变了）
    "expand_failed",      # 下拉展不开
    "no_items",           # 展开后列表项没出现
    "item_not_found",     # 列表项在，但无一项含目标令牌（文本不匹配）
    "ambiguous_match",    # 多项含目标令牌（歧义，绝不任选）
    "select_error",       # SelectionItem.Select() 异常
    "uia_error",          # comtypes/COM 层异常
])
def test_ensure_switch_failed_reasons_failclosed(monkeypatch, reason):
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-王*美", None))
    monkeypatch.setattr(hs, "_select_account_and_verify",
                        lambda w, hint: {"ok": False, "reason": reason,
                                         "identity_label": "广发证券-王*美"})
    out = hs.ensure_target_account(FakeWindow(), _cfg())
    assert out["status"] == "switch_failed"
    assert out["reason"] == reason       # reason 原样上报（排障面）
    assert "fail-closed" in out["note"]


def test_select_and_verify_ok_then_label_flips(monkeypatch):
    """Select 发出 → OCR 轮询读到目标令牌才算数。"""
    labels = iter(["广发证券-王*美", "广发证券-曾*"])
    monkeypatch.setattr(hs, "_uia_select_account",
                        lambda hwnd, hint: {"ok": True, "matched": "广发证券-曾*",
                                            "items": ["广发证券-曾*", "广发证券-王*美"]})
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: (next(labels), None))
    res = hs._select_account_and_verify(FakeWindow(), "曾")
    assert res["ok"] is True
    assert res["identity_label"] == "广发证券-曾*"
    assert res["selected_item"] == "广发证券-曾*"


def test_select_and_verify_uia_not_ok_short_circuits(monkeypatch):
    """UIA 选择未通过（找不到项/文本不匹配等）→ 不进 OCR 轮询直接 fail。"""
    ocr = []
    monkeypatch.setattr(hs, "_uia_select_account",
                        lambda hwnd, hint: {"ok": False, "reason": "item_not_found",
                                            "items": ["广发证券-王*美"]})
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ocr.append(1) or ("广发证券-王*美", None))
    res = hs._select_account_and_verify(FakeWindow(), "曾")
    assert res["ok"] is False
    assert res["reason"] == "item_not_found"
    assert res["detail"]["items"] == ["广发证券-王*美"]
    assert ocr == []  # 选择未过：绝不做身份轮询（也绝无"碰巧读到"通道）


def test_select_and_verify_uia_exception_wrapped(monkeypatch):
    def boom(hwnd, hint):
        raise OSError("comtypes unavailable")
    monkeypatch.setattr(hs, "_uia_select_account", boom)
    res = hs._select_account_and_verify(FakeWindow(), "曾")
    assert res["ok"] is False
    assert res["reason"] == "uia_error"
    assert "OSError" in res["error"]


def test_select_and_verify_post_switch_identity_fail(monkeypatch):
    """切换后自证失败：Select 已发出但 OCR 恒读旧账号 → fail-closed。"""
    monkeypatch.setattr(hs, "_uia_select_account",
                        lambda hwnd, hint: {"ok": True, "matched": "广发证券-曾*"})
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-王*美", None))
    monkeypatch.setattr(hs, "SWITCH_WAIT_ATTEMPTS", 2)
    monkeypatch.setattr(hs, "SWITCH_WAIT_SLEEP_S", 0)
    res = hs._select_account_and_verify(FakeWindow(), "曾")
    assert res["ok"] is False
    assert res["reason"] == "verify_failed"
    assert res["identity_label"] == "广发证券-王*美"


# ── 1b) 下拉文本匹配纯函数（找不到项/文本不匹配/歧义）──────────────

def test_match_names_unique_hit():
    idx, reason = hs.match_account_item_names(
        ["广发证券-曾*", "广发证券-王*美"], "曾")
    assert (idx, reason) == (0, "")


def test_match_names_no_items():
    assert hs.match_account_item_names([], "曾") == (None, "no_items")
    assert hs.match_account_item_names(["", "  "], "曾") == (None, "no_items")


def test_match_names_text_mismatch_item_not_found():
    """文本不匹配：列表项存在但无一项含目标令牌。"""
    assert hs.match_account_item_names(
        ["广发证券-王*美", "广发证券-李四"], "曾") == (None, "item_not_found")


def test_match_names_ambiguous():
    assert hs.match_account_item_names(
        ["广发证券-王*美", "广发证券-王*二"], "王") == (None, "ambiguous_match")


def test_match_names_empty_hint():
    assert hs.match_account_item_names(["广发证券-曾*"], "") == (None, "no_hint")


# ── 1c) 切回常态位 ──────────────────────────────────────────────────

def test_restore_home_ok_and_failed(monkeypatch):
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-王*美", None))
    monkeypatch.setattr(hs, "_select_account_and_verify",
                        lambda w, hint: {"ok": True,
                                         "identity_label": "广发证券-曾*"})
    assert hs.restore_home_account(FakeWindow(), _cfg()) is None

    monkeypatch.setattr(hs, "_select_account_and_verify",
                        lambda w, hint: {"ok": False, "reason": "verify_failed",
                                         "attempts": 12})
    out = hs.restore_home_account(FakeWindow(), _cfg())
    assert out["status"] == "switchback_failed"
    assert out["reason"] == "verify_failed"


def test_restore_home_noop_when_already_home(monkeypatch):
    sent = []
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-曾*", None))
    monkeypatch.setattr(hs, "_select_account_and_verify",
                        lambda w, hint: sent.append(hint) or {"ok": True})
    assert hs.restore_home_account(FakeWindow(), _cfg()) is None
    assert sent == []


def test_restore_home_no_hint_failclosed(monkeypatch):
    out = hs.restore_home_account(FakeWindow(), _cfg(home_hint=""))
    assert out["status"] == "switchback_failed"
    assert out["reason"] == "home_no_hint"


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

    def fake_select(w, hint):
        log.append(("select", hint))
        if switch_ok:
            state["label"] = f"广发证券-{'曾*' if hint == '曾' else '王*美'}"
            return {"ok": True, "identity_label": state["label"]}
        return {"ok": False, "reason": "item_not_found",
                "identity_label": state["label"], "attempts": 12}

    monkeypatch.setattr(hs, "_select_account_and_verify", fake_select)

    def fake_restore(w, cfg):
        log.append(("restore", cfg["home_hint"]))
        if restore_ok:
            state["label"] = f"广发证券-{'曾*' if cfg['home_hint'] == '曾' else '王*美'}"
            return None
        return {"status": "switchback_failed", "reason": "verify_failed",
                "attempts": 12}

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
    assert cap["reason"] == "item_not_found"    # reason 原样透传（排障面）
    assert log == [("select", "曾")]
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
            target_hint="王", restore_after=True))
    assert cap["status"] == "ok"
    assert cap["holdings"][0]["code"] == "603881"
    assert cap["identity_label"] == "广发证券-王*美"
    assert ("select", "王") in log
    assert ("restore", "曾") in log  # 读毕切回曾（常态位）
    assert cap.get("switchback_failed") is None


def test_capture_switchback_failed_flag(monkeypatch, tmp_path):
    log = []
    panel = _patch_capture_env(monkeypatch, label="广发证券-王*美",
                               restore_ok=False, log=log)
    cap = poll.capture_panel_zero_touch(
        "acct2", panel, identity_hint="王",
        evidence_dir=tmp_path, hotswitch=_cfg(
            target_hint="王", restore_after=True))
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
    from services.ths_hotswitch import build_hotswitch_plan  # 实现本就在 services 层（旧 scripts.ths_holdings_poll 只是转发）

    reg = _type_stub_registry()
    plan = build_hotswitch_plan(reg, _live_panels())
    assert list(plan) == ["acct1", "acct2"]  # 曾（常态位）在前
    assert plan["acct1"]["identity_hint"] == "曾"
    assert plan["acct2"]["identity_hint"] == "王"
    assert plan["acct1"]["hotswitch"]["restore_after"] is False
    assert plan["acct2"]["hotswitch"]["restore_after"] is True  # 末位读毕切回
    assert plan["acct1"]["hotswitch"]["lock_name"] == "ths_window.lock"
    # R49c：ComboBox SelectionItem 机制标注；热键字段已除名（Alt+N 判死）
    for entry in plan.values():
        sw = entry.get("hotswitch") or {}
        assert sw.get("switch_mode") == "combobox_selection"
        assert "target_hotkey" not in sw and "home_hotkey" not in sw


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
                  "hotswitch": _cfg(target_hint="王",
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

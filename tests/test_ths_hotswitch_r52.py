# -*- coding: utf-8 -*-
"""R52 双账号热切换通道化回归（2026-11-14 凌晨实机取证）。

实机事实链（2026-11-13 晚~11-14 01:35）：
  * fills/20261114.unavailable.json：acct1 标记 label='广发证券-王*美' combo=''
    reason=mismatch——R51 身份门禁跑在 ensure_target_account 之前，面板停在
    王时切换代码从未执行（combo='' 只是备用锚未查询；实机普查证明
    ComboBox aid=2322 存在且 Value='广发证券-王*美' 正常可读）。
  * 实机 UIA 普查：展开后的下拉列表项被多路重复暴露（同名同 rect ×N），
    match_account_item_names 数出多项命中 → ambiguous_match 假歧义
    （R49c ComboBox 通道即使被执行也过不去）。

本文件锁定 R52 行为契约：
  ① 下拉项去重 + 非账号项剔除（假歧义回归）；
  ② Owner 活跃守卫：前台+输入空闲<90s → skipped_foreground 绝不切；
  ③ 通道链（热键A→ComboBox B）回退顺序与 reason 上报；
  ④ _capture_ths_views 热切换先于身份门禁（顺序回归）；
  ⑤ 计划构建携带通道链与热键（曾=1/王=2，home 热键换绑）。
全部 mock，零真机依赖（真机路径由 2026-11-14 凌晨实机验证覆盖）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from services import ths_account_bridge as bridge  # noqa: E402
from services import ths_hotswitch as hs  # noqa: E402
import ths_client as tc  # noqa: E402


# ── ① 下拉项清洗（假歧义回归：实机同名同 rect ×N 重复暴露）─────────

def test_normalize_dedupes_duplicate_items():
    """实机形态：每项 ×7 重复 → 去重后唯一。"""
    raw = ["广发证券-曾*", "广发证券-王*美", "编辑账户"] * 7
    assert hs.normalize_account_items(raw) == [
        "广发证券-曾*", "广发证券-王*美"]


def test_normalize_drops_non_account_items():
    assert hs.normalize_account_items(
        ["广发证券-曾*", "编辑账户", "添加账户"]) == ["广发证券-曾*"]
    assert hs.normalize_account_items(["编辑账户"]) == []


def test_normalize_keeps_unique_accounts():
    assert hs.normalize_account_items(
        ["广发证券-曾*", "广发证券-王*美"]) == ["广发证券-曾*", "广发证券-王*美"]
    assert hs.normalize_account_items([]) == []
    assert hs.normalize_account_items(["", "  "]) == []


def test_deduped_names_match_unique_no_false_ambiguous():
    """回归：不去重时 match_account_item_names 数出 7 个「曾」命中
    → ambiguous_match fail-closed；清洗后唯一命中。"""
    raw = ["广发证券-曾*", "广发证券-王*美", "编辑账户"] * 7
    idx, reason = hs.match_account_item_names(
        hs.normalize_account_items(raw), "曾")
    assert (idx, reason) == (0, "")


def test_two_accounts_sharing_token_still_ambiguous():
    """真歧义保护不放松：两个账号项都含目标令牌 → 仍然 ambiguous。"""
    idx, reason = hs.match_account_item_names(
        hs.normalize_account_items(["广发证券-王*美", "广发证券-王*二"]), "王")
    assert (idx, reason) == (None, "ambiguous_match")


def test_combo_anchor_prefers_value_over_blank_name():
    """实机实证：切换器 ComboBox Name 是单个空格——备用锚必须取 Value。"""
    assert hs._combo_anchor_text(" ", "广发证券-王*美") == "广发证券-王*美"
    assert hs._combo_anchor_text("", "广发证券-曾*") == "广发证券-曾*"
    assert hs._combo_anchor_text("  ", "  ") == ""          # 双空 → 空锚
    assert hs._combo_anchor_text("标签", "") == "标签"        # 无 Value 退 Name


def test_owner_active_decision_ignores_self_injection():
    """自家注入后的空闲 ≈ 注入以来时长 → 不算 Owner；注入后更新鲜的输入
    → 是 Owner。"""
    # 注入后 8s，空闲 8s（最后一次输入就是我们）→ 不活跃
    assert hs._owner_active_decision(8.0, 8.0) is False
    # 注入后 8s，空闲 3s（注入之后 Owner 又动了）→ 活跃
    assert hs._owner_active_decision(3.0, 8.0) is True
    # 从未注入（since_self=0/inf）：空闲 < 阈值即 Owner
    assert hs._owner_active_decision(10.0, 0.0) is True
    assert hs._owner_active_decision(10.0, float("inf")) is True
    # 空闲超过阈值：无论谁输入都不活跃
    assert hs._owner_active_decision(120.0, 0.0) is False


# ── ② Owner 活跃守卫 ─────────────────────────────────────────────

class FakeWindow:
    def __init__(self, handle: int = 0x1234):
        self.handle = handle

    def is_minimized(self) -> bool:
        return False


def test_owner_using_panel_false_without_handle():
    assert hs.owner_using_panel(FakeWindow(handle=0)) is False


def test_input_idle_seconds_non_negative():
    v = hs.input_idle_seconds()
    assert v >= 0  # Windows 主机上可读；异常路径返回 inf


# ── ③ 通道链：home 热键换绑（切回王=2 而非沿用曾=1）────────────────

def test_restore_home_rebinds_target_hotkey(monkeypatch):
    """读毕切回常态位：target_hotkey 必须换成 home 的热键。"""
    monkeypatch.setattr(hs, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-王*美", None))
    seen = {}

    def fake_switch(w, hint, cfg=None):
        seen["hotkey"] = (cfg or {}).get("target_hotkey")
        seen["hint"] = hint
        return {"ok": True, "identity_label": "广发证券-曾*"}

    monkeypatch.setattr(hs, "_switch_account_and_verify", fake_switch)
    cfg = {"target_hint": "王", "home_hint": "曾",
           "target_hotkey": "2", "home_hotkey": "1",
           "channels": [hs.CHANNEL_HOTKEY, hs.CHANNEL_COMBOBOX]}
    assert hs.restore_home_account(FakeWindow(), cfg) is None
    assert seen == {"hint": "曾", "hotkey": "1"}


def test_hotkey_channel_rejects_non_digit(monkeypatch):
    class _W:
        handle = 0  # 无句柄：先于 ctypes 分支拒绝
    assert hs.switch_account_via_hotkey(_W(), "abc")["reason"] == "hotkey_not_digit"
    assert hs.switch_account_via_hotkey(_W(), "")["reason"] == "hotkey_not_digit"
    assert hs.switch_account_via_hotkey(_W(), "12")["reason"] == "hotkey_not_digit"


# ── ④ _capture_ths_views：热切换先于身份门禁（顺序回归）────────────

def _patch_fills_capture_env(monkeypatch, *, start_label):
    """注入 fills 导出采集链：面板查找/锁/登录框/OCR/菜单/热切换。"""
    state = {"label": start_label}
    calls = {"ensure_before_gate": False, "ensure_order": [], "menus": []}

    window = FakeWindow()

    monkeypatch.setattr(bridge, "_find_xiadan_window", lambda: window)
    monkeypatch.setattr(bridge, "_ths_window_operation_lock",
                        lambda *a, **k: _null_cm())
    monkeypatch.setattr(bridge, "detect_xiadan_login_dialog",
                        lambda w: None)

    def fake_ocr_window():
        lines = [state["label"], "成交日期 成交时间 证券代码"]
        items = [{"x": 1, "y": 1, "text": t, "score": 1.0} for t in lines]
        return items, lines, "/tmp/fake_shot.png"

    monkeypatch.setattr(bridge, "_ocr_xiadan_window", fake_ocr_window)
    monkeypatch.setattr(bridge, "_parse_trades_from_ocr_items",
                        lambda items: [])
    monkeypatch.setattr(bridge, "_click_query_menu_item",
                        lambda name: calls.setdefault("menus", []).append(name))

    def fake_ensure(w, cfg):
        calls["ensure_before_gate"] = True
        calls.setdefault("ensure_order", []).append("ensure")
        state["label"] = "广发证券-曾*"  # 热切换成功：面板翻到目标账号
        return None

    monkeypatch.setattr(hs, "ensure_target_account", fake_ensure)
    monkeypatch.setattr(hs, "owner_using_panel", lambda w: False)

    def fake_robust(w, save_png=None):
        calls.setdefault("ensure_order", []).append("gate")
        return {"ocr_label": state["label"], "combo_text": "",
                "screenshot": None}

    monkeypatch.setattr(hs, "read_window_identity_robust", fake_robust)
    return state, calls


class _NullCM:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _null_cm():
    return _NullCM()


def _hotswitch_cfg(**over):
    cfg = {
        "target_hint": "曾",
        "known_hints": [("曾", "acct1"), ("王", "acct2")],
        "home_hint": "曾",
        "restore_after": True,
        "channels": [hs.CHANNEL_HOTKEY, hs.CHANNEL_COMBOBOX],
        "target_hotkey": "1",
        "home_hotkey": "1",
        "lock_name": "ths_window.lock",
    }
    cfg.update(over)
    return cfg


def test_capture_switches_before_identity_gate(monkeypatch):
    """回归 2026-11-14：面板停在王时导出 acct1，切换必须先于门禁执行。"""
    state, calls = _patch_fills_capture_env(
        monkeypatch, start_label="广发证券-王*美")
    monkeypatch.setattr(hs, "restore_home_account",
                        lambda w, cfg: None)
    out = tc._capture_ths_views(None, panel=None, account="acct1",
                                identity_hint="曾", hotswitch=_hotswitch_cfg())
    assert calls["ensure_before_gate"] is True
    assert calls["ensure_order"] == ["ensure", "gate"]  # 切换先于门禁
    assert state["label"] == "广发证券-曾*"
    assert calls.get("menus") == ["today_trades", "history_trades"]
    assert out["history_rows"] == []


def test_capture_switch_guard_skips_foreground_failclosed(monkeypatch):
    """Owner 活跃 → ensure 返回 skipped_foreground → PANEL_SKIPPED_FOREGROUND
    fail-closed（绝不抢焦点绝不清采集）。"""
    state, calls = _patch_fills_capture_env(
        monkeypatch, start_label="广发证券-王*美")

    def fake_ensure(w, cfg):
        calls["ensure_before_gate"] = True
        return {"status": "skipped_foreground",
                "identity_label": state["label"]}

    monkeypatch.setattr(hs, "ensure_target_account", fake_ensure)
    with pytest.raises(RuntimeError, match="PANEL_SKIPPED_FOREGROUND"):
        tc._capture_ths_views(None, panel=None, account="acct1",
                              identity_hint="曾", hotswitch=_hotswitch_cfg())
    assert calls["menus"] == []  # 门禁拦截在点菜单之前


def test_capture_switch_failed_reason_carried(monkeypatch):
    """通道全败 → PANEL_SWITCH_FAILED 且 status 原样带出（排障面）。"""
    state, calls = _patch_fills_capture_env(
        monkeypatch, start_label="广发证券-王*美")

    def fake_ensure(w, cfg):
        calls["ensure_before_gate"] = True
        return {"status": "switch_failed", "identity_label": state["label"],
                "reason": "all_channels_failed",
                "attempts": [{"channel": "hotkey_alt_n", "ok": False,
                              "reason": "foreground_failed"}]}

    monkeypatch.setattr(hs, "ensure_target_account", fake_ensure)
    with pytest.raises(RuntimeError) as ei:
        tc._capture_ths_views(None, panel=None, account="acct1",
                              identity_hint="曾", hotswitch=_hotswitch_cfg())
    assert "PANEL_SWITCH_FAILED" in str(ei.value)


# ── ⑤ 计划构建（通道链 + 热键；与 r49 文件的断言互补）─────────────

def test_plan_hotswitch_entry_carries_channels_and_hotkeys(tmp_path):
    reg = bridge.PanelRegistry(tmp_path / "ths_panels.json")
    reg.bind(account="acct1", broker_hint="广发证券",
             rule={"process": "xiadan.exe",
                   "title_contains": "网上股票交易系统"},
             identity_hint="曾", hotkey="1")
    reg.bind(account="acct2", broker_hint="广发证券",
             rule={"process": "xiadan.exe",
                   "title_contains": "网上股票交易系统"},
             identity_hint="王", hotkey="2")
    live = [{"pid": 101, "process": "xiadan.exe",
             "title": "网上股票交易系统5.0", "hwnd": "0x1",
             "rect": [0, 0, 900, 700]}]
    plan = hs.build_hotswitch_plan(reg, live)
    sw1 = plan["acct1"]["hotswitch"]
    sw2 = plan["acct2"]["hotswitch"]
    assert sw1["channels"] == [hs.CHANNEL_HOTKEY, hs.CHANNEL_COMBOBOX]
    assert (sw1["target_hotkey"], sw1["home_hotkey"]) == ("1", "1")
    assert (sw2["target_hotkey"], sw2["home_hotkey"]) == ("2", "1")
    assert sw2["restore_after"] is True and sw1["restore_after"] is False


def test_plan_without_hotkey_rules_keeps_legacy_semantics(tmp_path):
    reg = bridge.PanelRegistry(tmp_path / "ths_panels.json")
    reg.bind(account="acct2", broker_hint="XX证券",
             rule={"process": "xxbroker.exe",
                   "title_contains": "XX证券网上交易系统"})
    live = [{"pid": 101, "process": "xiadan.exe",
             "title": "网上股票交易系统5.0", "hwnd": "0x1",
             "rect": [0, 0, 900, 700]},
            {"pid": 202, "process": "xxbroker.exe",
             "title": "XX证券网上交易系统", "hwnd": "0x2",
             "rect": [0, 0, 900, 700]}]
    plan = hs.build_hotswitch_plan(reg, live)
    assert "hotswitch" not in (plan.get("acct2") or {})
    assert (plan.get("acct2") or {}).get("panel", {}).get("pid") == 202

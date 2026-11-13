# -*- coding: utf-8 -*-
"""R51 身份门禁加固回归（2026-11-13 实盘根因）。

实盘事实链（2026-11-13 19:52，xiadan pid 35584/hexin pid 37824 均为当日
16:26 重启）：面板停在登录框（可见 #32770：证券公司/站点列表/主板帐户
160100087340/交易密码空/保存密码✓/登录钮），标题壳窗「网上股票交易系统5.0」
最小化在场但骨架渲染——顶栏「资产：可用：当日：」无值、无「券商-姓名」chip、
账号切换器 ComboBox(aid=2322) 空。ensure_target_account/_capture_ths_views
双路径读身份恒 label='' → 误报 PANEL_IDENTITY_MISMATCH label=''。

本文件锁定四层修复的行为契约：
  ① 还原后再读身份（_capture_ths_views 流程内，见 ④ 用例的零点击断言）；
  ② 多锚读取决策 gate_identity_decision（OCR 主锚 + ComboBox 备用锚）；
  ③ 单账号允许备用锚降级验证；双账号 fail-closed；
  ④ 登录框在 → PANEL_LOGIN_DIALOG 真原因 fail-closed（点菜单之前拦截）；
  ⑤ ths_xiadan_client：壳窗+可见登录框 → 不再误报 logged_in。
全部 mock，零真机依赖。
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


# ── ②③ 纯函数决策矩阵 ─────────────────────────────────────────

def test_gate_no_hint_disables_gate():
    from ths_client import gate_identity_decision
    assert gate_identity_decision("", "", None, allow_combo_fallback=False) \
        == (True, "no_hint", "")


def test_gate_ocr_primary_anchor_match():
    from ths_client import gate_identity_decision
    assert gate_identity_decision("广发证券-曾*", "", "曾",
                                  allow_combo_fallback=False) \
        == (True, "ocr", "广发证券-曾*")


def test_gate_ocr_read_other_identity_no_fallback_saves():
    """主锚读到他人身份 = 明确换登，任何降级不救（fail-closed）。"""
    from ths_client import gate_identity_decision
    ok, reason, label = gate_identity_decision(
        "广发证券-王*美", "广发证券-曾*", "曾", allow_combo_fallback=True)
    assert (ok, reason, label) == (False, "mismatch", "广发证券-王*美")


def test_gate_ocr_empty_single_account_combo_fallback_passes():
    """主锚空（渲染缺层/OCR 波动）+ 单账号 + 备用锚读到目标 → 放行。"""
    from ths_client import gate_identity_decision
    assert gate_identity_decision("", "广发证券-曾*", "曾",
                                  allow_combo_fallback=True) \
        == (True, "combo_fallback", "广发证券-曾*")


def test_gate_ocr_empty_dual_account_stays_fail_closed():
    """双账号采集：备用锚读到目标账号也不降级（PM ③ fail-closed 语义）。"""
    from ths_client import gate_identity_decision
    ok, reason, _ = gate_identity_decision("", "广发证券-曾*", "曾",
                                           allow_combo_fallback=False)
    assert (ok, reason) == (False, "dual_account_fail_closed")


def test_gate_both_anchors_empty_fail_closed():
    """双锚皆空（登录框态骨架窗）→ fail-closed（真原因由登录框检测先报）。"""
    from ths_client import gate_identity_decision
    assert gate_identity_decision("", "", "曾", allow_combo_fallback=True)[:2] \
        == (False, "unreadable")


def test_gate_combo_reads_other_identity_rejected():
    from ths_client import gate_identity_decision
    ok, reason, _ = gate_identity_decision("", "广发证券-王*美", "曾",
                                           allow_combo_fallback=True)
    assert (ok, reason) == (False, "mismatch")


def test_assert_robust_raises_with_dual_anchor_evidence():
    from ths_client import assert_panel_identity_robust
    with pytest.raises(RuntimeError, match="PANEL_IDENTITY_MISMATCH") as ei:
        assert_panel_identity_robust(
            {"ocr_label": "", "combo_text": ""}, "曾", "acct1",
            allow_combo_fallback=True)
    assert "combo=''" in str(ei.value) and "reason=unreadable" in str(ei.value)


# ── ④ 登录框 fail-closed：点菜单之前拦截，零交互零采集 ──────────

@contextmanager
def _null_lock(**_kw):
    yield


class _FakeWindow:
    handle = 4242

    def is_minimized(self) -> bool:
        return False


def test_capture_login_dialog_fails_closed_before_any_menu_click(
        monkeypatch, tmp_path):
    """登录框在 → PANEL_LOGIN_DIALOG（真原因），绝不点击查询菜单。"""
    from services import ths_account_bridge as bridge
    import ths_client as fx

    clicked: list[str] = []
    monkeypatch.setattr(bridge, "_find_xiadan_window", lambda: _FakeWindow())
    monkeypatch.setattr(
        bridge, "detect_xiadan_login_dialog",
        lambda _w: {"dialog_hwnd": 6962218, "account": "160100087340",
                    "password_nonempty": False})
    monkeypatch.setattr(bridge, "_ths_window_operation_lock", _null_lock)
    monkeypatch.setattr(bridge, "_click_query_menu_item",
                        lambda item: clicked.append(item))

    with pytest.raises(RuntimeError, match="PANEL_LOGIN_DIALOG") as ei:
        fx._capture_ths_views(evidence_dir=tmp_path, panel=None,
                              account="acct1", identity_hint="曾")
    assert "未登录" in str(ei.value)
    assert clicked == []  # 门禁前置：登录框态零菜单交互


def test_capture_identity_gate_runs_before_menu_clicks(monkeypatch, tmp_path):
    """健康路径：多锚门禁在读身份处放行/拦截，均先于查询菜单点击。"""
    from services import ths_account_bridge as bridge
    import ths_client as fx

    clicked: list[str] = []
    monkeypatch.setattr(bridge, "_find_xiadan_window", lambda: _FakeWindow())
    monkeypatch.setattr(bridge, "detect_xiadan_login_dialog", lambda _w: None)
    monkeypatch.setattr(bridge, "_ths_window_operation_lock", _null_lock)
    monkeypatch.setattr(bridge, "_click_query_menu_item",
                        lambda item: clicked.append(item))

    import services.ths_hotswitch as hs
    monkeypatch.setattr(
        hs, "read_window_identity_robust",
        lambda _w, save_png=None: {"ocr_label": "", "combo_text": "广发证券-王*美",
                                   "screenshot": None})

    with pytest.raises(RuntimeError, match="PANEL_IDENTITY_MISMATCH"):
        fx._capture_ths_views(evidence_dir=tmp_path, panel=None,
                              account="acct1", identity_hint="曾")
    assert clicked == []  # 换登拦截也发生在任何菜单点击之前


def test_run_export_login_dialog_writes_unavailable_marker(tmp_path):
    """run_export 层：PANEL_LOGIN_DIALOG → unavailable 标记（真原因），无 fills。"""
    import ths_client as fx

    def bad_capture(_ev):
        raise RuntimeError(
            "PANEL_LOGIN_DIALOG: 面板停在登录框（未登录） account_field='160100087340' "
            "password_remembered=False expect~'曾' target_account=acct1")

    result = fx.run_export(for_date=date(2026, 11, 13),
                           now=datetime(2026, 11, 13, 20, 0),
                           out_root=tmp_path, account="acct1",
                           capture=bad_capture)
    assert result["status"] == "unavailable"
    assert "PANEL_LOGIN_DIALOG" in result["reason"]
    marker = json.loads((tmp_path / "fills" / "20261113.unavailable.json")
                        .read_text(encoding="utf-8"))
    assert "PANEL_LOGIN_DIALOG" in marker["reason"]
    assert not (tmp_path / "fills" / "20261113.json").exists()


# ── ⑤ ths_xiadan_client：壳窗 + 可见登录框 ≠ 已登录 ─────────────

def _client_harness(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import test_ths_xiadan_client as harness  # 复用已验收的 FakeWindows 桩
    fake, pids = harness.FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    harness.install(monkeypatch, fake, pids)
    return fake, harness


def test_client_shell_window_plus_visible_login_box_not_logged_in(
        monkeypatch, tmp_path):
    """2026-11-13 19:51 实盘复刻：标题壳窗在 + 可见登录框（密码空）。

    修复前：误报 logged_in → 导出门禁 label=''；修复后：need_manual_login。
    """
    fake, harness = _client_harness(monkeypatch, tmp_path)
    harness._main_window(fake, pid=200, hwnd=400)          # 壳窗（隐藏也算）
    harness._login_box(fake, pid=200, password_len=0)      # 可见登录框、密码空
    report = harness.ths.run(hexin_exe=tmp_path / "hexin.exe",
                             xiadan_exe=tmp_path / "xiadan.exe",
                             export=False, report_root=tmp_path / "rt",
                             **harness.FAST)
    assert report["status"] == "need_manual_login"
    assert report["credentials_touched"] is False


def test_client_shell_window_plus_login_box_remembered_password_clicks(
        monkeypatch, tmp_path):
    """壳窗 + 登录框（密码记住）→ 点登录到主窗口（既有授权路径不受影响）。"""
    fake, harness = _client_harness(monkeypatch, tmp_path)
    harness._main_window(fake, pid=200, hwnd=400)
    login_hwnd = harness._login_box(fake, pid=200, password_len=6)
    harness._main_window(fake, pid=200, hwnd=500)  # 点击后新壳窗（对话框移除）
    fake.on_click = lambda f, h: (f.remove(login_hwnd), f.remove(400)) and None
    report = harness.ths.run(hexin_exe=tmp_path / "hexin.exe",
                             xiadan_exe=tmp_path / "xiadan.exe",
                             export=False, report_root=tmp_path / "rt",
                             **harness.FAST)
    assert report["status"] == "logged_in"
    assert report["login_state"] == "logged_in_after_click"


def test_client_error_popup_with_shell_window_still_logged_in(
        monkeypatch, tmp_path):
    """错误弹窗（确定钮、无密码框）不构成登录框 → 仍判已登录。"""
    fake, harness = _client_harness(monkeypatch, tmp_path)
    harness._main_window(fake, pid=200, hwnd=400)
    ok_btn = fake.add(311, cls="Button", pid=200, title="确定")
    fake.add(310, cls="#32770", pid=200, title="", children=[ok_btn])
    report = harness.ths.run(hexin_exe=tmp_path / "hexin.exe",
                             xiadan_exe=tmp_path / "xiadan.exe",
                             export=False, report_root=tmp_path / "rt",
                             **harness.FAST)
    assert report["status"] == "logged_in"

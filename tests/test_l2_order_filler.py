# -*- coding: utf-8 -*-
"""R50 Phase 2 填单函单测——全 mock，不碰真实面板。

覆盖场景：
  1. panel_map.json 加载：缺文件/解析错/schema 不符/status!=ok -> fail-closed
  2. submit_buttons 存在性验证（买入[F1]/卖出[F2]）
  3. 窗口查找：pywinauto 不可用/窗口未找到 -> fail-closed
  4. 前台占用 -> skipped_foreground
  5. 账号不匹配 -> 调用 ths_hotswitch（mock 成功/失败）
  6. 左侧导航选择（成功/控件未找到）
  7. Edit 输入框（查找/标识/填入/读回）
  8. fill_and_verify 额外读回核对 + 截图
  9. 零 Invoke 断言（全 mock 但审计面板_map 标记不被误用）
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import ANY, MagicMock, PropertyMock, call, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import l2_order_filler as filler  # noqa: E402


# ──  fixtures ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_pywinauto():
    """在每个测试前将 filler.pywinauto 替换为 mock 对象。

    filler 在 import 时从 try/except 拿到 _PYWINSUCCESS=False，
    测试代码需 patch _PYWINSUCCESS=True 才能进入真实分支。
    本 fixture 确保 filler.pywinauto 属性存在（供 patch.object 使用）。
    """
    m = MagicMock()
    m.findwindows.find_windows.return_value = []
    m.Application = MagicMock()
    m.Application.return_value.connect.return_value = MagicMock()
    m.findwindows.ElementNotFoundError = type("ElementNotFound", (Exception,), {})
    m.findwindows.ElementAmbiguousError = type("ElementAmbiguous", (Exception,), {})
    with patch.object(filler, "pywinauto", m, create=True):
        yield


# ── 辅助构造 ───────────────────────────────────────────────


def _make_panel_map(
    status: str = "ok",
    account: str = "acct1",
    identity_label: str = "广发证券-曾*",
    schema: str = "l2_panel_map/1",
    hwnd: str = "0xc111e4",
    submit_buttons: list | None = None,
) -> dict:
    """构造合法的 panel_map.json 数据结构。"""
    sb = submit_buttons or [
        {
            "path": "/2/1/1/0/0/1",
            "automation_id": "",
            "name": "买入[F1]",
            "class_name": "",
            "control_type": "树项目",
            "rect": [416, 293, 465, 321],
            "patterns": {"selection_item": {"selected": False}},
            "enabled": True,
            "invoke_forbidden_phase1": True,
            "note": "Phase1 绝不 Invoke",
        },
        {
            "path": "/2/1/1/0/0/2",
            "automation_id": "",
            "name": "卖出[F2]",
            "class_name": "",
            "control_type": "树项目",
            "rect": [416, 321, 465, 349],
            "patterns": {"selection_item": {"selected": False}},
            "enabled": True,
            "invoke_forbidden_phase1": True,
            "note": "Phase1 绝不 Invoke",
        },
    ]
    return {
        "schema": schema,
        "status": status,
        "surveyed_at": "2026-11-11 23:21:51",
        "panel": {
            "pid": 32520,
            "process": "xiadan.exe",
            "title": "网上股票交易系统5.0",
            "hwnd": hwnd,
            "rect": [388, 210, 1381, 964],
        },
        "identity_label": identity_label,
        "account": account,
        "walker": "control",
        "node_count": 282,
        "was_minimized": False,
        "restored_to_minimized": False,
        "iron_law": {
            "zero_input": True,
            "zero_invoke": True,
            "zero_click": True,
            "zero_trade": True,
            "submit_buttons_never_invoke_phase1": True,
        },
        "tree": {
            "automation_id": "",
            "name": "网上股票交易系统5.0",
            "class_name": "Afx:004A0000:b:00010003:00000006:74BD1569",
            "control_type": 50032,
            "localized_type": "窗口",
            "rect": [388, 210, 1381, 964],
            "enabled": True,
            "offscreen": False,
            "keyboard_focusable": True,
            "runtime_id": [],
            "patterns": {},
            "is_password": False,
            "children": [],
        },
        "submit_buttons": sb,
        "sibling_dialogs": [],
        "screenshots": ["panel_full_232150.png"],
        "next_phase_note": "Phase2 填单演练须 Owner 在场",
    }


def _mock_edit(name: str = "", aid: str = "", value: str = ""):
    """构造 mock EditWrapper。"""
    m = MagicMock()
    info = MagicMock()
    info.automation_id = aid
    info.name = name
    type(m).element_info = PropertyMock(return_value=info)
    m.get_value.return_value = value
    m.window_text.return_value = value
    m.set_edit_text.return_value = None
    return m


# ════════════════════════════════════════════════════════════
# A. panel_map 加载与验证
# ════════════════════════════════════════════════════════════


class TestLoadPanelMap:
    def test_missing_file(self):
        with patch.object(filler, "PANEL_MAP",
                          Path("/nonexistent/panel_map.json")):
            with pytest.raises(RuntimeError, match="不存在"):
                filler.load_panel_map()

    def test_bad_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        with patch.object(filler, "PANEL_MAP", p):
            with pytest.raises(RuntimeError, match="解析失败"):
                filler.load_panel_map()

    def test_wrong_schema(self, tmp_path):
        p = tmp_path / "bad_schema.json"
        pm = _make_panel_map(schema="l2_panel_map/9")
        p.write_text(json.dumps(pm), encoding="utf-8")
        with patch.object(filler, "PANEL_MAP", p):
            with pytest.raises(RuntimeError, match="schema 不符"):
                filler.load_panel_map()

    @pytest.mark.parametrize(
        "bad_status",
        ["panel_absent", "skipped_foreground",
         "identity_mismatch", "switch_failed"],
    )
    def test_non_ok_status_rejected(self, tmp_path, bad_status):
        p = tmp_path / f"bad_{bad_status}.json"
        pm = _make_panel_map(status=bad_status)
        p.write_text(json.dumps(pm), encoding="utf-8")
        with patch.object(filler, "PANEL_MAP", p):
            with pytest.raises(RuntimeError, match="非 ok"):
                filler.load_panel_map()

    def test_happy_load(self, tmp_path):
        p = tmp_path / "ok.json"
        pm = _make_panel_map()
        p.write_text(json.dumps(pm), encoding="utf-8")
        with patch.object(filler, "PANEL_MAP", p):
            result = filler.load_panel_map()
        assert result["account"] == "acct1"
        assert result["identity_label"] == "广发证券-曾*"
        assert len(result["submit_buttons"]) == 2


# ════════════════════════════════════════════════════════════
# B. submit_buttons 验证（导航项必须存在于 panel_map）
# ════════════════════════════════════════════════════════════


class TestSubmitButtons:
    def test_find_buy(self):
        pm = _make_panel_map()
        sb = filler.get_submit_buttons(pm)
        buy = filler._find_submit_button(sb, "买入[F1]")
        assert buy is not None
        assert buy["name"] == "买入[F1]"

    def test_find_sell(self):
        pm = _make_panel_map()
        sb = filler.get_submit_buttons(pm)
        sell = filler._find_submit_button(sb, "卖出[F2]")
        assert sell is not None
        assert sell["name"] == "卖出[F2]"

    def test_buy_has_selection_item(self):
        pm = _make_panel_map()
        sb = filler.get_submit_buttons(pm)
        buy = filler._find_submit_button(sb, "买入[F1]")
        pat = buy.get("patterns", {})
        assert "selection_item" in pat

    def test_missing_nav_rejected(self):
        pm = _make_panel_map(submit_buttons=[
            {
                "name": "卖出[F2]",
                "control_type": "树项目",
                "patterns": {"selection_item": {"selected": False}},
            }
        ])
        with patch.object(filler, "load_panel_map", return_value=pm):
            with patch.object(filler, "_PYWINSUCCESS", True):
                result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert not result["success"]
        assert "未找到" in result["error"]


# ════════════════════════════════════════════════════════════
# C. 窗口 / 前台
# ════════════════════════════════════════════════════════════


class TestWindowChecks:
    def test_pywinauto_unavailable(self):
        with patch.object(filler, "_PYWINSUCCESS", False):
            result = filler.fill_order("acct1", "002015", 13.50, 100)
        assert not result["success"]
        assert "pywinauto 不可用" in result["error"]

    def test_window_not_found(self):
        pm = _make_panel_map()
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(None, "窗口未找到 测试模拟")),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100)
        assert not result["success"]
        assert "未找到" in result["error"]

    def test_foreground_occupied(self):
        pm = _make_panel_map()
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground",
                         return_value="用户正在操作面板（前台命中），跳过 fill_order"),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100)
        assert not result["success"]
        assert "前台命中" in result["error"]


# ════════════════════════════════════════════════════════════
# D. 账号切换
# ════════════════════════════════════════════════════════════


class TestAccountSwitch:
    def test_account_mismatch_triggers_hotswitch(self):
        pm = _make_panel_map(account="acct2",
                             identity_label="广发证券-王*美")
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=[_mock_edit()] * 3),
            patch.object(filler, "_identify_edits",
                         return_value={
                             "code": _mock_edit(value="002015"),
                             "price": _mock_edit(value="13.50"),
                             "qty": _mock_edit(value="100"),
                         }),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_switch_account_if_needed") as mock_sw,
        ):
            mock_sw.return_value = None
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert result["success"]
        mock_sw.assert_called_once()

    def test_account_switch_failure(self):
        pm = _make_panel_map(account="acct2",
                             identity_label="广发证券-王*美")
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed") as mock_sw,
        ):
            mock_sw.return_value = "账号切换失败 status=switch_failed"
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert not result["success"]
        assert "切换失败" in result["error"]


# ════════════════════════════════════════════════════════════
# E. 左侧导航
# ════════════════════════════════════════════════════════════


class TestNavSelection:
    def test_nav_item_with_invoke_marked_forbidden(self):
        pm = _make_panel_map()
        for sb in pm["submit_buttons"]:
            patterns = sb.get("patterns", {})
            invoke = patterns.get("invoke", {})
            assert "selection_item" in patterns
            if invoke:
                assert invoke.get("invoke_forbidden_phase1") is True

    def test_nav_selection_failure(self):
        pm = _make_panel_map()
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item",
                         return_value="导航项 买入[F1] 未找到"),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert not result["success"]
        assert "未找到" in result["error"]


# ════════════════════════════════════════════════════════════
# F. 完整填写成功路径
# ════════════════════════════════════════════════════════════


class TestFillSuccess:
    def test_happy_path_buy(self):
        pm = _make_panel_map()
        edits = [
            _mock_edit(name="证券代码", value="002015"),
            _mock_edit(name="委托价格", value="13.50"),
            _mock_edit(name="委托数量", value="100"),
        ]
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=edits),
            patch.object(filler, "_ensure_restored", return_value=False),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert result["success"]
        assert result["filled"]["code"] == "002015"
        assert result["filled"]["price"] == "13.50"
        assert result["filled"]["qty"] == "100"
        assert result["filled"]["side"] == "买入"
        assert result["filled"]["account"] == "acct1"
        for ed in edits:
            ed.set_edit_text.assert_any_call("")

    def test_happy_path_sell(self):
        pm = _make_panel_map()
        edits = [
            _mock_edit(name="证券代码", value="603881"),
            _mock_edit(name="委托价格", value="20.60"),
            _mock_edit(name="委托数量", value="200"),
        ]
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=edits),
            patch.object(filler, "_ensure_restored", return_value=False),
        ):
            result = filler.fill_order("acct2", "603881", 20.60, 200, "sell")
        assert result["success"]
        assert result["filled"]["side"] == "卖出"
        assert result["filled"]["account"] == "acct2"

    def test_missing_edit_controls(self):
        pm = _make_panel_map()
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls", return_value=None),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert not result["success"]
        assert "Edit" in result["error"]

    def test_edit_fill_failure(self):
        pm = _make_panel_map()
        edits = [
            _mock_edit(name="证券代码", value="002015"),
            _mock_edit(name="委托价格", value="13.50"),
            _mock_edit(name="委托数量", value="100"),
        ]
        edits[1].set_edit_text.side_effect = RuntimeError("write blocked")
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=edits),
            patch.object(filler, "_ensure_restored", return_value=False),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert not result["success"]
        assert "填入部分失败" in result["error"]

    def test_edit_identification_by_name(self):
        edits = [
            _mock_edit(name="证券代码", value="000001"),
            _mock_edit(name="委托价格", value="10.00"),
            _mock_edit(name="委托数量", value="500"),
        ]
        labeled = filler._identify_edits(edits)
        assert "code" in labeled
        assert "price" in labeled
        assert "qty" in labeled
        assert labeled["code"].element_info.name == "证券代码"
        assert labeled["price"].element_info.name == "委托价格"
        assert labeled["qty"].element_info.name == "委托数量"

    def test_edit_identification_position_fallback(self):
        edits = [
            _mock_edit(name="", value="000001"),
            _mock_edit(name="", value="10.00"),
            _mock_edit(name="", value="500"),
        ]
        labeled = filler._identify_edits(edits)
        assert "code" in labeled
        assert "price" in labeled
        assert "qty" in labeled


# ════════════════════════════════════════════════════════════
# G. fill_and_verify
# ════════════════════════════════════════════════════════════


class TestFillAndVerify:
    def test_verify_match(self):
        pm = _make_panel_map()
        edits = [
            _mock_edit(name="证券代码", value="002015"),
            _mock_edit(name="委托价格", value="13.50"),
            _mock_edit(name="委托数量", value="100"),
        ]
        mock_img = MagicMock()
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_PILSUCCESS", True),
            patch.object(filler, "ImageGrab", mock_img, create=True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=edits),
            patch.object(filler, "_ensure_restored", return_value=False),
        ):
            mock_img.grab.return_value.save.return_value = None
            result = filler.fill_and_verify("acct1", "002015", 13.50, 100, "buy")
        assert result["success"]
        assert "screenshot" in result
        assert result.get("verify", {}).get("all_match") is True

    def test_verify_mismatch(self):
        pm = _make_panel_map()
        edits = [
            _mock_edit(name="证券代码", value="999999"),
            _mock_edit(name="委托价格", value="13.50"),
            _mock_edit(name="委托数量", value="100"),
        ]
        mock_img = MagicMock()
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_PILSUCCESS", True),
            patch.object(filler, "ImageGrab", mock_img, create=True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=edits),
            patch.object(filler, "_ensure_restored", return_value=False),
        ):
            mock_img.grab.return_value.save.return_value = None
            result = filler.fill_and_verify("acct1", "002015", 13.50, 100, "buy")
        assert result["success"]
        assert result.get("verify", {}).get("all_match") is False


# ════════════════════════════════════════════════════════════
# H. 零 Invoke 断言
# ════════════════════════════════════════════════════════════


class TestZeroInvoke:
    def test_source_has_no_invoke_calls(self):
        src = Path(filler.__file__).read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), 1):
            stripped = line.split("#")[0]
            if ".invoke(" in stripped.lower() and "invoke_forbidden" not in stripped:
                pytest.fail(
                    f"第 {lineno} 行含有 .invoke( 调用: {line.strip()}"
                )

    def test_fill_order_never_calls_click_or_invoke(self):
        pm = _make_panel_map()
        edits = [
            _mock_edit(name="证券代码", value="002015"),
            _mock_edit(name="委托价格", value="13.50"),
            _mock_edit(name="委托数量", value="100"),
        ]
        invoked_calls: list[str] = []

        class SpyMagic(MagicMock):
            def __getattr__(self, name):
                if name.lower() in ("click", "invoke"):
                    invoked_calls.append(name)
                return super().__getattr__(name)

        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(SpyMagic(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=edits),
            patch.object(filler, "_ensure_restored", return_value=False),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert result["success"]
        assert not invoked_calls, (
            f"fill_order 不应调用 click/invoke，实际调用了: {invoked_calls}"
        )


# ════════════════════════════════════════════════════════════
# I. 账号切换模块调用验证
# ════════════════════════════════════════════════════════════


class TestSwitchAccountLogic:
    def _patch_hs(self):
        """Inject mock ths_hotswitch as a submodule of services."""
        hs_mock = MagicMock()
        # Create services module with ths_hotswitch as attribute
        svc_mock = MagicMock(spec=[])
        svc_mock.ths_hotswitch = hs_mock
        return patch.dict("sys.modules", {
            "services": svc_mock,
            "services.ths_hotswitch": hs_mock,
        }), hs_mock

    def test_switch_account_same_account_skipped(self):
        pm = _make_panel_map(account="acct1")
        ctx, hs_mock = self._patch_hs()
        with ctx:
            err = filler._switch_account_if_needed(pm, "acct1")
        assert err is None
        hs_mock.ensure_target_account.assert_not_called()

    def test_switch_account_different_calls_hs(self):
        pm = _make_panel_map(account="acct2", identity_label="广发证券-王*美")
        ctx, hs_mock = self._patch_hs()
        hs_mock.ensure_target_account.return_value = None
        with ctx:
            err = filler._switch_account_if_needed(pm, "acct1")
        assert err is None
        hs_mock.ensure_target_account.assert_called_once()
        assert pm["account"] == "acct1"
        assert pm["identity_label"] == "广发证券-曾*"

    def test_switch_account_hs_failure(self):
        pm = _make_panel_map(account="acct2", identity_label="广发证券-王*美")
        ctx, hs_mock = self._patch_hs()
        hs_mock.ensure_target_account.return_value = {
            "status": "switch_failed",
            "reason": "ocr mismatch",
        }
        with ctx:
            err = filler._switch_account_if_needed(pm, "acct1")
        assert err is not None
        assert "switch_failed" in err
        hs_mock.ensure_target_account.assert_called_once()

    def test_switch_account_hs_unavailable(self):
        """ths_hotswitch 模块不可用时返回错误。"""
        pm = _make_panel_map(account="acct2")
        svc_mock = MagicMock(spec=[])
        del svc_mock.ths_hotswitch  # remove attribute to trigger AttributeError
        with patch.dict("sys.modules", {
            "services": svc_mock,
        }):
            err = filler._switch_account_if_needed(pm, "acct1")
        assert err is not None
        assert "不可用" in err


# ════════════════════════════════════════════════════════════
# J. 最小化恢复
# ════════════════════════════════════════════════════════════


class TestMinimizedWindow:
    def test_minimized_restored_on_exit(self):
        pm = _make_panel_map()
        edits = [
            _mock_edit(name="证券代码", value="002015"),
            _mock_edit(name="委托价格", value="13.50"),
            _mock_edit(name="委托数量", value="100"),
        ]
        window_calls: list[int] = []

        def track_show(hwnd, cmd):
            window_calls.append(cmd)

        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=edits),
            patch.object(filler, "_ensure_restored", return_value=True),
            patch.object(filler, "_show_window", side_effect=track_show),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert result["success"]
        min_calls = [c for c in window_calls if c == 6]
        assert len(min_calls) >= 1, "exit 时未恢复最小化"


# ════════════════════════════════════════════════════════════
# K. fill_order 返回结构完整性
# ════════════════════════════════════════════════════════════


class TestResultStructure:
    def test_result_has_schema(self):
        pm = _make_panel_map()
        edits = [
            _mock_edit(name="证券代码", value="X"),
            _mock_edit(name="委托价格", value="Y"),
            _mock_edit(name="委托数量", value="Z"),
        ]
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=edits),
            patch.object(filler, "_ensure_restored", return_value=False),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert result["schema"] == filler.FILL_SCHEMA
        assert "ts" in result
        assert "steps" in result
        assert isinstance(result["steps"], list)

    def test_steps_are_descriptive(self):
        pm = _make_panel_map()
        edits = [
            _mock_edit(name="证券代码", value="X"),
            _mock_edit(name="委托价格", value="Y"),
            _mock_edit(name="委托数量", value="Z"),
        ]
        with (
            patch.object(filler, "load_panel_map", return_value=pm),
            patch.object(filler, "_PYWINSUCCESS", True),
            patch.object(filler, "_find_window",
                         return_value=(MagicMock(), None)),
            patch.object(filler, "_check_foreground", return_value=None),
            patch.object(filler, "_switch_account_if_needed",
                         return_value=None),
            patch.object(filler, "_select_nav_item", return_value=None),
            patch.object(filler, "_find_edit_controls",
                         return_value=edits),
            patch.object(filler, "_ensure_restored", return_value=False),
        ):
            result = filler.fill_order("acct1", "002015", 13.50, 100, "buy")
        assert result["success"]
        step_text = " ".join(result["steps"])
        assert "panel_map" in step_text
        assert "买入[F1]" in step_text or "导航" in step_text
        assert "Edit" in step_text
        assert "未提交" in step_text
# -*- coding: utf-8 -*-
"""R50 L2 面板勘察脚本单测（纯逻辑 + fail-closed 分支，零真实面板依赖）。

覆盖：源码自审计（干净/抓到危险调用）、提交类按钮分类（今晚绝不 Invoke
标注）、flatten 路径、panel_absent / skipped_foreground / identity_mismatch
三个 fail-closed 出口的落盘与退出码。
"""
from __future__ import annotations

import contextlib
import importlib
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

l2ps = importlib.import_module("scripts.l2_panel_survey")


def _btn(name: str, ctl: str = "按钮", invoke: bool = True) -> dict:
    return {"name": name, "localized_type": ctl, "automation_id": "",
            "class_name": "Button", "rect": [1, 2, 3, 4],
            "patterns": ({"invoke": {"available": True}} if invoke else {}),
            "enabled": True}


def _repoint_outdir(monkeypatch, tmp_path: Path) -> Path:
    out = tmp_path / "l2_r50_test"
    monkeypatch.setattr(l2ps, "OUT_DIR", out)
    monkeypatch.setattr(l2ps, "PANEL_MAP", out / "panel_map.json")
    monkeypatch.setattr(l2ps, "JOURNAL", out / "journal.md")
    return out


# ── 1) 源码自审计 ──────────────────────────────────────────────────

def test_self_audit_clean():
    l2ps._self_audit()   # 不抛即过


def test_self_audit_catches_injected_primitive(tmp_path, monkeypatch):
    src = Path(l2ps.__file__).read_text(encoding="utf-8")
    bad = tmp_path / "l2_panel_survey_bad.py"
    bad.write_text(src + "\nx." + "invoke" + "(1)\n", encoding="utf-8")
    monkeypatch.setattr(l2ps, "__file__", str(bad))
    with pytest.raises(RuntimeError, match="self-audit FAILED"):
        l2ps._self_audit()


# ── 2) 提交类按钮分类 ──────────────────────────────────────────────

def test_classify_submit_buttons_marks_all_never_invoke():
    tree = {"name": "root", "children": [
        _btn("买入[B100]"),
        _btn("卖出[S100]"),
        _btn("撤单"),
        _btn("查询"),
        _btn("刷新"),
        {"name": "证券代码", "localized_type": "编辑",
         "patterns": {"value": {"read_only": False}}, "children": []},
    ]}
    submits = l2ps.classify_submit_buttons(l2ps.flatten(tree))
    names = [s["name"] for s in submits]
    assert "买入[B100]" in names and "卖出[S100]" in names and "撤单" in names
    assert "查询" not in names and "刷新" not in names
    assert all(s["invoke_forbidden_phase1"] is True for s in submits)
    assert all(s["invoke_available"] is True for s in submits)
    # 非按钮控件绝不入清单
    assert all("编辑" != s["control_type"] for s in submits)


def test_flatten_paths_dfs_order():
    tree = {"name": "r", "children": [
        {"name": "a", "children": [{"name": "a0", "children": []}]},
        {"name": "b", "children": []},
    ]}
    got = {path: n["name"] for n, path in l2ps.flatten(tree)}
    assert got == {"": "r", "/0": "a", "/0/0": "a0", "/1": "b"}


# ── 3) fail-closed 出口 ────────────────────────────────────────────

def _fake_panel(hwnd: int = 0x1234, pid: int = 999) -> dict:
    return {"hwnd": hex(hwnd), "pid": pid, "process": "xiadan.exe",
            "title": "网上股票交易系统5.0", "rect": [10, 10, 800, 600]}


def test_panel_absent_rc2_map(monkeypatch, tmp_path):
    out = _repoint_outdir(monkeypatch, tmp_path)
    monkeypatch.setattr(l2ps.bridge, "enumerate_trade_panels",
                        lambda include_minimized=False: [])
    assert l2ps.main([]) == 2
    data = json.loads((out / "panel_map.json").read_text(encoding="utf-8"))
    assert data["status"] == "panel_absent"
    assert data["schema"] == l2ps.PANEL_MAP_SCHEMA
    assert (out / "journal.md").exists()


def test_ambiguous_panels_rc6(monkeypatch, tmp_path):
    out = _repoint_outdir(monkeypatch, tmp_path)
    monkeypatch.setattr(l2ps.bridge, "enumerate_trade_panels",
                        lambda include_minimized=False:
                        [_fake_panel(0x11), _fake_panel(0x22)])
    assert l2ps.main([]) == 6
    data = json.loads((out / "panel_map.json").read_text(encoding="utf-8"))
    assert data["status"] == "ambiguous_panels"


def test_foreground_skip_rc3_zero_touch(monkeypatch, tmp_path):
    out = _repoint_outdir(monkeypatch, tmp_path)
    monkeypatch.setattr(l2ps.bridge, "enumerate_trade_panels",
                        lambda include_minimized=False: [_fake_panel()])
    monkeypatch.setattr(l2ps.bridge, "_ths_window_operation_lock",
                        lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(l2ps.poll, "_is_foreground", lambda hwnd: True)
    assert l2ps.main([]) == 3
    data = json.loads((out / "panel_map.json").read_text(encoding="utf-8"))
    assert data["status"] == "skipped_foreground"


def test_identity_mismatch_rc4_zero_touch(monkeypatch, tmp_path):
    """未知身份（非曾/王）→ 零 UIA 采集零触碰退出。"""
    out = _repoint_outdir(monkeypatch, tmp_path)
    monkeypatch.setattr(l2ps.bridge, "enumerate_trade_panels",
                        lambda include_minimized=False: [_fake_panel()])
    monkeypatch.setattr(l2ps.bridge, "_ths_window_operation_lock",
                        lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(l2ps.poll, "_is_foreground", lambda hwnd: False)
    monkeypatch.setattr(l2ps, "_is_iconic", lambda hwnd: False)
    monkeypatch.setattr(l2ps.hotswitch, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-张三", None))
    called = []
    monkeypatch.setattr(l2ps, "UiaReader",
                        lambda: called.append(1) or pytest.fail(
                            "identity fail-closed 不得触 UIA"))
    assert l2ps.main([]) == 4
    data = json.loads((out / "panel_map.json").read_text(encoding="utf-8"))
    assert data["status"] == "identity_mismatch"
    assert data["identity_label"] == "广发证券-张三"
    assert not called


def test_not_target_account_no_hotswitch_rc4(monkeypatch, tmp_path):
    """面板在另一已知账号（王）且未授权热切换 → 让位退出，绝不 Alt 按键。"""
    out = _repoint_outdir(monkeypatch, tmp_path)
    monkeypatch.setattr(l2ps.bridge, "enumerate_trade_panels",
                        lambda include_minimized=False: [_fake_panel()])
    monkeypatch.setattr(l2ps.bridge, "_ths_window_operation_lock",
                        lambda **kw: contextlib.nullcontext())
    monkeypatch.setattr(l2ps.poll, "_is_foreground", lambda hwnd: False)
    monkeypatch.setattr(l2ps, "_is_iconic", lambda hwnd: False)
    monkeypatch.setattr(l2ps.hotswitch, "read_window_identity",
                        lambda w, save_png=None: ("广发证券-王*美", None))
    monkeypatch.setattr(l2ps.hotswitch, "ensure_target_account",
                        lambda w, cfg: pytest.fail("未授权不得热切换"))
    assert l2ps.main([]) == 4
    data = json.loads((out / "panel_map.json").read_text(encoding="utf-8"))
    assert data["status"] == "not_target_account_no_hotswitch"

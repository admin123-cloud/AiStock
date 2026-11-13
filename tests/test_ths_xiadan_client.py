# -*- coding: utf-8 -*-
"""ths_xiadan_client 状态机测试：全部 mock win32/进程层，零真机依赖。

场景覆盖（任务第 6 条）：未运行(启动链) / 登录框+凭证记住(点登录) /
登录框+空密码(fail-closed need_manual_login) / 已登录(短路) / 超时(进程
未起、无可见窗口、点击无过渡)；另覆盖 --dry-run、--export(PYTHONPATH
剥离 + stdout 透传)、退出码映射、密码框只读长度不读文本的源码契约。
"""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ths_client as ths  # noqa: E402  # 实现已收拢进统一入口（原 ths_xiadan_client 为薄 wrapper）


FAST = {"launch_wait": 0.02, "direct_wait": 0.02, "window_wait": 0.02,
        "login_timeout": 0.3}


class FakeWindows:
    """hwnd 纯数据数据库，模拟 win32 层；绝不接触真机窗口/进程。"""

    def __init__(self):
        self.windows: dict[int, dict] = {}
        self.messages: list[tuple[int, int]] = []      # (hwnd, msg) 经 _message_int
        self.popen_calls: list[tuple[str, str]] = []   # (exe, cwd)
        self.coord_clicks: list[int] = []              # _click_window 兜底
        self.popen_handler: Callable[[str, dict], None] | None = None  # 场景注入：Popen 副作用
        self.on_click: Callable[[FakeWindows, int], Any] | None = None  # 场景注入：BM_CLICK 副作用

    def add(self, hwnd, *, cls, pid, visible=True, title="", children=(),
            style=0, textlen=0):
        self.windows[hwnd] = {"class": cls, "pid": pid, "visible": visible,
                              "title": title, "children": list(children),
                              "style": style, "textlen": textlen}
        return hwnd

    def remove(self, hwnd):
        for child in self.windows.get(hwnd, {}).get("children", []):
            self.remove(child)
        self.windows.pop(hwnd, None)


def install(monkeypatch, fake, pids):
    """把 ths 模块全部 win32/process 原语替换为 FakeWindows 实现。"""
    monkeypatch.setattr(ths, "_pids_of",
                        lambda name: list(pids.get(name.lower(), [])))

    def top_level():
        # 只返回真顶层窗口：被他人收作 child 的控件（Edit/按钮）不算
        current_children = {c for w in fake.windows.values() for c in w["children"]}
        return [h for h in fake.windows if h not in current_children]

    monkeypatch.setattr(ths, "_top_level_windows", top_level)
    monkeypatch.setattr(ths, "_is_visible",
                        lambda h: fake.windows.get(h, {}).get("visible", False))
    monkeypatch.setattr(ths, "_process_id",
                        lambda h: fake.windows.get(h, {}).get("pid", 0))
    monkeypatch.setattr(ths, "_class_name",
                        lambda h: fake.windows.get(h, {}).get("class", ""))
    monkeypatch.setattr(ths, "_window_text",
                        lambda h: fake.windows.get(h, {}).get("title", ""))
    monkeypatch.setattr(ths, "_children",
                        lambda h: list(fake.windows.get(h, {}).get("children", [])))
    monkeypatch.setattr(ths, "_window_style",
                        lambda h: fake.windows.get(h, {}).get("style", 0))
    monkeypatch.setattr(ths, "_bring_to_front", lambda h: None)
    def click_window(h):
        fake.coord_clicks.append(h)
        if fake.on_click:
            fake.on_click(fake, h)
    monkeypatch.setattr(ths, "_click_window", click_window)
    monkeypatch.setattr(ths.bridge, "_ths_window_operation_lock", lambda **_: nullcontext())
    monkeypatch.setattr(ths, "_read_default_account_label", lambda _: "广发证券-曾*")

    def message_int(hwnd, msg):
        fake.messages.append((hwnd, msg))
        info = fake.windows.get(hwnd, {})
        if msg == ths.BM_CLICK and info.get("class") == "Button" and fake.on_click:
            fake.on_click(fake, hwnd)
        return info.get("textlen", 0)

    monkeypatch.setattr(ths, "_message_int", message_int)

    def popen(exe, cwd):
        fake.popen_calls.append((str(exe), str(cwd)))
        if fake.popen_handler:
            fake.popen_handler(Path(exe).name.lower(), pids)

    monkeypatch.setattr(ths, "_popen", popen)
    monkeypatch.setattr(ths.time, "sleep", lambda _s: None)
    return fake


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def _login_box(fake, pid=200, password_len=6):
    """实盘事实复刻：#32770 空标题登录框（账号 Edit + 密码 Edit + 登录按钮）。"""
    fake.add(301, cls="Edit", pid=pid, style=0, textlen=8)
    fake.add(302, cls="Edit", pid=pid, style=ths.ES_PASSWORD, textlen=password_len)
    fake.add(303, cls="Button", pid=pid, title="登录")
    return fake.add(300, cls="#32770", pid=pid, title="", children=[301, 302, 303])


def _main_window(fake, pid=200, hwnd=400):
    return fake.add(hwnd, cls="XiadanMainFrame", pid=pid, title="网上股票交易系统")


def _read_report(tmp_path):
    files = sorted((tmp_path / "rt").glob("report_*.json"))
    assert len(files) == 1, f"expected exactly one report, got {files}"
    return json.loads(files[0].read_text(encoding="utf-8"))


# ── 场景 1：未运行 → 启动链 ──────────────────────────────────────

def test_not_running_launches_hexin_with_cwd_then_direct_xiadan(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {}
    install(monkeypatch, fake, pids)
    # hexin 拉起后 xiadan 不自动出现 → 必须兜底直接 Popen xiadan
    fake.popen_handler = lambda name, p: p.setdefault(
        "hexin.exe", [100]) if name == "hexin.exe" else p.setdefault("xiadan.exe", [200])
    _main_window(fake, pid=200)  # 直接拉起 xiadan 后即处于已登录主窗口

    hexin = _touch(tmp_path / "ths" / "hexin.exe")
    xiadan = _touch(tmp_path / "ths" / "xiadan.exe")
    report = ths.run(hexin_exe=hexin, xiadan_exe=xiadan, export=False,
                     report_root=tmp_path / "rt", **FAST)

    assert report["status"] == "logged_in"
    assert report["login_state"] == "logged_in_after_launch"
    assert report["xiadan_pids"] == [200]
    assert report["credentials_touched"] is False
    # 铁律：两次 Popen 都必须带 cwd=exe 所在目录，hexin 在前
    assert [exe for exe, _ in fake.popen_calls] == [str(hexin), str(xiadan)]
    assert all(cwd == str(Path(exe).parent) for exe, cwd in fake.popen_calls)
    assert [s["step"] for s in report["launch"]] == [
        "launched_hexin", "waited_auto_pull", "launched_xiadan_direct"]
    assert _read_report(tmp_path)["schema"] == ths.SCHEMA


def test_hexin_launch_missing_exe_fails_closed(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {}
    install(monkeypatch, fake, pids)
    report = ths.run(hexin_exe=tmp_path / "nope.exe",
                     xiadan_exe=_touch(tmp_path / "xiadan.exe"),
                     report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "failure"
    assert "executable not found" in report["reason"]
    assert fake.popen_calls == []


# ── 场景 2：登录框 + 凭证记住 → 点登录 → 主窗口 ──────────────────

def test_login_box_remembered_password_clicks_login_and_reaches_main(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _login_box(fake, password_len=6)
    fake.on_click = lambda fk, _h: (fk.remove(300), _main_window(fk))  # 登录成功过渡

    report = ths.run(export=False, report_root=tmp_path / "rt", **FAST)

    assert report["status"] == "logged_in"
    assert report["login_state"] == "logged_in_after_click"
    assert report["password_remembered"] is True
    assert report["password_edit_selector"] == "es_password"
    assert fake.popen_calls == []  # 两进程都在跑：零 Popen
    assert fake.coord_clicks == [303]
    assert report["clicks"] == [{"method": "coordinate_click", "button_hwnd": 303}]
    assert report["account"] == "acct1"
    # 登录框只记 class，不记标题/子控件文本
    assert "title" not in report["windows"][0]


# ── 场景 3：登录框 + 空密码 → fail-closed ────────────────────────

def test_login_box_empty_password_fails_closed_without_clicking(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _login_box(fake, password_len=0)  # 密码框空：凭证未记住

    report = ths.run(export=True, report_root=tmp_path / "rt", **FAST)

    assert report["status"] == "need_manual_login"
    assert report["password_remembered"] is False
    assert report["clicks"] == []
    assert (303, ths.BM_CLICK) not in fake.messages
    assert fake.coord_clicks == []
    assert "export" not in report          # 未就绪绝不导出
    assert report["export_skipped"] == "not_logged_in"
    assert report["credentials_touched"] is False


# ── 场景 4：已登录（非 #32770 主窗口）→ 短路 ─────────────────────

def test_already_logged_in_main_window_short_circuits(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _main_window(fake)

    report = ths.run(export=False, report_root=tmp_path / "rt", **FAST)

    assert report["status"] == "logged_in"
    assert report["login_state"] == "logged_in"
    assert report["main_window"] == {"hwnd": 400, "class": "XiadanMainFrame",
                                     "title": "网上股票交易系统", "visible": True}
    assert report["windows"] == []  # 主窗口单独记账，可见窗口证据为空
    assert report["clicks"] == []
    assert fake.popen_calls == []
    assert report["password_remembered"] is None


def test_hidden_trade_window_still_counts_as_logged_in(monkeypatch, tmp_path):
    """实盘 2026-11-13 19:48 取证：盘后面板最小化（IsWindowVisible=False），
    主窗口仍在、标题「网上股票交易系统5.0」→ 已登录（fills 导出器自会恢复）。"""
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    fake.add(5050042, cls="#32770", pid=200, visible=False, title="")  # 遗留隐藏对话框
    fake.add(11207446, cls="Afx:00EA0000:b:00010003:00000006:0FDF2671",
             pid=200, visible=False, title="网上股票交易系统5.0")

    report = ths.run(export=False, report_root=tmp_path / "rt", **FAST)

    assert report["status"] == "logged_in"
    assert report["login_state"] == "logged_in"
    assert report["main_window"]["hwnd"] == 11207446
    assert report["main_window"]["visible"] is False
    assert report["windows"] == []  # 隐藏 #32770 不算登录框


# ── 场景 5：超时 ─────────────────────────────────────────────────

def test_timeout_xiadan_never_starts(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {}
    install(monkeypatch, fake, pids)  # Popen 无副作用：xiadan 永不出现
    report = ths.run(hexin_exe=_touch(tmp_path / "hexin.exe"),
                     xiadan_exe=_touch(tmp_path / "xiadan.exe"),
                     report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "failure"
    assert "xiadan.exe not running" in report["reason"]
    assert [s["step"] for s in report["launch"]] == [
        "launched_hexin", "waited_auto_pull", "launched_xiadan_direct"]
    assert len(fake.popen_calls) == 2  # hexin + 兜底 xiadan 都试过


def test_timeout_no_visible_window(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    # xiadan 进程在但无任何可见顶层窗口（登录态未就绪且无登录框）
    report = ths.run(report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "failure"
    assert "no visible xiadan top-level window" in report["reason"]


def test_login_click_without_transition_fails_closed(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _login_box(fake, password_len=6)  # on_click=None：点登录后永远停在登录框

    report = ths.run(export=False, report_root=tmp_path / "rt", **FAST)

    assert report["status"] == "failure"
    assert "login click did not reach" in report["reason"]
    assert fake.coord_clicks == [303]


def test_login_click_with_shell_and_login_dialog_is_not_success(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _login_box(fake)
    _main_window(fake)
    report = ths.run(report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "failure"
    assert "login click did not reach" in report["reason"]


def test_visible_non_trade_window_does_not_prove_login(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    fake.add(400, cls="OtherWindow", pid=200, title="启动提示")
    report = ths.run(report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "failure"


def test_unknown_dialog_with_trade_shell_does_not_prove_login(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _main_window(fake)
    fake.add(500, cls="#32770", pid=200)
    report = ths.run(report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "failure"
    assert "unrecognized dialog" in report["reason"]


def test_missing_identity_blocks_login_success_and_export(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _main_window(fake)
    monkeypatch.setattr(ths, "_read_default_account_label", lambda _: "")
    monkeypatch.setattr(ths, "_wait_for", lambda predicate, *_args: predicate())
    monkeypatch.setattr(ths, "_run_export", lambda *_: pytest.fail("must not export"))
    report = ths.run(export=True, report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "failure"
    assert "identity could not be verified" in report["reason"]
    assert fake.coord_clicks == []


def test_default_second_account_is_accepted_without_account_selection(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _main_window(fake)
    monkeypatch.setattr(ths, "_read_default_account_label", lambda _: "广发证券-王*美")
    report = ths.run(report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "logged_in" and report["account"] == "acct2"
    assert fake.coord_clicks == []


def test_extra_dialog_blocks_default_login(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _login_box(fake)
    fake.add(500, cls="#32770", pid=200)
    report = ths.run(report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "failure"
    assert fake.coord_clicks == []


def test_single_edit_cannot_be_assumed_to_be_password(monkeypatch):
    fake, pids = FakeWindows(), {}
    install(monkeypatch, fake, pids)
    _login_box(fake)
    fake.windows[300]["children"] = [301, 303]
    assert ths._password_state(300)["password_edit_found"] is False


def test_account_read_uses_uia_value_property(monkeypatch):
    combo = SimpleNamespace(iface_value=SimpleNamespace(CurrentValue="广发证券-曾*"))
    class Window:
        def descendants(self, **kwargs):
            assert kwargs == {"auto_id": "2322", "control_type": "ComboBox"}
            return [combo]
    class App:
        def __init__(self, **_kwargs): pass
        def connect(self, **_kwargs): return self
        def window(self, **_kwargs): return Window()
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Application=App))
    assert ths._read_default_account_label(123) == "广发证券-曾*"


def test_window_lock_is_released_before_export(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _main_window(fake)
    state = {"locked": False}
    @contextmanager
    def lock(**_kwargs):
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False
    monkeypatch.setattr(ths.bridge, "_ths_window_operation_lock", lock)
    def run_export(*_args):
        assert not state["locked"]
        return {"returncode": 0}
    monkeypatch.setattr(ths, "_run_export", run_export)
    report = ths.run(export=True, report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "success"


def test_unrecognized_dialog_is_never_clicked(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    # #32770 但无编辑框（可能是错误弹窗）→ 形态不识别即停
    fake.add(303, cls="Button", pid=200, title="确定")
    fake.add(300, cls="#32770", pid=200, children=[303])

    report = ths.run(report_root=tmp_path / "rt", **FAST)

    assert report["status"] == "failure"
    assert "shape not recognized" in report["reason"]
    assert fake.messages == []


# ── --dry-run ────────────────────────────────────────────────────

def test_dry_run_stops_before_click_and_export(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _login_box(fake, password_len=6)

    def _fail_export(*_a, **_k):  # pragma: no cover - dry-run 绝不导出
        raise AssertionError("export must not run in dry-run")

    monkeypatch.setattr(ths, "_run_export", _fail_export)
    report = ths.run(dry_run=True, export=True, report_root=tmp_path / "rt", **FAST)

    assert report["status"] == "dry_run"
    assert report["login_state"] == "login_box"
    assert report["would_click_login"] is True
    assert report["password_remembered"] is True
    assert (303, ths.BM_CLICK) not in fake.messages
    assert fake.coord_clicks == []
    assert report["export_skipped"] == "dry_run"


# ── --export：PYTHONPATH 剥离 + stdout 透传 ──────────────────────

def test_export_strips_pythonpath_and_passes_stdout_into_report(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _main_window(fake)
    monkeypatch.setenv("PYTHONPATH", "C:/polluted/win32api")

    python_exe = _touch(tmp_path / "py" / "python.exe")
    export_script = _touch(tmp_path / "py" / "ths_fills_export.py")
    captured = {}

    class _Proc:
        returncode = 0
        stdout = '{"schema": "ths_fills_export_multi/1", "rc": 0, "fills_total": 3}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        return _Proc()

    monkeypatch.setattr(ths.subprocess, "run", fake_run)

    report = ths.run(python_exe=python_exe, export_script=export_script,
                     export=True, report_root=tmp_path / "rt", **FAST)

    assert report["status"] == "success"
    assert captured["cmd"] == [str(python_exe), str(export_script),
                               "export-fills", "--once"]
    assert "PYTHONPATH" not in captured["env"]          # 剥离（cron DLL 污染事故）
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"
    assert captured["cwd"] == str(export_script.parent)
    assert "ths_fills_export_multi" in report["export"]["stdout"]  # stdout 透传
    assert report["export"]["returncode"] == 0
    assert report["export"]["pythonpath_stripped"] is True


def test_export_nonzero_rc_marks_export_failed(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _main_window(fake)
    python_exe = _touch(tmp_path / "py" / "python.exe")
    export_script = _touch(tmp_path / "py" / "ths_fills_export.py")

    class _Proc:
        returncode = 2  # exporter 语义：2=unavailable
        stdout = '{"rc": 2}'
        stderr = "ProcessNotFoundError xiadan.exe"

    monkeypatch.setattr(ths.subprocess, "run", lambda cmd, **kw: _Proc())
    report = ths.run(python_exe=python_exe, export_script=export_script,
                     export=True, report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "export_failed"
    assert report["export"]["returncode"] == 2


def test_export_missing_python_fails_closed(monkeypatch, tmp_path):
    fake, pids = FakeWindows(), {"hexin.exe": [100], "xiadan.exe": [200]}
    install(monkeypatch, fake, pids)
    _main_window(fake)
    report = ths.run(python_exe=tmp_path / "no_python.exe", export=True,
                     report_root=tmp_path / "rt", **FAST)
    assert report["status"] == "failure"
    assert "export python_exe not found" in report["reason"]


# ── 密码框判定单元（只读长度/样式，绝不读文本） ────────────────────

def test_password_state_prefers_es_password_and_never_reads_text(monkeypatch):
    fake, pids = FakeWindows(), {}
    install(monkeypatch, fake, pids)
    _login_box(fake, password_len=6)
    # 任何对编辑框的 _window_text 调用都视为凭证接触 → 直接失败
    monkeypatch.setattr(ths, "_window_text",
                        lambda h: (_ for _ in ()).throw(
                            AssertionError("must not read edit window text")))

    state = ths._password_state(300)
    assert state == {"password_edit_found": True, "password_nonempty": True,
                     "selector": "es_password"}
    assert (302, ths.WM_GETTEXTLENGTH) in fake.messages  # 只发长度查询
    assert all(msg != 0x000D for _h, msg in fake.messages)  # 无 WM_GETTEXT


def test_password_state_fallback_and_empty_rules(monkeypatch):
    fake, pids = FakeWindows(), {}
    install(monkeypatch, fake, pids)
    _login_box(fake, password_len=0)
    assert ths._password_state(300)["password_nonempty"] is False  # 空密码

    fake.windows[302]["style"] = 0  # 无 ES_PASSWORD → 兜底最后一个编辑框
    state = ths._password_state(300)
    assert state["selector"] == "last_edit" and state["password_edit_found"] is True

    fake.windows[300]["children"] = [303]  # 无编辑框 → 无法判定
    state = ths._password_state(300)
    assert state == {"password_edit_found": False, "password_nonempty": False,
                     "selector": "none"}


def test_login_button_label_matcher():
    assert ths._is_login_button_label("登录")
    assert ths._is_login_button_label("登 录")
    assert ths._is_login_button_label("登陆(&L)")
    assert ths._is_login_button_label("登录(L)")
    assert not ths._is_login_button_label("退出")
    assert not ths._is_login_button_label("登录并下单")


# ── 退出码映射 + 源码契约 ────────────────────────────────────────

@pytest.mark.parametrize("status,rc", [
    ("logged_in", 0), ("success", 0), ("dry_run", 0),
    ("need_manual_login", 1), ("failure", 1), ("export_failed", 1),
])
def test_exit_code_mapping(monkeypatch, status, rc):
    monkeypatch.setattr(ths, "run", lambda **_kw: {"schema": ths.SCHEMA,
                                                   "status": status})
    monkeypatch.setattr(ths, "_write_report", lambda *_a, **_k: Path("x"))
    assert ths.main(["login"]) == rc


def test_source_contract_never_reads_or_writes_credentials():
    source = Path(ths.__file__ or "").read_text(encoding="utf-8")
    assert 'credentials_touched": False' in source
    # 密码框只允许 WM_GETTEXTLENGTH（长度位）；不得出现取文本/写文本消息
    assert "WM_GETTEXTLENGTH" in source
    assert source.count("WM_GETTEXT") == source.count("WM_GETTEXTLENGTH")
    assert "WM_SETTEXT" not in source
    assert "EM_SETSEL" not in source
    assert "GetDlgItemText" not in source

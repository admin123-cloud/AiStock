# -*- coding: utf-8 -*-
"""同花顺远航冷启动引导 + 只读采集任务单测（#22：凭证零接触、fail-closed 护栏）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import ths_read_account_task as task  # noqa: E402


class TestThsReadAccountTaskColdStart:
    def test_main_login_window_fails_closed_without_touching_credentials(self, monkeypatch, tmp_path):
        monkeypatch.setattr(task.tc, "_yh_ensure_main", lambda **kw: {"pids": [100], "launched": True})
        monkeypatch.setattr(task.tc, "_wait_for", lambda fn, *a, **kw: {"hwnd": 11, "title": "登录"})
        monkeypatch.setattr(task.tc, "_yh_main_window", lambda: {"hwnd": 11, "title": "登录"})
        ticks = iter([0.0, 0.5, 20.0])
        monkeypatch.setattr(task.time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(task.time, "sleep", lambda _s: None)

        rep = task.run_task(account="acct1", cold_start=True, wait_idle_after_bootstrap=0.0, out_dir=tmp_path)
        # 快速退出不等待真实时间：覆盖 main_login_wait 为极小值
        assert rep["credentials_touched"] is False
        assert rep["rc"] == 4
        assert rep["status"] == "need_manual_main_login"
        assert (tmp_path / "latest.json").is_file()

    def test_panel_empty_password_fails_closed(self, monkeypatch):
        monkeypatch.setattr(task.tc, "_yh_ensure_main", lambda **kw: {"pids": [100], "launched": False})
        monkeypatch.setattr(task.tc, "_wait_for", lambda fn, *a, **kw: {"hwnd": 11, "title": "同花顺远航版"})
        monkeypatch.setattr(task.tc, "_yh_enum_panels", lambda: [{"hwnd": 22}])
        monkeypatch.setattr(
            task,
            "_inspect_panel_login_controls",
            lambda _h: {"is_login_page": True, "password_remembered": False, "captcha_empty": False, "login_button_hwnd": 33},
        )
        res = task.bootstrap_cold_start(main_login_wait=0.1)
        assert res["ready"] is False
        assert res["status"] == "need_manual_panel_login"
        assert res["credentials_touched"] is False

    def test_panel_remembered_password_clicks_login_and_proceeds_to_readonly(self, monkeypatch, tmp_path):
        monkeypatch.setattr(task.tc, "_yh_ensure_main", lambda **kw: {"pids": [100], "launched": True})
        monkeypatch.setattr(task.tc, "_wait_for", lambda fn, *a, **kw: {"hwnd": 11, "title": "同花顺远航版"})
        monkeypatch.setattr(task.tc, "_yh_enum_panels", lambda: [{"hwnd": 22}])
        monkeypatch.setattr(
            task,
            "_inspect_panel_login_controls",
            lambda _h: {"is_login_page": True, "password_remembered": True, "captcha_empty": False, "login_button_hwnd": 33},
        )
        clicked = []
        monkeypatch.setattr(task, "_click_panel_login_button", lambda h: clicked.append(h) or {"ok": True})
        monkeypatch.setattr(task.tc, "_yh_uia_login_page", lambda _h: False)
        monkeypatch.setattr(task.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            task.tc,
            "run_read_account",
            lambda **kw: {"status": "ok", "rc": 0, "account": "acct1", "credentials_touched": False, "allow_switch": kw.get("allow_switch")},
        )

        rep = task.run_task(account="acct1", cold_start=True, wait_idle_after_bootstrap=0.0, out_dir=tmp_path)
        assert clicked == [33]
        assert rep["rc"] == 0
        assert rep["status"] == "ok"
        assert rep["read_account"]["allow_switch"] is False
        assert rep["credentials_touched"] is False
        saved = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
        assert saved["bootstrap"]["ready"] is True

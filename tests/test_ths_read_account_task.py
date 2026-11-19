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
        ticks = iter([0.0, 0.5, 40.0])
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
        monkeypatch.setattr(task.tc, "_yh_ro_chain_snapshot", lambda *_a, **_kw: None)
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
        monkeypatch.setattr(task.tc, "_yh_ro_chain_snapshot", lambda *_a, **_kw: None)
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

    def test_multi_account_reads_active_first_switches_to_second_and_isolates_reports(self, monkeypatch, tmp_path):
        state = {"active": "acct1"}
        switches = []

        monkeypatch.setattr(task, "bootstrap_cold_start", lambda **kw: {"ready": True, "status": "bootstrap_ready", "interacted": False})
        monkeypatch.setattr(task, "_detect_active_account", lambda _exe: state["active"])

        def fake_switch(*, to, happ_exe, dry_run=False):
            switches.append((to, dry_run))
            state["active"] = to
            return {"status": "switched", "rc": 0, "to": to, "clicks": [{"target": to}]}

        monkeypatch.setattr(task.tc, "run_switch_account", fake_switch)
        monkeypatch.setattr(
            task.tc,
            "run_read_account",
            lambda **kw: {"status": "ok", "rc": 0, "account": kw["account"], "allow_switch": kw["allow_switch"]},
        )

        rep = task.run_multi_account_task(
            accounts=["acct2", "acct1"],
            wait_idle_after_bootstrap=0.0,
            out_dir=tmp_path,
        )
        # 当前已在 acct1，自动优先读 acct1 再切到 acct2（仅切 1 次）
        assert rep["rc"] == 0
        assert rep["accounts_requested"] == ["acct1", "acct2"]
        assert switches == [("acct2", False)]
        assert (tmp_path / "latest_acct1.json").is_file()
        assert (tmp_path / "latest_acct2.json").is_file()
        assert (tmp_path / "latest_multi.json").is_file()

    def test_multi_account_switch_failure_fails_closed_without_cross_account_pollution(self, monkeypatch, tmp_path):
        monkeypatch.setattr(task, "bootstrap_cold_start", lambda **kw: {"ready": True, "status": "bootstrap_ready", "interacted": False})
        monkeypatch.setattr(task, "_detect_active_account", lambda _exe: "acct1")
        monkeypatch.setattr(
            task.tc,
            "run_switch_account",
            lambda **kw: {"status": "menu_not_located", "rc": 4, "reason": "menu_not_located"},
        )
        read_calls = []
        monkeypatch.setattr(
            task.tc,
            "run_read_account",
            lambda **kw: read_calls.append(kw["account"]) or {"status": "ok", "rc": 0, "account": kw["account"]},
        )

        rep = task.run_multi_account_task(
            accounts=["acct1", "acct2"],
            wait_idle_after_bootstrap=0.0,
            out_dir=tmp_path,
        )
        assert rep["rc"] == 4
        assert read_calls == ["acct1"]  # acct2 切号失败后严禁调用 read_account 防止串账
        assert rep["accounts"]["acct2"]["read_account"] is None
        assert rep["accounts"]["acct2"]["status"] == "menu_not_located"

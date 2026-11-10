# -*- coding: utf-8 -*-
"""R42：pipeline_scheduler 任务 holding_tick_qmt_alert 的注册与节拍测试。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261110_purify_holding_tick_analysis_r42.md

契约：
  - 窗口守卫 09:25 ≤ 北京钟 ≤ 15:10（cron 触发面 09:00~15:50 由它收口）；
  - 节拍 = _run_t_tracker_step 复用：窗口外 no_op_window、非交易日 no_op_non_trading、
    交易日判断异常 fail-closed；命令指向 scripts/holding_tick_qmt_alert.py --once；
  - 注册：mon-fri、hour 9-15、minute */10、Asia/Shanghai、max_instances=1、coalesce；
  - 版本号推进 1.3.0。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pipeline_scheduler as ps  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
TRADING_MONDAY = date(2026, 11, 9)


class _FakeScheduler:
    def __init__(self, timezone=None):
        self.timezone = timezone
        self.jobs = []

    def add_job(self, func, **kw):
        class _Job:
            def __init__(self, kw):
                self.id = kw.get("id")
                self.trigger = kw.get("trigger")
                self.max_instances = kw.get("max_instances")
                self.coalesce = kw.get("coalesce")
                self.next_run_time = None
        self.jobs.append(_Job(kw))
        return self.jobs[-1]

    def get_job(self, job_id):
        return next((job for job in self.jobs if job.id == job_id), None)


@pytest.mark.parametrize("hh,mm,expected", [
    (9, 24, False), (9, 25, True), (12, 0, True), (15, 10, True), (15, 11, False),
])
def test_window_boundaries(hh, mm, expected):
    now = datetime.combine(TRADING_MONDAY, datetime.min.time(), tzinfo=BEIJING).replace(hour=hh, minute=mm)
    assert ps.in_holding_tick_alert_window(now) is expected


def test_beat_command_targets_r42_script():
    commands = []

    def fake_runner(cmd):
        commands.append(list(cmd))
        return {"returncode": 0, "stdout": "{}", "stderr": ""}

    result = ps.run_holding_tick_qmt_alert(
        mode="scheduled", is_trading_day=lambda d: True, runner=fake_runner,
        now_fn=lambda: datetime.combine(TRADING_MONDAY, datetime.min.time(), tzinfo=BEIJING).replace(hour=10, minute=0))
    assert result["status"] == "success"
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd[0].endswith("python.exe") or cmd[0].endswith("python") or "python" in Path(cmd[0]).name
    assert Path(cmd[1]).name == "holding_tick_qmt_alert.py"
    assert cmd[2] == "--once"
    assert Path(cmd[1]).parent == Path(ps.__file__).parent


def test_out_of_window_noop_does_not_run_script():
    commands = []

    def fake_runner(cmd):
        commands.append(list(cmd))
        return {"returncode": 0}

    result = ps.run_holding_tick_qmt_alert(
        mode="scheduled", is_trading_day=lambda d: True, runner=fake_runner,
        now_fn=lambda: datetime.combine(TRADING_MONDAY, datetime.min.time(), tzinfo=BEIJING).replace(hour=15, minute=11))
    assert result["status"] == "no_op_window"
    assert commands == []


def test_non_trading_day_noop():
    result = ps.run_holding_tick_qmt_alert(
        mode="scheduled", is_trading_day=lambda d: False, runner=lambda cmd: {"returncode": 0},
        now_fn=lambda: datetime.combine(TRADING_MONDAY, datetime.min.time(), tzinfo=BEIJING).replace(hour=10, minute=0))
    assert result["status"] == "no_op_non_trading"


def test_trading_day_check_error_fails_closed():
    def _boom(d):
        raise RuntimeError("calendar down")
    result = ps.run_holding_tick_qmt_alert(
        mode="scheduled", is_trading_day=_boom, runner=lambda cmd: {"returncode": 0},
        now_fn=lambda: datetime.combine(TRADING_MONDAY, datetime.min.time(), tzinfo=BEIJING).replace(hour=10, minute=0))
    assert result["status"] == "failure"
    assert "trading_day_check_failed" in result["reason"]


def test_rc_nonzero_maps_to_failure():
    result = ps.run_holding_tick_qmt_alert(
        mode="scheduled", is_trading_day=lambda d: True, runner=lambda cmd: {"returncode": 1},
        now_fn=lambda: datetime.combine(TRADING_MONDAY, datetime.min.time(), tzinfo=BEIJING).replace(hour=10, minute=0))
    assert result["status"] == "failure" and result["rc"] == 1


def test_register_job_cron_fields():
    scheduler = _FakeScheduler()
    ps.register_holding_tick_alert_job(scheduler)
    job = scheduler.get_job(ps.HOLDING_TICK_ALERT_JOB_ID)
    assert job is not None
    trigger = job.trigger
    fields = getattr(trigger, "fields", None)
    if fields is not None:  # apscheduler 可用时校验 cron 字段
        texts = {str(f): str(f) for f in fields}
        joined = " ".join(str(f) for f in fields)
        assert "mon-fri" in joined
        assert "9-15" in joined
        assert "*/10" in joined
    assert job.max_instances == 1
    assert job.coalesce is True


def test_serve_registers_holding_tick_alert_job(monkeypatch):
    registered = []
    monkeypatch.setattr(ps, "register_postclose_job", lambda s, f=None: registered.append("postclose"))
    monkeypatch.setattr(ps, "register_intraday_job", lambda s, f=None: registered.append("intraday"))
    monkeypatch.setattr(ps, "register_t_tracker_jobs", lambda s: registered.append("t_tracker"))
    monkeypatch.setattr(ps, "register_holding_tick_alert_job", lambda s: registered.append("holding_tick_qmt_alert"))

    class _ShutdownScheduler(_FakeScheduler):
        def add_listener(self, cb, mask):
            self.listeners = getattr(self, "listeners", [])
            self.listeners.append((cb, mask))

        def start(self):
            raise KeyboardInterrupt()

    rc = ps.serve(["--once"], scheduler_factory=_ShutdownScheduler, pipeline_fn=lambda **kw: {"status": "success"})
    assert rc == 0
    assert registered == ["postclose", "intraday", "t_tracker", "holding_tick_qmt_alert"]


def test_version_bumped():
    assert ps.__version__ == "1.3.0"

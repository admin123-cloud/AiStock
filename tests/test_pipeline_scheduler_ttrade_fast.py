# -*- coding: utf-8 -*-
"""R46 做T 1 分钟快车道调度注册单测（scripts/pipeline_scheduler.py）。

覆盖：t_trade_signal_fast 任务注册（cron 表达式/窗口守卫 09:31~14:59/交易日
fail-closed/--fast-tick CLI 旗标/错峰秒 23），及既有做T任务不受影响。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pipeline_scheduler as ps  # noqa: E402

BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")


def bj(h, m, s=0):
    return datetime(2026, 11, 11, h, m, s, tzinfo=BEIJING)


class TestFastWindowGuard:
    def test_window_bounds(self):
        assert ps.in_t_tracker_fast_window(bj(9, 30, 59)) is False   # 窗口前
        assert ps.in_t_tracker_fast_window(bj(9, 31)) is True
        assert ps.in_t_tracker_fast_window(bj(11, 30)) is True        # 午休仍守卫（数据侧自处理）
        assert ps.in_t_tracker_fast_window(bj(14, 59)) is True
        assert ps.in_t_tracker_fast_window(bj(15, 0)) is False        # 窗口后

    def test_naive_datetime_treated_as_beijing(self):
        assert ps.in_t_tracker_fast_window(datetime(2026, 11, 11, 9, 31)) is True
        assert ps.in_t_tracker_fast_window(datetime(2026, 11, 11, 15, 0)) is False


class TestRunFastTick:
    def _run(self, now_bj, *, trading=True, runner=None):
        calls = []

        def fake_runner(cmd):
            calls.append(list(cmd))
            return {"returncode": 0, "stdout": "", "stderr": ""}

        result = ps.run_t_tracker_fast_tick(
            mode="scheduled",
            is_trading_day=lambda d: trading,
            runner=runner or fake_runner,
            now_fn=lambda: now_bj)
        return result, calls

    def test_in_window_trading_day_runs_cli(self):
        result, calls = self._run(bj(10, 3, 23))
        assert result["status"] == "success"
        assert result["step"] == "fast_tick"
        assert len(calls) == 1
        cmd = calls[0]
        assert cmd[-1] == "--fast-tick"
        assert cmd[-2].endswith("t_trade_tracker.py")

    def test_out_of_window_no_op(self):
        result, calls = self._run(bj(9, 30, 23))
        assert result["status"] == "no_op_window"
        assert calls == []
        result2, _ = self._run(bj(15, 0, 23))
        assert result2["status"] == "no_op_window"

    def test_non_trading_day_no_op(self):
        result, calls = self._run(bj(10, 3, 23), trading=False)
        assert result["status"] == "no_op_non_trading"
        assert calls == []

    def test_trading_day_check_failure_fail_closed(self):
        def boom(d):
            raise RuntimeError("calendar down")
        result = ps.run_t_tracker_fast_tick(
            mode="scheduled", is_trading_day=boom,
            runner=lambda cmd: {"returncode": 0},
            now_fn=lambda: bj(10, 3, 23))
        assert result["status"] == "failure"
        assert "trading_day_check_failed" in result["reason"]

    def test_nonzero_rc_reported_failure(self):
        result, _ = self._run(bj(10, 3, 23),
                              runner=lambda cmd: {"returncode": 1, "stderr": "boom"})
        assert result["status"] == "failure" and result["rc"] == 1


class TestFastJobRegistration:
    def test_register_t_tracker_jobs_includes_fast(self):
        from apscheduler.schedulers.base import BaseScheduler

        class FakeScheduler:
            def __init__(self):
                self.jobs = {}

            def add_job(self, func, *, trigger, id, name=None, **kw):
                self.jobs[id] = {"id": id, "func": func, "trigger": trigger,
                                 "name": name}

            def get_job(self, job_id):
                return self.jobs.get(job_id)

        sched = FakeScheduler()
        jobs = ps.register_t_tracker_jobs(sched)
        got = [j["id"] for j in jobs if j]
        assert ps.TTRACKER_FAST_JOB_ID in got
        fast = sched.jobs[ps.TTRACKER_FAST_JOB_ID]
        assert fast["func"] is ps.run_t_tracker_fast_tick
        assert "R46" in fast["name"]

    def test_fast_cron_trigger_fields(self):
        trig = ps.build_t_tracker_fast_cron_trigger()
        fields = {f.name: str(f) for f in trig.fields}
        assert fields["minute"] == "*"
        assert fields["hour"] == "9-14"
        assert fields["second"] == "23"
        assert fields["day_of_week"] == "mon-fri"

    def test_fast_cron_first_fire_in_window(self):
        from apscheduler.triggers.cron import CronTrigger
        trig = ps.build_t_tracker_fast_cron_trigger()
        # cron 触发面 09:00~14:59 每分钟（秒 23）；09:00~09:30 由窗口守卫 no_op
        start = datetime(2026, 11, 11, 9, 0, 0, tzinfo=BEIJING)  # 周三
        nxt = trig.get_next_fire_time(None, start)
        assert (nxt.hour, nxt.minute, nxt.second) == (9, 0, 23)
        assert ps.in_t_tracker_fast_window(nxt) is False        # 守卫收口
        # 窗口内首个实际生效触发 = 09:31:23
        start2 = datetime(2026, 11, 11, 9, 30, 30, tzinfo=BEIJING)
        nxt2 = trig.get_next_fire_time(None, start2)
        assert (nxt2.hour, nxt2.minute, nxt2.second) == (9, 31, 23)
        assert ps.in_t_tracker_fast_window(nxt2) is True

    def test_scheduler_version_bumped(self):
        # R46 快车道 1.4.0 → R55 涨停生态 1.5.0（2026-11-11）
        assert ps.__version__ == "1.5.0"

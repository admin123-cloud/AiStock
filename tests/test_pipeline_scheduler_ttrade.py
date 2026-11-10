# -*- coding: utf-8 -*-
"""R37 L1 做T调度任务单测（scripts/pipeline_scheduler.py 新增三任务）——纯单元。

  - beat 窗口守卫（09:36~14:56）；
  - 错峰断言：做T节拍 fire 分钟集与快照任务（*/5）两两相距恰 1min；
  - 三任务注册（regime 09:25 / beat / reconcile 15:10）+ 16:15 盘后任务不受影响；
  - 交易日/窗口 no-op、日历异常 fail-closed、subprocess rc 语义。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pipeline_scheduler as ps  # noqa: E402

UTC = timezone.utc
DAY = date(2026, 11, 11)  # 周三


def _ok(**over):
    res = {"returncode": 0, "stdout": "ok", "stderr": ""}
    res.update(over)
    return res


class _Runner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        return _ok()


def _bj(hhmm: str) -> datetime:
    return datetime.combine(DAY, datetime.strptime(hhmm, "%H:%M").time()).replace(
        tzinfo=ps.BEIJING_TZ)


class TestBeatWindow:
    def test_window_bounds(self):
        assert not ps.in_t_tracker_beat_window(_bj("09:35"))
        assert ps.in_t_tracker_beat_window(_bj("09:36"))
        assert ps.in_t_tracker_beat_window(_bj("11:40"))   # 午休由引擎时段窗收口
        assert ps.in_t_tracker_beat_window(_bj("14:56"))
        assert not ps.in_t_tracker_beat_window(_bj("14:57"))

    def test_naive_datetime_accepted(self):
        assert ps.in_t_tracker_beat_window(datetime(2026, 11, 11, 10, 1))


class TestStaggering:
    def test_beat_cron_staggers_snapshot_by_one_minute(self):
        snap = ps.build_intraday_cron_trigger()
        beat = ps.build_t_tracker_beat_cron_trigger()
        start = datetime(2026, 11, 11, 0, 0, tzinfo=ps.BEIJING_TZ)

        def fire_minutes(trigger, until_hour_end):
            out, t = [], start
            end = start.replace(hour=until_hour_end, minute=59)
            while t < end:
                nxt = trigger.get_next_fire_time(None, t)
                if nxt is None or nxt >= end:
                    break
                out.append(nxt.astimezone(ps.BEIJING_TZ).strftime("%H:%M"))
                t = nxt + timedelta(seconds=1)
            return out

        snap_min = fire_minutes(snap, 15)
        beat_min = fire_minutes(beat, 15)
        # 错峰：同一小时内做T节拍与快照节拍两两相距 1min（非同一分钟）
        assert all(bm not in snap_min for bm in beat_min)
        for bm in beat_min:
            hh, mm = bm.split(":")
            assert f"{hh}:{int(mm) - 1:02d}" in snap_min or int(mm) == 1

    def test_beat_minutes_follow_one_mod_five(self):
        beat = ps.build_t_tracker_beat_cron_trigger()
        start = datetime(2026, 11, 11, 9, 0, tzinfo=ps.BEIJING_TZ)
        times = []
        t = start
        while len(times) < 6:
            nxt = beat.get_next_fire_time(None, t)
            times.append(nxt.astimezone(ps.BEIJING_TZ))
            t = nxt + timedelta(seconds=1)
        minutes = [dt.minute for dt in times]
        assert minutes == [1, 6, 11, 16, 21, 26]
        assert all(dt.second == 0 for dt in times)


class TestRegistration:
    def test_three_jobs_registered_and_postclose_untouched(self):
        from apscheduler.schedulers.blocking import BlockingScheduler

        sched = BlockingScheduler(timezone=ps.BEIJING_TZ)
        try:
            ps.register_postclose_job(sched)
            ps.register_intraday_job(sched)
            jobs = ps.register_t_tracker_jobs(sched)
            assert all(j is not None for j in jobs)
            ids = {job.id for job in sched.get_jobs()}
            assert {ps.TTRACKER_REGIME_JOB_ID, ps.TTRACKER_BEAT_JOB_ID,
                    ps.TTRACKER_RECONCILE_JOB_ID} <= ids
            assert ps.JOB_ID in ids and ps.INTRADAY_JOB_ID in ids
            beat = sched.get_job(ps.TTRACKER_BEAT_JOB_ID)
            assert beat.max_instances == 1 and beat.coalesce is True
        finally:
            sched.shutdown(wait=False) if sched.running else None


class TestStepSemantics:
    def test_regime_runs_subprocess_on_trading_day(self):
        runner = _Runner()
        report = ps.run_t_tracker_regime(
            is_trading_day=lambda d: True, runner=runner,
            now_fn=lambda: _bj("09:25"), logger_=None)
        assert report["status"] == "success"
        assert runner.calls[0][1].endswith("t_trade_tracker.py")
        assert runner.calls[0][-1] == "--regime"

    def test_reconcile_runs_subprocess(self):
        runner = _Runner()
        report = ps.run_t_tracker_reconcile(
            is_trading_day=lambda d: True, runner=runner,
            now_fn=lambda: _bj("15:10"))
        assert report["status"] == "success"
        assert runner.calls[0][-1] == "--reconcile"

    def test_beat_window_guard_noop_before_0936(self):
        runner = _Runner()
        report = ps.run_t_tracker_beat(
            is_trading_day=lambda d: True, runner=runner, now_fn=lambda: _bj("09:31"))
        assert report["status"] == "no_op_window" and runner.calls == []

    def test_non_trading_day_noop(self):
        runner = _Runner()
        report = ps.run_t_tracker_beat(
            is_trading_day=lambda d: False, runner=runner, now_fn=lambda: _bj("10:01"))
        assert report["status"] == "no_op_non_trading" and runner.calls == []

    def test_calendar_error_fail_closed(self):
        def boom(d):
            raise RuntimeError("ch down")
        report = ps.run_t_tracker_regime(
            is_trading_day=boom, runner=_Runner(), now_fn=lambda: _bj("09:25"))
        assert report["status"] == "failure"
        assert "trading_day_check_failed" in report["reason"]

    def test_subprocess_failure_reported(self):
        report = ps.run_t_tracker_reconcile(
            is_trading_day=lambda d: True,
            runner=lambda cmd: _ok(returncode=1, stderr="boom"),
            now_fn=lambda: _bj("15:10"))
        assert report["status"] == "failure" and report["rc"] == 1

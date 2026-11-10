# -*- coding: utf-8 -*-
"""scripts/pipeline_scheduler.py 的纯单元测试（R24 盘后管道调度器）。

覆盖任务书 20261109_pipeline_scheduler_r24 的契约：
  - 调度注册参数（Asia/Shanghai、工作日 16:15 cron、job 装配）；
  - 编排函数按序调用（fake runner 注入，零 subprocess/网络/DB）；
  - 日线失败重试 3 次/间隔可注入；重试耗尽 → failure 且后续阶段不执行；
  - minute_collect rc 语义：0/1 继续、3 报警终止、其他 rc fail-closed；
  - 非交易日 no-op 报告；交易日判断异常 fail-closed；
  - 每次尝试 stdout/stderr 落盘（文件名含日期+阶段+尝试序号）与汇总 JSON；
  - 步骤自身报告的关键计数提取；SIGTERM 优雅 shutdown；--run-now/--once CLI。
"""
from __future__ import annotations

import json
import logging
import signal
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pipeline_scheduler as ps  # noqa: E402

UTC = timezone.utc
RUN_DATE = date(2026, 11, 9)  # 周一（交易日）
STAMP = "20261109"

_QUIET = logging.getLogger("ps-test")
_QUIET.addHandler(logging.NullHandler())
_QUIET.setLevel(logging.CRITICAL)
_QUIET.propagate = False


class FakeRunner:
    """按序返回预设结果；只剩最后一个时复用（模拟恒定成功/恒定失败）。"""

    def __init__(self, results, on_call=None):
        self.results = [dict(r) for r in results]
        self.calls: list[list[str]] = []
        self.on_call = on_call

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        if self.on_call is not None:
            self.on_call(list(cmd))
        if len(self.results) > 1:
            return dict(self.results.pop(0))
        return dict(self.results[0])

    @property
    def stems(self) -> list[str]:
        return [Path(c[1]).stem for c in self.calls]


def _ok(stdout: str = "ok") -> dict:
    return {"returncode": 0, "stdout": stdout, "stderr": ""}


def _run(tmp_path, runner, *, trading=True, is_trading_day=None, **kw):
    check = is_trading_day if is_trading_day is not None else (lambda d: trading)
    sleeps: list[float] = []
    report = ps.run_postclose_pipelines(
        run_date=RUN_DATE,
        out_root=tmp_path,
        is_trading_day=check,
        runner=runner,
        retry_interval_sec=0.0,
        sleep_fn=sleeps.append,
        logger_=_QUIET,
        **kw,
    )
    return report, sleeps


# ---------------------------------------------------------------------------
# 调度注册
# ---------------------------------------------------------------------------

def test_cron_trigger_workday_1615_beijing():
    trigger = ps.build_cron_trigger()
    assert str(trigger) == "cron[day_of_week='mon-fri', hour='16', minute='15']"
    # 周一 10:00 北京（02:00 UTC，未到 16:15）→ 当日 16:15 = 08:15 UTC
    nxt = trigger.get_next_fire_time(None, datetime(2026, 11, 9, 2, 0, tzinfo=UTC))
    assert nxt == datetime(2026, 11, 9, 8, 15, tzinfo=UTC)
    # 周五 17:00 北京（已过 16:15）→ 下周一
    nxt2 = trigger.get_next_fire_time(None, datetime(2026, 11, 6, 9, 0, tzinfo=UTC))
    assert nxt2 == datetime(2026, 11, 9, 8, 15, tzinfo=UTC)
    # 周六全天 → 下周一（跳过周末）
    nxt3 = trigger.get_next_fire_time(None, datetime(2026, 11, 7, 2, 0, tzinfo=UTC))
    assert nxt3 == datetime(2026, 11, 9, 8, 15, tzinfo=UTC)


def test_register_postclose_job_assembles_job():
    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone=ps.BEIJING_TZ)
    try:
        job = ps.register_postclose_job(scheduler, job_func=lambda: None)
        assert job is not None
        assert job.id == ps.JOB_ID
        assert str(job.trigger) == "cron[day_of_week='mon-fri', hour='16', minute='15']"
        assert job.max_instances == 1  # 盘后编排不允许并行重叠
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 阶段命令契约
# ---------------------------------------------------------------------------

def test_stage_commands_contract(tmp_path):
    specs = ps.build_stage_commands(RUN_DATE, tmp_path)

    daily = specs[ps.STAGE_DAILY]["cmd"]
    assert Path(daily[1]) == ps.SCRIPTS_DIR / "daily_kline_pipeline.py"
    assert Path(daily[1]).is_file()
    assert "--formal-write" in daily
    assert daily[daily.index("--output") + 1].endswith(f"{STAMP}_daily_kline_report.json")

    minute = specs[ps.STAGE_MINUTE]["cmd"]
    assert Path(minute[1]).name == "minute_collect_auto.py"
    assert minute[minute.index("--mode") + 1] == "formal-write"
    assert minute[minute.index("--out-dir") + 1] == str(tmp_path)

    agg = specs[ps.STAGE_AGG]["cmd"]
    assert Path(agg[1]).name == "minute_agg_backfill_window.py"
    assert agg[agg.index("--mode") + 1] == "formal-write"
    # 当日-5d ~ 当日（含端点）
    assert agg[agg.index("--start-date") + 1] == "2026-11-04"
    assert agg[agg.index("--end-date") + 1] == "2026-11-09"


# ---------------------------------------------------------------------------
# 编排：顺序 / rc 语义 / 重试 / no-op / fail-closed
# ---------------------------------------------------------------------------

def test_success_runs_three_stages_in_order_and_writes_summary(tmp_path):
    runner = FakeRunner([_ok("out-daily"), _ok("out-minute"), _ok("out-agg")])
    report, sleeps = _run(tmp_path, runner)
    assert report["status"] == "success"
    assert report["failed_stage"] is None and report["alarm"] is False
    assert runner.stems == [
        "daily_kline_pipeline", "minute_collect_auto", "minute_agg_backfill_window",
    ]
    assert all(st["rc"] == 0 and st["attempts"] == 1 for st in report["stages"].values())
    assert sleeps == []  # 全程无重试等待
    assert ps.report_exit_code(report) == 0

    summary = tmp_path / f"postclose_report_{STAMP}.json"
    data = json.loads(summary.read_text(encoding="utf-8"))
    assert data["schema"] == ps.REPORT_SCHEMA
    assert data["version"] == ps.__version__
    assert data["status"] == "success"
    assert data["run_date"] == "2026-11-09"
    assert set(data["stages"]) == set(ps.STAGE_ORDER)  # JSON 按键排序，比较集合


def test_non_trading_day_is_noop_with_report(tmp_path):
    runner = FakeRunner([_ok()])
    report, _ = _run(tmp_path, runner, trading=False)
    assert report["status"] == "no_op"
    assert report["reason"] == "non_trading_day"
    assert report["stages"] == {}
    assert runner.calls == []  # 未启动任何管道
    data = json.loads((tmp_path / f"postclose_report_{STAMP}.json").read_text("utf-8"))
    assert data["status"] == "no_op"
    assert ps.report_exit_code(report) == 0


def test_trading_day_check_error_fails_closed(tmp_path):
    def boom(_d):
        raise RuntimeError("calendar db down")

    runner = FakeRunner([_ok()])
    report, _ = _run(tmp_path, runner, is_trading_day=boom)
    assert report["status"] == "failure"
    assert report["failed_stage"] == "trading_day_check"
    assert "trading_day_check_failed" in report["reason"]
    assert runner.calls == []  # 判断失败绝不带病编排
    assert ps.report_exit_code(report) == 1


def test_daily_retry_then_success(tmp_path):
    runner = FakeRunner([
        {"returncode": 2, "stdout": "", "stderr": "boom"},
        {"returncode": 2, "stdout": "", "stderr": "boom"},
        _ok("recovered"),
    ])
    report, sleeps = _run(tmp_path, runner)
    assert report["status"] == "success"
    daily = report["stages"][ps.STAGE_DAILY]
    assert daily["attempts"] == 3 and daily["retries"] == 2 and daily["rc"] == 0
    assert len(sleeps) == 2  # 每次失败后等待一次（间隔注入为 0）
    assert runner.stems[:3] == ["daily_kline_pipeline"] * 3  # 重试都是日线
    assert len(runner.calls) == 5  # 日线 3 次 + 采集 1 次 + 聚合 1 次


def test_daily_retry_exhausted_stops_pipeline(tmp_path):
    runner = FakeRunner([{"returncode": 2, "stdout": "", "stderr": "dead"}])
    report, sleeps = _run(tmp_path, runner)
    assert report["status"] == "failure"
    assert report["failed_stage"] == ps.STAGE_DAILY
    assert "retry_exhausted" in report["reason"]
    daily = report["stages"][ps.STAGE_DAILY]
    assert daily["attempts"] == 1 + ps.DAILY_RETRY_MAX  # 首次 + 3 次重试
    assert len(sleeps) == ps.DAILY_RETRY_MAX
    assert runner.stems == ["daily_kline_pipeline"] * (1 + ps.DAILY_RETRY_MAX)
    assert ps.STAGE_MINUTE not in report["stages"]  # 后续阶段未执行


def test_minute_collect_rc1_continues(tmp_path):
    runner = FakeRunner([_ok(), {"returncode": 1, "stdout": "fatal-ish", "stderr": ""},
                         _ok()])
    report, _ = _run(tmp_path, runner)
    assert report["status"] == "success"
    assert report["stages"][ps.STAGE_MINUTE]["rc"] == 1
    assert len(runner.calls) == 3  # rc=1 继续跑聚合


def test_minute_collect_rc3_alarm_terminates(tmp_path):
    runner = FakeRunner([_ok(), {"returncode": 3, "stdout": "unreachable", "stderr": ""}])
    report, _ = _run(tmp_path, runner)
    assert report["status"] == "failure"
    assert report["failed_stage"] == ps.STAGE_MINUTE
    assert report["alarm"] is True
    assert "rc3" in report["reason"]
    assert len(runner.calls) == 2  # 聚合未执行
    assert ps.STAGE_AGG not in report["stages"]


def test_minute_collect_unexpected_rc_fails_closed(tmp_path):
    runner = FakeRunner([_ok(), {"returncode": 4, "stdout": "degraded", "stderr": ""}])
    report, _ = _run(tmp_path, runner)
    assert report["status"] == "failure"
    assert report["failed_stage"] == ps.STAGE_MINUTE
    assert report["alarm"] is False
    assert "unexpected_rc_fail_closed" in report["reason"]
    assert len(runner.calls) == 2


def test_agg_failure_marks_failure(tmp_path):
    runner = FakeRunner([_ok(), _ok(), {"returncode": 1, "stdout": "", "stderr": "x"}])
    report, _ = _run(tmp_path, runner)
    assert report["status"] == "failure"
    assert report["failed_stage"] == ps.STAGE_AGG
    assert report["alarm"] is False
    assert ps.report_exit_code(report) == 1


# ---------------------------------------------------------------------------
# 落盘与关键计数
# ---------------------------------------------------------------------------

def test_attempt_logs_written_with_date_and_stage(tmp_path):
    runner = FakeRunner([_ok("OUT-daily"), _ok("OUT-minute"), _ok("OUT-agg")])
    report, _ = _run(tmp_path, runner)
    for stage, text in ((ps.STAGE_DAILY, "OUT-daily"),
                        (ps.STAGE_MINUTE, "OUT-minute"),
                        (ps.STAGE_AGG, "OUT-agg")):
        stdout_log = tmp_path / f"{STAMP}_{stage}.attempt1.stdout.log"
        stderr_log = tmp_path / f"{STAMP}_{stage}.attempt1.stderr.log"
        assert stdout_log.read_text("utf-8") == text
        assert stderr_log.exists()
        assert report["stages"][stage]["stdout_logs"] == [stdout_log.name]
    # 重试场景的尝试序号文件名
    tmp2 = tmp_path.parent / (tmp_path.name + "-retry")
    tmp2.mkdir(parents=True, exist_ok=True)
    report3, _ = _run(tmp2, FakeRunner([
        {"returncode": 2, "stdout": "f1", "stderr": ""},
        _ok("f2-ok"), _ok(), _ok(),
    ]))
    assert (tmp2 / f"{STAMP}_{ps.STAGE_DAILY}.attempt1.stdout.log").read_text("utf-8") == "f1"
    assert (tmp2 / f"{STAMP}_{ps.STAGE_DAILY}.attempt2.stdout.log").read_text("utf-8") == "f2-ok"
    assert report3["stages"][ps.STAGE_DAILY]["stdout_logs"] == [
        f"{STAMP}_{ps.STAGE_DAILY}.attempt1.stdout.log",
        f"{STAMP}_{ps.STAGE_DAILY}.attempt2.stdout.log",
    ]


def test_key_counts_extracted_from_step_report(tmp_path):
    def on_call(cmd):
        if Path(cmd[1]).stem == "daily_kline_pipeline":
            payload = {
                "status": "pass", "trade_date": "2026-11-09", "rows": 123,
                "row_counts": {"kept": 100, "rejected": 2},
                "gates": {"unit": "pass"},      # 嵌套 str 不提取
                "notes": "忽略非计数文本",
            }
            (tmp_path / f"{STAMP}_daily_kline_report.json").write_text(
                json.dumps(payload), encoding="utf-8")

    runner = FakeRunner([_ok(), _ok(), _ok()], on_call=on_call)
    report, _ = _run(tmp_path, runner)
    assert report["stages"][ps.STAGE_DAILY]["key_counts"] == {
        "status": "pass",
        "trade_date": "2026-11-09",
        "rows": 123,
        "row_counts.kept": 100,
        "row_counts.rejected": 2,
    }
    assert report["stages"][ps.STAGE_MINUTE]["key_counts"] == {}  # 无报告文件→空


# ---------------------------------------------------------------------------
# SIGTERM / CLI
# ---------------------------------------------------------------------------

class FakeScheduler:
    def __init__(self, raise_not_running=False):
        self.shutdown_waits: list = []
        self.jobs: list = []
        self.listeners: list = []
        self.started = False
        self._raise_not_running = raise_not_running

    def add_job(self, func, **kw):
        class _Job:
            id = kw.get("id")
            trigger = kw.get("trigger")
            max_instances = kw.get("max_instances")
            next_run_time = None
        self.jobs.append(_Job())
        return _Job()

    def get_job(self, job_id):
        for job in self.jobs:
            if job.id == job_id:
                return job
        return None

    def add_listener(self, cb, mask):
        self.listeners.append((cb, mask))

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        if self._raise_not_running:
            from apscheduler.schedulers import SchedulerNotRunningError
            raise SchedulerNotRunningError()
        self.shutdown_waits.append(wait)


def test_sigterm_request_shutdown_graceful():
    fake = FakeScheduler()
    ps._request_shutdown(fake, _QUIET)
    assert fake.shutdown_waits == [False]  # 不等待，已在跑的编排自然跑完

    raising = FakeScheduler(raise_not_running=True)
    ps._request_shutdown(raising, _QUIET)  # 已停止时静默，不抛出


def test_cli_run_now_executes_once_and_maps_exit_code():
    seen: list[dict] = []

    def ok_pipeline(**kw):
        seen.append(kw)
        return {"status": "success"}

    assert ps.serve(["--run-now"], pipeline_fn=ok_pipeline) == 0
    assert seen == [{"mode": "manual"}]  # 只执行一次、不启动调度器

    assert ps.serve(["--run-now"], pipeline_fn=lambda **kw: {"status": "failure"}) == 1


def test_cli_once_registers_job_listener_and_signals(monkeypatch):
    fake = FakeScheduler()
    signal_regs: list = []
    monkeypatch.setattr(ps.signal, "signal", lambda sig, handler: signal_regs.append(sig))

    rc = ps.serve(["--once"], pipeline_fn=lambda **kw: {"status": "success"},
                  scheduler_factory=lambda **kw: fake)
    assert rc == 0
    assert fake.started and fake.jobs and fake.jobs[0].id == ps.JOB_ID
    assert len(fake.listeners) == 1  # 单次执行后退出的监听器
    assert signal.SIGTERM in signal_regs and signal.SIGINT in signal_regs

    # 模拟任务触发完成事件 → 触发优雅退出
    callback, _mask = fake.listeners[0]
    callback(None)
    assert fake.shutdown_waits == [False]


# ---------------------------------------------------------------------------
# R28（2026-11-10）：盘中东财实时快照任务
# ---------------------------------------------------------------------------

class TestIntradayWindow:
    @pytest.mark.parametrize("hh,mm,expected", [
        (9, 30, False), (9, 34, False), (9, 35, True), (11, 0, True),
        (14, 59, True), (15, 5, True), (15, 6, False), (15, 55, False),
    ])
    def test_window_boundaries(self, hh, mm, expected):
        now_bj = datetime(2026, 11, 10, hh, mm, tzinfo=ps.BEIJING_TZ)
        assert ps.in_intraday_window(now_bj) is expected

    def test_naive_beijing_time_also_supported(self):
        assert ps.in_intraday_window(datetime(2026, 11, 10, 10, 0)) is True
        assert ps.in_intraday_window(datetime(2026, 11, 10, 16, 0)) is False


class TestIntradayBeat:
    def _beat(self, tmp_path, *, runner, now, trading=True, trading_exc=None):
        def check(_day):
            if trading_exc is not None:
                raise trading_exc
            return trading
        return ps.run_intraday_snapshot_beat(
            mode="scheduled",
            out_root=tmp_path / "runtime" / "postclose_auto",
            is_trading_day=check,
            runner=runner,
            now_fn=lambda: now,
            logger_=_QUIET,
        )

    def test_success_resets_failure_state(self, tmp_path):
        state_dir = ps.intraday_out_dir(tmp_path / "runtime" / "postclose_auto")
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "scheduler_state.json").write_text(
            json.dumps({"consecutive_failures": 2, "alarm": False}), encoding="utf-8")
        now = datetime(2026, 11, 10, 10, 35, tzinfo=ps.BEIJING_TZ)
        report = self._beat(tmp_path, runner=FakeRunner([_ok()]), now=now)
        assert report["status"] == "success"
        assert report["consecutive_failures"] == 0 and report["alarm"] is False
        state = json.loads((state_dir / "scheduler_state.json").read_text(encoding="utf-8"))
        assert state["consecutive_failures"] == 0 and state["alarm"] is False
        assert state["last_success_ts"]

    def test_consecutive_failures_raise_alarm_at_threshold(self, tmp_path):
        now = datetime(2026, 11, 10, 10, 35, tzinfo=ps.BEIJING_TZ)
        runner = FakeRunner([{"returncode": 3, "stdout": "", "stderr": "src"}])
        r1 = self._beat(tmp_path, runner=runner, now=now)
        r2 = self._beat(tmp_path, runner=runner, now=now)
        r3 = self._beat(tmp_path, runner=runner, now=now)
        assert [r["consecutive_failures"] for r in (r1, r2, r3)] == [1, 2, 3]
        assert r1["alarm"] is False and r2["alarm"] is False and r3["alarm"] is True
        state = json.loads(
            (ps.intraday_out_dir(tmp_path / "runtime" / "postclose_auto")
             / "scheduler_state.json").read_text(encoding="utf-8"))
        assert state["alarm"] is True and state["last_failure_reason"] == "collector_rc_3"
        assert len(state["recent_failures"]) == 3

    def test_out_of_window_noop_no_state_touch(self, tmp_path):
        now = datetime(2026, 11, 10, 15, 10, tzinfo=ps.BEIJING_TZ)
        runner = FakeRunner([_ok()])
        report = self._beat(tmp_path, runner=runner, now=now)
        assert report["status"] == "no_op_window" and runner.calls == []

    def test_non_trading_day_noop(self, tmp_path):
        now = datetime(2026, 11, 10, 10, 35, tzinfo=ps.BEIJING_TZ)
        runner = FakeRunner([_ok()])
        report = self._beat(tmp_path, runner=runner, now=now, trading=False)
        assert report["status"] == "no_op_non_trading" and runner.calls == []

    def test_calendar_error_fail_closed(self, tmp_path):
        now = datetime(2026, 11, 10, 10, 35, tzinfo=ps.BEIJING_TZ)
        runner = FakeRunner([_ok()])
        report = self._beat(tmp_path, runner=runner, now=now,
                            trading_exc=RuntimeError("db down"))
        assert report["status"] == "failure_calendar"
        assert runner.calls == []  # 判不出交易日绝不采集

    def test_command_targets_intraday_collector(self, tmp_path):
        now = datetime(2026, 11, 10, 10, 35, tzinfo=ps.BEIJING_TZ)
        runner = FakeRunner([_ok()])
        self._beat(tmp_path, runner=runner, now=now)
        cmd = runner.calls[0]
        assert cmd[0] == sys.executable
        assert Path(cmd[1]).name == "intraday_snapshot_collect.py"
        assert "--out-dir" in cmd

    def test_rc1_counts_as_failure(self, tmp_path):
        now = datetime(2026, 11, 10, 10, 35, tzinfo=ps.BEIJING_TZ)
        runner = FakeRunner([{"returncode": 1, "stdout": "", "stderr": "x"}])
        report = self._beat(tmp_path, runner=runner, now=now)
        assert report["status"] == "failure" and report["rc"] == 1


class TestIntradayRegistration:
    def test_cron_fields(self):
        trigger = ps.build_intraday_cron_trigger()
        fields = {f.name: str(f) for f in trigger.fields}
        assert fields["day_of_week"] == "mon-fri"
        assert fields["hour"] == "9-15"
        assert fields["minute"] == "*/5"
        assert str(trigger.timezone) in ("Asia/Shanghai", "UTC+08:00")

    def test_serve_registers_both_jobs(self, monkeypatch):
        fake = FakeScheduler()
        monkeypatch.setattr(ps.signal, "signal", lambda *_a: None)
        rc = ps.serve(
            ["--once"], pipeline_fn=lambda **kw: {"status": "success"},
            scheduler_factory=lambda **kw: fake,
        )
        assert rc == 0
        job_ids = [j.id for j in fake.jobs]
        assert ps.JOB_ID in job_ids and ps.INTRADAY_JOB_ID in job_ids

    def test_register_intraday_job_adds_with_coalesce(self):
        fake = FakeScheduler()
        job = ps.register_intraday_job(fake)
        assert job is not None and job.id == ps.INTRADAY_JOB_ID
        assert job.max_instances == 1  # 单实例防重叠（FakeScheduler._Job 暴露项）

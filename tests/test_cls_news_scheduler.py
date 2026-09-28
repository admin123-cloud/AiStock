from __future__ import annotations

from scheduler import cls_news_scheduler as module


class _FakeScheduler:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.jobs = {}
        self.running = False

    def add_job(self, func, trigger, **kwargs):
        self.jobs[kwargs["id"]] = {"func": func, "trigger": trigger, **kwargs}

    def start(self):
        self.running = True

    def shutdown(self, wait=False):
        self.running = False

    def get_job(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            return None

        class _Job:
            next_run_time = job["next_run_time"]

        return _Job()


def test_cls_news_schedule_is_hourly(monkeypatch):
    monkeypatch.setattr(module, "BackgroundScheduler", _FakeScheduler)
    runner = module.ClsNewsScheduler()

    status = runner.start()

    assert module.SCHEDULER_CONFIG["jobs"]["cls_news"]["interval"] == 3600
    assert status["running"] is True
    assert status["interval_seconds"] == 3600
    job = runner._scheduler.jobs[module.CLS_NEWS_JOB_ID]
    assert job["trigger"].interval.total_seconds() == 3600
    assert job["coalesce"] is True
    assert job["max_instances"] == 1
    runner.stop()
    assert runner.status()["running"] is False

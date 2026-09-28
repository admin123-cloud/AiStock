"""Runtime scheduler for the CLS news radar data producer."""

from __future__ import annotations

from datetime import datetime
from threading import RLock
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from scheduler.config import SCHEDULER_CONFIG
from utils.logger import get_logger


logger = get_logger("ClsNewsScheduler")
SH_TZ = ZoneInfo("Asia/Shanghai")
CLS_NEWS_JOB_ID = "cls_news_hourly_refresh"
DEFAULT_INTERVAL_SECONDS = 60 * 60


def _job_config() -> Dict[str, Any]:
    return dict(SCHEDULER_CONFIG.get("jobs", {}).get("cls_news", {}))


def _interval_seconds() -> int:
    try:
        configured = int(_job_config().get("interval", DEFAULT_INTERVAL_SECONDS))
    except (TypeError, ValueError):
        configured = DEFAULT_INTERVAL_SECONDS
    return max(60, configured)


class ClsNewsScheduler:
    """Own the single in-process hourly CLS news refresh job."""

    def __init__(self) -> None:
        self._scheduler: Optional[BackgroundScheduler] = None
        self._lock = RLock()

    def start(self) -> Dict[str, Any]:
        config = _job_config()
        if not bool(config.get("enabled", False)):
            return self.status()

        with self._lock:
            if self._scheduler is None:
                scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
                scheduler.add_job(
                    self._run_once,
                    trigger=IntervalTrigger(seconds=_interval_seconds(), timezone="Asia/Shanghai"),
                    id=CLS_NEWS_JOB_ID,
                    name="CLS news hourly refresh",
                    kwargs={
                        "pages": max(1, int(config.get("pages", 1))),
                        "rn": max(1, int(config.get("rn", 50))),
                    },
                    replace_existing=True,
                    next_run_time=datetime.now(SH_TZ),
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=max(60, min(_interval_seconds(), 1800)),
                )
                scheduler.start()
                self._scheduler = scheduler
                logger.info("CLS news scheduler started: interval=%ss", _interval_seconds())
        return self.status()

    def stop(self) -> None:
        with self._lock:
            scheduler = self._scheduler
            self._scheduler = None
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            logger.info("CLS news scheduler stopped")

    def status(self) -> Dict[str, Any]:
        config = _job_config()
        scheduler = self._scheduler
        job = scheduler.get_job(CLS_NEWS_JOB_ID) if scheduler is not None else None
        next_run_time = getattr(job, "next_run_time", None) if job is not None else None
        return {
            "enabled": bool(config.get("enabled", False)),
            "running": bool(scheduler is not None and scheduler.running),
            "interval_seconds": _interval_seconds(),
            "next_run_at": next_run_time.isoformat() if next_run_time else None,
        }

    @staticmethod
    def _run_once(*, pages: int, rn: int) -> None:
        try:
            from scheduler.tasks.cls_news_task import ClsNewsTask

            result = ClsNewsTask().execute(pages=pages, rn=rn)
            logger.info(
                "CLS news hourly refresh completed: fetched=%s new_rows=%s latest_id=%s",
                result.get("fetched", 0),
                result.get("new_rows", 0),
                result.get("latest_id"),
            )
        except Exception:
            logger.exception("CLS news hourly refresh failed")


_runtime_scheduler = ClsNewsScheduler()


def start_cls_news_scheduler() -> Dict[str, Any]:
    return _runtime_scheduler.start()


def stop_cls_news_scheduler() -> None:
    _runtime_scheduler.stop()


def get_cls_news_scheduler_status() -> Dict[str, Any]:
    return _runtime_scheduler.status()

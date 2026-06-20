"""Task scheduler runtime."""

import asyncio
import signal
from datetime import datetime
from typing import Optional

from scheduler.config import SCHEDULER_CONFIG
from scheduler import tasks as task_exports
from utils.logger import get_logger


logger = get_logger("Scheduler")


class TaskScheduler:
    """Simple in-process scheduler loop."""

    def __init__(self):
        self.tasks = {
            "full_sync_kline": self._build_task("FullSyncKlineTask"),
            "realtime_quotes": self._build_task("RealtimeQuotesTask"),
            "realtime_indices": self._build_task("RealtimeIndicesTask"),
            "intraday_sentiment": self._build_task("IntradaySentimentTask"),
            "minute_kline": self._build_task("MinuteKlineTask"),
            "cls_news": self._build_task("ClsNewsTask"),
        }
        self._running = False
        self._scheduled_tasks = []

    async def start(self):
        if not SCHEDULER_CONFIG.get("enabled", False):
            logger.info("Scheduler disabled by config")
            return
        if self._running:
            logger.warning("Scheduler already running")
            return

        logger.info("=" * 60)
        logger.info("Starting task scheduler")
        logger.info("=" * 60)

        self._running = True
        await self._create_scheduled_tasks()
        logger.info("Task scheduler started")

    async def stop(self):
        if not self._running:
            return
        logger.info("Stopping task scheduler...")
        self._running = False

        for task in self._scheduled_tasks:
            if not task.done():
                task.cancel()

        if self._scheduled_tasks:
            await asyncio.gather(*self._scheduled_tasks, return_exceptions=True)

        self._scheduled_tasks.clear()
        logger.info("Task scheduler stopped")

    async def _create_scheduled_tasks(self):
        jobs = SCHEDULER_CONFIG.get("jobs", {})
        if jobs.get("full_sync_kline", {}).get("enabled", False) and self.tasks.get("full_sync_kline"):
            self._scheduled_tasks.append(asyncio.create_task(self._schedule_full_sync_kline()))
        if jobs.get("realtime_quotes", {}).get("enabled", False) and self.tasks.get("realtime_quotes"):
            self._scheduled_tasks.append(asyncio.create_task(self._schedule_realtime_quotes()))
        if jobs.get("realtime_indices", {}).get("enabled", False) and self.tasks.get("realtime_indices"):
            self._scheduled_tasks.append(asyncio.create_task(self._schedule_realtime_indices()))
        if jobs.get("intraday_sentiment", {}).get("enabled", False) and self.tasks.get("intraday_sentiment"):
            self._scheduled_tasks.append(asyncio.create_task(self._schedule_intraday_sentiment()))
        if jobs.get("minute_kline", {}).get("enabled", False) and self.tasks.get("minute_kline"):
            self._scheduled_tasks.append(asyncio.create_task(self._schedule_minute_kline()))
        if jobs.get("cls_news", {}).get("enabled", False) and self.tasks.get("cls_news"):
            self._scheduled_tasks.append(asyncio.create_task(self._schedule_cls_news()))

    def _build_task(self, class_name: str):
        cls = getattr(task_exports, class_name, None)
        if cls is None:
            logger.warning(f"Task class unavailable: {class_name}")
            return None
        try:
            return cls()
        except Exception as exc:
            logger.warning(f"Task init failed: {class_name}: {exc}")
            return None

    async def _schedule_full_sync_kline(self):
        job_config = SCHEDULER_CONFIG["jobs"]["full_sync_kline"]
        logger.info(f"Scheduled full_sync_kline: {job_config.get('cron_expression')}")
        while self._running:
            try:
                now = datetime.now()
                if now.weekday() == 5 and now.hour == 1 and now.minute == 0:
                    logger.info("Trigger full sync kline task")
                    await self.tasks["full_sync_kline"].execute()
                    await asyncio.sleep(3600)
                    continue
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                logger.info("full_sync_kline task cancelled")
                break
            except Exception as exc:
                logger.error(f"full_sync_kline schedule failed: {exc}")
                await asyncio.sleep(60)

    async def _schedule_realtime_quotes(self):
        job_config = SCHEDULER_CONFIG["jobs"]["realtime_quotes"]
        interval = int(job_config.get("interval", 60))
        logger.info(f"Scheduled realtime_quotes every {interval}s")
        while self._running:
            try:
                await self.tasks["realtime_quotes"].execute()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("realtime_quotes task cancelled")
                break
            except Exception as exc:
                logger.error(f"realtime_quotes schedule failed: {exc}")
                await asyncio.sleep(interval)

    async def _schedule_realtime_indices(self):
        job_config = SCHEDULER_CONFIG["jobs"]["realtime_indices"]
        interval = int(job_config.get("interval", 120))
        logger.info(f"Scheduled realtime_indices every {interval}s")
        while self._running:
            try:
                await self.tasks["realtime_indices"].execute()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("realtime_indices task cancelled")
                break
            except Exception as exc:
                logger.error(f"realtime_indices schedule failed: {exc}")
                await asyncio.sleep(interval)

    async def _schedule_intraday_sentiment(self):
        job_config = SCHEDULER_CONFIG["jobs"]["intraday_sentiment"]
        interval = int(job_config.get("interval", 120))
        logger.info(f"Scheduled intraday_sentiment every {interval}s")
        while self._running:
            try:
                now = datetime.now()
                if now.hour == 18 and now.minute == 0:
                    logger.info("Trigger post-close sentiment consolidation")
                    await asyncio.to_thread(self.tasks["intraday_sentiment"].execute, force_tushare=True)
                    await asyncio.sleep(3600)
                    continue
                await asyncio.to_thread(self.tasks["intraday_sentiment"].execute)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("intraday_sentiment task cancelled")
                break
            except Exception as exc:
                logger.error(f"intraday_sentiment schedule failed: {exc}")
                await asyncio.sleep(interval)

    async def _schedule_minute_kline(self):
        job_config = SCHEDULER_CONFIG["jobs"]["minute_kline"]
        interval = int(job_config.get("interval", 900))
        trading_days_only = bool(job_config.get("trading_days_only", True))
        trading_hours = job_config.get("trading_hours", {})
        logger.info(f"Scheduled minute_kline every {interval}s")

        while self._running:
            try:
                if trading_days_only:
                    from scheduler.trading_calendar import TradingCalendar

                    if not TradingCalendar.is_trading_day(datetime.now()):
                        await asyncio.sleep(600)
                        continue

                if trading_hours:
                    now_time = datetime.now().time()
                    start_time = datetime.strptime(trading_hours.get("start", "09:30"), "%H:%M").time()
                    end_time = datetime.strptime(trading_hours.get("end", "15:00"), "%H:%M").time()
                    if not (start_time <= now_time <= end_time):
                        await asyncio.sleep(600)
                        continue

                await self.tasks["minute_kline"].execute()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("minute_kline task cancelled")
                break
            except Exception as exc:
                logger.error(f"minute_kline schedule failed: {exc}")
                await asyncio.sleep(interval)

    async def _schedule_cls_news(self):
        job_config = SCHEDULER_CONFIG["jobs"]["cls_news"]
        interval = int(job_config.get("interval", 60))
        pages = int(job_config.get("pages", 1))
        rn = int(job_config.get("rn", 50))
        logger.info(f"Scheduled cls_news every {interval}s")
        while self._running:
            try:
                await asyncio.to_thread(self.tasks["cls_news"].execute, pages=pages, rn=rn)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logger.info("cls_news task cancelled")
                break
            except Exception as exc:
                logger.error(f"cls_news schedule failed: {exc}")
                await asyncio.sleep(interval)

    def register_signal_handlers(self):
        try:
            signal.signal(signal.SIGINT, self._signal_handler)
        except Exception as exc:
            logger.warning(f"register signal handler failed: {exc}")

    def _signal_handler(self, signum, frame):
        logger.info(f"Signal received: {signum}, stopping scheduler")
        asyncio.create_task(self.stop())


class SchedulerManager:
    """Scheduler singleton manager."""

    _instance: Optional[TaskScheduler] = None

    @classmethod
    def get_instance(cls) -> TaskScheduler:
        if cls._instance is None:
            cls._instance = TaskScheduler()
        return cls._instance

    @classmethod
    def reset(cls):
        cls._instance = None

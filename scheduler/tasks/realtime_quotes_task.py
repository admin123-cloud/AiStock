"""Realtime quote sync task."""

from scheduler.trading_calendar import TradingCalendar
from utils.logger import get_logger


logger = get_logger("RealtimeQuotesTask")


class RealtimeQuotesTask:
    """Synchronize realtime stock quotes during trading hours."""

    async def execute(self):
        if not TradingCalendar.is_trading_time():
            logger.debug("Skip realtime quote sync outside trading hours")
            return
        logger.info("Start realtime quote sync task")

"""Intraday sentiment refresh task."""

from datetime import datetime

from scheduler.trading_calendar import TradingCalendar
from utils.logger import get_logger


logger = get_logger("IntradaySentimentTask")


class IntradaySentimentTask:
    """Refresh after-close sentiment data when the schedule allows it."""

    def execute(self, force_tushare: bool = False):
        now = datetime.now()
        is_trading = TradingCalendar.is_trading_time()
        is_after_close = now.hour == 18 and now.minute == 0

        if force_tushare or is_after_close:
            logger.info("Trigger after-close sentiment refresh")
            return
        if is_trading:
            logger.info("Skip after-close sentiment refresh during trading hours")
            return
        logger.debug("Sentiment refresh conditions are not met")

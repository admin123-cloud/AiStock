"""鐩樹腑鎯呯华浠诲姟銆?""

from datetime import datetime
from utils.logger import get_logger
from scheduler.trading_calendar import TradingCalendar

logger = get_logger("IntradaySentimentTask")


class IntradaySentimentTask:
    """鐩樹腑/鐩樺悗鎯呯华鏁版嵁鍒锋柊浠诲姟銆?""


    def execute(self, force_tushare: bool = False):
        now = datetime.now()
        is_trading = TradingCalendar.is_trading_time()
        is_after_close = now.hour == 18 and now.minute == 0

        if force_tushare or is_after_close:
            logger.info("瑙﹀彂鐩樺悗鎯呯华鏁版嵁鍒锋柊")
            return
        if is_trading:
            logger.info("浜ゆ槗鏃堕棿鍐呰烦杩囩洏鍚庢儏缁埛鏂?)
            return
        logger.debug("褰撳墠涓嶆弧瓒虫儏缁暟鎹埛鏂版潯浠?)


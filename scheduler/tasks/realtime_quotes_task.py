"""瀹炴椂琛屾儏浠诲姟銆?""

from utils.logger import get_logger
from scheduler.trading_calendar import TradingCalendar

logger = get_logger("RealtimeQuotesTask")


class RealtimeQuotesTask:
    """瀹炴椂鑲＄エ琛屾儏鍚屾銆?""


    async def execute(self):
        if not TradingCalendar.is_trading_time():
            logger.debug("闈炰氦鏄撴椂闂达紝璺宠繃瀹炴椂琛屾儏鍚屾")
            return
        logger.info("寮€濮嬫墽琛屽疄鏃惰鎯呭悓姝ヤ换鍔?)


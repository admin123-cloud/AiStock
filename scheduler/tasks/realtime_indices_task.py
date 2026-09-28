"""实时指数行情任务。"""

from utils.logger import get_logger
from scheduler.trading_calendar import TradingCalendar

logger = get_logger("RealtimeIndicesTask")


class RealtimeIndicesTask:
    """实时指数行情同步。"""

    def __init__(self):
        self.indices = [
            ("000001.SH", "上证指数"),
            ("399001.SZ", "深证成指"),
            ("399006.SZ", "创业板指"),
            ("000300.SH", "沪深300"),
            ("000016.SH", "上证50"),
        ]

    async def execute(self):
        if not TradingCalendar.is_trading_time():
            logger.debug("非交易时间，跳过实时指数同步")
            return
        logger.info("开始执行实时指数同步任务，indices=%s", len(self.indices))

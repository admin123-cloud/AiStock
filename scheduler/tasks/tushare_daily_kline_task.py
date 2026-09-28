"""Tushare 日线补充同步任务。"""

from datetime import datetime
from utils.logger import get_logger

logger = get_logger("TushareDailyKlineTask")


class TushareDailyKlineTask:
    """Tushare 日线同步任务。"""

    def __init__(self):
        try:
            from data_fetcher.tushare import TushareDataSource
            self.tushare = TushareDataSource({"token": ""})
            self.pro = getattr(self.tushare, "pro", None)
        except Exception:
            self.tushare = None
            self.pro = None

    def execute(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if not self.pro:
            logger.warning("Tushare 未初始化，跳过日线同步")
            return
        logger.info("Tushare 日线同步开始: %s，等待后续实现", today)

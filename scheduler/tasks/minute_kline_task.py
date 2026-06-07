"""分钟 K 线同步任务。"""

from utils.logger import get_logger

logger = get_logger("MinuteKlineTask")


class MinuteKlineTask:
    """分钟 K 线数据同步。"""

    def __init__(self):
        self.periods = ["1m", "5m", "15m", "30m", "60m"]

    async def execute(self):
        logger.info("开始执行分钟 K 线同步任务，periods=%s", ",".join(self.periods))

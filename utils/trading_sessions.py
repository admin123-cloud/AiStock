"""共享A股交易时段；不依赖行情客户端或策略。"""
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo('Asia/Shanghai')
SESSION_MINUTES = ((570, 690), (780, 900))


def completed_bar_times(period: int) -> list[str]:
    if period <= 0:
        raise ValueError('period must be positive')
    return [f'{minute // 60:02d}:{minute % 60:02d}'
            for start, end in SESSION_MINUTES
            for minute in range(start + period, end + 1, period)]

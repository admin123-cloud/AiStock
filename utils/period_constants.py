"""
全局时间单位常量定义
统一前后端使用的时间单位格式

使用规范：
- 1m: 1分钟
- 5m: 5分钟
- 15m: 15分钟
- 30m: 30分钟
- 60m: 60分钟
- 1d: 1天(日线)
- 1w: 1周(周线)
- 1mon: 1个月(月线)
- 1q: 1季度(季线)
- 1y: 1年(年线)
"""

from typing import Dict, List, Tuple

# 标准时间单位列表
STANDARD_PERIODS = [
    "1m",    # 1分钟
    "5m",    # 5分钟
    "15m",   # 15分钟
    "30m",   # 30分钟
    "60m",   # 60分钟
    "1d",    # 日线
    "1w",    # 周线
    "1mon",  # 月线
    "1q",    # 季线
    "1y",    # 年线
]

# 时间单位显示名称
PERIOD_DISPLAY_NAMES: Dict[str, str] = {
    "1m": "1分钟",
    "5m": "5分钟",
    "15m": "15分钟",
    "30m": "30分钟",
    "60m": "60分钟",
    "1d": "日线",
    "1w": "周线",
    "1mon": "月线",
    "1q": "季线",
    "1y": "年线",
}

# 时间单位到数据库表名的映射
PERIOD_TO_TABLE: Dict[str, str] = {
    "1m": "kline_minute_1",
    "5m": "kline_minute_5",
    "15m": "kline_minute_15",
    "30m": "kline_minute_30",
    "60m": "kline_minute_60",
    "1d": "kline_daily",
    "1w": "kline_weekly",
    "1mon": "kline_monthly",
    "1q": "kline_quarterly",
    "1y": "kline_yearly",
}

# 时间单位到日期列名的映射
PERIOD_TO_DATE_COLUMN: Dict[str, str] = {
    "1m": "datetime",
    "5m": "datetime",
    "15m": "datetime",
    "30m": "datetime",
    "60m": "datetime",
    "1d": "trade_date",
    "1w": "week_start_date",
    "1mon": "month_start_date",
    "1q": "year",  # 季线使用year和quarter
    "1y": "year",
}

# 时间单位到通达信API的映射
PERIOD_TO_TDX: Dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
    "1d": "daily",
    "1w": "weekly",
    "1mon": "monthly",
    "1q": "quarterly",
    "1y": "yearly",
}

# 旧格式到新格式的映射（用于兼容转换）
OLD_TO_NEW_PERIOD: Dict[str, str] = {
    # 分钟线
    "1min": "1m",
    "5min": "5m",
    "15min": "15m",
    "30min": "30m",
    "60min": "60m",
    "1h": "60m",  # 1小时 = 60分钟
    # 日线
    "daily": "1d",
    # 周线
    "weekly": "1w",
    # 月线
    "monthly": "1mon",
    "1M": "1mon",
    # 季线
    "quarterly": "1q",
    "1Q": "1q",
    # 年线
    "yearly": "1y",
    "1Y": "1y",
}


def normalize_period(period: str) -> str:
    """
    将各种格式的时间单位统一转换为标准格式
    
    Args:
        period: 输入的时间单位字符串
        
    Returns:
        标准格式的时间单位字符串
        
    Examples:
        >>> normalize_period("1min")
        "1m"
        >>> normalize_period("60min")
        "60m"
        >>> normalize_period("1M")
        "1mon"
        >>> normalize_period("daily")
        "1d"
    """
    if not period:
        return "1d"  # 默认返回日线
    
    period = period.lower().strip()
    
    # 如果已经是标准格式，直接返回
    if period in STANDARD_PERIODS:
        return period
    
    # 尝试转换旧格式
    if period in OLD_TO_NEW_PERIOD:
        return OLD_TO_NEW_PERIOD[period]
    
    # 如果无法识别，返回默认值并记录警告
    import logging
    logger = logging.getLogger(__name__)
    logger.warning(f"未知的时间单位格式: {period}，使用默认值 1d")
    return "1d"


def get_period_display_name(period: str) -> str:
    """获取时间单位的显示名称"""
    standard = normalize_period(period)
    return PERIOD_DISPLAY_NAMES.get(standard, standard)


def get_period_table(period: str) -> str:
    """获取时间单位对应的数据库表名"""
    standard = normalize_period(period)
    return PERIOD_TO_TABLE.get(standard, "kline_daily")


def get_period_date_column(period: str) -> str:
    """获取时间单位对应的日期列名"""
    standard = normalize_period(period)
    return PERIOD_TO_DATE_COLUMN.get(standard, "trade_date")


def get_period_tdx_value(period: str) -> str:
    """获取时间单位对应的通达信API值"""
    standard = normalize_period(period)
    return PERIOD_TO_TDX.get(standard, "daily")


# 前端使用的选项列表（用于Vue组件）
FRONTEND_PERIOD_OPTIONS: List[Dict[str, str]] = [
    {"value": "1m", "label": "1分钟"},
    {"value": "5m", "label": "5分钟"},
    {"value": "15m", "label": "15分钟"},
    {"value": "30m", "label": "30分钟"},
    {"value": "60m", "label": "60分钟"},
    {"value": "1d", "label": "日线"},
    {"value": "1w", "label": "周线"},
    {"value": "1mon", "label": "月线"},
    {"value": "1q", "label": "季线"},
    {"value": "1y", "label": "年线"},
]

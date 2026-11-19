"""
辅助工具函数模块
"""

from datetime import datetime, time
from typing import Tuple, Optional
import re


def format_stock_code(code: str, market: str = "SH") -> str:
    """
    格式化股票代码
    
    Args:
        code: 股票代码（如 "000001"）
        market: 市场代码（SH, SZ, BJ）
    
    Returns:
        格式化后的代码（如 "SH000001"）
    """
    code = code.strip()
    
    # 如果已经包含市场前缀，直接返回
    if code.startswith(("SH", "SZ", "BJ")):
        return code
    
    # 自动判断市场
    if market is None:
        market = detect_market(code)
    
    return f"{market}{code}"


def detect_market(code: str) -> str:
    """
    根据股票代码判断市场
    
    Args:
        code: 股票代码
    
    Returns:
        市场代码（SH, SZ, BJ）
    """
    code = code.strip()
    
    # 上交所
    if code.startswith(("60", "68")):
        return "SH"
    # 深交所
    elif code.startswith(("00", "30")):
        return "SZ"
    # 北交所
    elif code.startswith(("4", "8")):
        return "BJ"
    else:
        # 默认深交所
        return "SZ"


def parse_market_code(full_code: str) -> Tuple[str, str]:
    """
    解析包含市场前缀的股票代码
    
    Args:
        full_code: 完整代码（如 "SH000001"）
    
    Returns:
        (市场代码, 股票代码) 元组
    """
    full_code = full_code.strip()
    
    # 匹配市场前缀
    match = re.match(r'^(SH|SZ|BJ)?(\d+)$', full_code)
    if match:
        market = match.group(1) or detect_market(match.group(2))
        code = match.group(2)
        return market, code
    
    return "SZ", full_code


def is_trading_time(dt: Optional[datetime] = None) -> bool:
    """
    判断是否在交易时间内
    
    Args:
        dt: 时间对象，None 表示当前时间
    
    Returns:
        是否在交易时间内
    """
    if dt is None:
        dt = datetime.now()
    
    # 周末不交易
    if dt.weekday() >= 5:
        return False
    
    current_time = dt.time()
    
    # 上午交易时间：9:30 - 11:30
    morning_start = time(9, 30)
    morning_end = time(11, 30)
    
    # 下午交易时间：13:00 - 15:00
    afternoon_start = time(13, 0)
    afternoon_end = time(15, 0)
    
    # 判断是否在交易时段
    is_morning = morning_start <= current_time <= morning_end
    is_afternoon = afternoon_start <= current_time <= afternoon_end
    
    return is_morning or is_afternoon


def is_trading_day(dt: Optional[datetime] = None) -> bool:
    """
    判断是否为交易日（简单判断，未考虑节假日）
    
    Args:
        dt: 日期对象，None 表示当前日期
    
    Returns:
        是否为交易日
    """
    if dt is None:
        dt = datetime.now()
    
    # 周末不交易
    if dt.weekday() >= 5:
        return False
    
    # TODO: 添加节假日判断
    return True


def calculate_change(current: float, previous: float) -> Tuple[float, float]:
    """
    计算涨跌金额和涨跌幅
    
    Args:
        current: 当前价格
        previous: 之前价格
    
    Returns:
        (涨跌金额, 涨跌幅百分比)
    """
    change = current - previous
    change_percent = (change / previous * 100) if previous != 0 else 0.0
    return change, change_percent


def format_number(value: float, decimal_places: int = 2) -> str:
    """
    格式化数字（添加单位）
    
    Args:
        value: 数值
        decimal_places: 小数位数
    
    Returns:
        格式化后的字符串
    """
    if abs(value) >= 1e8:
        return f"{value / 1e8:.{decimal_places}f}亿"
    elif abs(value) >= 1e4:
        return f"{value / 1e4:.{decimal_places}f}万"
    else:
        return f"{value:.{decimal_places}f}"


def validate_stock_code(code: str) -> bool:
    """
    验证股票代码格式
    
    Args:
        code: 股票代码
    
    Returns:
        是否有效
    """
    # 移除市场前缀
    if code.startswith(("SH", "SZ", "BJ")):
        code = code[2:]
    
    # 检查是否为6位数字
    return bool(re.match(r'^\d{6}$', code))

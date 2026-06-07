import asyncio
import uuid
from typing import Optional, List
from datetime import datetime, timedelta
import heapq
from urllib.parse import quote
import threading
import json
from sqlalchemy import text
from fastapi import APIRouter, HTTPException, Query, Body, Request
from pydantic import BaseModel

from utils.database import db
from scheduler.trading_calendar import get_trading_dates_from_db
from utils.logger import get_logger

logger = get_logger(__name__)

# 创建路由对象
router = APIRouter(prefix="/kline-check", tags=["K线完整性检查"])


class CheckRequest(BaseModel):
    """
    K线数据检查请求模型
    """
    code: str
    name: str
    period: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    list_date: Optional[str] = None
    data_type: str = "stock"


class CheckResult:
    """
    K线数据检查结果类
    """
    def __init__(self, code: str, name: str, period: str, total_expected: int, 
                 actual_count: int, missing_count: int, missing_dates: List[str], 
                 missing_rate: float, status: str, last_date: Optional[str], 
                 first_date: Optional[str]):
        self.code = code
        self.name = name
        self.period = period
        self.total_expected = total_expected
        self.actual_count = actual_count
        self.missing_count = missing_count
        self.missing_dates = missing_dates
        self.missing_rate = missing_rate
        self.status = status
        self.last_date = last_date
        self.first_date = first_date


SUPPORTED_PERIODS_STOCK_INDEX = ["1d"]
SUPPORTED_PERIODS_SECTOR = ["1d"]
BARS_PER_DAY_MAP = {"1m": 240, "5m": 48, "15m": 16, "30m": 8, "60m": 4}
MISSING_RATE_TOLERANCE = 2.0
MANUAL_CONFIRM_SOURCE = "tdxquant/tqcenter"


def get_table_name(period: str) -> str:
    """
    根据K线周期获取对应的数据库表名
    
    Args:
        period: K线周期
    
    Returns:
        数据库表名
    """
    table_map = {
        '1m': 'kline_minute_1',
        '5m': 'kline_minute_5',
        '15m': 'kline_minute_15',
        '30m': 'kline_minute_30',
        '60m': 'kline_minute_60',
        '1d': 'kline_daily',
        '1w': 'kline_weekly',
        '1mon': 'kline_monthly',
        '1q': 'kline_quarterly',
        '1y': 'kline_yearly'
    }
    return table_map.get(period, 'kline_daily')

def get_date_column(period: str) -> str:
    """
    根据K线周期获取对应的日期列名
    
    Args:
        period: K线周期
    
    Returns:
        日期列名
    """
    if period in ['1m', '5m', '15m', '30m', '60m']:
        return 'datetime'
    elif period == '1d':
        return 'trade_date'
    elif period == '1w':
        return 'week_start_date'
    elif period == '1mon':
        return 'month_start_date'
    elif period == '1q':
        return 'quarter'
    elif period == '1y':
        return 'year'
    else:
        return 'trade_date'

def get_sector_table_name(period: str) -> str:
    """
    根据K线周期获取对应的板块数据库表名
    
    Args:
        period: K线周期
    
    Returns:
        板块数据库表名
    """
    if period == '1d':
        return 'sector_kline_daily'
    return None


def _safe_date_str(value) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def _should_mark_pending_confirm(data_type: str, actual: int, status: str) -> bool:
    """
    个股日线来自 tdxquant/tqcenter 时，缺口更可能由停牌/退市等业务原因造成，
    先进入人工确认队列，不直接判为异常。
    """
    return data_type == "stock" and int(actual or 0) > 0 and status in ("incomplete", "error")


def _apply_manual_confirm_policy(data_type: str, snap: dict) -> dict:
    normalized = dict(snap or {})
    status = str(normalized.get("status") or "")
    actual = int(normalized.get("actual") or 0)
    if not _should_mark_pending_confirm(data_type, actual, status):
        return normalized

    normalized["original_status"] = status
    normalized["status"] = "pending_confirm"
    normalized["reason"] = "manual_confirm_tdxquant"
    normalized["manual_confirm"] = True
    normalized["manual_confirm_source"] = MANUAL_CONFIRM_SOURCE
    return normalized


def _count_actual_fast(code: str, table_name: str, date_column: str, start_date: Optional[str], end_date: Optional[str]):
    """
    快速统计实际条数与首末日期（避免拉出全量日期列表）。
    """
    engine = db.engine
    with engine.connect() as conn:
        if start_date and end_date:
            sql = f"""
            SELECT COUNT(*) AS cnt, MIN({date_column}) AS first_dt, MAX({date_column}) AS last_dt
            FROM {table_name}
            WHERE code = :code AND {date_column} >= :start_date AND {date_column} <= :end_date
            """
            row = conn.execute(text(sql), {"code": code, "start_date": start_date, "end_date": end_date}).fetchone()
        else:
            sql = f"""
            SELECT COUNT(*) AS cnt, MIN({date_column}) AS first_dt, MAX({date_column}) AS last_dt
            FROM {table_name}
            WHERE code = :code
            """
            row = conn.execute(text(sql), {"code": code}).fetchone()

    actual_count = int(row[0] or 0) if row else 0
    first_date = _safe_date_str(row[1]) if row and row[1] else None
    last_date = _safe_date_str(row[2]) if row and row[2] else None
    return actual_count, first_date, last_date


def _count_trading_days(start_date: str, end_date: str) -> int:
    engine = db.engine
    with engine.connect() as conn:
        sql = """
        SELECT COUNT(DISTINCT trade_date)
        FROM trade_calendar
        WHERE is_trading = 1 AND trade_date >= :start_date AND trade_date <= :end_date
        """
        row = conn.execute(text(sql), {"start_date": start_date, "end_date": end_date}).fetchone()
        return int(row[0] or 0) if row else 0


def _expected_count(period: str, start_date: str, end_date: str) -> int:
    """
    计算预期条数：日线用交易日历计数，分钟线用 trading_days*bards_per_day，
    周/月/季/年用时间范围估算。
    """
    if period == "1d":
        return _count_trading_days(start_date, end_date)

    if period in BARS_PER_DAY_MAP:
        trading_days = _count_trading_days(start_date, end_date)
        return trading_days * BARS_PER_DAY_MAP[period]

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    if end_dt < start_dt:
        return 0

    if period == "1w":
        return (end_dt - start_dt).days // 7 + 1
    if period == "1mon":
        return (end_dt.year - start_dt.year) * 12 + (end_dt.month - start_dt.month) + 1
    if period == "1q":
        return (end_dt.year - start_dt.year) * 4 + ((end_dt.month - 1) // 3) - ((start_dt.month - 1) // 3) + 1
    if period == "1y":
        return end_dt.year - start_dt.year + 1
    return _count_trading_days(start_date, end_date)


def _evaluate_gap_snapshot(
    code: str,
    data_type: str,
    period: str,
    start_date: Optional[str],
    end_date: Optional[str],
    list_date: Optional[str] = None,
) -> dict:
    """
    统一计算某 code/period 的预期、实际、缺失及状态，用于巡检与修复前后对比。
    """
    start = str(start_date or "2020-01-01")[:10]
    end = str(end_date or datetime.now().strftime("%Y-%m-%d"))[:10]

    if data_type == "sector":
        table_name = get_sector_table_name(period)
        date_column = "trade_date"
        if not table_name:
            return {
                "expected": 0,
                "actual": 0,
                "missing": 0,
                "missing_rate": 0.0,
                "first_date": None,
                "last_date": None,
                "status": "error",
                "reason": "unsupported_period",
                "start_date": start,
                "end_date": end,
            }
    else:
        table_name = get_table_name(period)
        date_column = get_date_column(period)

    if data_type in ("stock", "index") and list_date:
        list_date_norm = str(list_date)[:10]
        if list_date_norm > start:
            start = list_date_norm

    actual, first_date, last_date = _count_actual_fast(code, table_name, date_column, start, end)
    effective_start = start
    if first_date and str(first_date) > str(start):
        effective_start = str(first_date)[:10]

    expected = _expected_count(period, effective_start, end)
    missing = max(0, expected - actual)
    missing_rate = (missing / expected * 100) if expected > 0 else 0.0

    if int(actual) == 0:
        status = "no_data"
        reason = "no_data"
    elif missing_rate <= MISSING_RATE_TOLERANCE:
        status = "complete"
        reason = None
    elif missing_rate < 10:
        status = "incomplete"
        reason = None
    else:
        status = "error"
        reason = None

    return _apply_manual_confirm_policy(data_type, {
        "expected": int(expected),
        "actual": int(actual),
        "missing": int(missing),
        "missing_rate": round(float(missing_rate), 4),
        "first_date": first_date,
        "last_date": last_date,
        "status": status,
        "reason": reason,
        "start_date": effective_start,
        "end_date": end,
    })

def get_sector_first_kline_date(code: str, period: str) -> Optional[str]:
    """获取板块的第一根K线日期。"""
    table_name = get_table_name(period)
    date_column = get_date_column(period)

    engine = db.engine
    try:
        with engine.connect() as conn:
            sql = f"""
            SELECT MIN({date_column}) as first_date
            FROM {table_name}
            WHERE code = :code
            """
            result = conn.execute(text(sql), {'code': code})
            row = result.fetchone()
            if row and row[0]:
                return str(row[0])
            return None
    except Exception as e:
        logger.error(f"获取板块 {code} 的第一根K线日期失败: {e}")
        return None


def check_single_stock(code: str, name: str, period: str, 
                     start_date: Optional[str], end_date: Optional[str],
                     list_date: Optional[str], data_type: str = 'stock') -> CheckResult:
    """检查单个股票的K线数据完整性"""
    table_name = get_table_name(period)
    date_column = get_date_column(period)
    
    engine = db.engine
    
    try:
        with engine.connect() as conn:
            # 获取实际数据
            if start_date and end_date:
                sql = f"""
                SELECT {date_column}, COUNT(*) as count
                FROM {table_name}
                WHERE code = :code AND {date_column} >= :start_date AND {date_column} <= :end_date
                GROUP BY {date_column}
                ORDER BY {date_column}
                """
                # 使用命名参数传递
                result = conn.execute(text(sql), {'code': code, 'start_date': start_date, 'end_date': end_date})
            else:
                sql = f"""
                SELECT {date_column}, COUNT(*) as count
                FROM {table_name}
                WHERE code = :code
                GROUP BY {date_column}
                ORDER BY {date_column}
                """
                # 使用命名参数传递
                result = conn.execute(text(sql), {'code': code})
            actual_dates = [str(row[0]) for row in result.fetchall()]
            
            if not actual_dates:
                # 检查是否为新股或次新股（上市日期与今天相差不超过5个交易日）
                today = datetime.now().strftime('%Y-%m-%d')
                if data_type == 'sector':
                    # 对于板块，即使没有K线数据，也使用30天前作为起始日期
                    base_start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
                    check_end_date = today
                    
                    # 获取交易日历
                    trading_dates = get_trading_dates_from_db(base_start_date, check_end_date)
                    
                    # 计算预期数据条数
                    total_expected = 0
                    if trading_dates:
                        if period == '1d':
                            total_expected = len(trading_dates)
                        elif period in ['1w', '1mon', '1q', '1y']:
                            start_dt = datetime.strptime(trading_dates[0], '%Y-%m-%d')
                            end_dt = datetime.strptime(trading_dates[-1], '%Y-%m-%d')
                            
                            if period == '1w':
                                total_expected = (end_dt - start_dt).days // 7 + 1
                            elif period == '1mon':
                                total_expected = (end_dt.year - start_dt.year) * 12 + (end_dt.month - start_dt.month) + 1
                            elif period == '1q':
                                total_quarters = (end_dt.year - start_dt.year) * 4 + (end_dt.month - 1) // 3 - (start_dt.month - 1) // 3 + 1
                                total_expected = total_quarters
                            elif period == '1y':
                                total_expected = end_dt.year - start_dt.year + 1
                        elif period in ['1m', '5m', '15m', '30m', '60m']:
                            if period == '1m':
                                minutes_per_day = 240  # 4小时交易时间
                            elif period == '5m':
                                minutes_per_day = 48
                            elif period == '15m':
                                minutes_per_day = 16
                            elif period == '30m':
                                minutes_per_day = 8
                            else:  # 60m
                                minutes_per_day = 4
                            total_expected = len(trading_dates) * minutes_per_day
                    
                    return CheckResult(
                        code=code,
                        name=name,
                        period=period,
                        total_expected=total_expected,
                        actual_count=0,
                        missing_count=total_expected,
                        missing_dates=[],
                        missing_rate=100.0 if total_expected > 0 else 0.0,
                        status="incomplete",
                        last_date=None,
                        first_date=None
                    )
                elif list_date:
                    # 计算上市日期到今天的日历天数差
                    # 处理完整的datetime格式（如 2026-04-01 01:47:46）
                    try:
                        if ' ' in list_date:
                            list_date_obj = datetime.strptime(list_date, '%Y-%m-%d %H:%M:%S')
                        else:
                            list_date_obj = datetime.strptime(list_date, '%Y-%m-%d')
                        today_obj = datetime.strptime(today, '%Y-%m-%d')
                        days_diff = (today_obj - list_date_obj).days
                        # 如果是新股，返回完整状态
                        if days_diff <= 10:
                            return CheckResult(
                                code=code,
                                name=name,
                                period=period,
                                total_expected=0,
                                actual_count=0,
                                missing_count=0,
                                missing_dates=[],
                                missing_rate=0.0,
                                status="complete",
                                last_date=None,
                                first_date=None
                            )
                    except ValueError:
                        # 日期格式错误，忽略
                        pass
                # 否则返回错误状态
                return CheckResult(
                    code=code,
                    name=name,
                    period=period,
                    total_expected=0,
                    actual_count=0,
                    missing_count=0,
                    missing_dates=[],
                    missing_rate=0.0,
                    status="error",
                    last_date=None,
                    first_date=None
                )
            
            first_date = actual_dates[0]
            last_date = actual_dates[-1]
            
            # 检查是否为新股或次新股（上市日期与今天相差不超过5个交易日）
            today = datetime.now().strftime('%Y-%m-%d')
            if list_date:
                # 计算上市日期到今天的交易日数量
                # 处理完整的datetime格式（如 2026-04-01 01:47:46）
                try:
                    if ' ' in list_date:
                        list_date_obj = datetime.strptime(list_date, '%Y-%m-%d %H:%M:%S')
                    else:
                        list_date_obj = datetime.strptime(list_date, '%Y-%m-%d')
                    today_obj = datetime.strptime(today, '%Y-%m-%d')
                    # 先计算日历天数差
                    days_diff = (today_obj - list_date_obj).days
                    # 如果日历天数差不超过10天（约5个交易日），则认为是新股
                    if days_diff <= 10:
                        return CheckResult(
                            code=code,
                            name=name,
                            period=period,
                            total_expected=len(actual_dates),
                            actual_count=len(actual_dates),
                            missing_count=0,
                            missing_dates=[],
                            missing_rate=0.0,
                            status="complete",
                            last_date=last_date,
                            first_date=first_date
                        )
                except ValueError:
                    # 日期格式错误，忽略
                    pass
            
            # 处理2020年以前的数据：不判断完整性
            if first_date < '2020-01-01':
                return CheckResult(
                    code=code,
                    name=name,
                    period=period,
                    total_expected=len(actual_dates),
                    actual_count=len(actual_dates),
                    missing_count=0,
                    missing_dates=[],
                    missing_rate=0.0,
                    status="complete",
                    last_date=last_date,
                    first_date=first_date
                )
            
            # 2020年以后的数据：使用交易日历判断
            # 确定检查的起始日期：取 start_date、2020-01-01 和股票上市日期中的最大值
            base_start_date = '2020-01-01'
            if start_date and start_date > base_start_date:
                base_start_date = start_date
            
            # 对于板块，忽略 list_date，直接使用30天前的日期作为起始日期
            if data_type == 'sector':
                # 尝试获取板块的第一根K线数据作为第一天
                sector_first_date = get_sector_first_kline_date(code, period)
                if sector_first_date:
                    base_start_date = sector_first_date
                else:
                    # 如果历史上没有K线，那么就取30天前的日期
                    base_start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
            elif list_date and list_date > base_start_date:
                # 对于非板块数据，使用 list_date
                base_start_date = list_date
            
            check_start_date = base_start_date
            check_end_date = end_date if end_date else last_date
            
            # 获取交易日历
            trading_dates = get_trading_dates_from_db(check_start_date, check_end_date)
            
            if period == '1d':
                # 日线：直接使用交易日历
                total_expected = len(trading_dates)
            elif period in ['1w', '1mon', '1q', '1y']:
                # 周线、月线、季线、年线：计算周期数
                if not trading_dates:
                    total_expected = 0
                else:
                    start_dt = datetime.strptime(trading_dates[0], '%Y-%m-%d')
                    end_dt = datetime.strptime(trading_dates[-1], '%Y-%m-%d')
                    
                    if period == '1w':
                        total_expected = (end_dt - start_dt).days // 7 + 1
                    elif period == '1mon':
                        total_expected = (end_dt.year - start_dt.year) * 12 + (end_dt.month - start_dt.month) + 1
                    elif period == '1q':
                        total_quarters = (end_dt.year - start_dt.year) * 4 + (end_dt.month - 1) // 3 - (start_dt.month - 1) // 3 + 1
                        total_expected = total_quarters
                    elif period == '1y':
                        total_expected = end_dt.year - start_dt.year + 1
            elif period in ['1m', '5m', '15m', '30m', '60m']:
                # 分钟线：交易日数量 × 每分钟数据条数
                if not trading_dates:
                    total_expected = 0
                else:
                    if period == '1m':
                        minutes_per_day = 240  # 4小时交易时间
                    elif period == '5m':
                        minutes_per_day = 48
                    elif period == '15m':
                        minutes_per_day = 16
                    elif period == '30m':
                        minutes_per_day = 8
                    else:  # 60m
                        minutes_per_day = 4
                    total_expected = len(trading_dates) * minutes_per_day
            else:
                # 其他周期：使用简化计算
                start_dt = datetime.strptime(check_start_date, '%Y-%m-%d')
                end_dt = datetime.strptime(check_end_date, '%Y-%m-%d')
                total_days = (end_dt - start_dt).days
                trading_days = total_days * 5 // 7
                total_expected = trading_days
            
            actual_count = len(actual_dates)
            missing_count = max(0, total_expected - actual_count)
            missing_rate = (missing_count / total_expected * 100) if total_expected > 0 else 0.0
            
            # 判断状态
            if missing_rate == 0:
                status = "complete"
            elif missing_rate < 10:
                status = "incomplete"
            else:
                status = "error"
            
            # 获取缺失日期（只返回前100个）
            missing_dates = []
            if missing_count > 0 and missing_count <= 100:
                # 计算缺失的日期
                actual_dates_set = set(actual_dates)
                
                # 根据周期生成预期日期列表
                expected_dates = []
                if period == '1d':
                    expected_dates = trading_dates
                elif period in ['1w', '1mon', '1q', '1y']:
                    if trading_dates:
                        start_dt = datetime.strptime(trading_dates[0], '%Y-%m-%d')
                        end_dt = datetime.strptime(trading_dates[-1], '%Y-%m-%d')
                        
                        if period == '1w':
                            current = start_dt
                            while current <= end_dt:
                                week_start = current - timedelta(days=current.weekday())
                                expected_dates.append(week_start.strftime('%Y-%m-%d'))
                                current += timedelta(days=7)
                        elif period == '1mon':
                            current = start_dt
                            while current <= end_dt:
                                month_start = current.replace(day=1)
                                expected_dates.append(month_start.strftime('%Y-%m-%d'))
                                # 移到下一个月
                                if current.month == 12:
                                    current = current.replace(year=current.year + 1, month=1, day=1)
                                else:
                                    current = current.replace(month=current.month + 1, day=1)
                        elif period == '1q':
                            current = start_dt
                            while current <= end_dt:
                                quarter = (current.month - 1) // 3 + 1
                                quarter_start = current.replace(month=(quarter-1)*3 + 1, day=1)
                                expected_dates.append(f"{quarter_start.year}-Q{quarter}")
                                # 移到下一个季度
                                if quarter == 4:
                                    current = current.replace(year=current.year + 1, month=1, day=1)
                                else:
                                    current = current.replace(month=quarter*3 + 1, day=1)
                        elif period == '1y':
                            current = start_dt
                            while current <= end_dt:
                                expected_dates.append(str(current.year))
                                current = current.replace(year=current.year + 1, month=1, day=1)
                
                # 找出缺失的日期
                for expected_date in expected_dates:
                    if expected_date not in actual_dates_set:
                        missing_dates.append(expected_date)
                        if len(missing_dates) >= 100:
                            break
            
            return CheckResult(
                code=code,
                name=name,
                period=period,
                total_expected=total_expected,
                actual_count=actual_count,
                missing_count=missing_count,
                missing_dates=missing_dates,
                missing_rate=missing_rate,
                status=status,
                last_date=last_date,
                first_date=first_date
            )
    except Exception as e:
        logger.error(f"检查股票 {code} {period} 失败: {e}")
        return CheckResult(
            code=code,
            name=name,
            period=period,
            total_expected=0,
            actual_count=0,
            missing_count=0,
            missing_dates=[],
            missing_rate=0.0,
            status="error",
            last_date=None,
            first_date=None
        )


@router.get("/check-single")
def check_single(
    code: str = Query(..., description="股票代码"),
    name: str = Query(..., description="股票名称"),
    period: str = Query(..., description="K线周期"),
    start_date: Optional[str] = Query(None, description="开始日期"),
    end_date: Optional[str] = Query(None, description="结束日期"),
    list_date: Optional[str] = Query(None, description="上市日期"),
    data_type: str = Query("stock", description="数据类型: stock, index, sector")
):
    """
    检查单个股票的K线数据完整性
    """
    result = check_single_stock(code, name, period, start_date, end_date, list_date, data_type)
    return {
        "code": result.code,
        "name": result.name,
        "period": result.period,
        "total_expected": result.total_expected,
        "actual_count": result.actual_count,
        "missing_count": result.missing_count,
        "missing_dates": result.missing_dates,
        "missing_rate": result.missing_rate,
        "status": result.status,
        "last_date": result.last_date,
        "first_date": result.first_date
    }


@router.get("/health")
def health_check():
    """
    健康检查接口
    """
    return {"status": "ok", "service": "Kline Check API"}


@router.post("/check")
async def check_kline_data(
    request: Request,
    data_type: str = Body(..., description="数据类型: stock, index, sector"),
    period: str = Body(..., description="K线周期"),
    time_mode: str = Body(..., description="时间模式: full, recent")
):
    """
    检查K线数据完整性（POST请求）
    """
    try:
        body = await request.json()
        logger.info(f"接收到K线检查请求体: {body}")
        logger.info(f"接收到K线检查请求参数: data_type={data_type}, period={period}, time_mode={time_mode}")
    except Exception as e:
        logger.error(f"解析请求体失败: {e}")
    
    # 生成任务ID
    task_id = str(uuid.uuid4())
    
    # 异步处理检查任务
    import asyncio
    asyncio.create_task(process_check_task(task_id, data_type, period, time_mode))
    
    return {"task_id": task_id}


async def process_check_task(task_id: str, data_type: str, period: str, time_mode: str):
    """
    异步处理K线检查任务
    """
    try:
        # 获取要检查的代码列表
        codes = get_codes_by_data_type(data_type)
        
        # 计算日期范围
        end_date = datetime.now().strftime('%Y-%m-%d')
        if time_mode == 'recent':
            # 最近两周
            start_date = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
        else:
            # 历史全量
            start_date = '2020-01-01'
        
        # 初始化进度信息
        total = len(codes)
        processed = 0
        complete_count = 0
        pending_confirm_count = 0
        incomplete_count = 0
        error_count = 0
        
        # 存储检查结果
        results = []
        
        # 开始检查
        for code_info in codes:
            code = code_info['code']
            name = code_info['name']
            
            try:
                # 检查单个股票
                result = check_single_stock(code, name, period, start_date, end_date, None, data_type)
                result_status = result.status
                if _should_mark_pending_confirm(data_type, result.actual_count, result.status):
                    result_status = "pending_confirm"

                results.append({
                    "code": result.code,
                    "name": result.name,
                    "period": result.period,
                    "total_expected": result.total_expected,
                    "actual_count": result.actual_count,
                    "missing_count": result.missing_count,
                    "missing_rate": result.missing_rate,
                    "status": result_status,
                    "first_date": result.first_date,
                    "last_date": result.last_date
                })
                
                # 更新进度
                processed += 1
                if result_status == 'complete':
                    complete_count += 1
                elif result_status == 'pending_confirm':
                    pending_confirm_count += 1
                elif result_status == 'incomplete':
                    incomplete_count += 1
                else:
                    error_count += 1
                
                # 更新进度信息到缓存
                update_check_progress(task_id, {
                    "total": total,
                    "processed": processed,
                    "complete": complete_count,
                    "pending_confirm": pending_confirm_count,
                    "incomplete": incomplete_count,
                    "error": error_count,
                    "status": "running"
                })
                
                # 避免请求过快
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"检查 {code} 失败: {e}")
                processed += 1
                error_count += 1
                results.append({
                    "code": code,
                    "name": name,
                    "period": period,
                    "status": "error",
                    "error": str(e)
                })
                
                # 更新进度信息到缓存
                update_check_progress(task_id, {
                    "total": total,
                    "processed": processed,
                    "complete": complete_count,
                    "pending_confirm": pending_confirm_count,
                    "incomplete": incomplete_count,
                    "error": error_count,
                    "status": "running"
                })
        
        # 任务完成
        update_check_progress(task_id, {
            "total": total,
            "processed": processed,
            "complete": complete_count,
            "pending_confirm": pending_confirm_count,
            "incomplete": incomplete_count,
            "error": error_count,
            "status": "completed"
        })
        
        # 存储检查结果
        store_check_results(task_id, results)
        
        logger.info(f"K线检查任务 {task_id} 完成，共检查 {total} 个代码")
        
    except Exception as e:
        logger.error(f"执行检查任务失败: {e}")
        # 更新任务状态为失败
        update_check_progress(task_id, {
            "status": "failed",
            "error": str(e)
        })


def get_codes_by_data_type(data_type: str) -> list:
    """
    根据数据类型获取代码列表
    """
    if data_type == 'stock':
        # 从数据库获取股票列表
        session = next(db.get_session())
        try:
            from models.stock_models import Stock
            stocks = session.query(Stock).filter(Stock.type == 'stock', Stock.quit == False).all()
            return [{'code': stock.code, 'name': stock.name} for stock in stocks]
        finally:
            session.close()
    elif data_type == 'index':
        # 从数据库获取指数列表
        session = next(db.get_session())
        try:
            from models.stock_models import Stock
            indices = session.query(Stock).filter(Stock.type == 'index').all()
            return [{'code': index.code, 'name': index.name} for index in indices]
        finally:
            session.close()
    elif data_type == 'sector':
        # 从数据库获取板块列表
        session = next(db.get_session())
        try:
            from models.stock_models import Sector
            sectors = session.query(Sector).all()
            return [{'code': sector.code, 'name': sector.name} for sector in sectors]
        finally:
            session.close()
    else:
        return []


# 缓存存储检查进度和结果
check_progress_cache = {}
check_results_cache = {}
check_all_results_cache = {}

# 修复任务缓存
repair_progress_cache = {}
repair_results_cache = {}


def update_check_progress(task_id: str, progress: dict):
    """
    更新检查进度
    """
    check_progress_cache[task_id] = progress


def store_check_results(task_id: str, results: list):
    """
    存储检查结果
    """
    check_results_cache[task_id] = results


def store_check_all_results(task_id: str, payload: dict):
    check_all_results_cache[task_id] = payload


def update_repair_progress(task_id: str, progress: dict):
    repair_progress_cache[task_id] = progress


def store_repair_results(task_id: str, payload: dict):
    repair_results_cache[task_id] = payload


@router.get("/progress/{task_id}")
def get_check_progress(task_id: str):
    """
    获取检查进度
    """
    progress = check_progress_cache.get(task_id, {
        "total": 0,
        "processed": 0,
        "complete": 0,
        "pending_confirm": 0,
        "incomplete": 0,
        "error": 0,
        "status": "not_found"
    })
    return progress


@router.get("/results/{task_id}")
def get_check_results(task_id: str):
    """
    获取检查结果
    """
    results = check_results_cache.get(task_id, [])
    return {"results": results}


@router.post("/check-all")
async def check_kline_data_all(
    limit_codes: int = Body(1000, description="返回问题最严重的 code 数量上限（TopN）"),
    start_base_date: str = Body("2020-01-01", description="非分钟线的默认巡检起始日期"),
):
    """
    一键巡检：所有类型(股票/指数/板块) + 所有周期，结果按 code 聚合并返回 TopN 问题最严重 code。
    """
    task_id = str(uuid.uuid4())
    # 注意：巡检过程包含大量同步数据库查询，不能跑在事件循环里，否则会阻塞整个 API 导致超时
    threading.Thread(
        target=process_check_all_task,
        args=(task_id, limit_codes, start_base_date),
        daemon=True,
    ).start()
    return {"task_id": task_id}


@router.get("/results-all/{task_id}")
def get_check_all_results(task_id: str):
    payload = check_all_results_cache.get(task_id)
    if not payload:
        return {"items": [], "summary": None, "generated_at": None}
    return payload


def process_check_all_task(task_id: str, limit_codes: int, start_base_date: str):
    """
    执行全量 K 线检查，并返回 TopN 问题代码。

    说明：
    - missing_rate <= MISSING_RATE_TOLERANCE 视为 complete
    - no_data 表示当前时间窗口内未拉到有效据
æªæå°æææ°æ®
    """
    try:
        stock_codes = get_codes_by_data_type("stock")
        index_codes = get_codes_by_data_type("index")
        sector_codes = get_codes_by_data_type("sector")

        all_codes = (
            [{"data_type": "stock", **item} for item in stock_codes]
            + [{"data_type": "index", **item} for item in index_codes]
            + [{"data_type": "sector", **item} for item in sector_codes]
        )

        total = len(all_codes)
        processed = 0
        problem_codes = 0
        pending_confirm_codes = 0
        actionable_problem_codes = 0
        no_data_codes = 0

        update_check_progress(
            task_id,
            {
                "total": total,
                "processed": 0,
                "complete": 0,
                "pending_confirm": 0,
                "incomplete": 0,
                "no_data": 0,
                "error": 0,
                "status": "running",
            },
        )

        heap = []
        seq = 0
        today = datetime.now().strftime("%Y-%m-%d")

        list_date_map = {}
        try:
            session = next(db.get_session())
            try:
                from models.stock_models import Stock

                rows = session.query(Stock.code, Stock.list_date).filter(Stock.type.in_(["stock", "index"])).all()
                for code_, ld in rows:
                    if code_:
                        list_date_map[str(code_)] = ld.isoformat() if ld else None
            finally:
                session.close()
        except Exception as e:
            logger.warning(f"获取 list_date 失败，已回退到 start_base_date: {e}")

        periods = ["1d"]
        limit_codes = max(1, int(limit_codes or 1000))

        for info in all_codes:
            code = info["code"]
            name = info.get("name") or ""
            data_type = info["data_type"]

            complete_periods = []
            pending_confirm_periods = []
            incomplete_periods = []
            error_periods = []
            no_data_periods = []
            period_stats = {}

            max_missing_rate = 0.0
            error_period_count = 0

            for period in periods:
                try:
                    snap = _evaluate_gap_snapshot(
                        code=code,
                        data_type=data_type,
                        period=period,
                        start_date=start_base_date,
                        end_date=today,
                        list_date=list_date_map.get(str(code)) if data_type in ("stock", "index") else None,
                    )
                except Exception as e:
                    logger.warning(f"检查快照失败 {data_type} {code} {period}: {e}")
                    snap = {
                        "expected": 0,
                        "actual": 0,
                        "missing": 0,
                        "missing_rate": 0.0,
                        "first_date": None,
                        "last_date": None,
                        "status": "error",
                        "error": str(e),
                        "start_date": str(start_base_date)[:10],
                        "end_date": today,
                    }

                status = snap.get("status")
                if status == "complete":
                    complete_periods.append(period)
                elif status == "pending_confirm":
                    pending_confirm_periods.append(period)
                elif status == "incomplete":
                    incomplete_periods.append(period)
                elif status == "no_data":
                    no_data_periods.append(period)
                else:
                    error_periods.append(period)
                    error_period_count += 1

                max_missing_rate = max(max_missing_rate, float(snap.get("missing_rate") or 0.0))
                period_stats[period] = snap

            score = float(max_missing_rate) + float(error_period_count) * 100.0
            has_problem = len(pending_confirm_periods) > 0 or len(incomplete_periods) > 0 or len(error_periods) > 0
            has_actionable_problem = len(incomplete_periods) > 0 or len(error_periods) > 0
            is_no_data_only = (not has_problem) and (len(no_data_periods) > 0)

            if has_problem:
                problem_codes += 1
                if pending_confirm_periods:
                    pending_confirm_codes += 1
                if has_actionable_problem:
                    actionable_problem_codes += 1
                item = {
                    "code": code,
                    "name": name,
                    "data_type": data_type,
                    "complete_periods": complete_periods,
                    "pending_confirm_periods": pending_confirm_periods,
                    "incomplete_periods": incomplete_periods,
                    "error_periods": error_periods,
                    "no_data_periods": no_data_periods,
                    "period_stats": period_stats,
                    "score": round(score, 4),
                    "worst_period": None,
                }

                worst = None
                worst_val = -1
                for p, s in period_stats.items():
                    val = float(s.get("missing_rate") or 0.0)
                    if s.get("status") == "error":
                        val = 999.0
                    if val > worst_val:
                        worst_val = val
                        worst = p
                item["worst_period"] = worst

                seq += 1
                if len(heap) < limit_codes:
                    heapq.heappush(heap, (score, seq, item))
                elif heap and score > heap[0][0]:
                    heapq.heapreplace(heap, (score, seq, item))
            elif is_no_data_only:
                no_data_codes += 1

            processed += 1
            update_check_progress(
                task_id,
                {
                    "total": total,
                    "processed": processed,
                    "complete": max(0, processed - problem_codes - no_data_codes),
                    "pending_confirm": pending_confirm_codes,
                    "incomplete": actionable_problem_codes,
                    "no_data": no_data_codes,
                    "error": 0,
                    "status": "running",
                },
            )

            if processed % 200 == 0:
                try:
                    import time
                    time.sleep(0)
                except Exception:
                    pass

        items = [x[2] for x in heap]
        items.sort(key=lambda x: x.get("score", 0), reverse=True)

        payload = {
            "items": items,
            "summary": {
                "total_codes": total,
                "problem_codes": problem_codes,
                "pending_confirm_codes": pending_confirm_codes,
                "actionable_problem_codes": actionable_problem_codes,
                "no_data_codes": no_data_codes,
                "complete_codes": max(0, processed - problem_codes - no_data_codes),
                "returned_codes": len(items),
                "periods_stock_index": ["1d"],
                "periods_sector": ["1d"],
                "start_base_date": start_base_date,
                "missing_rate_tolerance": MISSING_RATE_TOLERANCE,
            },
            "generated_at": datetime.now().isoformat(),
        }
        store_check_all_results(task_id, payload)

        try:
            from models.stock_models import KlineCheckResult

            session = next(db.get_session())
            try:
                rec = KlineCheckResult(
                    check_batch_id=task_id.replace("-", "")[:32],
                    check_time=datetime.now(),
                    data_type="all",
                    period="1d",
                    time_mode="full",
                    total_checked=int(total),
                    complete_count=int(max(0, processed - problem_codes - no_data_codes)),
                    incomplete_count=int(pending_confirm_codes + actionable_problem_codes),
                    error_count=0,
                    details=json.dumps(payload, ensure_ascii=False),
                    status="completed",
                    error_message=None,
                )
                session.add(rec)
                session.commit()
            finally:
                try:
                    session.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"保存检查记录失败: {e}")

        update_check_progress(
            task_id,
            {
                "total": total,
                "processed": processed,
                "complete": max(0, processed - problem_codes - no_data_codes),
                "pending_confirm": pending_confirm_codes,
                "incomplete": actionable_problem_codes,
                "no_data": no_data_codes,
                "error": 0,
                "status": "completed",
            },
        )
        logger.info(
            f"检查任务 {task_id} 完成: total={total}, problem={problem_codes}, no_data={no_data_codes}, returned={len(items)}"
        )
    except Exception as e:
        logger.error(f"执行检查任务失败: {e}")
        update_check_progress(task_id, {"status": "failed", "error": str(e)})

        try:
            from models.stock_models import KlineCheckResult

            session = next(db.get_session())
            try:
                rec = KlineCheckResult(
                    check_batch_id=task_id.replace("-", "")[:32],
                    check_time=datetime.now(),
                    data_type="all",
                    period="1d",
                    time_mode="full",
                    total_checked=0,
                    complete_count=0,
                    incomplete_count=0,
                    error_count=1,
                    details=None,
                    status="failed",
                    error_message=str(e),
                )
                session.add(rec)
                session.commit()
            finally:
                try:
                    session.close()
                except Exception:
                    pass
        except Exception:
            pass


@router.post("/repair")
async def repair_kline_data(
    data_type: str = Body(..., description="数据类型: stock, index, sector"),
    period: str = Body(..., description="K线周期"),
    start_date: Optional[str] = Body(None, description="开始日期"),
    end_date: Optional[str] = Body(None, description="结束日期"),
    codes: List[str] = Body(..., description="要修复的代码列表")
):
    """
    修复K线数据
    """
    logger.info(f"接收到修复请求: data_type={data_type}, period={period}, codes={codes}")
    
    # 生成任务ID
    task_id = str(uuid.uuid4())
    
    # 异步处理修复任务
    import asyncio
    asyncio.create_task(process_repair_task(task_id, data_type, period, start_date, end_date, codes))
    
    return {"task_id": task_id}


async def process_repair_task(task_id: str, data_type: str, period: str, start_date: Optional[str], end_date: Optional[str], codes: List[str]):
    """
    异步处理K线修复任务
    """
    try:
        logger.info(f"开始修复任务 {task_id}: {codes}")
        
        # 这里应该实现实际的修复逻辑
        # 例如调用数据同步函数重新获取数据
        
        # 模拟修复过程
        for code in codes:
            logger.info(f"修复 {code} 的 {period} 数据")
            await asyncio.sleep(0.5)
        
        logger.info(f"修复任务 {task_id} 完成")
        
    except Exception as e:
        logger.error(f"处理修复任务失败: {e}")


@router.post("/repair-trade-date")
async def repair_trade_date_data(
    request: dict = Body(..., description="按交易日修复请求：{trade_date:'YYYY-MM-DD', data_type?: 'stock'|'index'|'all'}"),
):
    trade_date = str((request or {}).get("trade_date") or "").strip()
    if not trade_date:
        raise HTTPException(status_code=400, detail="trade_date 不能为空")

    data_type = str((request or {}).get("data_type") or "all").strip().lower() or "all"
    max_workers = int((request or {}).get("max_workers") or 2)
    task_id = str(uuid.uuid4())

    threading.Thread(
        target=process_repair_trade_date_task,
        args=(task_id, trade_date, data_type, max_workers),
        daemon=True,
    ).start()
    return {"task_id": task_id}


def process_repair_trade_date_task(task_id: str, trade_date: str, data_type: str = "all", max_workers: int = 2):
    from scripts.sync_all_klines import KlineSyncer

    update_repair_progress(
        task_id,
        {
            "total": 1,
            "processed": 0,
            "success": 0,
            "failed": 0,
            "status": "running",
            "current": f"{trade_date} 1d",
        },
    )

    try:
        syncer = KlineSyncer()
        normalized_type = None if data_type in ("", "all", "none") else data_type
        result = syncer.repair_trade_date(
            trade_date=trade_date,
            sync_type=normalized_type,
            max_workers=max(1, max_workers),
        )

        update_repair_progress(
            task_id,
            {
                "total": 1,
                "processed": 1,
                "success": 1 if result["status"] == "completed" else 0,
                "failed": 0 if result["status"] == "completed" else 1,
                "status": "completed" if result["status"] == "completed" else "failed",
                "current": None,
            },
        )
        store_repair_results(
            task_id,
            {
                "items": [
                    {
                        "trade_date": trade_date,
                        "data_type": normalized_type or "all",
                        "period": "1d",
                        "success": result["status"] == "completed",
                        "before_count": result["before"]["actual_count"],
                        "after_count": result["after"]["actual_count"],
                        "baseline_count": result["after"]["baseline_count"],
                        "minimum_required": result["after"]["minimum_required"],
                        "remaining_count": result["remaining_count"],
                        "remaining_codes": result["remaining_codes"],
                    }
                ],
                "summary": {
                    "total": 1,
                    "success": 1 if result["status"] == "completed" else 0,
                    "failed": 0 if result["status"] == "completed" else 1,
                    "trade_date": trade_date,
                },
                "generated_at": datetime.now().isoformat(),
                "result": result,
            },
        )
    except Exception as e:
        logger.error(f"按交易日修复失败 trade_date={trade_date}: {e}")
        update_repair_progress(
            task_id,
            {
                "total": 1,
                "processed": 1,
                "success": 0,
                "failed": 1,
                "status": "failed",
                "error": str(e),
                "current": None,
            },
        )
        store_repair_results(
            task_id,
            {
                "items": [],
                "summary": {"total": 1, "success": 0, "failed": 1, "trade_date": trade_date},
                "generated_at": datetime.now().isoformat(),
                "error": str(e),
            },
        )


@router.post("/repair-batch")
async def repair_kline_data_batch(
    request: dict = Body(..., description="修复请求：{items:[{code,data_type,periods:[...]}]}"),
):
    """
    分批修复：对选中的 code 按指定周期执行“重拉修复”。

    - stock/index：调用 scripts.sync_all_klines.KlineSyncer.sync_kline_for_stock 进行重拉同步
    - sector：暂不支持（仅保留巡检），后续如有板块K线同步器再接入
    """
    items = (request or {}).get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items 必须为非空数组")

    # 可选：修复日期范围（默认由同步器自己决定；为了“补齐缺失日线”，前端会传入巡检起始日）
    start_date = (request or {}).get("start_date")
    end_date = (request or {}).get("end_date")

    task_id = str(uuid.uuid4())
    # 注意：修复会调用同步数据源/数据库写入，不能跑在事件循环里，否则会阻塞 API
    threading.Thread(
        target=process_repair_batch_task,
        args=(task_id, items, start_date, end_date),
        daemon=True,
    ).start()
    return {"task_id": task_id}


@router.get("/repair-progress/{task_id}")
def get_repair_progress(task_id: str):
    return repair_progress_cache.get(
        task_id,
        {"total": 0, "processed": 0, "success": 0, "failed": 0, "status": "not_found", "current": None},
    )


@router.get("/repair-results/{task_id}")
def get_repair_results(task_id: str):
    payload = repair_results_cache.get(task_id)
    if not payload:
        return {"items": [], "summary": None, "generated_at": None}
    return payload


def process_repair_batch_task(task_id: str, items: list, start_date: Optional[str] = None, end_date: Optional[str] = None):
    """
    按批次修复 K 线数据。
    每个项目可同时指定多个 code/period 组合进行修复。
    """
    from scripts.sync_all_klines import KlineSyncer
    from datetime import datetime as _dt

    total = 0
    normalized = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        code = raw.get("code")
        data_type = raw.get("data_type")
        periods = raw.get("periods") or []
        if not code or not data_type or not isinstance(periods, list) or not periods:
            continue
        periods = [str(p) for p in periods if p]
        if not periods:
            continue
        normalized.append({"code": str(code), "data_type": str(data_type), "periods": periods})
        total += len(periods)

    if total == 0:
        update_repair_progress(task_id, {"total": 0, "processed": 0, "success": 0, "failed": 0, "status": "failed", "error": "没有可修复的项目", "current": None})
        store_repair_results(task_id, {"items": [], "summary": {"total": 0, "success": 0, "failed": 0}, "generated_at": datetime.now().isoformat()})
        return

    processed = 0
    success = 0
    failed = 0
    improved = 0
    became_complete = 0
    result_items = []

    update_repair_progress(task_id, {"total": total, "processed": 0, "success": 0, "failed": 0, "status": "running", "current": None})

    session = next(db.get_session())
    try:
        from models.stock_models import Stock, Sector
        from scripts.generate_sector_kline import generate_sector_klines_for_sector_range

        syncer = KlineSyncer()

        def _parse_date(s: Optional[str]):
            if not s:
                return None
            return _dt.strptime(str(s)[:10], "%Y-%m-%d").date()

        sector_start = _parse_date(start_date) or _dt.strptime("2020-01-01", "%Y-%m-%d").date()
        sector_end = _parse_date(end_date) or _dt.now().date()

        def _to_list_date(value):
            if not value:
                return None
            try:
                return value.isoformat() if hasattr(value, "isoformat") else str(value)[:10]
            except Exception:
                return str(value)[:10]

        for item in normalized:
            code = item["code"]
            data_type = item["data_type"]
            periods = item["periods"]

            if data_type == "sector":
                sector = session.query(Sector).filter(Sector.code == code).first()
                if not sector:
                    for period in periods:
                        processed += 1
                        failed += 1
                        result_items.append({"code": code, "data_type": data_type, "period": period, "success": False, "error": "板块代码不存在"})
                        update_repair_progress(task_id, {"total": total, "processed": processed, "success": success, "failed": failed, "status": "running", "current": f"{code} {period}"})
                    continue

                for period in periods:
                    ok = False
                    err = None
                    before_snap = _evaluate_gap_snapshot(code, data_type, period, start_date, end_date, None)
                    after_snap = before_snap
                    try:
                        if period != "1d":
                            raise ValueError("板块仅支持 1d 周期")
                        stat = generate_sector_klines_for_sector_range(session, code, sector_start, sector_end)
                        ok = bool(stat.get("saved", 0) > 0)
                        after_snap = _evaluate_gap_snapshot(code, data_type, period, start_date, end_date, None)
                    except Exception as e:
                        ok = False
                        err = str(e)

                    processed += 1
                    if ok:
                        success += 1
                    else:
                        failed += 1

                    miss_delta = int((after_snap or {}).get("missing", 0)) - int((before_snap or {}).get("missing", 0))
                    rate_delta = round(float((after_snap or {}).get("missing_rate", 0.0)) - float((before_snap or {}).get("missing_rate", 0.0)), 4)
                    is_improved = int((after_snap or {}).get("missing", 0)) < int((before_snap or {}).get("missing", 0))
                    is_became_complete = (before_snap or {}).get("status") != "complete" and (after_snap or {}).get("status") == "complete"
                    if is_improved:
                        improved += 1
                    if is_became_complete:
                        became_complete += 1

                    result_items.append({
                        "code": code,
                        "name": sector.name,
                        "data_type": data_type,
                        "period": period,
                        "success": ok,
                        "error": err,
                        "before": before_snap,
                        "after": after_snap,
                        "missing_delta": miss_delta,
                        "missing_rate_delta": rate_delta,
                        "improved": is_improved,
                        "became_complete": is_became_complete,
                    })
                    update_repair_progress(task_id, {"total": total, "processed": processed, "success": success, "failed": failed, "status": "running", "current": f"{code} {period}"})
                continue

            if data_type not in ("stock", "index"):
                for period in periods:
                    processed += 1
                    failed += 1
                    result_items.append({"code": code, "data_type": data_type, "period": period, "success": False, "error": "不支持的数据类型"})
                    update_repair_progress(task_id, {"total": total, "processed": processed, "success": success, "failed": failed, "status": "running", "current": f"{code} {period}"})
                continue

            stock = session.query(Stock).filter(Stock.code == code).first()
            if not stock:
                for period in periods:
                    processed += 1
                    failed += 1
                    result_items.append({"code": code, "data_type": data_type, "period": period, "success": False, "error": "代码不存在"})
                    update_repair_progress(task_id, {"total": total, "processed": processed, "success": success, "failed": failed, "status": "running", "current": f"{code} {period}"})
                continue

            stock_dict = {"code": stock.code, "name": stock.name, "type": stock.type, "market": stock.market}
            stock_list_date = _to_list_date(stock.list_date)
            for period in periods:
                ok = False
                err = None
                before_snap = _evaluate_gap_snapshot(code, data_type, period, start_date, end_date, stock_list_date)
                after_snap = before_snap
                try:
                    ok = bool(syncer.sync_kline_for_stock(stock_dict, period, start_date=start_date, end_date=end_date))
                    after_snap = _evaluate_gap_snapshot(code, data_type, period, start_date, end_date, stock_list_date)
                except Exception as e:
                    ok = False
                    err = str(e)

                processed += 1
                if ok:
                    success += 1
                else:
                    failed += 1

                miss_delta = int((after_snap or {}).get("missing", 0)) - int((before_snap or {}).get("missing", 0))
                rate_delta = round(float((after_snap or {}).get("missing_rate", 0.0)) - float((before_snap or {}).get("missing_rate", 0.0)), 4)
                is_improved = int((after_snap or {}).get("missing", 0)) < int((before_snap or {}).get("missing", 0))
                is_became_complete = (before_snap or {}).get("status") != "complete" and (after_snap or {}).get("status") == "complete"
                if is_improved:
                    improved += 1
                if is_became_complete:
                    became_complete += 1

                result_items.append({
                    "code": code,
                    "name": stock.name,
                    "data_type": data_type,
                    "period": period,
                    "success": ok,
                    "error": err,
                    "before": before_snap,
                    "after": after_snap,
                    "missing_delta": miss_delta,
                    "missing_rate_delta": rate_delta,
                    "improved": is_improved,
                    "became_complete": is_became_complete,
                })
                update_repair_progress(task_id, {"total": total, "processed": processed, "success": success, "failed": failed, "status": "running", "current": f"{code} {period}"})

                if processed % 20 == 0:
                    try:
                        import time
                        time.sleep(0)
                    except Exception:
                        pass

    finally:
        try:
            session.close()
        except Exception:
            pass

    summary = {
        "total": total,
        "success": success,
        "failed": failed,
        "improved": improved,
        "became_complete": became_complete,
    }
    store_repair_results(task_id, {"items": result_items, "summary": summary, "generated_at": datetime.now().isoformat()})
    update_repair_progress(task_id, {"total": total, "processed": processed, "success": success, "failed": failed, "status": "completed", "current": None})

    try:
        from models.stock_models import KlineCheckResult

        session2 = next(db.get_session())
        try:
            payload = {"items": result_items, "summary": summary, "generated_at": datetime.now().isoformat()}
            rec = KlineCheckResult(
                check_batch_id=task_id.replace("-", "")[:32],
                check_time=datetime.now(),
                data_type="repair",
                period="1d",
                time_mode="repair",
                total_checked=int(total),
                complete_count=int(success),
                incomplete_count=int(failed),
                error_count=0,
                details=json.dumps(payload, ensure_ascii=False),
                status="completed",
                error_message=None,
            )
            session2.add(rec)
            session2.commit()
        finally:
            try:
                session2.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"保存修复记录失败: {e}")


@router.get("/history")
def get_check_history(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页数量"),
    data_type: Optional[str] = Query(None, description="数据类型过滤: stock, index, sector, all, repair"),
    period: Optional[str] = Query(None, description="周期过滤，如 1d"),
):
    """
    获取 K 线检查历史记录。
    """
    from models.stock_models import KlineCheckResult
    
    session = next(db.get_session())
    try:
        # 计算偏移量
        skip = (page - 1) * page_size
        
        # 查询检查历史记录
        query = session.query(KlineCheckResult).order_by(KlineCheckResult.check_time.desc())

        if data_type:
            query = query.filter(KlineCheckResult.data_type == data_type)
        if period:
            query = query.filter(KlineCheckResult.period == period)
        
        # 获取总数
        total = query.count()
        
        # 分页查询
        history_records = query.offset(skip).limit(page_size).all()
        
        # 转换为响应格式
        records = []
        for record in history_records:
            details_obj = None
            pending_confirm_count = 0
            actionable_problem_count = record.incomplete_count
            if record.details:
                try:
                    details_obj = json.loads(record.details)
                    summary = (details_obj or {}).get("summary") or {}
                    pending_confirm_count = int(summary.get("pending_confirm_codes") or 0)
                    actionable_problem_count = int(summary.get("actionable_problem_codes") or record.incomplete_count or 0)
                except Exception:
                    details_obj = None
            records.append({
                "id": record.id,
                "check_batch_id": record.check_batch_id,
                "check_time": record.check_time.isoformat() if record.check_time else None,
                "data_type": record.data_type,
                "period": record.period,
                "time_mode": record.time_mode,
                "total_checked": record.total_checked,
                "complete_count": record.complete_count,
                "incomplete_count": record.incomplete_count,
                "pending_confirm_count": pending_confirm_count,
                "actionable_problem_count": actionable_problem_count,
                "error_count": record.error_count,
                "status": record.status,
                "created_at": record.created_at.isoformat() if record.created_at else None
            })
        
        return {
            "items": records,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size
        }
    except Exception as e:
        logger.error(f"获取检查历史失败: {e}")
        raise HTTPException(status_code=500, detail="获取检查历史失败")
    finally:
        session.close()


@router.get("/history/{check_id}")
def get_check_history_detail(check_id: int):
    """
    获取历史记录详情（包含 details JSON）。
    """
    from models.stock_models import KlineCheckResult

    session = next(db.get_session())
    try:
        record = session.query(KlineCheckResult).filter(KlineCheckResult.id == check_id).first()
        if not record:
            raise HTTPException(status_code=404, detail="记录不存在")

        details_obj = None
        if record.details:
            try:
                details_obj = json.loads(record.details)
            except Exception:
                details_obj = record.details

        return {
            "id": record.id,
            "check_batch_id": record.check_batch_id,
            "check_time": record.check_time.isoformat() if record.check_time else None,
            "data_type": record.data_type,
            "period": record.period,
            "time_mode": record.time_mode,
            "total_checked": record.total_checked,
            "complete_count": record.complete_count,
            "incomplete_count": record.incomplete_count,
            "pending_confirm_count": int((((details_obj or {}).get("summary") or {}).get("pending_confirm_codes") or 0) if isinstance(details_obj, dict) else 0),
            "actionable_problem_count": int((((details_obj or {}).get("summary") or {}).get("actionable_problem_codes") or record.incomplete_count or 0) if isinstance(details_obj, dict) else (record.incomplete_count or 0)),
            "error_count": record.error_count,
            "status": record.status,
            "error_message": record.error_message,
            "created_at": record.created_at.isoformat() if record.created_at else None,
            "details": details_obj,
        }
    finally:
        session.close()

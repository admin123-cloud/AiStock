"""交易日历工具（ClickHouse 查询）"""
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Optional, List

from utils.logger import get_logger
from utils.market_warehouse import clickhouse_scalar, clickhouse_query_df, clickhouse_available, clickhouse_table_exists

logger = get_logger("TradingCalendar")


class TradingCalendar:
    """交易日历"""

    WEEKEND_DAYS = [5, 6]  # 周六、周日
    MORNING_START = dt_time(9, 30)
    MORNING_END = dt_time(11, 30)
    AFTERNOON_START = dt_time(13, 0)
    AFTERNOON_END = dt_time(15, 0)

    _last_calendar_warning_at: Optional[datetime] = None
    _calendar_warning_interval_sec = 300

    @classmethod
    def _warn_calendar_fallback(cls, message: str) -> None:
        now = datetime.now()
        if (
            cls._last_calendar_warning_at is None
            or (now - cls._last_calendar_warning_at).total_seconds() >= cls._calendar_warning_interval_sec
        ):
            logger.warning(message)
            cls._last_calendar_warning_at = now

    @classmethod
    def _db_calendar_available(cls) -> bool:
        if clickhouse_available():
            ok = clickhouse_table_exists("trade_calendar")
            if not ok:
                cls._warn_calendar_fallback(
                    "trade_calendar 在 ClickHouse 中不存在，回退到周末判断"
                )
            return ok
        return True

    @staticmethod
    def _normalize_db_date(value: Any):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
            try:
                return datetime(int(value.year), int(value.month), int(value.day)).date()
            except Exception:
                pass
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    @classmethod
    def _db_calendar_bounds(cls):
        min_dt = clickhouse_scalar(
            """
            SELECT min(trade_date)
            FROM trade_calendar
            WHERE market = 'SH' AND is_trading = 1
            """
        )
        max_dt = clickhouse_scalar(
            """
            SELECT max(trade_date)
            FROM trade_calendar
            WHERE market = 'SH' AND is_trading = 1
            """
        )
        return cls._normalize_db_date(min_dt), cls._normalize_db_date(max_dt)

    @classmethod
    def is_trading_day(cls, date: datetime = None) -> bool:
        if date is None:
            date = datetime.now()

        try:
            target_date = date.date()
            trade_date = target_date.isoformat()
            if cls._db_calendar_available():
                row = clickhouse_scalar(
                    """
                    SELECT is_trading
                    FROM trade_calendar
                    WHERE trade_date = ? AND market = 'SH'
                    LIMIT 1
                    """,
                    [trade_date],
                )
                if row is not None:
                    return bool(row)
                min_date, max_date = cls._db_calendar_bounds()
                if min_date and max_date and min_date <= target_date <= max_date:
                    return False
        except Exception as e:
            cls._warn_calendar_fallback(f"数据库判断交易日失败，回退到周末判断: {e}")

        return date.weekday() not in cls.WEEKEND_DAYS

    @classmethod
    def is_trading_time(cls, now: datetime = None) -> bool:
        if now is None:
            now = datetime.now()

        if not cls.is_trading_day(now):
            return False

        current_time = now.time()
        if cls.MORNING_START <= current_time <= cls.MORNING_END:
            return True
        if cls.AFTERNOON_START <= current_time <= cls.AFTERNOON_END:
            return True
        return False

    @classmethod
    def is_market_open(cls, now: datetime = None) -> bool:
        if now is None:
            now = datetime.now()
        if not cls.is_trading_day(now):
            return False
        current_time = now.time()
        return cls.MORNING_START <= current_time <= cls.AFTERNOON_END

    @classmethod
    def get_next_trading_day(cls, date: datetime = None) -> datetime:
        if date is None:
            date = datetime.now()

        try:
            trade_date = date.date().isoformat()
            if cls._db_calendar_available():
                next_dt = clickhouse_scalar(
                    """
                    SELECT MIN(trade_date)
                    FROM trade_calendar
                    WHERE market = 'SH' AND is_trading = 1 AND trade_date > ?
                    """,
                    [trade_date],
                )
                if next_dt:
                    return datetime.combine(next_dt, date.time())
        except Exception as e:
            logger.warning(f"数据库获取下一个交易日失败，回退计算: {e}")

        next_day = date + timedelta(days=1)
        while not cls.is_trading_day(next_day):
            next_day += timedelta(days=1)
        return next_day

    @classmethod
    def get_previous_trading_day(cls, date: datetime = None) -> datetime:
        if date is None:
            date = datetime.now()

        try:
            trade_date = date.date().isoformat()
            if cls._db_calendar_available():
                prev_dt = clickhouse_scalar(
                    """
                    SELECT MAX(trade_date)
                    FROM trade_calendar
                    WHERE market = 'SH' AND is_trading = 1 AND trade_date < ?
                    """,
                    [trade_date],
                )
                if prev_dt:
                    return datetime.combine(prev_dt, date.time())
        except Exception as e:
            logger.warning(f"数据库获取上一个交易日失败，回退计算: {e}")

        prev_day = date - timedelta(days=1)
        while not cls.is_trading_day(prev_day):
            prev_day -= timedelta(days=1)
        return prev_day

    @classmethod
    def time_to_market_open(cls, now: datetime = None) -> Optional[timedelta]:
        if now is None:
            now = datetime.now()

        if not cls.is_trading_day(now):
            next_trading_day = cls.get_next_trading_day(now)
            market_open = datetime.combine(next_trading_day.date(), cls.MORNING_START)
            return market_open - now

        current_time = now.time()
        if cls.MORNING_START <= current_time <= cls.MORNING_END:
            return None
        if cls.MORNING_END < current_time < cls.AFTERNOON_START:
            market_open = datetime.combine(now.date(), cls.AFTERNOON_START)
            return market_open - now
        if cls.AFTERNOON_START <= current_time <= cls.AFTERNOON_END:
            return None
        if current_time < cls.MORNING_START:
            market_open = datetime.combine(now.date(), cls.MORNING_START)
            return market_open - now

        next_trading_day = cls.get_next_trading_day(now)
        market_open = datetime.combine(next_trading_day.date(), cls.MORNING_START)
        return market_open - now

    @classmethod
    def time_to_market_close(cls, now: datetime = None) -> Optional[timedelta]:
        if now is None:
            now = datetime.now()
        if not cls.is_trading_day(now):
            return None
        if now.time() > cls.AFTERNOON_END:
            return None
        market_close = datetime.combine(now.date(), cls.AFTERNOON_END)
        return market_close - now


def get_trading_dates_from_db(start_date: str, end_date: str) -> List[str]:
    try:
        if clickhouse_available() and not clickhouse_table_exists("trade_calendar"):
            TradingCalendar._warn_calendar_fallback(
                "trade_calendar 在 ClickHouse 中不存在，区间交易日改用周末回退"
            )
            raise RuntimeError("trade_calendar table missing")

        df = clickhouse_query_df(
            """
            SELECT trade_date
            FROM trade_calendar
            WHERE trade_date >= ? AND trade_date <= ?
              AND is_trading = 1
            ORDER BY trade_date
            """,
            [start_date, end_date],
        )
        trading_dates = [str(x) for x in df["trade_date"].tolist()] if not df.empty else []
        if trading_dates:
            return trading_dates
    except Exception as e:
        logger.error(f"从数据库获取交易日历失败，改用周末回退: {e}")

    trading_dates: List[str] = []
    current_date = datetime.strptime(start_date, "%Y-%m-%d")
    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
    while current_date <= end_date_obj:
        if TradingCalendar.is_trading_day(current_date):
            trading_dates.append(current_date.strftime("%Y-%m-%d"))
        current_date += timedelta(days=1)
    return trading_dates

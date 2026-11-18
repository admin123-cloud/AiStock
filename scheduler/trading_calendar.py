"""交易日历工具（ClickHouse 查询）"""
from datetime import datetime, time as dt_time, timedelta
from dataclasses import dataclass
from typing import Any, Optional, List

from utils.logger import get_logger
from utils.market_warehouse import clickhouse_scalar, clickhouse_query_df, clickhouse_available, clickhouse_table_exists

logger = get_logger("TradingCalendar")


@dataclass(frozen=True)
class CalendarDayResult:
    state: str
    source: str
    evidence: str


class CalendarUnknownError(RuntimeError):
    """The calendar cannot provide a safe answer."""


class TradingCalendar:
    """交易日历"""

    WEEKEND_DAYS = [5, 6]  # 周六、周日
    MORNING_START = dt_time(9, 30)
    MORNING_END = dt_time(11, 30)
    AFTERNOON_START = dt_time(13, 0)
    AFTERNOON_END = dt_time(15, 0)

    _last_calendar_warning_at: Optional[datetime] = None
    _calendar_warning_interval_sec = 300

    # R44（2026-11-11）：日历答案 TTL 缓存。retirement_window_audit 等链单请求
    # 触发 24+ 次 get_next_trading_day，每次 2 个 CH HTTP 往返（实测 0.5s/请求）。
    # trade_calendar 是静态历史+未来表（变更极少），300s TTL 封顶兜日历刷新。
    _LOOKUP_CACHE: dict = {}
    _LOOKUP_CACHE_TTL_SEC = 300.0

    @classmethod
    def _cached_lookup(cls, key: str, compute):
        import time as _time

        now = _time.time()
        hit = cls._LOOKUP_CACHE.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        value = compute()
        if len(cls._LOOKUP_CACHE) > 4096:
            cls._LOOKUP_CACHE.clear()
        cls._LOOKUP_CACHE[key] = (now + cls._LOOKUP_CACHE_TTL_SEC, value)
        return value

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
        def _probe() -> bool:
            if not clickhouse_available():
                return False
            return bool(clickhouse_table_exists("trade_calendar"))

        return cls._cached_lookup("table_exists:trade_calendar", _probe)

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
    def resolve_trading_day(cls, date: datetime = None) -> CalendarDayResult:
        if date is None:
            date = datetime.now()

        try:
            target_date = date.date()
            trade_date = target_date.isoformat()
            if not cls._db_calendar_available():
                return CalendarDayResult("unknown", "trade_calendar", "database unavailable or table missing")
            row = cls._cached_lookup(
                f"is_trading:{trade_date}",
                lambda: clickhouse_scalar(
                    """
                    SELECT is_trading
                    FROM trade_calendar
                    WHERE trade_date = ? AND market = 'SH'
                    LIMIT 1
                    """,
                    [trade_date],
                ),
            )
            if row is not None:
                return CalendarDayResult("trading" if bool(row) else "closed", "trade_calendar", "explicit row")
            min_date, max_date = cls._db_calendar_bounds()
            if min_date and max_date and min_date <= target_date <= max_date:
                return CalendarDayResult("closed", "trade_calendar", "no row within existing bounds")
            return CalendarDayResult("unknown", "trade_calendar", "empty calendar or date outside bounds")
        except Exception as e:
            return CalendarDayResult("unknown", "trade_calendar", f"query failed ({type(e).__name__})")

    @classmethod
    def is_trading_day(cls, date: datetime = None) -> bool:
        return cls.resolve_trading_day(date).state == "trading"

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
        return cls._adjacent_trading_day(date or datetime.now(), 1)

    @classmethod
    def get_previous_trading_day(cls, date: datetime = None) -> datetime:
        return cls._adjacent_trading_day(date or datetime.now(), -1)

    @classmethod
    def _adjacent_trading_day(cls, date: datetime, direction: int) -> datetime:
        trade_date = date.date().isoformat()
        aggregate, operator, prefix = ("minOrNull", ">", "next") if direction == 1 else ("maxOrNull", "<", "prev")
        try:
            if not cls._db_calendar_available():
                raise CalendarUnknownError("trade calendar unavailable")
            found = cls._cached_lookup(
                f"{prefix}_trading:{trade_date}",
                lambda: clickhouse_scalar(
                    f"SELECT {aggregate}(trade_date) FROM trade_calendar "
                    f"WHERE market = 'SH' AND is_trading = 1 AND trade_date {operator} ?",
                    [trade_date],
                ),
            )
            if found:
                found_date = cls._normalize_db_date(found)
                if (found_date is None or
                        (direction == 1 and found_date <= date.date()) or
                        (direction == -1 and found_date >= date.date())):
                    raise CalendarUnknownError("invalid adjacent calendar result")
                return datetime.combine(found_date, date.time())
        except CalendarUnknownError:
            raise
        except Exception as exc:
            raise CalendarUnknownError(f"adjacent calendar query failed ({type(exc).__name__})") from None

        for step in range(1, 31):
            try:
                candidate = date + timedelta(days=direction * step)
            except OverflowError:
                raise CalendarUnknownError("calendar date range exceeded") from None
            result = cls.resolve_trading_day(candidate)
            if result.state == "unknown":
                raise CalendarUnknownError(f"calendar unknown: {result.evidence}")
            if result.state == "trading":
                return candidate
        raise CalendarUnknownError("no trading day within 30 calendar days")

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

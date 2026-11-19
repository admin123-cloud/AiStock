# -*- coding: utf-8 -*-
"""盘中阶段门禁与三态日历守卫（P0 S2/S4 运行时收口层）。

把 S1 纯判定层（``utils.market_phase_policy``）、三态交易日历
（``scheduler.trading_calendar``）与各盘中/API 入口桥接起来：
1. 三态交易日历（``trading`` / ``closed`` / ``unknown``）：``unknown`` 严格 fail-closed，
   禁止退化成“仅看周末”或静默返回 ``False`` 冒充休市。
2. 15:05:00（Asia/Shanghai）硬截断：严格采用左闭右开 ``< 15:05:00``，
   15:05:00 起禁止任何盘中启动、持续写入与预测落盘。
3. 零重型顶层导入：仅按需延迟加载 ``scheduler.trading_calendar`` 与
   ``utils.market_phase_policy``，保证调度器与离线单元测试可轻量导入。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Callable, Optional, Tuple
from zoneinfo import ZoneInfo

BEIJING_TZ = ZoneInfo("Asia/Shanghai")

INTRADAY_WINDOW_START = time(9, 35)
INTRADAY_WINDOW_END = time(15, 5)  # 严格硬截断上界：< 15:05:00

_SUPPORTED_MARKETS = ("SH", "SZ", "BJ")


def to_beijing(dt: datetime) -> datetime:
    """规范化时刻到 Asia/Shanghai（naive 视作北京墙钟）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)


def in_intraday_schedule_window(now_bj: datetime) -> bool:
    """盘中调度硬窗口：``09:35:00 <= 北京钟 < 15:05:00``（左闭右开）。"""
    current = to_beijing(now_bj).timetz().replace(tzinfo=None)
    return INTRADAY_WINDOW_START <= current < INTRADAY_WINDOW_END


def is_before_postclose_cutoff(now_bj: datetime) -> bool:
    """盘前/盘中写入硬截断：北京墙钟必须严格早于 ``15:05:00``。"""
    current = to_beijing(now_bj).timetz().replace(tzinfo=None)
    return current < INTRADAY_WINDOW_END


def resolve_three_state_trading_day(day: date) -> bool:
    """三态交易日判定：``trading``→True，``closed``→False，``unknown`` 抛 ``CalendarUnknownError``。"""
    from scheduler.trading_calendar import CalendarUnknownError, TradingCalendar

    probe = datetime(day.year, day.month, day.day, 12, 0, tzinfo=BEIJING_TZ)
    result = TradingCalendar.resolve_trading_day(probe)
    state = getattr(result, "state", "unknown")
    if state == "trading":
        return True
    if state == "closed":
        return False
    source = getattr(result, "source", "unknown")
    evidence = getattr(result, "evidence", "")
    raise CalendarUnknownError(f"trading_day unknown ({source}: {evidence})")


def inspect_three_state_trading_day(day: date):
    """返回 ``CalendarDayResult(state, source, evidence)``，供需区分 closed/unknown 的只读路径使用。"""
    from scheduler.trading_calendar import TradingCalendar

    probe = datetime(day.year, day.month, day.day, 12, 0, tzinfo=BEIJING_TZ)
    return TradingCalendar.resolve_trading_day(probe)


def evaluate_intraday_phase(
    now_aware: datetime,
    calendar_state: str,
    *,
    worst_seconds: float = 5.0,
    margin_seconds: float = 2.0,
    generation: int = 1,
    calendar_version: str = "runtime-calendar-v1",
):
    """桥接 S1 纯判定器 ``utils.market_phase_policy.evaluate_phase``。"""
    from services.postclose_contracts import Purpose
    from utils.market_phase_policy import CalendarEvidence, PhaseBudget, evaluate_phase

    if now_aware.tzinfo is None:
        # 显式把 naive 拦截权交给 evaluate_phase（fail-closed）
        target_day = now_aware.date()
        checked_at = now_aware - timedelta(seconds=1)
        expires_at = datetime.combine(target_day, time(23, 59, 59))
        previous_wall = now_aware - timedelta(seconds=1)
    else:
        bj_now = now_aware.astimezone(BEIJING_TZ)
        target_day = bj_now.date()
        checked_at = bj_now - timedelta(seconds=1)
        expires_at = datetime.combine(target_day, time(23, 59, 59), tzinfo=BEIJING_TZ)
        previous_wall = bj_now - timedelta(seconds=1)

    evidence = CalendarEvidence(
        state=calendar_state,
        target_date=target_day,
        markets=_SUPPORTED_MARKETS,
        version=calendar_version,
        coverage_complete=True,
        checked_at=checked_at,
        expires_at=expires_at,
        generation=generation,
    )
    budget = PhaseBudget(
        measured_worst_total=timedelta(seconds=max(0.0, float(worst_seconds))),
        margin=timedelta(seconds=max(0.0, float(margin_seconds))),
        previous_wall=previous_wall,
        monotonic_elapsed=timedelta(seconds=1),
    )
    return evaluate_phase(
        now_aware,
        evidence,
        Purpose.INTRADAY_PROVISIONAL,
        target_day,
        budget,
        generation,
    )


def guard_intraday_execution(
    now: datetime,
    *,
    trading_day_checker: Optional[Callable[[date], bool]] = None,
    require_window_start: bool = False,
) -> Tuple[bool, str]:
    """统一盘中执行/落盘前守卫（三态日历 fail-closed + 15:05:00 硬截断）。

    Returns:
        ``(allowed, reason)``：``allowed=True`` 仅在“确认为交易日 且 未到 15:05:00
        （若 ``require_window_start=True`` 还须 >= 09:35:00）”。
    """
    bj_now = to_beijing(now)
    current = bj_now.timetz().replace(tzinfo=None)
    if current >= INTRADAY_WINDOW_END:
        return False, "after_1505_cutoff"
    if require_window_start and current < INTRADAY_WINDOW_START:
        return False, "before_0935_window"

    checker = trading_day_checker or resolve_three_state_trading_day
    try:
        is_trading = bool(checker(bj_now.date()))
    except Exception as exc:
        return False, f"calendar_unknown:{exc}"

    if not is_trading:
        return False, "non_trading_day"
    return True, "ok"

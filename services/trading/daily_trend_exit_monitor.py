"""Deterministic daily rising-trend-line exit signal for G3 holdings.

The calculation deliberately uses only confirmed pivot lows.  A pivot at day
``t`` becomes usable only after ``pivot_window`` later daily bars exist, so a
backtest cannot accidentally use tomorrow's low when drawing today's line.
"""

from __future__ import annotations

from typing import Any, Iterable


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clean_bars(bars: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for item in bars:
        if not isinstance(item, dict):
            continue
        low = _number(item.get("low"))
        close = _number(item.get("close"))
        date = str(item.get("trade_date") or item.get("date") or "").strip()[:10]
        if low is None or close is None or not date:
            continue
        clean.append({"trade_date": date, "low": low, "close": close})
    clean.sort(key=lambda item: item["trade_date"])
    return clean


def _confirmed_pivot_lows(bars: list[dict[str, Any]], pivot_window: int) -> list[int]:
    pivots: list[int] = []
    for index in range(pivot_window, len(bars) - pivot_window):
        low = bars[index]["low"]
        window = [bars[pos]["low"] for pos in range(index - pivot_window, index + pivot_window + 1)]
        # Equal lows are not used: the line must be anchored to one unique,
        # confirmed structural low rather than an arbitrary flat cluster.
        if low == min(window) and window.count(low) == 1:
            pivots.append(index)
    return pivots


def evaluate_daily_rising_trend_exit(
    bars: Iterable[dict[str, Any]],
    *,
    pivot_window: int = 3,
    min_pivot_separation: int = 5,
    break_buffer_pct: float = 0.0,
) -> dict[str, Any]:
    """Return a daily-close sell-next-open signal for a rising trend line.

    ``break_buffer_pct`` is optional noise protection; the user's strict
    interpretation is represented by its default value of zero.
    """
    if pivot_window < 1 or min_pivot_separation < 1 or break_buffer_pct < 0:
        raise ValueError("invalid daily trend-line parameters")

    rows = _clean_bars(bars)
    minimum_bars = pivot_window * 2 + min_pivot_separation + 2
    base: dict[str, Any] = {
        "contract": "daily_rising_trend_line_exit_v1",
        "pivot_window": pivot_window,
        "min_pivot_separation": min_pivot_separation,
        "break_buffer_pct": break_buffer_pct,
        "bar_count": len(rows),
        "signal_date": rows[-1]["trade_date"] if rows else None,
        "action": "HOLD",
        "signal": "insufficient_daily_bars",
        "should_sell_next_open": False,
    }
    if len(rows) < minimum_bars:
        return base

    pivots = _confirmed_pivot_lows(rows, pivot_window)
    anchor_pair: tuple[int, int] | None = None
    # Use the latest pair of confirmed higher lows.  Requiring separation
    # avoids fitting a steep line through adjacent noise candles.
    for right_pos in range(len(pivots) - 1, 0, -1):
        right = pivots[right_pos]
        for left_pos in range(right_pos - 1, -1, -1):
            left = pivots[left_pos]
            if right - left < min_pivot_separation:
                continue
            if rows[right]["low"] > rows[left]["low"]:
                anchor_pair = (left, right)
                break
        if anchor_pair is not None:
            break
    if anchor_pair is None:
        base.update({"signal": "no_confirmed_higher_low_pair", "confirmed_pivot_count": len(pivots)})
        return base

    left, right = anchor_pair
    slope = (rows[right]["low"] - rows[left]["low"]) / (right - left)
    latest_index = len(rows) - 1
    previous_index = latest_index - 1
    trend_line = rows[right]["low"] + slope * (latest_index - right)
    previous_line = rows[right]["low"] + slope * (previous_index - right)
    break_level = trend_line * (1.0 - break_buffer_pct)
    previous_break_level = previous_line * (1.0 - break_buffer_pct)
    latest_close = rows[latest_index]["close"]
    previous_close = rows[previous_index]["close"]
    broken = latest_close < break_level
    new_break = broken and previous_close >= previous_break_level

    base.update(
        {
            "confirmed_pivot_count": len(pivots),
            "anchor_low_1": {"trade_date": rows[left]["trade_date"], "price": rows[left]["low"], "index": left},
            "anchor_low_2": {"trade_date": rows[right]["trade_date"], "price": rows[right]["low"], "index": right},
            "trend_slope_per_day": slope,
            "daily_close": latest_close,
            "trend_line_price": trend_line,
            "break_level": break_level,
            "distance_to_trend_pct": latest_close / trend_line - 1.0,
            "trend_broken": broken,
            "new_break": new_break,
            "signal": "daily_trend_break" if broken else "trend_intact",
            "action": "SELL_NEXT_OPEN" if broken else "HOLD",
            "should_sell_next_open": broken,
            "reason": (
                "日线收盘有效跌破由两个已确认抬高波段低点形成的上升趋势线；下一交易日执行卖出计划。"
                if broken
                else "日线收盘仍在上升趋势线之上，继续持有观察。"
            ),
        }
    )
    return base


def evaluate_intraday_rising_trend_line(
    bars: Iterable[dict[str, Any]],
    current_price: Any,
    *,
    pivot_window: int = 3,
    min_pivot_separation: int = 5,
    break_buffer_pct: float = 0.0,
    current_session_in_daily_bars: bool = False,
) -> dict[str, Any]:
    """Project the confirmed daily trend line into the current session.

    Unlike the daily-close function this is intentionally an intraday warning:
    it compares the latest QMT price to the projected daily support line and is
    suitable for a five-minute watch loop, not for a backtest exit fill.
    """
    price = _number(current_price)
    daily = evaluate_daily_rising_trend_exit(
        bars,
        pivot_window=pivot_window,
        min_pivot_separation=min_pivot_separation,
        break_buffer_pct=break_buffer_pct,
    )
    if price is None or not daily.get("anchor_low_1") or not daily.get("anchor_low_2"):
        return {**daily, "intraday_price": price, "intraday_signal": "data_insufficient", "intraday_trend_broken": False}
    left = int(daily["anchor_low_1"]["index"])
    right = int(daily["anchor_low_2"]["index"])
    checked_index = int(daily["bar_count"]) - 1 if current_session_in_daily_bars else int(daily["bar_count"])
    line = float(daily["anchor_low_2"]["price"]) + float(daily["trend_slope_per_day"]) * (checked_index - right)
    break_level = line * (1.0 - break_buffer_pct)
    broken = price < break_level
    return {
        **daily,
        "intraday_price": price,
        "intraday_trend_line_price": line,
        "intraday_break_level": break_level,
        "intraday_distance_to_trend_pct": price / line - 1.0,
        "intraday_trend_broken": broken,
        "intraday_signal": "intraday_trend_break" if broken else "intraday_trend_intact",
        "intraday_action": "SELL_REVIEW" if broken else "HOLD",
        "intraday_reason": "盘中价格跌破日线主升趋势线，进入卖出/换股复核。" if broken else "盘中价格仍在日线主升趋势线之上。",
    }

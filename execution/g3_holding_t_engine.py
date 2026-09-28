"""Read-only intraday T-trading decision engine for existing QMT holdings.

The engine deliberately produces plans, never orders.  It is designed for
A-share T+1: a position can only be bought back after an audited same-day
T-leg sale, and it can never increase the original holding exposure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class HoldingTPolicy:
    """Conservative, explainable defaults for a single intraday T cycle."""

    max_t_ratio: float = 0.25
    min_lot: int = 100
    max_tick_age_seconds: int = 20
    max_minute_age_seconds: int = 8 * 60
    sell_vwap_premium_pct: float = 0.006
    buy_vwap_discount_pct: float = 0.002
    # 70 made this two-symbol T monitor effectively inert even when price was
    # well above VWAP and the five-level order book was bid-dominant.  55 keeps
    # a momentum requirement while leaving VWAP and order-book confirmation as
    # independent hard gates.
    sell_rsi6_min: float = 55.0
    buy_rsi6_max: float = 35.0
    sell_order_book_imbalance_min: float = 0.15
    buy_order_book_imbalance_max: float = -0.10
    benchmark_stock_weight: float = 0.50
    t_leg_total_capital_weight: float = 0.25
    sell_cost_rate: float = 0.0008
    buy_cost_rate: float = 0.0003


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _lot_down(shares: int, lot: int) -> int:
    return max(0, int(shares) // lot * lot)


def paper_t_cycle_excess(
    sell_price: Any,
    buyback_price: Any,
    *,
    policy: HoldingTPolicy = HoldingTPolicy(),
) -> dict[str, float] | None:
    """Return paper T-cycle excess versus holding the same shares."""
    sell = _number(sell_price)
    buy = _number(buyback_price)
    if sell is None or buy is None or sell <= 0 or buy <= 0:
        return None
    leg_excess = sell * (1.0 - policy.sell_cost_rate) / (buy * (1.0 + policy.buy_cost_rate)) - 1.0
    return {
        "t_leg_excess_pct": leg_excess * 100.0,
        "total_capital_excess_pct": leg_excess * policy.t_leg_total_capital_weight * 100.0,
        "break_even_buyback_price": sell * (1.0 - policy.sell_cost_rate) / (1.0 + policy.buy_cost_rate),
    }


def _in_t_window(checked_at: datetime) -> bool:
    if checked_at.weekday() >= 5:
        return False
    value = checked_at.strftime("%H:%M")
    # Cover the full A-share continuous-auction session.  The 14:57 closing
    # call auction is intentionally excluded because it is not suitable for a
    # same-day T decision that still needs a possible buyback.
    return ("09:30" <= value <= "11:30") or ("13:00" <= value <= "14:56")


def evaluate_holding_t(
    position: dict[str, Any],
    tick: dict[str, Any],
    minute: dict[str, Any],
    *,
    sold_t_shares_today: int = 0,
    pending_sell_shares_today: int = 0,
    checked_at: datetime | None = None,
    policy: HoldingTPolicy = HoldingTPolicy(),
) -> dict[str, Any]:
    """Return a non-executable plan for one holding.

    Required tick inputs are ``last_price`` (or QMT's ``lastPrice``) and
    ``tick_age_seconds``.  Minute inputs are ``vwap_5m`` and ``rsi6``.  The
    optional order-book imbalance is normalized as (bid - ask)/(bid + ask).
    """
    now = checked_at or datetime.now()
    code = str(position.get("code") or position.get("stock_code") or "").upper()
    shares = int(_number(position.get("shares") or position.get("volume")) or 0)
    available = int(_number(position.get("available_shares") or position.get("can_use_volume")) or 0)
    last = _number(tick.get("last_price") if tick.get("last_price") is not None else tick.get("lastPrice"))
    age = _number(tick.get("tick_age_seconds"))
    vwap = _number(minute.get("vwap_5m"))
    rsi6 = _number(minute.get("rsi6"))
    imbalance = _number(tick.get("order_book_imbalance"))
    hard_stop = _number(position.get("hard_stop"))
    sell_cap = _lot_down(min(available, int(shares * policy.max_t_ratio)), policy.min_lot)
    prior_t_sale = _lot_down(min(int(sold_t_shares_today), shares), policy.min_lot)
    pending_sale = _lot_down(min(int(pending_sell_shares_today), shares), policy.min_lot)

    plan: dict[str, Any] = {
        "code": code,
        "mode": "paper_only_holding_t",
        "order_path_enabled": False,
        "action": "observe",
        "shares": shares,
        "available_shares": available,
        "sell_cap_shares": sell_cap,
        "buyback_cap_shares": prior_t_sale,
        "benchmark_stock_weight_pct": round(policy.benchmark_stock_weight * 100.0, 2),
        "t_leg_total_capital_weight_pct": round(policy.t_leg_total_capital_weight * 100.0, 2),
        "checked_at": now.isoformat(sep=" ", timespec="seconds"),
        "reasons": [],
    }
    if not code or shares < policy.min_lot:
        plan.update(action="blocked", reasons=["持仓不足一手，不能做T"])
        return plan
    if not _in_t_window(now):
        plan.update(action="blocked", reasons=["非做T时段；避免开盘噪声、午间断档和尾盘无法回补"])
        return plan
    if age is None or age > policy.max_tick_age_seconds or not bool(tick.get("tick_fresh", age is not None)):
        plan.update(action="blocked", reasons=["Tick不新鲜，禁止依据陈旧行情做T"])
        return plan
    if last is None or last <= 0 or vwap is None or vwap <= 0 or rsi6 is None:
        plan.update(action="blocked", reasons=["Tick或5分钟指标不完整"])
        return plan
    minute_age = _number(minute.get("minute_age_seconds"))
    if minute.get("minute_fresh") is False or (
        minute_age is not None and minute_age > policy.max_minute_age_seconds
    ):
        plan.update(action="blocked", reasons=["5分钟指标数据已过期，禁止依据陈旧VWAP/RSI做T"])
        return plan
    if hard_stop is not None and hard_stop > 0 and last <= hard_stop:
        plan.update(action="risk_exit_no_t", reasons=["触及G3硬止损，风控退出优先于做T"])
        return plan
    if bool(tick.get("limit_up")) or bool(tick.get("limit_down")):
        plan.update(action="blocked", reasons=["涨跌停附近流动性/回补风险过高，不做T"])
        return plan

    premium = last / vwap - 1.0
    plan["price_vs_vwap_pct"] = round(premium * 100, 3)
    plan["market_price"] = round(last, 3)
    plan["rsi6"] = round(rsi6, 2)
    plan["order_book_imbalance"] = imbalance
    sell_signal = (
        prior_t_sale < policy.min_lot
        and pending_sale < policy.min_lot
        and sell_cap >= policy.min_lot
        and premium >= policy.sell_vwap_premium_pct
        and rsi6 >= policy.sell_rsi6_min
        and imbalance is not None
        and imbalance >= policy.sell_order_book_imbalance_min
    )
    buy_signal = (
        prior_t_sale >= policy.min_lot
        and premium <= -policy.buy_vwap_discount_pct
        and rsi6 <= policy.buy_rsi6_max
        and imbalance is not None
        and imbalance <= policy.buy_order_book_imbalance_max
    )
    if sell_signal:
        cycle = paper_t_cycle_excess(last, last, policy=policy)
        plan.update(
            action="paper_sell_t_leg",
            proposed_shares=sell_cap,
            break_even_buyback_price=round(float((cycle or {}).get("break_even_buyback_price") or 0.0), 3),
            reasons=["价格显著高于5分钟VWAP、短周期过热且买盘占优；仅卖出可回补的T仓"],
        )
    elif buy_signal:
        plan.update(
            action="paper_buyback_t_leg",
            proposed_shares=prior_t_sale,
            reasons=["仅回补已卖T仓：价格低于5分钟VWAP、短周期超卖且卖压缓和"],
        )
    elif prior_t_sale >= policy.min_lot:
        plan["reasons"] = ["已有T仓卖出记录，但回补条件未同时满足；不追价回补"]
    elif pending_sale >= policy.min_lot:
        plan["reasons"] = ["已有待确认的卖出T仓提醒；确认实际卖出后才启动买回跟踪"]
    else:
        plan["reasons"] = ["未出现高确定性做T触发；趋势持仓不为做T而做T"]
    return plan


def evaluate_portfolio_t(
    positions: list[dict[str, Any]],
    ticks_by_code: dict[str, dict[str, Any]],
    minute_by_code: dict[str, dict[str, Any]],
    sold_t_shares_by_code: dict[str, int] | None = None,
    pending_sell_shares_by_code: dict[str, int] | None = None,
    *,
    checked_at: datetime | None = None,
    policy: HoldingTPolicy = HoldingTPolicy(),
) -> dict[str, Any]:
    """Evaluate every supplied holding; missing data becomes a block, not a guess."""
    sold = sold_t_shares_by_code or {}
    pending = pending_sell_shares_by_code or {}
    rows = []
    for position in positions:
        code = str(position.get("code") or position.get("stock_code") or "").upper()
        rows.append(evaluate_holding_t(
            position, ticks_by_code.get(code, {}), minute_by_code.get(code, {}),
            sold_t_shares_today=int(sold.get(code) or 0), pending_sell_shares_today=int(pending.get(code) or 0), checked_at=checked_at, policy=policy,
        ))
    return {
        "mode": "paper_only_holding_t",
        "order_path_enabled": False,
        "checked_at": (checked_at or datetime.now()).isoformat(sep=" ", timespec="seconds"),
        "evaluated_count": len(rows),
        "plans": rows,
    }

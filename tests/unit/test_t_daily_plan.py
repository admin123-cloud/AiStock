# -*- coding: utf-8 -*-
"""G3 每日做T规划引擎单元测试。"""
from __future__ import annotations

import services.trading.t_daily_plan as tdp


def test_resolve_effective_params_rebases_stale_calib():
    raw = {
        "buy_band": [13.0, 13.2],
        "sell_band": [13.7, 13.9],
        "buy_rsi5_max": 35.0,
        "sell_rsi5_min": 70.0,
        "stop": 12.9,
        "source": "R54-calib-20261111",
    }
    eff, stale, reason = tdp.resolve_effective_params(
        regime="sideways",
        raw_params=raw,
        ma5=12.60,
        ma10=12.95,
        ma20=12.80,
        hi20=14.20,
        lo20=11.86,
        prev_close=12.41,
        live_price=12.16,
    )
    assert stale is True
    assert reason and "已过期" in reason
    assert eff["source"] == "dynamic-sideways-rebased"
    assert eff["buy_band"][0] <= 12.16
    assert eff["stop"] < eff["buy_band"][0]
    assert eff["sell_band"][0] > 12.41


def test_build_market_directive_weak_defensive():
    d = tdp.build_market_directive({
        "date": "2026-11-19",
        "above_ma20_pct": 14.76,
        "new_high_60d": 91,
        "new_low_60d": 1972,
        "hl_ratio": 0.05,
        "level": "weak",
    })
    assert d["mode"] == "defensive_reverse_t"
    assert d["tone"] == "red"
    assert len(d["rules"]) >= 2


def test_build_stock_daily_plan_eligible_and_ineligible():
    mkt = tdp.build_market_directive({"above_ma20_pct": 15.0, "level": "weak"})
    eff = {
        "buy_band": [11.86, 12.20],
        "sell_band": [12.66, 12.95],
        "buy_rsi5_max": 30.0,
        "sell_rsi5_min": 70.0,
        "stop": 11.68,
        "source": "dynamic-sideways-rebased",
    }
    plan = tdp.build_stock_daily_plan(
        code="002015",
        name="协鑫能科",
        regime="sideways",
        total_shares=5100,
        available_shares=5100,
        t_lot_cap=1700,
        available_cash=5000.0,
        effective_params=eff,
        params_stale=True,
        params_stale_reason="已切换动态价带",
        live_quote={"price": 12.16, "vwap": 12.25, "rsi5m": 28.5, "rsi15m": 34.2},
        ma5=12.60,
        ma10=12.95,
        ma20=12.80,
        prev_close=12.41,
        market_directive=mkt,
    )
    assert plan["eligible"] is True
    assert plan["action_mode"] == "reverse_t_first"
    assert plan["recommended_lot"] == 1700
    assert plan["cash_note"] is not None
    assert plan["price_gauge"]["zone"] == "in_buy_band"
    assert len(plan["price_ladder"]["buy_levels"]) == 2
    assert len(plan["price_ladder"]["sell_levels"]) == 2
    assert any("RSI5m" in a["text"] for a in plan["alerts"])

    ineligible = tdp.build_stock_daily_plan(
        code="300854",
        name="聚龙股份",
        regime="unknown",
        total_shares=100,
        available_shares=100,
        t_lot_cap=0,
        available_cash=50000.0,
        effective_params={},
        params_stale=False,
        params_stale_reason=None,
        live_quote={"price": 59.52},
        ma5=None,
        ma10=None,
        ma20=None,
        prev_close=None,
        market_directive=mkt,
    )
    assert ineligible["eligible"] is False
    assert ineligible["priority"] == 99


def test_summarize_intraday_executions():
    rows = [
        {"id": "1", "code": "002015", "side": "SELL", "price": 12.50, "shares": 1000, "amount": 12500.0},
        {"id": "2", "code": "002015", "side": "BUY", "price": 12.20, "shares": 1000, "amount": 12200.0},
        {"id": "3", "code": "603881", "side": "SELL", "price": 18.80, "shares": 200, "amount": 3760.0},
    ]
    by_code, summary = tdp.summarize_intraday_executions(rows)
    assert by_code["002015"]["net_shares"] == 0
    assert by_code["002015"]["status"] == "flat"
    assert by_code["002015"]["realized_pnl"] > 280.0
    assert by_code["603881"]["net_shares"] == -200
    assert by_code["603881"]["status"] == "short_open"
    assert summary["open_exposure_codes"] == 1
    assert summary["open_exposure_shares"] == 200

# -*- coding: utf-8 -*-
"""acct2 独立托管引擎单元测试（风控熔断 / A股规则 / 存量处置 / 纸面闭环）。"""
from __future__ import annotations

from datetime import datetime

from services.trading.acct2_managed_engine import (
    Acct2ManagedConfig,
    Acct2State,
    LiveQuote,
    MarketBar,
    MarketBreadth,
    OrderIntent,
    Position,
    apply_eod_settlement,
    calc_trade_fees,
    check_order_gate,
    evaluate_risk_tier,
    execute_paper_order,
    generate_strategy_intents,
    target_position_cap,
)


def _make_base_state() -> Acct2State:
    return Acct2State(
        base_date="2026-11-20",
        base_assets=15026.82,
        hwm_assets=15026.82,
        prev_eod_assets=15026.82,
        cash=4076.82,
        positions={
            "603881.SH": Position(
                code="603881.SH",
                name="数据港",
                asset_type="stock",
                qty=600,
                available_qty=600,
                avg_cost=18.25,
                last_price=18.25,
                legacy_cost_price=33.993,
                entry_date="2026-11-20",
            )
        },
        benchmark_cash=4076.82,
        benchmark_holdings={"603881.SH": 600},
    )


def _make_uptrend_bars(start: float = 1.0, step: float = 0.01, count: int = 25) -> list[MarketBar]:
    bars = []
    for i in range(count):
        px = round(start + i * step, 4)
        bars.append(MarketBar(date=f"2026-10-{i+1:02d}", open=px, close=px, high=px * 1.01, low=px * 0.99, volume=100000))
    return bars


def test_fee_calculation_etf_exempt_stamp_tax():
    buy_etf = calc_trade_fees("buy", 4000.0, "etf")
    sell_etf = calc_trade_fees("sell", 4000.0, "etf")
    sell_stock = calc_trade_fees("sell", 10000.0, "stock")
    assert buy_etf["commission"] == 5.0 and buy_etf["stamp_tax"] == 0.0
    assert sell_etf["commission"] == 5.0 and sell_etf["stamp_tax"] == 0.0
    assert sell_stock["commission"] == 5.0 and sell_stock["stamp_tax"] == 5.0


def test_three_tier_drawdown_breakers():
    cfg = Acct2ManagedConfig()
    st = _make_base_state()
    st.hwm_assets = 16000.0

    # 回撤 6.5% -> yellow（降档 50%，禁买个股）
    st.cash = 14960.0
    st.positions = {}
    tier, cap = evaluate_risk_tier(st, cfg)
    assert tier == "yellow" and cap == 0.50

    q_stock = LiveQuote(code="002015.SZ", name="协鑫能科", price=12.0, prev_close=12.0)
    intent_stock = OrderIntent("002015.SZ", "协鑫能科", "stock", "buy", 300, 12.0, "test")
    res = check_order_gate(intent_stock, q_stock, st, cfg, datetime(2026, 11, 23, 10, 0))
    assert not res.approved and res.reason == "yellow_drawdown_block_stock_buy"

    # 回撤 10.5% -> orange（禁止一切新开仓）
    st.cash = 14320.0
    q_etf = LiveQuote(code="518880.SH", name="黄金ETF", price=5.0, prev_close=5.0)
    intent_etf = OrderIntent("518880.SH", "黄金ETF", "etf", "buy", 600, 5.0, "test")
    res_o = check_order_gate(intent_etf, q_etf, st, cfg, datetime(2026, 11, 23, 10, 0))
    assert not res_o.approved and res_o.reason == "orange_drawdown_no_new_buy"

    # 回撤 12.2% -> red（彻底硬熔断，连常规信号也停机并由 EOD 置位 kill_switch）
    st.cash = 14048.0
    res_r = check_order_gate(intent_etf, q_etf, st, cfg, datetime(2026, 11, 23, 10, 0))
    assert not res_r.approved and res_r.reason == "hard_drawdown_or_kill_switch"
    eod = apply_eod_settlement(st, cfg, {}, "2026-11-23")
    assert st.kill_switch is True and eod["kill_switch"] is True


def test_session_midday_1430_and_t1_gates():
    cfg = Acct2ManagedConfig()
    st = _make_base_state()
    q_etf = LiveQuote(code="518880.SH", name="黄金ETF", price=5.0, prev_close=5.0)
    buy_etf = OrderIntent("518880.SH", "黄金ETF", "etf", "buy", 600, 5.0, "test")

    # 午休 12:00 拒单
    res_mid = check_order_gate(buy_etf, q_etf, st, cfg, datetime(2026, 11, 23, 12, 0))
    assert not res_mid.approved and res_mid.reason == "outside_trading_session"

    # 14:35 禁开新仓
    res_late = check_order_gate(buy_etf, q_etf, st, cfg, datetime(2026, 11, 23, 14, 35))
    assert not res_late.approved and res_late.reason == "no_new_buy_after_1430"

    # 10:15 买入通过并执行 T+1 冻结
    res_ok = check_order_gate(buy_etf, q_etf, st, cfg, datetime(2026, 11, 23, 10, 15))
    assert res_ok.approved
    execute_paper_order(buy_etf, q_etf, st, cfg, "2026-11-23", datetime(2026, 11, 23, 10, 15))
    assert st.positions["518880.SH"].qty == 600
    assert st.positions["518880.SH"].available_qty == 0

    # 当日试图卖出刚买的 ETF -> T+1 拒卖
    sell_etf = OrderIntent("518880.SH", "黄金ETF", "etf", "sell", 600, 5.0, "test")
    res_t1 = check_order_gate(sell_etf, q_etf, st, cfg, datetime(2026, 11, 23, 13, 30))
    assert not res_t1.approved and res_t1.reason == "insufficient_t1_available_qty"

    # 盘后结算后 T+1 解冻
    apply_eod_settlement(st, cfg, {"518880.SH": q_etf}, "2026-11-23")
    assert st.positions["518880.SH"].available_qty == 600


def test_legacy_603881_defense_and_rebound_trim():
    cfg = Acct2ManagedConfig()
    breadth = MarketBreadth(date="2026-11-20", above_ma20_pct=11.0, level="weak")

    # 场景 1：跌破 17.50 硬防守位 -> 触发 300 股减仓
    st1 = _make_base_state()
    q_down = {"603881.SH": LiveQuote("603881.SH", "数据港", price=17.45, prev_close=18.25)}
    intents1 = generate_strategy_intents(st1, cfg, breadth, q_down, {})
    assert len(intents1) == 1
    assert intents1[0].side == "sell" and intents1[0].qty == 300
    assert "破位硬防守" in intents1[0].reason

    # 场景 2：盘中反抽 +2.5% -> 触发单票超重逢反抽降仓 300 股
    st2 = _make_base_state()
    q_up = {"603881.SH": LiveQuote("603881.SH", "数据港", price=18.72, prev_close=18.25)}
    intents2 = generate_strategy_intents(st2, cfg, breadth, q_up, {})
    assert len(intents2) == 1
    assert intents2[0].side == "sell" and intents2[0].qty == 300
    assert "逢反抽降仓" in intents2[0].reason


def test_weak_market_only_allows_defensive_etf():
    cfg = Acct2ManagedConfig()
    st = _make_base_state()
    st.positions = {}
    st.cash = 15000.0
    st.base_assets = 15000.0
    st.hwm_assets = 15000.0

    breadth = MarketBreadth(date="2026-11-20", above_ma20_pct=11.0, level="weak")
    cap, tier = target_position_cap(breadth, st, cfg)
    assert tier == "normal" and cap == 0.30

    quotes = {
        "512480.SH": LiveQuote("512480.SH", "半导体ETF", price=1.26, prev_close=1.24),
        "518880.SH": LiveQuote("518880.SH", "黄金ETF", price=5.26, prev_close=5.24),
    }
    kline_map = {
        "512480.SH": _make_uptrend_bars(start=1.00, step=0.01, count=25),  # sector 桶
        "518880.SH": _make_uptrend_bars(start=5.00, step=0.01, count=25),  # defensive 桶
    }
    intents = generate_strategy_intents(st, cfg, breadth, quotes, kline_map)
    assert len(intents) == 1
    assert intents[0].code == "518880.SH"  # 冰点弱市只选防御 ETF，过滤掉半导体 ETF
    assert intents[0].qty * intents[0].limit_price <= 15000.0 * 0.30 + 1e-6

from datetime import datetime

from execution.g3_holding_t_engine import evaluate_holding_t, paper_t_cycle_excess


AT = datetime(2026, 7, 14, 10, 30)
POSITION = {"code": "002245.SZ", "shares": 2200, "available_shares": 2200, "hard_stop": 19.69}


def test_sell_t_leg_is_lot_limited_and_paper_only():
    plan = evaluate_holding_t(
        POSITION,
        {"last_price": 21.2, "tick_age_seconds": 2, "tick_fresh": True, "order_book_imbalance": 0.2},
        {"vwap_5m": 21.0, "rsi6": 75}, checked_at=AT,
    )
    assert plan["action"] == "paper_sell_t_leg"
    assert plan["proposed_shares"] == 500
    assert plan["order_path_enabled"] is False
    assert plan["benchmark_stock_weight_pct"] == 50.0
    assert plan["t_leg_total_capital_weight_pct"] == 25.0


def test_paper_t_cycle_excess_is_scaled_to_a_quarter_of_total_capital():
    result = paper_t_cycle_excess(21.2, 21.0)
    assert result is not None
    assert result["t_leg_excess_pct"] > 0
    assert round(result["total_capital_excess_pct"], 6) == round(result["t_leg_excess_pct"] * 0.25, 6)


def test_sell_t_uses_the_relaxed_but_still_confirmed_rsi_threshold():
    plan = evaluate_holding_t(
        POSITION,
        {"last_price": 21.2, "tick_age_seconds": 2, "tick_fresh": True, "order_book_imbalance": 0.2},
        {"vwap_5m": 21.0, "rsi6": 55}, checked_at=AT,
    )
    assert plan["action"] == "paper_sell_t_leg"


def test_buyback_never_exceeds_audited_t_sale():
    plan = evaluate_holding_t(
        POSITION,
        {"last_price": 20.8, "tick_age_seconds": 2, "tick_fresh": True, "order_book_imbalance": -0.2},
        {"vwap_5m": 21.0, "rsi6": 30}, sold_t_shares_today=300, checked_at=AT,
    )
    assert plan["action"] == "paper_buyback_t_leg"
    assert plan["proposed_shares"] == 300


def test_existing_t_sale_cannot_emit_a_second_sell_signal():
    plan = evaluate_holding_t(
        POSITION,
        {"last_price": 21.2, "tick_age_seconds": 2, "tick_fresh": True, "order_book_imbalance": 0.2},
        {"vwap_5m": 21.0, "rsi6": 75},
        sold_t_shares_today=500,
        checked_at=AT,
    )
    assert plan["action"] == "observe"


def test_stale_tick_is_a_hard_block():
    plan = evaluate_holding_t(
        POSITION,
        {"last_price": 21.2, "tick_age_seconds": 21, "tick_fresh": False, "order_book_imbalance": 0.2},
        {"vwap_5m": 21.0, "rsi6": 75}, checked_at=AT,
    )
    assert plan["action"] == "blocked"


def test_stale_minute_indicators_are_a_hard_block():
    plan = evaluate_holding_t(
        POSITION,
        {"last_price": 21.2, "tick_age_seconds": 2, "tick_fresh": True, "order_book_imbalance": 0.2},
        {"vwap_5m": 21.0, "rsi6": 75, "minute_age_seconds": 481, "minute_fresh": False}, checked_at=AT,
    )
    assert plan["action"] == "blocked"
    assert "过期" in plan["reasons"][0]


def test_full_continuous_auction_window_is_scanned_but_closing_call_is_excluded():
    early = evaluate_holding_t(
        POSITION,
        {"last_price": 21.2, "tick_age_seconds": 2, "tick_fresh": True, "order_book_imbalance": 0.2},
        {"vwap_5m": 21.0, "rsi6": 55}, checked_at=datetime(2026, 7, 14, 9, 30),
    )
    closing_call = evaluate_holding_t(
        POSITION,
        {"last_price": 21.2, "tick_age_seconds": 2, "tick_fresh": True, "order_book_imbalance": 0.2},
        {"vwap_5m": 21.0, "rsi6": 55}, checked_at=datetime(2026, 7, 14, 14, 57),
    )
    assert early["action"] == "paper_sell_t_leg"
    assert closing_call["action"] == "blocked"


def test_hard_stop_beats_t_signal():
    plan = evaluate_holding_t(
        POSITION,
        {"last_price": 19.5, "tick_age_seconds": 2, "tick_fresh": True, "order_book_imbalance": 0.2},
        {"vwap_5m": 19.0, "rsi6": 80}, checked_at=AT,
    )
    assert plan["action"] == "risk_exit_no_t"

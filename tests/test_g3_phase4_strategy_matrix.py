# -*- coding: utf-8 -*-
"""G3 Phase 4（多策略三腿互补矩阵、动态配额、稳健腿 ETF 波动率目标趋势与全站联动）单元测试。"""
from __future__ import annotations

from pathlib import Path

from services.g3.premarket_brief import generate_premarket_brief
from services.g3.strategy_matrix import build_strategy_matrix, compute_voltarget_trend_signal


def test_voltarget_trend_signal_long_vs_cash_defense():
    up_closes = [3000.0 + i * 10.0 for i in range(70)]
    up_sig = compute_voltarget_trend_signal(up_closes)
    assert up_sig["signal"] == "long_trend"
    assert up_sig["vol_target_weight_pct"] > 0

    down_closes = [4000.0 - i * 15.0 for i in range(70)]
    down_sig = compute_voltarget_trend_signal(down_closes)
    assert down_sig["signal"] == "cash_defense"
    assert down_sig["vol_target_weight_pct"] == 0.0


def test_strategy_matrix_defensive_switches_to_ar3_cross_asset_etf():
    down_closes = [4000.0 - i * 10.0 for i in range(70)]
    fetcher = lambda: {
        code: ("2026-11-20", down_closes)
        for code in ("000016.SH", "000300.SH", "000905.SH", "000852.SH", "399006.SZ", "000688.SH")
    }
    res = build_strategy_matrix(
        market_gate={
            "regime": "defensive",
            "regime_label": "防守模式",
            "allow_new_mainwave": False,
            "r51_waterline": {"actual_pct": 83.0, "status": "overweight"},
        },
        attribution_summary={"alpha_pnl": -1600.0, "t_trade_pnl": -140.0},
        coach_report={"metrics": {"stop_ignored_codes": 2}},
        index_closes_fetcher=fetcher,
    )

    # AR3 v3.0：防守期不再把压舱 ETF 归零转现金，而是 100% 切换至 #01 非 A 股跨资产避险 ETF
    assert res["defense_auto_cash"] is False
    assert res["allocation"]["offense_pct"] == 0.0
    assert res["allocation"]["defense_etf_pct"] == 100.0
    assert res["allocation"]["t_enhance_pct"] == 0.0
    assert res["allocation"]["cash_pct"] == 0.0
    assert res["contracts_total"] >= 1

    legs = {lg["leg_id"]: lg for lg in res["legs"]}
    assert legs["offense"]["status"] == "halted"
    assert legs["defense_etf"]["status"] == "active"
    assert legs["t_enhance"]["status"] == "review_needed"


def test_strategy_matrix_offensive_with_ar3_dual_core_and_brief_integration(tmp_path: Path):
    up_closes = [3000.0 + i * 8.0 for i in range(70)]
    fetcher = lambda: {"000300.SH": ("2026-11-20", up_closes)}
    gate = {
        "ref_date": "2026-11-21",
        "regime": "offensive",
        "regime_label": "进攻模式",
        "tone": "emerald",
        "allow_new_mainwave": True,
        "target_position_cap_pct": 100.0,
        "r51_waterline": {"actual_pct": 50.0, "status": "underweight", "advice": "按 AR3 双核目标分批调仓"},
        "sources": {},
    }
    matrix = build_strategy_matrix(market_gate=gate, index_closes_fetcher=fetcher)
    assert matrix["defense_auto_cash"] is False
    assert matrix["allocation"]["offense_pct"] == 50.0
    assert matrix["allocation"]["defense_etf_pct"] == 50.0
    assert matrix["allocation"]["cash_pct"] == 0.0

    ar3_plan = {
        "trade_date": "2026-11-23",
        "regime": "offensive",
        "regime_label": "进攻模式",
        "account": {
            "account_id": "acct1",
            "label": "广发证券-曾* (ct1曾)",
            "total_assets": 108104.58,
            "available_cash": 16736.58,
            "position_pct": 84.4,
        },
        "cash_flow": {
            "est_sell_proceeds": 52020.0,
        },
        "config": {
            "stock_budget_cap_pct": 50,
            "active_stock_mode": "ab_blend",
            "etf_core_mode": "regime_focus",
        },
        "money_effect_20d": {
            "headline": "近20日 F3A +5.1% vs F2A +4.2%，建议维持 AB混合",
        },
        "sell_orders": [
            {
                "code": "002015",
                "name": "协鑫能科",
                "suggested_shares": 5100,
                "limit_price": 10.2,
                "est_amount": 52020.0,
            }
        ],
        "buy_orders": [
            {
                "code": "513100",
                "name": "纳指ETF",
                "immediate_shares": 9000,
                "post_sell_shares": 25000,
                "limit_price": 1.85,
                "stop_loss_price": 1.67,
            }
        ],
    }

    brief = generate_premarket_brief(
        market_gate=gate,
        strategy_matrix=matrix,
        ar3_plan=ar3_plan,
        base_dir=tmp_path / "brief",
    )
    assert brief["schema"] == "g3_premarket_brief/2_ar3"
    assert "AR4.5-L3b" in brief["markdown_text"]
    assert "广发证券-曾*" in brief["markdown_text"]
    assert "513100" in brief["markdown_text"]
    assert "主升浪" not in brief["markdown_text"]

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


def test_strategy_matrix_defensive_auto_cash_when_all_etfs_broken():
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

    assert res["defense_auto_cash"] is True
    assert res["allocation"]["offense_pct"] == 0.0
    assert res["allocation"]["defense_etf_pct"] == 0.0
    assert res["allocation"]["t_enhance_pct"] == 10.0
    assert res["allocation"]["cash_pct"] == 90.0
    assert res["contracts_total"] >= 1

    legs = {lg["leg_id"]: lg for lg in res["legs"]}
    assert legs["offense"]["status"] == "halted"
    assert legs["defense_etf"]["status"] == "cash_defense"
    assert legs["t_enhance"]["status"] == "review_needed"


def test_strategy_matrix_offensive_with_long_etf_and_brief_integration(tmp_path: Path):
    up_closes = [3000.0 + i * 8.0 for i in range(70)]
    fetcher = lambda: {"000300.SH": ("2026-11-20", up_closes)}
    gate = {
        "ref_date": "2026-11-21",
        "regime": "offensive",
        "regime_label": "进攻模式",
        "tone": "emerald",
        "allow_new_mainwave": True,
        "target_position_cap_pct": 75.0,
        "r51_waterline": {"actual_pct": 50.0, "status": "underweight", "advice": "回踩买带分步加仓"},
        "sources": {},
    }
    matrix = build_strategy_matrix(market_gate=gate, index_closes_fetcher=fetcher)
    assert matrix["defense_auto_cash"] is False
    assert matrix["allocation"]["offense_pct"] == 45.0
    assert matrix["allocation"]["defense_etf_pct"] == 15.0
    assert matrix["allocation"]["cash_pct"] == 25.0

    brief = generate_premarket_brief(
        market_gate=gate,
        strategy_matrix=matrix,
        base_dir=tmp_path / "brief",
    )
    assert "三腿配额" in brief["markdown_text"]
    assert "进攻腿(主升) 45%" in brief["markdown_text"]

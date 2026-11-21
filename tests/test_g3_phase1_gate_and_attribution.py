# -*- coding: utf-8 -*-
"""G3 Phase 1A（大盘择时总闸门 × R51 水位）与 Phase 1B（四维收益归因）单元测试。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from services.g3.attribution import build_four_dim_attribution
from services.g3.market_regime_gate import (
    compute_r51_index_regime,
    evaluate_market_regime_gate,
)


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 1A：R51 指数周期与择时总闸门测试
# ---------------------------------------------------------------------------

def test_r51_index_regime_up_and_n2_debounce():
    # 构造稳定上升序列（65 日）
    closes = [3000.0 + i * 5.0 for i in range(65)]
    res = compute_r51_index_regime(closes, confirm_days=2)
    assert res["status"] == "fresh"
    assert res["raw_regime"] == "up"
    assert res["confirmed_regime"] == "up"
    assert res["index_mom60"] is not None and res["index_mom60"] > 0

    # 单日急跌破 MA5：raw 变 sideways，但因 N=2 防抖，confirmed 仍保持 up
    closes_1d = closes + [closes[-1] * 0.94]
    res_1d = compute_r51_index_regime(closes_1d, confirm_days=2)
    assert res_1d["raw_regime"] != "up"
    assert res_1d["confirmed_regime"] == "up"

    # 连续第 2 日仍非 up：confirmed 正式翻转
    closes_2d = closes_1d + [closes[-1] * 0.93]
    res_2d = compute_r51_index_regime(closes_2d, confirm_days=2)
    assert res_2d["confirmed_regime"] != "up"


def test_r51_index_regime_missing_when_insufficient_bars():
    res = compute_r51_index_regime([3000.0] * 10)
    assert res["status"] == "missing"
    assert res["confirmed_regime"] == "unknown"


def test_market_regime_gate_freezing_mood_forces_defensive_and_overweight(tmp_path: Path):
    mood_dir = tmp_path / "market_mood"
    forecast_dir = tmp_path / "market_turnover_forecast"
    _write_json(
        mood_dir / "20261120.json",
        {
            "date": "2026-11-20",
            "final": {
                "limit_up_count": 36,
                "limit_down_count": 106,
                "broke_count": 30,
                "seal_rate": 0.5455,
                "max_streak": 4,
            },
        },
    )
    # 故意放一个 6 天前的过期成交额预测
    _write_json(
        forecast_dir / "latest.json",
        {
            "available": True,
            "trade_date": "2026-11-14",
            "forecast_amount": 1.15e12,
            "confidence": 0.88,
        },
    )

    up_closes = [3000.0 + i * 2.0 for i in range(65)]
    res = evaluate_market_regime_gate(
        breadth={"date": "2026-11-20", "level": "neutral", "above_ma20_pct": 42.0},
        total_assets=100000.0,
        total_market_value=68000.0,
        today=date(2026, 11, 20),
        mood_dir=mood_dir,
        forecast_dir=forecast_dir,
        index_fetcher=lambda _: ("2026-11-20", up_closes),
        turnover_fetcher=lambda _: None,
    )

    assert res["regime"] == "defensive"
    assert res["target_position_cap"] == 0.10
    assert res["allow_new_mainwave"] is False
    assert res["sources"]["turnover_forecast"]["status"] == "stale"
    assert any("已过期" in w for w in res["warnings"])
    assert res["r51_waterline"]["status"] == "overweight"
    assert res["r51_waterline"]["actual_pct"] == 68.0
    assert "≤30.0%" in res["r51_waterline"]["advice"]


def test_market_regime_gate_offensive_and_underweight(tmp_path: Path):
    mood_dir = tmp_path / "market_mood"
    forecast_dir = tmp_path / "market_turnover_forecast"
    _write_json(
        mood_dir / "20261120.json",
        {
            "date": "2026-11-20",
            "final": {
                "limit_up_count": 82,
                "limit_down_count": 4,
                "broke_count": 12,
                "seal_rate": 0.85,
                "max_streak": 6,
            },
        },
    )
    up_closes = [3000.0 + i * 1.5 for i in range(65)]
    res = evaluate_market_regime_gate(
        breadth={"date": "2026-11-20", "level": "strong", "above_ma20_pct": 66.0},
        total_assets=100000.0,
        total_market_value=45000.0,
        today=date(2026, 11, 20),
        mood_dir=mood_dir,
        forecast_dir=forecast_dir,
        index_fetcher=lambda _: ("2026-11-20", up_closes),
        turnover_fetcher=lambda _: None,
    )
    assert res["regime"] == "offensive"
    assert res["target_position_cap"] == 0.85
    assert res["allow_new_mainwave"] is True
    assert res["r51_waterline"]["status"] == "underweight"


def test_market_regime_gate_fail_closed_when_all_sources_missing(tmp_path: Path):
    res = evaluate_market_regime_gate(
        breadth=None,
        today=date(2026, 11, 20),
        mood_dir=tmp_path / "no_mood",
        forecast_dir=tmp_path / "no_fc",
        index_fetcher=lambda _: (None, []),
        turnover_fetcher=lambda _: None,
    )
    assert res["regime"] == "defensive"
    assert res["degraded"] is True
    assert res["allow_new_mainwave"] is False


def test_market_regime_gate_turnover_daily_actual_fallback(tmp_path: Path):
    forecast_dir = tmp_path / "market_turnover_forecast"
    _write_json(
        forecast_dir / "latest.json",
        {"available": True, "trade_date": "2026-11-14", "forecast_amount": 1.15e12},
    )
    up_closes = [3000.0 + i * 1.5 for i in range(65)]
    res = evaluate_market_regime_gate(
        breadth={"date": "2026-11-20", "level": "neutral", "above_ma20_pct": 45.0},
        today=date(2026, 11, 20),
        mood_dir=tmp_path / "no_mood",
        forecast_dir=forecast_dir,
        index_fetcher=lambda _: ("2026-11-20", up_closes),
        turnover_fetcher=lambda _: {
            "trade_date": "2026-11-20",
            "amount": 1.44e12,
            "trillion": 1.44,
            "ma5_trillion": 1.48,
            "vol_ratio_5d": 0.98,
            "stock_count": 5573,
        },
    )
    tf = res["sources"]["turnover_forecast"]
    assert tf["status"] == "fresh"
    assert tf["mode"] == "daily_actual"
    assert tf["forecast_trillion"] == 1.44
    assert tf["vol_ratio_5d"] == 0.98


# ---------------------------------------------------------------------------
# Phase 1B：四维收益归因测试
# ---------------------------------------------------------------------------

def test_four_dim_attribution_exact_decomposition_and_stop_drag(tmp_path: Path):
    ths_dir = tmp_path / "ths_read_account"
    trk_dir = tmp_path / "t_tracker"
    plan_dir = tmp_path / "g3_desk_plans"

    # 模拟 2026-11-20 双账号快照：总资产 100,000，市值 80,000，当日盈亏 +1,500，持有 002015 1000股
    _write_json(
        ths_dir / "read_multi_20261120_151000.json",
        {
            "generated_at": "2026-11-20T15:10:00+08:00",
            "accounts": {
                "acct1": {
                    "read_account": {
                        "funds": {"total_assets": 100000.0, "market_value": 80000.0},
                        "holdings": [
                            {"code": "002015", "quantity": 1000, "daily_profit": 1500.0}
                        ],
                    }
                }
            },
        },
    )

    # 模拟当日 ledger 对账：做T净赚 +300，违约 1 次（002015 跌破 12.90，日低 11.62 未卖）
    _write_json(
        trk_dir / "ledger" / "20261120_summary.json",
        {
            "date": "2026-11-20",
            "t_round_trips": 1,
            "net_pnl": 300.0,
            "discipline_rate": 0.8,
            "off_plan_count": 0,
            "unmatched_draft_ids": ["D1"],
            "coaching": {
                "纪律评分": 75,
                "violations_total": 1,
                "contract": [
                    {
                        "type": "contract_ignored",
                        "code": "002015",
                        "hard_stop": 12.90,
                        "day_low": 11.62,
                        "label": "止损契约触发未执行",
                    }
                ],
            },
        },
    )

    # 基准指数当日涨 +1.0% => Beta = 80,000 * 0.01 = +800
    # Alpha = 1500 - 800 - 300 = +400
    # 止损拖延估算 = -(12.90 - 11.62) * 1000 = -1280.0
    res = build_four_dim_attribution(
        days=30,
        ths_dir=ths_dir,
        tracker_dir=trk_dir,
        desk_plan_dir=plan_dir,
        benchmark_fetcher=lambda _s, _e, _c: {"2026-11-20": 0.01},
        today_date="2026-11-20",
    )

    s = res["summary"]
    assert s["total_pnl"] == 1500.0
    assert s["beta_pnl"] == 800.0
    assert s["t_trade_pnl"] == 300.0
    assert s["alpha_pnl"] == 400.0
    assert s["avg_discipline_score"] == 75.0
    assert s["violations_total"] == 1
    assert s["estimated_stop_drag_pnl"] == -1280.0
    assert len(res["friction_events"]) == 1
    assert res["friction_events"][0]["estimated_drag_pnl"] == -1280.0


def test_four_dim_attribution_manual_t_fallback_and_honest_nulls(tmp_path: Path):
    ths_dir = tmp_path / "ths_read_account"
    trk_dir = tmp_path / "t_tracker"
    plan_dir = tmp_path / "g3_desk_plans"

    # 仅有一条手工做T登记，无双账号快照、无基准
    _write_json(
        plan_dir / "executions_20261120.json",
        {
            "executions": [
                {"code": "002015", "side": "BUY", "price": 12.0, "shares": 500},
                {"code": "002015", "side": "SELL", "price": 12.5, "shares": 500},
            ]
        },
    )

    res = build_four_dim_attribution(
        days=30,
        ths_dir=ths_dir,
        tracker_dir=trk_dir,
        desk_plan_dir=plan_dir,
        benchmark_fetcher=lambda _s, _e, _c: {},
        today_date="2026-11-20",
    )
    s = res["summary"]
    assert s["t_trade_pnl"] == 250.0
    assert s["t_round_trips"] == 1
    assert s["total_pnl"] is None
    assert s["beta_pnl"] is None
    assert s["alpha_pnl"] is None

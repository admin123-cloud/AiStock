# -*- coding: utf-8 -*-
"""G3 Phase 3A（AI 交易教练）、Phase 3B（R51 分型台账）与 Phase 3C（L2 风控预检）单元测试。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from services.g3.order_prefill import evaluate_and_build_prefill_draft
from services.g3.trading_coach import build_trading_coach_report, save_agent_coach_notes
from services.trading.t_daily_plan import summarize_intraday_executions

BJ_TZ = timezone(timedelta(hours=8))


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 3A：AI 交易教练测试
# ---------------------------------------------------------------------------

def test_trading_coach_detects_bleeding_points_and_agent_notes(tmp_path: Path):
    ths_dir = tmp_path / "ths"
    trk_dir = tmp_path / "tracker"
    plan_dir = tmp_path / "plans"
    coach_dir = tmp_path / "coach"

    _write_json(
        ths_dir / "read_multi_20261120_151000.json",
        {
            "generated_at": "2026-11-20T15:10:00+08:00",
            "accounts": {
                "acct1": {
                    "read_account": {
                        "funds": {"total_assets": 100000.0, "market_value": 83000.0},
                        "holdings": [{"code": "002015", "quantity": 5000, "daily_profit": -1200.0}],
                    }
                }
            },
        },
    )
    _write_json(
        trk_dir / "ledger" / "20261120_summary.json",
        {
            "date": "2026-11-20",
            "t_round_trips": 1,
            "net_pnl": -120.0,
            "off_plan_count": 1,
            "unmatched_draft_ids": ["D1", "D2"],
            "coaching": {
                "纪律评分": 75,
                "knife_catch_count": 1,
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

    save_agent_coach_notes(
        report_date="2026-11-20",
        headline="止损执行力是本周最大漏点",
        weekly_diagnosis="002015 跌破硬止损未走导致回撤扩大，下周第一任务是降水位并严守止损。",
        red_lines=["铁律1：跌破硬止损无条件执行", "铁律2：防守期严禁逆势加仓"],
        base_dir=coach_dir,
    )

    rep = build_trading_coach_report(
        days=14,
        market_gate={
            "regime": "defensive",
            "r51_waterline": {"actual_pct": 83.0, "target_cap_pct": 25.0, "status": "overweight", "advice": "逢反弹减仓"},
        },
        ths_dir=ths_dir,
        tracker_dir=trk_dir,
        desk_plan_dir=plan_dir,
        coach_dir=coach_dir,
        benchmark_fetcher=lambda _s, _e, _c: {"2026-11-20": -0.01},
        today_date="2026-11-20",
    )

    tags = {b["tag"] for b in rep["bleeding_points"]}
    assert "contract_ignored" in tags
    assert "overweight_in_defense" in tags
    assert "knife_catch" in tags
    assert "off_plan_trade" in tags
    assert rep["agent_notes"] is not None
    assert rep["next_week_red_lines"][0].startswith("铁律1")


# ---------------------------------------------------------------------------
# Phase 3B：R51 分型台账（t_trade vs base_rebalance）
# ---------------------------------------------------------------------------

def test_r51_split_ledger_rebalance_does_not_trigger_open_t_exposure():
    execs = [
        {"code": "002015", "side": "SELL", "order_type": "base_rebalance", "price": 12.5, "shares": 1000, "amount": 12500.0},
        {"code": "002015", "side": "BUY", "order_type": "t_trade", "price": 12.0, "shares": 500, "amount": 6000.0},
        {"code": "002015", "side": "SELL", "order_type": "t_trade", "price": 12.4, "shares": 500, "amount": 6200.0},
    ]
    by_code, summary = summarize_intraday_executions(execs)
    assert summary["execution_count"] == 3
    assert summary["t_trade_count"] == 2
    assert summary["rebalance_count"] == 1
    assert summary["rebalance_sell_amount"] == 12500.0
    # 做T已日内平仓，水位调仓卖出的 1000 股绝不触发未平 T 敞口！
    assert summary["open_exposure_codes"] == 0
    assert summary["open_exposure_shares"] == 0
    assert by_code["002015"]["status"] == "flat"
    assert summary["realized_t_pnl"] > 0


# ---------------------------------------------------------------------------
# Phase 3C：L2 安全半自动风控预检测试
# ---------------------------------------------------------------------------

def test_l2_prefill_blocks_defensive_rebalance_buy_and_after_1430(tmp_path: Path):
    res = evaluate_and_build_prefill_draft(
        code="002015",
        name="协鑫能科",
        side="BUY",
        price=12.00,
        shares=500,
        order_type="base_rebalance",
        live_price=12.00,
        total_shares=3000,
        available_shares=3000,
        available_cash=20000.0,
        market_gate={"regime": "defensive", "regime_label": "防守模式", "allow_new_mainwave": False},
        now_bj=datetime(2026, 11, 20, 14, 35, 0, tzinfo=BJ_TZ),
        tracker_dir=tmp_path / "no_cfg",
        draft_dir=tmp_path / "drafts",
    )
    assert res["approved"] is False
    assert res["execution_mode"] == "human_confirm_only"
    assert any("大盘总闸门拦截" in b for b in res["blockers"])
    assert any("14:30" in b for b in res["blockers"])


def test_l2_prefill_approves_valid_rebalance_sell_and_persists_draft(tmp_path: Path):
    draft_dir = tmp_path / "drafts"
    res = evaluate_and_build_prefill_draft(
        code="002015",
        name="协鑫能科",
        side="SELL",
        price=12.02,
        shares=1000,
        order_type="base_rebalance",
        live_price=12.00,
        total_shares=7100,
        available_shares=7100,
        market_gate={"regime": "defensive", "regime_label": "防守模式", "allow_new_mainwave": False},
        now_bj=datetime(2026, 11, 20, 10, 15, 0, tzinfo=BJ_TZ),
        tracker_dir=tmp_path / "no_cfg",
        draft_dir=draft_dir,
    )
    assert res["approved"] is True
    assert res["blockers"] == []
    assert (draft_dir / "20261120.jsonl").exists()

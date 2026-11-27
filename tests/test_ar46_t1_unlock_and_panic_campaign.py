# -*- coding: utf-8 -*-
"""AR4.6 跨日 T+1 自动解冻与冰点战役跨日持久化（B 档退出纪律）单元测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from services.g3.panic_campaign import (
    evaluate_panic_campaign_state,
    save_active_panic_campaign,
)
from services.mining.ar3_rebalance_compiler import (
    _extract_account_snapshot,
    compile_ar3_rebalance_plan,
)

BJ_TZ = timezone(timedelta(hours=8))


def _sample_overview(*, generated_at: str, avail_stocks: int = 0, today_locked: int = 0) -> dict:
    return {
        "accounts": [
            {
                "account_id": "acct1",
                "label": "广发证券-曾*",
                "generated_at": generated_at,
                "funds": {
                    "total_assets": 101183.16,
                    "available": 2167.26,
                    "market_value": 98915.90,
                },
                "holdings": [
                    {
                        "code": "511260",
                        "name": "十年国债",
                        "quantity": 200,
                        "available_quantity": 200,
                        "current_price": 135.926,
                        "cost_price": 136.115,
                        "market_value": 27185.20,
                    },
                    {
                        "code": "512890",
                        "name": "红利低波",
                        "quantity": 20700,
                        "available_quantity": 20700,
                        "current_price": 1.261,
                        "cost_price": 1.269,
                        "market_value": 26102.70,
                    },
                    {
                        "code": "600801",
                        "name": "华新建材",
                        "quantity": 400,
                        "available_quantity": avail_stocks,
                        "today_locked_shares": today_locked,
                        "current_price": 27.20,
                        "cost_price": 27.188,
                        "market_value": 10880.00,
                    },
                    {
                        "code": "000711",
                        "name": "京蓝科技",
                        "quantity": 2900,
                        "available_quantity": avail_stocks,
                        "current_price": 4.12,
                        "cost_price": 4.272,
                        "market_value": 11948.00,
                    },
                    {
                        "code": "301606",
                        "name": "绿联科技",
                        "quantity": 200,
                        "available_quantity": avail_stocks,
                        "current_price": 62.10,
                        "cost_price": 62.406,
                        "market_value": 12420.00,
                    },
                    {
                        "code": "301697",
                        "name": "贝特利",
                        "quantity": 400,
                        "available_quantity": avail_stocks,
                        "current_price": 25.95,
                        "cost_price": 26.043,
                        "market_value": 10380.00,
                    },
                ],
                "fills": [],
            }
        ]
    }


def _sample_radar() -> dict:
    return {
        "regime": "defensive",
        "is_offensive": False,
        "config": {"max_slots": 4, "stock_budget_cap_pct": 50},
        "etf_radar": {
            "panic_triggered": False,
            "targets": [
                {"code": "511260", "name": "十年国债", "basket_weight_pct": 73.5, "close": 135.926, "ma20": 135.80},
                {"code": "512890", "name": "红利低波", "basket_weight_pct": 26.5, "close": 1.261, "ma20": 1.245},
            ],
        },
        "stock_radar": {
            "available": True,
            "is_panic_ab_active": False,
            "f2a_candidates": [],
            "f3a_candidates": [],
            "exec_queue": [],
        },
    }


def _seed_campaign(tmp_path: Path) -> None:
    save_active_panic_campaign(
        {
            "schema": "ar46_panic_campaign/1",
            "account_id": "acct1",
            "status": "active",
            "entry_date": "2026-11-26",
            "max_hold_trading_days": 5,
            "cooldown_trading_days": 6,
            "positions": {
                "600801": {"code": "600801", "name": "华新建材", "entry_price": 27.188, "shares": 400, "peak_price": 27.20, "screening_violation": False},
                "301606": {"code": "301606", "name": "绿联科技", "entry_price": 62.406, "shares": 200, "peak_price": 62.406, "screening_violation": False},
                "301697": {"code": "301697", "name": "贝特利", "entry_price": 26.043, "shares": 400, "peak_price": 26.043, "screening_violation": False},
                "000711": {
                    "code": "000711",
                    "name": "京蓝科技",
                    "entry_price": 4.272,
                    "shares": 2900,
                    "peak_price": 4.272,
                    "screening_violation": True,
                    "violation_note": "排雷黄灯 2/5 + 近12个月刚摘帽",
                },
            },
        },
        base_dir=tmp_path,
    )


def test_prev_day_snapshot_auto_unlocks_t1_but_preserves_today_buys():
    # 昨日 15:56 快照（2026-11-26T07:56:12Z），在今日 2026-11-27 应自动推算 T+1 解冻，但今日新买 100 股保持锁仓
    ov = _sample_overview(generated_at="2026-11-26T07:56:12+00:00", avail_stocks=0, today_locked=100)
    acct = _extract_account_snapshot(ov, account_id="acct1", today_str="2026-11-27")
    assert acct["snapshot_stale_prev_day"] is True
    assert acct["t1_inferred_unlocked_count"] == 4
    by_code = {h["code"]: h for h in acct["holdings"]}
    assert by_code["600801"]["available_shares"] == 300  # 400 - 今日新买锁仓 100
    assert by_code["000711"]["available_shares"] == 2900
    assert by_code["000711"]["t1_inferred_unlocked"] is True


def test_same_day_snapshot_respects_broker_t1_lock():
    # 今日快照若券商明确返回 available_quantity=0，不应被错误解冻
    ov = _sample_overview(generated_at="2026-11-27T01:40:00+00:00", avail_stocks=0)
    acct = _extract_account_snapshot(ov, account_id="acct1", today_str="2026-11-27")
    assert acct["snapshot_stale_prev_day"] is False
    assert acct["t1_inferred_unlocked_count"] == 0
    by_code = {h["code"]: h for h in acct["holdings"]}
    assert by_code["600801"]["available_shares"] == 0


def test_panic_campaign_t1_keeps_compliant_stocks_aligned_and_flags_violation(tmp_path: Path):
    _seed_campaign(tmp_path)
    ov = _sample_overview(generated_at="2026-11-26T07:56:12+00:00", avail_stocks=0)
    gate = {"regime": "defensive", "regime_label": "防守退潮·趋势下跌", "sources": {"breadth": {"above_ma20_pct": 6.49}}}

    with patch("services.mining.ar3_rebalance_compiler.fetch_batch_live_quotes", return_value={}):
        plan = compile_ar3_rebalance_plan(
            market_gate=gate,
            account_id="acct1",
            smooth_step=True,
            persist=False,
            accounts_overview=ov,
            ar3_radar=_sample_radar(),
            panic_campaign_dir=tmp_path,
            now_bj=datetime(2026, 11, 27, 9, 15, 0, tzinfo=BJ_TZ),
        )

    assert plan["panic_campaign"]["active"] is True
    assert plan["panic_campaign"]["held_days"] == 1
    assert plan["panic_campaign"]["in_cooldown"] is True

    by_code = {r["code"]: r for r in plan["decision_holdings"]}
    # 3 只合规冰点真龙：策略预期内、无卖出按钮
    for code in ("600801", "301606", "301697"):
        assert by_code[code]["alignment"] == "aligned"
        assert by_code[code]["can_execute"] is False
        assert by_code[code]["action_side"] is None
        assert "冰点 B 档持有 (T+1/5)" in by_code[code]["ai_status_label"]
        assert by_code[code]["available_shares"] > 0  # 已自动跨日解冻

    # 000711 排雷硬违规遗留票：预期外 + 可立即一键卖出
    assert by_code["000711"]["alignment"] == "unexpected"
    assert by_code["000711"]["can_execute"] is True
    assert by_code["000711"]["action_side"] == "SELL"
    assert "排雷违规" in by_code["000711"]["ai_status_label"]


def test_panic_campaign_btier_hard_stop_and_trailing_stop_trigger_sell(tmp_path: Path):
    _seed_campaign(tmp_path)
    ov = _sample_overview(generated_at="2026-11-27T01:13:15+00:00", avail_stocks=400)
    gate = {"regime": "defensive", "regime_label": "防守退潮·趋势下跌", "sources": {"breadth": {"above_ma20_pct": 12.0}}}

    # 600801 跌破 -6% 硬止损（27.188 * 0.93 = 25.28）；301606 冲高 +8% 后不触发，再测追踪回撤
    quotes = {
        "600801": {"price": 25.20},
        "301606": {"price": 67.50},  # +8.16% 刷新峰值
        "301697": {"price": 26.20},
        "000711": {"price": 4.10},
    }
    with patch("services.mining.ar3_rebalance_compiler.fetch_batch_live_quotes", return_value=quotes):
        plan1 = compile_ar3_rebalance_plan(
            market_gate=gate,
            account_id="acct1",
            smooth_step=True,
            persist=True,
            accounts_overview=ov,
            ar3_radar=_sample_radar(),
            panic_campaign_dir=tmp_path,
            now_bj=datetime(2026, 11, 27, 10, 15, 0, tzinfo=BJ_TZ),
        )

    by_code1 = {r["code"]: r for r in plan1["decision_holdings"]}
    assert by_code1["600801"]["alignment"] == "unexpected"
    assert by_code1["600801"]["can_execute"] is True
    assert "-6% 硬止损" in by_code1["600801"]["ai_status_label"]
    sell_600801 = next(s for s in plan1["sell_orders"] if s["code"] == "600801")
    assert sell_600801["bypass_panic_floor_guard"] is True
    assert by_code1["301606"]["alignment"] == "aligned"  # 创峰值 +8.16%，未回撤继续持有

    # 随后 301606 从 67.50 回撤 -4.0% 到 64.80 → 触发追踪止盈
    quotes2 = {**quotes, "301606": {"price": 64.80}}
    with patch("services.mining.ar3_rebalance_compiler.fetch_batch_live_quotes", return_value=quotes2):
        plan2 = compile_ar3_rebalance_plan(
            market_gate=gate,
            account_id="acct1",
            smooth_step=True,
            persist=False,
            accounts_overview=ov,
            ar3_radar=_sample_radar(),
            panic_campaign_dir=tmp_path,
            now_bj=datetime(2026, 11, 27, 13, 30, 0, tzinfo=BJ_TZ),
        )
    by_code2 = {r["code"]: r for r in plan2["decision_holdings"]}
    assert by_code2["301606"]["can_execute"] is True
    assert "追踪止盈" in by_code2["301606"]["ai_status_label"]


def test_panic_campaign_breadth_and_time_stop(tmp_path: Path):
    _seed_campaign(tmp_path)
    holdings = [{"code": "600801", "name": "华新建材", "shares": 400, "current_price": 27.5, "cost_price": 27.188}]
    # 宽度 >= 38% 触发清退
    st_b = evaluate_panic_campaign_state(
        holdings=holdings,
        live_prices={"600801": 27.5},
        breadth_pct=39.2,
        now_bj=datetime(2026, 11, 27, 10, 0, 0, tzinfo=BJ_TZ),
        base_dir=tmp_path,
        persist_updates=False,
    )
    assert st_b["exit_signals"]["600801"]["action"] == "EXIT_BREADTH_RECOVERY"

    # T0+5（2026-12-03）14:42 触发时间止损
    st_t = evaluate_panic_campaign_state(
        holdings=holdings,
        live_prices={"600801": 27.5},
        breadth_pct=20.0,
        now_bj=datetime(2026, 12, 3, 14, 42, 0, tzinfo=BJ_TZ),
        base_dir=tmp_path,
        persist_updates=False,
    )
    assert st_t["held_days"] == 5
    assert st_t["exit_signals"]["600801"]["action"] == "EXIT_TIME_STOP"

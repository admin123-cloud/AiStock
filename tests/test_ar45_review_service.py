"""AR4.5-L3b 实盘复盘与三腿真钱归因服务单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from services.g3 import ar45_review_service as svc


def test_build_ar45_review_report_deposit_immune_nav_and_three_legs(tmp_path: Path):
    """验证多日快照净值抗入金免疫、三腿盈亏拆分与破止损纪律扣分。"""
    snap_dir = tmp_path / "ths_read_account"
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Day 1: 总资产 100,000，当日盈亏 0
    (snap_dir / "read_multi_20261119_151000.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-11-19 15:10:00",
                "accounts": {
                    "acct1": {
                        "read_account": {
                            "funds": {
                                "total_assets": 100000.0,
                                "market_value": 80000.0,
                                "available": 20000.0,
                                "daily_profit": 0.0,
                                "holding_profit": -1000.0,
                            },
                            "holdings": [],
                        }
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # Day 2: 即使中途转入 50,000 现金（总资产跳到 152,000），当日真盈亏为 +2,000，
    # 抗入金免疫收益率应为 +2000 / 100000 = +2.00%，而不是 +52%！
    (snap_dir / "read_multi_20261120_151000.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-11-20 15:10:00",
                "accounts": {
                    "acct1": {
                        "read_account": {
                            "funds": {
                                "total_assets": 152000.0,
                                "market_value": 90000.0,
                                "available": 62000.0,
                                "daily_profit": 2000.0,
                                "holding_profit": -5000.0,
                            },
                            "holdings": [],
                        }
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mock_overview = {
        "accounts": [
            {
                "account_id": "acct1",
                "label": "广发证券-曾*",
                "generated_at": "2026-11-20 15:10:00",
                "funds": {
                    "total_assets": 152000.0,
                    "market_value": 90000.0,
                    "available": 62000.0,
                    "daily_profit": 2000.0,
                    "holding_profit": -5000.0,
                },
                "holdings": [
                    # 1) AR4.5 跨资产 ETF 腿（512890 红利低波ETF）
                    {
                        "code": "512890",
                        "name": "红利低波ETF",
                        "quantity": 20000,
                        "cost_price": 1.20,
                        "current_price": 1.26,
                        "market_value": 25200.0,
                        "daily_profit": 400.0,
                        "floating_profit": 1200.0,
                    },
                    # 2) AR4.5 个股腿（命中最新计划目标）
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "quantity": 10,
                        "cost_price": 1500.0,
                        "current_price": 1530.0,
                        "market_value": 15300.0,
                        "daily_profit": 300.0,
                        "floating_profit": 300.0,
                    },
                    # 3) 历史遗留腿（不在 AR4.5 名单且亏损 -15% 跌破 -10% 止损线）
                    {
                        "code": "002015",
                        "name": "协鑫能科",
                        "quantity": 4000,
                        "cost_price": 14.0,
                        "current_price": 11.9,
                        "market_value": 47600.0,
                        "daily_profit": -600.0,
                        "floating_profit": -8400.0,
                    },
                ],
            }
        ]
    }

    mock_plan = {
        "target_allocation": [
            {"code": "512890"},
            {"code": "600519"},
        ]
    }
    mock_reconcile = {
        "schema": "g3.ar3.shadow_reconcile.v1",
        "account_id": "acct1",
        "account_label": "广发证券-曾*",
        "days_tracked": 2,
        "total_planned_orders": 4,
        "total_executed_orders": 3,
        "overall_completion_pct": 75.0,
        "avg_slippage_bps": 4.2,
        "daily_records": [],
    }
    mock_coach = {
        "schema": "g3.coach.v1",
        "window_days": 30,
        "headline": "测试教练结论",
        "score": 80,
        "level": "良好",
        "summary": "测试总结",
        "findings": [],
        "action_items": [],
        "metrics": {},
    }

    with (
        patch.object(svc, "_fetch_hs300_close_map", return_value={"2026-11-19": 4000.0, "2026-11-20": 4040.0}),
        patch.object(svc, "_load_ar45_backtest_curve_map", return_value={"2026-11-19": 3.80, "2026-11-20": 3.838}),
        patch.object(svc, "reconcile_ar3_shadow_ledger", return_value=mock_reconcile),
        patch.object(svc, "build_four_dim_attribution", return_value={"summary": {"t_trade_pnl": 320.5}}),
        patch.object(svc, "build_trading_coach_report", return_value=mock_coach),
        patch.object(
            svc,
            "_read_json_safe",
            side_effect=lambda p: mock_plan if "latest_acct1.json" in str(p) else json.loads(Path(p).read_text(encoding="utf-8")),
        ),
    ):
        report = svc.build_ar45_review_report(
            account_id="acct1",
            days=30,
            accounts_overview=mock_overview,
            snap_dir=snap_dir,
        )

    assert report["schema"] == "ar45_review_attribution/1"
    assert report["kpis"]["tracked_days"] == 2
    # 抗入金免疫：+2000 / 100000 = +2.00%
    assert report["kpis"]["actual_ret_pct"] == 2.0
    assert report["kpis"]["hs300_ret_pct"] == 1.0
    assert report["kpis"]["excess_vs_hs300_pct"] == 1.0
    assert report["kpis"]["ar45_ideal_ret_pct"] == 1.0
    assert report["kpis"]["t_trade_pnl"] == 320.5

    # 三腿归因校验
    leg_map = {x["leg_id"]: x for x in report["legs"]}
    assert leg_map["ar45_etf"]["count"] == 1
    assert leg_map["ar45_etf"]["floating_profit"] == 1200.0
    assert leg_map["ar45_stock"]["count"] == 1
    assert leg_map["ar45_stock"]["floating_profit"] == 300.0
    assert leg_map["legacy_and_t"]["count"] == 1
    assert leg_map["legacy_and_t"]["floating_profit"] == -8400.0

    # 策略内总浮盈 = 1200 + 300 = 1500；历史遗留拖累 = -8400
    assert report["kpis"]["in_strategy_floating_pnl"] == 1500.0
    assert report["kpis"]["legacy_drag_floating_pnl"] == -8400.0

    # 破止损计数 = 1（协鑫能科 -15% <= -10%）
    assert report["kpis"]["stop_violation_count"] == 1
    assert len(report["stop_violations"]) == 1
    assert report["stop_violations"][0]["code"] == "002015"
    # 纪律分：100 - 20(1次破止损) - (80-75)*0.4 = 78
    assert report["kpis"]["discipline_score"] == 78

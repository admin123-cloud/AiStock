"""acct1 10 分钟实时待填单落盘、微信静默控制与 /g3/ai-live 播报流单测。"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.g3 import acct1_intraday_advisor as adv  # noqa: E402

BJ_TZ = timezone(timedelta(hours=8))


def test_broadcast_feed_and_live_signals_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("AISTOCK_AI_BUS_DIR", str(tmp_path / "ai_bus"))
    (tmp_path / "push_policy.json").write_text(
        json.dumps({"pause_intraday_auto_push": False, "pause_ar2_radar_broadcast": True}),
        encoding="utf-8",
    )
    # 构造模拟账户与计划，验证：
    # 1. suppress_wechat=True 时 0 条微信发出，但 live_signals 与 broadcasts 均正确落盘；
    # 2. get_acct1_advisor_status 返回 live_signals.orders；
    # 3. get_intraday_broadcast_feed 返回规则快照播报，且绝不推微信。
    wechat_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        adv,
        "send_wechat_notification",
        lambda title, body, **kw: (wechat_calls.append((title, body)) or {"ok": True}),
    )
    monkeypatch.setattr(
        adv,
        "compile_ar3_rebalance_plan",
        lambda **kw: {
            "regime": "defensive",
            "regime_label": "防守退潮·趋势下跌",
            "is_offensive": False,
            "account": {
                "label": "广发证券-曾*",
                "total_assets": 100000.0,
                "available_cash": 15000.0,
                "position_pct": 85.0,
            },
            "sell_orders": [
                {
                    "code": "511260",
                    "name": "十年国债ETF",
                    "suggested_shares": 100,
                    "full_clear_shares": 100,
                    "current_shares": 100,
                    "available_shares": 100,
                    "ref_price": 135.9,
                    "is_etf": True,
                    "blocked_t1": False,
                    "reason": "AR4.6 调仓减持",
                }
            ],
            "buy_orders": [
                {
                    "code": "512890",
                    "name": "红利低波ETF",
                    "immediate_shares": 5000,
                    "ref_price": 1.26,
                    "is_etf": True,
                    "target_weight_pct": 50.0,
                    "stop_loss_price": 1.18,
                    "reason": "AR4.6 目标配置建仓",
                }
            ],
        },
    )

    now = datetime(2026, 11, 26, 10, 35, 0, tzinfo=BJ_TZ)
    overview = {
        "accounts": [
            {
                "account_id": "acct1",
                "generated_at": "2026-11-26T09:30:00+08:00",
                "funds": {"available": 15000.0, "total_assets": 100000.0},
                "holdings": [],
            }
        ]
    }
    radar = {
        "regime": "defensive",
        "is_offensive": False,
        "market_breadth_pct": 12.5,
        "etf_radar": {"targets": [{"code": "512890", "name": "红利低波ETF", "account_weight_pct": 100.0, "ma20": 1.24}]},
    }
    quotes = {
        "511260": {"price": 135.9, "change_pct": 0.1},
        "512890": {"price": 1.26, "change_pct": 0.2},
    }

    # Case A: 手动播报（suppress_wechat=True）→ 0 微信，但页面待填单与播报流均落盘
    res_silent = adv.run_acct1_advisor(
        mode="intraday",
        dry_run=False,
        bypass_session=True,
        suppress_wechat=True,
        now_bj=now,
        base_dir=tmp_path,
        overview_override=overview,
        radar_override=radar,
        quotes_override=quotes,
    )
    assert res_silent["status"] == "ok"
    assert res_silent["executable_count"] == 2
    assert res_silent["pushed_count"] == 0
    assert len(wechat_calls) == 0

    st = adv.get_acct1_advisor_status(now_bj=now, base_dir=tmp_path)
    assert len(st["live_signals"]["orders"]) == 2
    assert st["live_signals"]["orders"][0]["side"] == "SELL"
    assert st["live_signals"]["orders"][1]["side"] == "BUY"

    feed = adv.get_intraday_broadcast_feed(limit=10, now_bj=now, base_dir=tmp_path)
    assert feed["ok"] is True
    assert feed["live_orders_count"] == 2
    assert any(it["channel"] == "rule_snapshot" for it in feed["items"])

    # Case B: 正常盘中 10 分钟轮询（suppress_wechat=False）→ 2 笔真实交易单推微信并附 /g3/desk 链接
    res_live = adv.run_acct1_advisor(
        mode="intraday",
        dry_run=False,
        bypass_session=False,
        suppress_wechat=False,
        now_bj=now,
        base_dir=tmp_path,
        overview_override=overview,
        radar_override=radar,
        quotes_override=quotes,
    )
    assert res_live["pushed_count"] == 2
    assert len(wechat_calls) == 2
    assert all("stock.aistock3.fun/g3/desk" in body for _, body in wechat_calls)

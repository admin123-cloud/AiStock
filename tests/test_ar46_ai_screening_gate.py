# -*- coding: utf-8 -*-
"""AR4.6 全链路 AI 排雷严格版硬门禁与底线规则单元测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.g3.ai_screening import (
    evaluate_buy_risk_gate,
    get_screening_freshness,
)
from services.g3.order_prefill import evaluate_and_build_prefill_draft
from services.g3.trading_plan_compiler import compile_daily_trading_command
from services.mining.ar3_rebalance_compiler import compile_ar3_rebalance_plan

BJ_TZ = timezone(timedelta(hours=8))

SAMPLE_SCREENING_DOC = {
    "schema": "g3_ai_screening/1",
    "decision_date": "2026-11-23",
    "generated_at": "2026-11-23T17:51:07+08:00",
    "items_by_code": {
        "000711": {
            "code": "000711.SZ",
            "code6": "000711",
            "name": "京蓝科技",
            "risk_light": "yellow",
            "risk_flags": ["2026-08-31刚摘帽且历史存在财务造假行政处罚"],
            "catalyst_score": 2,
            "one_line_thesis": "概念炒作叠加高质押，严禁追高",
            "source": "agent_enriched",
        },
        "688617": {
            "code": "688617.SH",
            "code6": "688617",
            "name": "惠泰医疗",
            "risk_light": "yellow",
            "risk_flags": ["子公司导丝产线飞检整顿"],
            "catalyst_score": 3,
            "one_line_thesis": "三季报仍增但需防合规冲击",
            "source": "agent_enriched",
        },
        "600801": {
            "code": "600801.SH",
            "code6": "600801",
            "name": "华新建材",
            "risk_light": "green",
            "risk_flags": [],
            "catalyst_score": 5,
            "one_line_thesis": "业绩与分红增持共振",
            "source": "agent_enriched",
        },
        "000999": {
            "code": "000999.SZ",
            "code6": "000999",
            "name": "模拟红灯股",
            "risk_light": "red",
            "risk_flags": ["立案调查与重大减持"],
            "catalyst_score": 1,
            "one_line_thesis": "重大合规雷区",
            "source": "agent_enriched",
        },
    },
}

SAMPLE_ST_MAP = {
    "000711": {"old_name": "ST京蓝", "last_st_at": "2026-08-17"},
    "603517": {"old_name": "ST绝味", "last_st_at": "2026-08-17"},
}


def test_evaluate_buy_risk_gate_strict_policy() -> None:
    now = datetime(2026, 11, 27, 10, 30, tzinfo=BJ_TZ)

    # 1. 红灯硬拦截
    g_red = evaluate_buy_risk_gate("000999", name="模拟红灯股", saved_doc=SAMPLE_SCREENING_DOC, recent_st_map=SAMPLE_ST_MAP, now_bj=now)
    assert g_red["hard_blocked"] is True
    assert g_red["buy_action"] == "block"

    # 2. 黄灯且催化分 <= 2（000711 京蓝科技）严格版硬拦截
    g_y2 = evaluate_buy_risk_gate("000711", name="京蓝科技", saved_doc=SAMPLE_SCREENING_DOC, recent_st_map=SAMPLE_ST_MAP, now_bj=now)
    assert g_y2["hard_blocked"] is True
    assert g_y2["buy_action"] == "block"
    assert any("刚摘帽" in f for f in g_y2["risk_flags"])

    # 3. 黄灯且催化分 >= 3（688617）不硬拦，但降权警示
    g_y3 = evaluate_buy_risk_gate("688617", name="惠泰医疗", saved_doc=SAMPLE_SCREENING_DOC, recent_st_map=SAMPLE_ST_MAP, now_bj=now)
    assert g_y3["hard_blocked"] is False
    assert g_y3["buy_action"] == "demote_warn"

    # 4. 绿灯（600801）正常放行
    g_green = evaluate_buy_risk_gate("600801", name="华新建材", saved_doc=SAMPLE_SCREENING_DOC, recent_st_map=SAMPLE_ST_MAP, now_bj=now)
    assert g_green["hard_blocked"] is False
    assert g_green["buy_action"] == "allow"

    # 5. 无 AI 档案但近 12 个月曾 ST 刚摘帽（603517）-> 底线规则硬拦截
    g_st = evaluate_buy_risk_gate("603517", name="绝味食品", saved_doc=SAMPLE_SCREENING_DOC, recent_st_map=SAMPLE_ST_MAP, now_bj=now)
    assert g_st["hard_blocked"] is True
    assert g_st["buy_action"] == "block"
    assert g_st["source"] == "rule_baseline_recent_st"

    # 6. 无 AI 档案普通股（301697）-> 放行但提示未排雷
    g_unk = evaluate_buy_risk_gate("301697", name="贝特利", saved_doc=SAMPLE_SCREENING_DOC, recent_st_map=SAMPLE_ST_MAP, now_bj=now)
    assert g_unk["hard_blocked"] is False
    assert g_unk["buy_action"] == "unscreened_warn"

    # 7. ETF 豁免
    g_etf = evaluate_buy_risk_gate("512890", name="红利低波ETF", is_etf=True, saved_doc=SAMPLE_SCREENING_DOC, recent_st_map=SAMPLE_ST_MAP, now_bj=now)
    assert g_etf["hard_blocked"] is False
    assert g_etf["source"] == "etf_exempt"


def test_screening_freshness_detects_stale() -> None:
    now = datetime(2026, 11, 27, 10, 0, tzinfo=BJ_TZ)
    fresh = get_screening_freshness(SAMPLE_SCREENING_DOC, now_bj=now)
    assert fresh["decision_date"] == "2026-11-23"
    assert fresh["age_days"] == 4
    assert fresh["is_stale"] is True
    assert "滞后 4 天" in fresh["stale_notice"]


def test_order_prefill_blocks_yellow_le2_buy_but_allows_sell(tmp_path) -> None:
    now = datetime(2026, 11, 27, 10, 32, tzinfo=BJ_TZ)
    gate = {"regime": "offensive", "regime_label": "主升进攻", "allow_new_mainwave": True}

    # 买入 000711（黄灯 2/5）-> L2 预填必须硬阻断
    buy_res = evaluate_and_build_prefill_draft(
        code="000711",
        name="京蓝科技",
        side="BUY",
        price=4.27,
        shares=2900,
        order_type="base_rebalance",
        live_price=4.27,
        available_cash=20000.0,
        market_gate=gate,
        now_bj=now,
        draft_dir=tmp_path,
        persist=False,
        screening_doc=SAMPLE_SCREENING_DOC,
        recent_st_map=SAMPLE_ST_MAP,
    )
    assert buy_res["approved"] is False
    assert any("AI排雷硬拦截" in b for b in buy_res["blockers"])

    # 卖出 000711（已持仓止损卖出）-> 绝对不能被禁买规则误拦！
    sell_res = evaluate_and_build_prefill_draft(
        code="000711",
        name="京蓝科技",
        side="SELL",
        price=4.12,
        shares=2900,
        order_type="base_rebalance",
        live_price=4.12,
        total_shares=2900,
        available_shares=2900,
        market_gate=gate,
        now_bj=now,
        draft_dir=tmp_path,
        persist=False,
        screening_doc=SAMPLE_SCREENING_DOC,
        recent_st_map=SAMPLE_ST_MAP,
    )
    assert sell_res["approved"] is True
    assert not sell_res["blockers"]

    # 买入黄灯 >= 3（688617）-> 放行但必须有警告
    y3_res = evaluate_and_build_prefill_draft(
        code="688617",
        name="惠泰医疗",
        side="BUY",
        price=100.0,
        shares=200,
        order_type="base_rebalance",
        live_price=100.0,
        available_cash=25000.0,
        market_gate=gate,
        now_bj=now,
        draft_dir=tmp_path,
        persist=False,
        screening_doc=SAMPLE_SCREENING_DOC,
        recent_st_map=SAMPLE_ST_MAP,
    )
    assert y3_res["approved"] is True
    assert any("AI排雷黄灯警示" in w for w in y3_res["warnings"])


def test_rebalance_compiler_skips_hard_blocked_in_panic_mode(monkeypatch) -> None:
    monkeypatch.setattr("services.g3.ai_screening.load_saved_screening", lambda *a, **k: SAMPLE_SCREENING_DOC)
    monkeypatch.setattr("services.g3.ai_screening.load_recent_st_alias_map", lambda *a, **k: SAMPLE_ST_MAP)
    monkeypatch.setattr("services.mining.ar3_rebalance_compiler.fetch_batch_live_quotes", lambda codes: {})

    fake_overview = {
        "accounts": [{
            "account_id": "acct1",
            "label": "广发证券-曾*",
            "funds": {"total_assets": 100000.0, "available": 60000.0, "market_value": 40000.0},
            "holdings": [{
                "code": "511260",
                "name": "十年国债",
                "quantity": 300,
                "available_quantity": 300,
                "current_price": 135.0,
                "cost_price": 135.0,
                "market_value": 40500.0,
            }],
            "fills": [],
        }],
    }
    fake_radar = {
        "regime": "defensive",
        "is_offensive": False,
        "config": {"max_slots": 2, "stock_budget_cap_pct": 50, "effective_stock_budget_cap_pct": 50},
        "etf_radar": {
            "available": True,
            "panic_triggered": True,
            "targets": [{
                "code": "511260", "ts_code": "511260.SH", "name": "十年国债",
                "close": 135.0, "ma20": 134.0, "basket_weight_pct": 100.0,
            }],
        },
        "stock_radar": {
            "available": True,
            "is_panic_ab_active": True,
            "stock_budget_active_pct": 50,
            "f2a_candidates": [],
            "f3a_candidates": [],
            "exec_queue": [
                {
                    "code": "000711", "name": "京蓝科技", "industry": "环境保护",
                    "close": 4.27, "source_label": "F2A业绩趋势", "net_profit_yoy_pct": 197.7,
                    "executable_10w": True, "ai_hard_blocked": False,  # 模拟绕过上游，测二次双保险！
                },
                {
                    "code": "600801", "name": "华新建材", "industry": "水泥",
                    "close": 27.18, "source_label": "F3A绩优回踩", "net_profit_yoy_pct": 41.0,
                    "executable_10w": True, "ai_hard_blocked": False,
                },
                {
                    "code": "301606", "name": "绿联科技", "industry": "消费电子",
                    "close": 62.38, "source_label": "F3A绩优回踩", "net_profit_yoy_pct": 50.7,
                    "executable_10w": True, "ai_hard_blocked": False,
                },
            ],
        },
    }
    plan = compile_ar3_rebalance_plan(
        market_gate={"regime": "defensive", "regime_label": "防守退潮"},
        panic_signal={"triggered": True},
        account_id="acct1",
        persist=False,
        accounts_overview=fake_overview,
        ar3_radar=fake_radar,
    )
    buy_codes = [b["code"] for b in plan["buy_orders"]]
    assert "000711" not in buy_codes
    assert "600801" in buy_codes
    assert "301606" in buy_codes


def test_trading_plan_compiler_drops_stale_r51_waterline_reason() -> None:
    cmd = compile_daily_trading_command(
        market_gate={
            "regime": "defensive",
            "regime_label": "防守退潮",
            "allow_new_mainwave": False,
            "target_position_cap_pct": 10.0,
            "r51_waterline": {"actual_pct": 85.8, "gap_pct": 75.8, "max_step_pct": 30.0},
        },
        strategy_router={},
        cards=[{
            "code": "000711",
            "name": "京蓝科技",
            "in_holdings": True,
            "total_shares": 2900,
            "available_shares": 0,
            "holdings": [{"current_price": 4.12, "cost_price": 4.272, "market_value": 11948.0, "profit_pct": -3.55}],
            "ai_sentinel": {
                "risk_light": "yellow",
                "catalyst_score": 2,
                "risk_flags": ["2026-08-31刚摘帽且历史存在财务造假行政处罚"],
                "one_line_thesis": "严禁追高",
            },
        }],
        accounts_summary={"total_assets": 100000.0},
    )
    row = cmd["holding_actions"][0]
    assert all("双账号水位" not in r for r in row["reasons"])
    assert row["ai_risk_flags"] == ["2026-08-31刚摘帽且历史存在财务造假行政处罚"]

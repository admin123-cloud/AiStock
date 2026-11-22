# -*- coding: utf-8 -*-
"""AR4 国家队持仓周期分析、动态股仓上限判定及 AR3 联动单测。"""
from __future__ import annotations

from pathlib import Path

from services.g3.premarket_brief import generate_premarket_brief
from services.mining.ar3_rebalance_compiler import compile_ar3_rebalance_plan
from services.mining.ar4_macro_service import (
    _extract_cycle_zones,
    evaluate_ar4_macro_decision,
)


def test_evaluate_ar4_macro_decision_all_phases():
    # 1. ERP < 5.0% 极贵逃顶阀优先级最高，即使国家队分位 >= 70% 也强制锁 0%
    d_hot = evaluate_ar4_macro_decision(
        nt_pctile_250d=85.0,
        broad6_inflow_5d_yi=600.0,
        erp_pct=4.72,
        base_stock_cap_pct=50,
        enable_dynamic=True,
    )
    assert d_hot["phase"] == "erp_overheated"
    assert d_hot["effective_stock_cap_pct"] == 0
    assert d_hot["erp_zone"] == "overheated"

    # 2. 国家队重仓 (>=70%) + 5日净申购 >= 300亿 托底脉冲 -> 65%
    d_pulse = evaluate_ar4_macro_decision(
        nt_pctile_250d=78.0,
        broad6_inflow_5d_yi=1271.4,
        erp_pct=6.27,
        base_stock_cap_pct=50,
        enable_dynamic=True,
    )
    assert d_pulse["phase"] == "heavy_support_pulse"
    assert d_pulse["effective_stock_cap_pct"] == 65
    assert d_pulse["cap_delta_pct"] == 15
    assert d_pulse["erp_zone"] == "cheap"

    # 3. 国家队重仓但无脉冲 -> 65%
    d_heavy = evaluate_ar4_macro_decision(
        nt_pctile_250d=72.5,
        broad6_inflow_5d_yi=40.0,
        erp_pct=5.8,
        base_stock_cap_pct=50,
        enable_dynamic=True,
    )
    assert d_heavy["phase"] == "heavy_support"
    assert d_heavy["effective_stock_cap_pct"] == 65

    # 4. 国家队低位派发 (<30%) -> 35%
    d_low = evaluate_ar4_macro_decision(
        nt_pctile_250d=18.2,
        broad6_inflow_5d_yi=-150.0,
        erp_pct=5.4,
        base_stock_cap_pct=50,
        enable_dynamic=True,
    )
    assert d_low["phase"] == "distribution_low"
    assert d_low["effective_stock_cap_pct"] == 35
    assert d_low["cap_delta_pct"] == -15

    # 5. 中性区 (30%~70%) -> 保持基准 50%
    d_mid = evaluate_ar4_macro_decision(
        nt_pctile_250d=52.0,
        broad6_inflow_5d_yi=20.0,
        erp_pct=5.6,
        base_stock_cap_pct=50,
        enable_dynamic=True,
    )
    assert d_mid["phase"] == "neutral"
    assert d_mid["effective_stock_cap_pct"] == 50

    # 6. 关闭动态调节开关 -> 维持手动基准
    d_off = evaluate_ar4_macro_decision(
        nt_pctile_250d=88.0,
        broad6_inflow_5d_yi=900.0,
        erp_pct=4.5,
        base_stock_cap_pct=45,
        enable_dynamic=False,
    )
    assert d_off["effective_stock_cap_pct"] == 45
    assert d_off["enable_dynamic"] is False


def test_extract_cycle_zones_compresses_and_filters_noise():
    dates = [f"2026-01-{i:02d}" for i in range(1, 16)]
    # 前4天 heavy，中间2天 low(噪声<3天忽略)，再4天 overheat，最后5天 low
    pctiles = [75, 76, 78, 80, 20, 22, 60, 62, 65, 68, 25, 22, 18, 15, 12]
    erps = [6.2, 6.2, 6.1, 6.1, 5.5, 5.5, 4.8, 4.7, 4.6, 4.9, 5.6, 5.6, 5.7, 5.8, 5.9]
    zones = _extract_cycle_zones(dates, pctiles, erps)
    kinds = [z["kind"] for z in zones]
    assert kinds == ["heavy", "overheat", "low"]
    assert zones[0]["days"] == 4
    assert zones[1]["days"] == 4
    assert zones[2]["days"] == 5


def test_rebalance_compiler_respects_dynamic_effective_cap(tmp_path: Path):
    overview = {
        "accounts": [
            {
                "account_id": "acct1",
                "label": "广发-测试",
                "funds": {"total_assets": 400000.0, "available": 400000.0, "market_value": 0.0},
                "holdings": [],
                "fills": [],
            }
        ]
    }
    stock_cand = {
        "code": "600519",
        "name": "测试绩优A",
        "industry": "食品",
        "source_label": "F3A绩优回踩",
        "close": 100.0,
        "stop_loss_price": 90.0,
        "notice_date": "2026-10-25",
        "net_profit_yoy_pct": 35.0,
    }
    etf_target = {
        "code": "510300",
        "name": "沪深300ETF",
        "asset_class_label": "A股宽基",
        "close": 4.0,
        "ma20": 3.9,
        "basket_weight_pct": 100.0,
        "reason": "进攻期宽基",
    }

    # 场景A：国家队重仓把进攻股仓上限放大到 65%（4槽 -> 单槽 16.25%）
    radar_65 = {
        "regime": "offensive",
        "is_offensive": True,
        "config": {
            "active_stock_mode": "ab_blend",
            "etf_core_mode": "regime_focus",
            "stock_budget_cap_pct": 50,
            "enable_macro_dynamic_cap": True,
            "effective_stock_budget_cap_pct": 65,
            "max_slots": 4,
        },
        "etf_radar": {"available": True, "panic_triggered": False, "targets": [etf_target]},
        "stock_radar": {
            "available": True,
            "f2a_candidates": [],
            "f3a_candidates": [stock_cand],
            "exec_queue": [stock_cand],
        },
        "macro_dashboard": {
            "available": True,
            "nt_ratio_pctile_250d": 78.0,
            "broad6_net_inflow_5d_yi": 1271.4,
            "erp_pct": 6.27,
            "decision": {"phase_label": "国家队重仓 + 巨量托底脉冲"},
        },
    }
    plan_65 = compile_ar3_rebalance_plan(
        market_gate={"regime": "offensive", "regime_label": "主升进攻"},
        account_id="acct1",
        persist=False,
        accounts_overview=overview,
        ar3_radar=radar_65,
        base_dir=tmp_path,
    )
    stock_alloc = [x for x in plan_65["target_allocation"] if x["asset_kind"] == "stock"]
    assert len(stock_alloc) == 1
    assert stock_alloc[0]["target_weight_pct"] == 16.25  # 65 / 4

    # 场景B：ERP 过热把进攻股仓硬锁 0% -> 绝不分配任何个股槽位，全给 ETF
    radar_0 = {
        **radar_65,
        "config": {**radar_65["config"], "effective_stock_budget_cap_pct": 0},
    }
    plan_0 = compile_ar3_rebalance_plan(
        market_gate={"regime": "offensive", "regime_label": "主升进攻"},
        account_id="acct1",
        persist=False,
        accounts_overview=overview,
        ar3_radar=radar_0,
        base_dir=tmp_path,
    )
    assert [x for x in plan_0["target_allocation"] if x["asset_kind"] == "stock"] == []
    etf_alloc = [x for x in plan_0["target_allocation"] if x["asset_kind"] == "etf"]
    assert etf_alloc[0]["target_weight_pct"] == 100.0

    # 验证盘前简报渲染国家队与ERP行
    brief = generate_premarket_brief(
        market_gate={"regime": "offensive", "regime_label": "主升进攻"},
        ar3_plan=plan_65,
        base_dir=tmp_path,
    )
    assert "■ 国家队与ERP：" in brief["markdown_text"]
    assert "78.0%" in brief["markdown_text"]
    assert "+1,271亿" in brief["markdown_text"]
    assert "65%" in brief["markdown_text"]


def test_ar45_l3b_qmt_top2_invvol_and_southbound_parity():
    """验证 AR4.5-L3b 非进攻期 Top2 逆波动加权与港科南向滤网在 QMT 原生策略中口径一致。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "qmt_ar3_etf01_native",
        Path(__file__).resolve().parents[1] / "scripts" / "qmt_ar3_etf01_native.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rows = [
        {"code": "513180.SH", "name": "恒生科技", "asset_class": "overseas", "ret_20d_pct": 12.0, "vol_20d_pct": 30.0, "risk_adj_mom": 0.34, "eligible": True},
        {"code": "512890.SH", "name": "红利低波", "asset_class": "cn_dividend", "ret_20d_pct": 5.8, "vol_20d_pct": 12.0, "risk_adj_mom": 0.35, "eligible": True},
        {"code": "511260.SH", "name": "十年国债", "asset_class": "bond", "ret_20d_pct": 0.5, "vol_20d_pct": 4.0, "risk_adj_mom": 0.06, "eligible": True},
    ]

    # 默认允许港科：红利低波(0.35) + 恒生科技(0.34) 逆波动配权 -> 红利权重显著更高
    mod.ALLOW_HK_TECH = True
    mod.HK_SOUTHBOUND_BOOST = False
    t_allow, _ = mod.decide_ar3_etf01_targets({"is_offensive": False, "regime_label": "防守"}, rows)
    assert set(t_allow.keys()) == {"512890.SH", "513180.SH"}
    assert t_allow["512890.SH"] > 0.70  # 1/12 vs 1/30 -> 71.4% vs 28.6%
    assert abs(sum(t_allow.values()) - 1.0) < 1e-3

    # 南向20日净流出拦截港科：恒生科技被剔除 -> 自动切红利低波 + 十年国债
    mod.ALLOW_HK_TECH = False
    t_block, _ = mod.decide_ar3_etf01_targets({"is_offensive": False, "regime_label": "防守"}, rows)
    assert "513180.SH" not in t_block
    assert set(t_block.keys()) == {"512890.SH", "511260.SH"}
    mod.ALLOW_HK_TECH = True

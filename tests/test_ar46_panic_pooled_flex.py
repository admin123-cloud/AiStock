# -*- coding: utf-8 -*-
"""AR4.6 极端冰点 4 槽池化弹性整手分配器（pooled_flex + 自适应单手门槛）单元测试。"""
from __future__ import annotations

from services.mining.panic_rebound_monitor import (
    PANIC_MAX_LOT_YUAN,
    PANIC_SLOT_FLEX_MULT,
    PANIC_STOCK_BUDGET_PCT,
    allocate_panic_ab_pooled_flex,
)


def test_pooled_flex_10w_real_four_stocks_high_utilization_and_strict_50pct_cap():
    """以 2026-11-26 真实 4 只冰点真龙验证：总花费严守 50% 上限、单槽严守 1.25x、整手利用率显著高于 strict_slot。"""
    cands = [
        {"code": "301697", "name": "贝特利", "industry": "化工", "price": 26.04},
        {"code": "000711", "name": "京蓝科技", "industry": "环境保护", "price": 4.27},
        {"code": "301606", "name": "绿联科技", "industry": "元器件", "price": 62.40},
        {"code": "600801", "name": "华新建材", "industry": "建材", "price": 27.19},
    ]
    res = allocate_panic_ab_pooled_flex(
        cands,
        total_assets=101600.0,
        target_weight_pct=float(PANIC_STOCK_BUDGET_PCT),
        max_slots=4,
        flex_mult=PANIC_SLOT_FLEX_MULT,
        max_lot_yuan_floor=PANIC_MAX_LOT_YUAN,
    )
    assert res["mode"] == "pooled_flex"
    assert len(res["picked"]) == 4
    assert res["total_allocated_yuan"] <= res["total_budget_yuan"] + 1e-6
    assert res["effective_weight_pct"] <= 50.0
    assert res["utilization_pct"] >= 97.0
    for row in res["picked"]:
        assert row["suggested_shares"] >= 100
        assert row["suggested_shares"] % 100 == 0
        assert row["est_amount"] <= res["slot_cap_yuan"] + 1e-6


def test_pooled_flex_adaptive_lot_cap_10w_vs_30w():
    """验证自适应单手门槛：10万账户放行 1.68万/手、跳过 2.2万/手；30万账户自动放行 2.2万/手。"""
    cands = [
        {"code": "300001", "name": "高价A", "industry": "电子", "price": 220.0},   # 22,000/手
        {"code": "300002", "name": "中价B", "industry": "通信", "price": 168.0},   # 16,800/手
        {"code": "000001", "name": "低价C", "industry": "化工", "price": 20.0},    # 2,000/手
        {"code": "000002", "name": "低价D", "industry": "医药", "price": 15.0},    # 1,500/手
        {"code": "000003", "name": "低价E", "industry": "机械", "price": 10.0},    # 1,000/手
    ]
    res_10w = allocate_panic_ab_pooled_flex(cands, total_assets=100000.0)
    codes_10w = [x["code"] for x in res_10w["picked"]]
    assert "300001" not in codes_10w
    assert "300002" in codes_10w
    assert len(codes_10w) == 4
    assert res_10w["total_allocated_yuan"] <= 50000.0 + 1e-6
    assert any(s["code"] == "300001" for s in res_10w["skipped_over_lot"])

    res_30w = allocate_panic_ab_pooled_flex(cands, total_assets=300000.0)
    codes_30w = [x["code"] for x in res_30w["picked"]]
    assert "300001" in codes_30w
    assert len(codes_30w) == 4
    assert res_30w["total_allocated_yuan"] <= 150000.0 + 1e-6


def test_pooled_flex_star_market_200_min_and_industry_dedup():
    """验证科创板 688 起步 200 股、1 股步长，以及同行业限 1 只与涨停跳过。"""
    cands = [
        {"code": "688100", "name": "科创A", "industry": "半导体", "price": 50.0},      # 200股=10,000元
        {"code": "688200", "name": "科创同业", "industry": "半导体", "price": 30.0},    # 同业跳过
        {"code": "688300", "name": "科创超高", "industry": "软件", "price": 120.0},    # 200股=24,000元超限
        {"code": "300100", "name": "涨停票", "industry": "军工", "price": 20.0, "near_limit": True},
        {"code": "000100", "name": "主板B", "industry": "建材", "price": 25.0},
    ]
    res = allocate_panic_ab_pooled_flex(cands, total_assets=100000.0)
    codes = [x["code"] for x in res["picked"]]
    assert codes == ["688100", "000100"]
    star_row = res["picked"][0]
    assert star_row["min_lot_shares"] == 200
    assert star_row["suggested_shares"] >= 200
    assert star_row["est_amount"] <= res["slot_cap_yuan"] + 1e-6

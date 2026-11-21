# -*- coding: utf-8 -*-
"""G3 前向验真（AI 排雷 T+5/T+10）与稳健腿 6 大宽基 ETF 自动影子台账单元测试。"""
from __future__ import annotations

import json
from pathlib import Path

from services.g3.forward_eval import (
    evaluate_ai_screening_forward,
    evaluate_defense_etf_shadow,
)


def test_ai_screening_forward_t5_t10_and_green_vs_red_alpha(tmp_path: Path):
    sdir = tmp_path / "g3_ai_screening"
    sdir.mkdir(parents=True)
    doc = {
        "schema": "g3_ai_screening/1",
        "decision_date": "2026-11-02",
        "items_by_code": {
            "600000": {"code": "600000.SH", "name": "浦发银行", "industry": "金融", "risk_light": "green", "catalyst_score": 5},
            "600795": {"code": "600795.SH", "name": "国电电力", "industry": "电力", "risk_light": "red", "catalyst_score": 1},
        },
    }
    (sdir / "20261102.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    # 构造 11 个交易日（T0 + T1..T10）：浦发涨 +10% / +20%，国电跌 -10% / -15%
    dates = [f"2026-11-{d:02d}" for d in range(2, 13)]
    closes_map = {
        "600000": [(dates[i], 10.0 * (1.0 + i * 0.02)) for i in range(11)],
        "600795": [(dates[i], 10.0 * (1.0 - i * 0.015)) for i in range(11)],
    }

    res = evaluate_ai_screening_forward(
        screening_dir=sdir,
        closes_fetcher=lambda _codes, _min_d: closes_map,
    )
    assert res["batches_count"] == 1
    assert res["samples_total"] == 2
    assert res["by_light"]["green"]["t5"]["avg_pct"] == 10.0
    assert res["by_light"]["green"]["t5"]["win_rate_pct"] == 100.0
    assert res["by_light"]["red"]["t5"]["avg_pct"] == -7.5
    assert res["alpha_green_vs_red_5d_pct"] == 17.5


def test_defense_etf_shadow_cuts_drawdown_in_downtrend(tmp_path: Path):
    # 构造 100 个交易日：前 50 日上涨，后 50 日大跌 -30%
    dates = [f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(100)]
    prices = [3000.0 + i * 15.0 for i in range(50)] + [3750.0 - (i - 49) * 22.0 for i in range(50, 100)]
    series = {"000300.SH": list(zip(dates, prices))}

    res = evaluate_defense_etf_shadow(
        window_trading_days=50,
        series_fetcher=lambda: series,
        shadow_dir=tmp_path / "etf_shadow",
        persist=True,
    )
    assert res["available"] is True
    s = res["summary"]
    # 下跌趋势中波动率目标破位转现金，回撤显著小于满仓沪深300
    assert abs(s["max_drawdown_pct"]) < abs(s["bench_max_drawdown_pct"])
    assert s["drawdown_saved_pct"] > 0
    assert (tmp_path / "etf_shadow" / "latest.json").exists()

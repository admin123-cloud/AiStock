# -*- coding: utf-8 -*-
"""G3 Phase 2A（AI 候选股排雷与催化）与 Phase 2B（盘前 09:15 市场主线简报）单元测试。"""
from __future__ import annotations

from pathlib import Path

from services.g3.ai_screening import (
    evaluate_rule_baseline_item,
    merge_screening_into_mainwave,
    save_agent_screening,
)
from services.g3.premarket_brief import (
    generate_premarket_brief,
    load_latest_premarket_brief,
)


def test_rule_baseline_st_red_and_strong_green():
    st_item = evaluate_rule_baseline_item(
        {"code": "301030.SZ", "name": "*ST仕净", "industry": "环保", "score": 92.0, "resonance": 3, "stage": "waiting_30m"}
    )
    assert st_item["risk_light"] == "red"
    assert st_item["catalyst_score"] == 1

    good_item = evaluate_rule_baseline_item(
        {"code": "000530.SZ", "name": "冰山冷热", "industry": "通用设备", "score": 96.2, "resonance": 5, "stage": "waiting_30m"}
    )
    assert good_item["risk_light"] == "green"
    assert good_item["catalyst_score"] == 4


def test_agent_screening_merge_cannot_whitewash_st_red(tmp_path: Path):
    base_dir = tmp_path / "ai_screening"
    # Agent 试图把 ST 股标绿，并把另一只普通股标为高催化绿灯
    save_agent_screening(
        decision_date="2026-11-20",
        items=[
            {
                "code": "301030.SZ",
                "name": "*ST仕净",
                "risk_light": "green",
                "catalyst_score": 5,
                "catalyst_type": "业绩超预期",
                "one_line_thesis": "测试不可洗白红灯",
            },
            {
                "code": "000530.SZ",
                "name": "冰山冷热",
                "risk_light": "green",
                "catalyst_score": 5,
                "catalyst_type": "行业涨价/景气",
                "one_line_thesis": "冷链设备订单饱满，无近期减持解禁。",
                "risk_flags": [],
            },
        ],
        summary_note="已完成 2 只深度穿透",
        base_dir=base_dir,
    )

    mw = {
        "decision_date": "2026-11-20",
        "candidates": [
            {"code": "301030.SZ", "name": "*ST仕净", "industry": "环保", "score": 92.0, "resonance": 3, "stage": "waiting_30m"},
            {"code": "000530.SZ", "name": "冰山冷热", "industry": "通用设备", "score": 95.0, "resonance": 4, "stage": "waiting_30m"},
        ],
    }
    merged = merge_screening_into_mainwave(
        mw,
        market_gate={"regime": "defensive", "regime_label": "防守模式", "allow_new_mainwave": False},
        base_dir=base_dir,
    )

    by_code = {c["code"]: c["ai_screening"] for c in merged["candidates"]}
    assert by_code["301030.SZ"]["risk_light"] == "red"
    assert by_code["000530.SZ"]["risk_light"] == "green"
    assert by_code["000530.SZ"]["catalyst_score"] == 5

    summary = merged["ai_screening_summary"]
    assert summary["mode"] == "agent_enriched"
    assert summary["counts"] == {"green": 1, "yellow": 0, "red": 1}
    assert summary["allow_new_mainwave"] is False
    assert "严禁开新主升仓" in summary["gate_notice"]
    assert len(summary["top_picks"]) == 1
    assert summary["top_picks"][0]["code"] == "000530.SZ"


def test_premarket_brief_generation_and_persistence(tmp_path: Path):
    brief_dir = tmp_path / "brief"
    gate = {
        "ref_date": "2026-11-21",
        "regime": "defensive",
        "regime_label": "防守模式",
        "tone": "red",
        "allow_new_mainwave": False,
        "target_position_cap_pct": 25.0,
        "r51_waterline": {
            "actual_pct": 83.0,
            "status": "overweight",
            "advice": "当前水位 83.0% 超配，逢反弹分步减仓 ≤15.0%",
        },
        "sources": {
            "market_mood": {"mood_label": "退潮冰点", "limit_up_count": 36, "limit_down_count": 106},
            "index_r51": {"confirmed_regime": "down", "streak_days": 5},
            "turnover_forecast": {"forecast_trillion": 1.44},
        },
    }
    mw = {
        "candidates": [
            {"code": "000530.SZ", "name": "冰山冷热", "industry": "通用设备", "score": 95.0, "ai_screening": {"risk_light": "green"}},
            {"code": "000823.SZ", "name": "超声电子", "industry": "半导体", "score": 91.0, "ai_screening": {"risk_light": "green"}},
            {"code": "600360.SH", "name": "华微电子", "industry": "半导体", "score": 90.0, "ai_screening": {"risk_light": "green"}},
        ],
        "ai_screening_summary": {
            "top_picks": [{"code": "000530.SZ", "name": "冰山冷热", "catalyst_score": 5}],
        },
    }

    brief = generate_premarket_brief(
        market_gate=gate,
        mainwave_payload=mw,
        overnight_notes=["美股科技股小幅收跌"],
        agent_thesis="市场处于退潮冰点，今日严控仓位，仅跟踪半导体与通用设备前排抗跌表现。",
        source="agent_enriched",
        base_dir=brief_dir,
    )

    assert brief["date"] == "2026-11-21"
    assert brief["core_themes"][0]["industry"] == "半导体"
    assert brief["core_themes"][0]["candidate_count"] == 2
    assert "严禁开新主升仓" in brief["markdown_text"]
    assert "AI 主线研判" in brief["markdown_text"]

    loaded = load_latest_premarket_brief("2026-11-21", base_dir=brief_dir)
    assert loaded is not None
    assert loaded["source"] == "agent_enriched"

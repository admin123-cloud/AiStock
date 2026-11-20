# -*- coding: utf-8 -*-
"""G3 Phase 2A：AI 候选股排雷与催化过滤引擎（AI Stock Screener）。

职责：
1. 维护结构化排雷与催化契约（落盘 runtime/g3_ai_screening/YYYYMMDD.json 与 latest.json），
   支持由现有 Agent / 定时工作流注入深度公告/财报/减持/新闻穿透结果（source="agent_enriched"）；
2. 在 Agent 尚未注入当日深度结果前，提供确定性排雷底线（source="rule_baseline"）：
   - ST/*ST/退市风险红灯一票否决；
   - 数据阻断标黄；
   - 基于主升分与同业共振生成基础催化分与观察逻辑；
3. 将排雷与催化结果无缝融合进 /api/g3/mainwave，输出绿灯 Top 5 精选池并与 Phase 1A 大盘闸门联动。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))
VALID_LIGHTS = ("green", "yellow", "red")
LIGHT_RANK = {"green": 0, "yellow": 1, "red": 2}


def _screening_dir(base_dir: Optional[Path] = None) -> Path:
    p = base_dir or runtime_path("g3_ai_screening")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _norm_code6(code: Any) -> str:
    return str(code or "").split(".")[0].strip().zfill(6)


def _norm_date(raw: Any) -> str:
    s = str(raw or "").strip()
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d")


def _safe_float(v: Any) -> Optional[float]:
    if v in (None, "", "-"):
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _read_json_safe(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 1. 确定性底线排雷（无 Agent 注入时的诚实兜底）
# ---------------------------------------------------------------------------

def evaluate_rule_baseline_item(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """对单只主升候选执行确定性基础排雷与催化初评。"""
    code = str(candidate.get("code") or "")
    code6 = _norm_code6(code)
    name = str(candidate.get("name") or code)
    industry = str(candidate.get("industry") or "未分类")
    stage = str(candidate.get("stage") or "")
    score = _safe_float(candidate.get("score")) or 0.0
    resonance = int(_safe_float(candidate.get("resonance")) or 0)

    risk_flags: list[str] = []
    risk_light = "green"

    name_upper = name.upper()
    if "ST" in name_upper or "退" in name:
        risk_light = "red"
        risk_flags.append("ST/退市风险股（红灯一票否决）")

    if stage == "data_blocked":
        if risk_light != "red":
            risk_light = "yellow"
        risk_flags.append("30m/日线数据阻断，暂不可确认买点")

    if code6.startswith(("8", "4", "92")):
        if risk_light == "green":
            risk_light = "yellow"
        risk_flags.append("北交所高弹性标的，流动性与波动风险偏高")

    if resonance < 2:
        if risk_light == "green":
            risk_light = "yellow"
        risk_flags.append("同业共振偏弱（<2只）")

    # 基础催化评分（1~4分；5分保留给 Agent 确认业绩/政策/订单强催化）
    if risk_light == "red":
        catalyst_score = 1
        catalyst_type = "风险排除"
        thesis = f"触发红灯排雷：{'；'.join(risk_flags)}，禁止纳入开仓池。"
    else:
        if score >= 95 and resonance >= 4:
            catalyst_score = 4
            catalyst_type = "板块强共振"
        elif score >= 90 and resonance >= 3:
            catalyst_score = 3
            catalyst_type = "板块共振"
        else:
            catalyst_score = 2
            catalyst_type = "量价主升"
        if risk_flags:
            thesis = f"{industry}主升分 {score:.1f}（共振 {resonance} 只）；注意：{'；'.join(risk_flags)}。待 Agent 补充公告/财报穿透。"
        else:
            thesis = f"{industry}主升分 {score:.1f}（同业共振 {resonance} 只），基础量价形态健康；待 Agent 补充公告排雷与业绩催化穿透。"

    return {
        "code": code or code6,
        "code6": code6,
        "name": name,
        "industry": industry,
        "risk_light": risk_light,
        "risk_flags": risk_flags,
        "catalyst_score": catalyst_score,
        "catalyst_type": catalyst_type,
        "one_line_thesis": thesis,
        "source": "rule_baseline",
        "updated_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 2. Agent 深度排雷催化落盘与读取
# ---------------------------------------------------------------------------

def save_agent_screening(
    *,
    decision_date: str,
    items: Sequence[Mapping[str, Any]],
    summary_note: Optional[str] = None,
    agent_name: str = "antigravity_agent",
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """校验并落盘 Agent 产出的结构化排雷与催化评分结果。"""
    d_str = _norm_date(decision_date)
    ymd = d_str.replace("-", "")
    now_iso = datetime.now(BJ_TZ).isoformat(timespec="seconds")

    normalized_items: dict[str, dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        code_raw = str(raw.get("code") or "").strip()
        code6 = _norm_code6(code_raw)
        if not code6 or code6 == "000000":
            continue
        light = str(raw.get("risk_light") or "yellow").lower().strip()
        if light not in VALID_LIGHTS:
            light = "yellow"
        c_score = int(_safe_float(raw.get("catalyst_score")) or 3)
        c_score = max(1, min(5, c_score))
        flags = [str(x).strip() for x in (raw.get("risk_flags") or []) if str(x).strip()]
        normalized_items[code6] = {
            "code": code_raw or code6,
            "code6": code6,
            "name": str(raw.get("name") or ""),
            "industry": str(raw.get("industry") or ""),
            "risk_light": light,
            "risk_flags": flags,
            "catalyst_score": c_score,
            "catalyst_type": str(raw.get("catalyst_type") or "逻辑催化").strip(),
            "one_line_thesis": str(raw.get("one_line_thesis") or "").strip() or "Agent 已完成公告与催化复核。",
            "source": str(raw.get("source") or "agent_enriched"),
            "updated_at": now_iso,
        }

    doc = {
        "schema": "g3_ai_screening/1",
        "decision_date": d_str,
        "generated_at": now_iso,
        "agent_name": agent_name,
        "summary_note": summary_note or f"Agent 已完成 {len(normalized_items)} 只候选股公告排雷与催化逻辑评估",
        "items_by_code": normalized_items,
    }

    store_dir = _screening_dir(base_dir)
    dated_path = store_dir / f"{ymd}.json"
    latest_path = store_dir / "latest.json"
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    dated_path.write_text(text, encoding="utf-8")
    latest_path.write_text(text, encoding="utf-8")
    return doc


def load_saved_screening(
    decision_date: Optional[str] = None,
    *,
    base_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """优先按 decision_date 精确读取，未指定时回退 latest.json。"""
    store_dir = base_dir or runtime_path("g3_ai_screening")
    if not store_dir.exists():
        return None
    if decision_date:
        ymd = _norm_date(decision_date).replace("-", "")
        exact = _read_json_safe(store_dir / f"{ymd}.json")
        if exact:
            return exact
    return _read_json_safe(store_dir / "latest.json")


# ---------------------------------------------------------------------------
# 3. 融合进 /api/g3/mainwave 输出
# ---------------------------------------------------------------------------

def merge_screening_into_mainwave(
    mainwave_payload: Mapping[str, Any],
    *,
    market_gate: Optional[Mapping[str, Any]] = None,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """将 AI 排雷与催化结果融合进主升浪扫描输出，生成绿灯精选池并联动大盘总闸门。"""
    result = dict(mainwave_payload)
    decision_date = str(result.get("decision_date") or "")
    saved = load_saved_screening(decision_date or None, base_dir=base_dir)
    saved_same_day = bool(saved and decision_date and str(saved.get("decision_date") or "") == decision_date)
    saved_map = (saved.get("items_by_code") or {}) if saved_same_day else {}

    enriched_candidates: list[dict[str, Any]] = []
    counts = {"green": 0, "yellow": 0, "red": 0}
    agent_enriched_count = 0

    for raw_cand in result.get("candidates") or []:
        cand = dict(raw_cand)
        code6 = _norm_code6(cand.get("code"))
        baseline = evaluate_rule_baseline_item(cand)
        agent_item = saved_map.get(code6)
        if isinstance(agent_item, Mapping):
            # 保留红灯一票否决（若底线已触发 ST/退市红灯，Agent 不得洗白为绿灯）
            final_light = "red" if baseline["risk_light"] == "red" else str(agent_item.get("risk_light") or "yellow")
            merged_flags = list(dict.fromkeys([*baseline["risk_flags"], *(agent_item.get("risk_flags") or [])]))
            ai_info = {
                **baseline,
                **agent_item,
                "risk_light": final_light,
                "risk_flags": merged_flags,
                "source": str(agent_item.get("source") or "agent_enriched"),
            }
            if ai_info["source"] == "agent_enriched":
                agent_enriched_count += 1
        else:
            ai_info = baseline

        counts[ai_info["risk_light"]] = counts.get(ai_info["risk_light"], 0) + 1
        cand["ai_screening"] = ai_info
        enriched_candidates.append(cand)

    # 排序：绿灯优先 -> 催化分降序 -> 主升分降序
    top_picks_sorted = sorted(
        [c for c in enriched_candidates if (c.get("ai_screening") or {}).get("risk_light") == "green"],
        key=lambda x: (
            -int((x.get("ai_screening") or {}).get("catalyst_score") or 0),
            -(_safe_float(x.get("score")) or 0.0),
        ),
    )
    top_picks = [
        {
            "code": c.get("code"),
            "name": c.get("name"),
            "industry": c.get("industry"),
            "score": c.get("score"),
            "resonance": c.get("resonance"),
            "stage": c.get("stage"),
            "risk_light": c["ai_screening"]["risk_light"],
            "catalyst_score": c["ai_screening"]["catalyst_score"],
            "catalyst_type": c["ai_screening"]["catalyst_type"],
            "one_line_thesis": c["ai_screening"]["one_line_thesis"],
            "source": c["ai_screening"]["source"],
        }
        for c in top_picks_sorted[:5]
    ]

    allow_new_mainwave = True
    gate_regime = None
    gate_label = None
    if isinstance(market_gate, Mapping):
        allow_new_mainwave = bool(market_gate.get("allow_new_mainwave", True))
        gate_regime = market_gate.get("regime")
        gate_label = market_gate.get("regime_label")

    mode = "agent_enriched" if agent_enriched_count > 0 else "rule_baseline"
    gate_notice = (
        f"当前大盘处于【{gate_label or '防守模式'}】，严禁开新主升仓；以下绿灯精选仅作跟踪观察池，待大盘转暖再出手。"
        if not allow_new_mainwave
        else f"当前大盘【{gate_label or '允许开仓'}】，可优先从绿灯高催化池中结合 10:00 首根 30m 确认择优介入。"
    )

    result["candidates"] = enriched_candidates
    result["ai_screening_summary"] = {
        "schema": "g3_ai_screening_summary/1",
        "decision_date": decision_date,
        "mode": mode,
        "agent_enriched_count": agent_enriched_count,
        "total_candidates": len(enriched_candidates),
        "counts": counts,
        "allow_new_mainwave": allow_new_mainwave,
        "market_gate_regime": gate_regime,
        "gate_notice": gate_notice,
        "agent_summary_note": saved.get("summary_note") if saved_same_day else None,
        "generated_at": (saved.get("generated_at") if saved_same_day else datetime.now(BJ_TZ).isoformat(timespec="seconds")),
        "top_picks": top_picks,
    }
    return result

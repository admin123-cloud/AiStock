# -*- coding: utf-8 -*-
"""G3 Phase 2B：盘前 09:15 AI 市场主线简报引擎（Pre-Market Brief）。

职责：
1. 聚合 Phase 1A（大盘择时总闸门 × R51 仓位水位）+ Phase 2A（主升浪候选行业共振与 AI 排雷精选池）
   + 可选的 AI Agent 隔夜政策/行业新闻研判；
2. 生成结构化盘前简报（落盘 runtime/g3_premarket_brief/YYYYMMDD.json 与 latest.json）与一页式 Markdown；
3. 支持一键通过现有微信通道（services/trading/t_notify.py）推送盘前简报。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))


def _brief_dir(base_dir: Optional[Path] = None) -> Path:
    p = base_dir or runtime_path("g3_premarket_brief")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _norm_date(raw: Any) -> str:
    s = str(raw or "").strip()
    m = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d")


def _read_json_safe(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _extract_core_themes(candidates: Sequence[Mapping[str, Any]], top_n: int = 3) -> list[dict[str, Any]]:
    """从主升浪候选池按行业聚合出当前最强的 1~3 条板块主线。"""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for c in candidates:
        ind = str(c.get("industry") or "").strip()
        if not ind or ind in ("未分类", "未知"):
            continue
        ai = c.get("ai_screening") if isinstance(c.get("ai_screening"), Mapping) else {}
        if ai.get("risk_light") == "red":
            continue
        groups.setdefault(ind, []).append(c)

    themes: list[dict[str, Any]] = []
    for ind, rows in groups.items():
        scores = [float(r.get("score") or 0) for r in rows]
        green_cnt = sum(1 for r in rows if (r.get("ai_screening") or {}).get("risk_light") == "green")
        avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0
        reps = [f"{r.get('name') or r.get('code')}({str(r.get('code') or '').split('.')[0]})" for r in rows[:3]]
        themes.append({
            "industry": ind,
            "candidate_count": len(rows),
            "green_count": green_cnt,
            "avg_score": avg_score,
            "representatives": reps,
            "strength_score": round(len(rows) * 10 + green_cnt * 4 + avg_score * 0.2, 2),
        })

    themes.sort(key=lambda x: x["strength_score"], reverse=True)
    return themes[:top_n]


def generate_premarket_brief(
    *,
    market_gate: Mapping[str, Any],
    mainwave_payload: Optional[Mapping[str, Any]] = None,
    strategy_matrix: Optional[Mapping[str, Any]] = None,
    overnight_notes: Optional[Sequence[str]] = None,
    agent_thesis: Optional[str] = None,
    source: str = "auto_baseline",
    trade_date: Optional[str] = None,
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """生成并落盘当日 09:15 盘前市场主线简报。"""
    now_bj = datetime.now(BJ_TZ)
    d_str = _norm_date(trade_date or market_gate.get("ref_date") or now_bj.strftime("%Y-%m-%d"))
    ymd = d_str.replace("-", "")

    wl = market_gate.get("r51_waterline") if isinstance(market_gate.get("r51_waterline"), Mapping) else {}
    sources = market_gate.get("sources") if isinstance(market_gate.get("sources"), Mapping) else {}
    mood = sources.get("market_mood") if isinstance(sources.get("market_mood"), Mapping) else {}
    idx = sources.get("index_r51") if isinstance(sources.get("index_r51"), Mapping) else {}
    tf = sources.get("turnover_forecast") if isinstance(sources.get("turnover_forecast"), Mapping) else {}

    mw = dict(mainwave_payload or {})
    candidates = mw.get("candidates") or []
    ai_sum = mw.get("ai_screening_summary") if isinstance(mw.get("ai_screening_summary"), Mapping) else {}
    top_picks = list(ai_sum.get("top_picks") or [])[:5]
    core_themes = _extract_core_themes(candidates, top_n=3)

    regime = str(market_gate.get("regime") or "defensive")
    regime_label = str(market_gate.get("regime_label") or "防守模式")
    allow_new = bool(market_gate.get("allow_new_mainwave", False))
    cap_pct = float(market_gate.get("target_position_cap_pct") or 25.0)
    actual_pct = wl.get("actual_pct")

    # 生成今日行动清单
    checklist: list[str] = []
    if not allow_new:
        checklist.append(f"【总开关】今日为{regime_label}（上限 ≤{cap_pct:.0f}%），严禁开新主升仓。")
    else:
        checklist.append(f"【总开关】今日为{regime_label}（上限 ≤{cap_pct:.0f}%），允许从绿灯精选池中择优关注 10:00 首根 30m 确认。")

    if wl.get("advice"):
        checklist.append(f"【R51水位】{wl['advice']}")

    if core_themes:
        theme_str = "、".join(f"{t['industry']}({t['candidate_count']}只)" for t in core_themes)
        checklist.append(f"【板块主线】主升共振靠前行业：{theme_str}。")
    else:
        checklist.append("【板块主线】当前无明显主升共振主线，耐心等待右侧信号。")

    if top_picks and allow_new:
        picks_str = "、".join(f"{p.get('name')}({str(p.get('code') or '').split('.')[0]})" for p in top_picks[:3])
        checklist.append(f"【精选关注】绿灯高催化池前排：{picks_str}。")
    elif top_picks and not allow_new:
        checklist.append(f"【观察名单】绿灯池现有 {len(top_picks)} 只储备标的，因大盘防守仅观察不开仓。")

    # 融合 Phase 4 三腿配额到作战清单与 Markdown 简报
    alloc = (strategy_matrix or {}).get("allocation") if isinstance(strategy_matrix, Mapping) else None
    alloc_line = None
    if isinstance(alloc, Mapping):
        alloc_line = (
            f"进攻腿(主升) {alloc.get('offense_pct', 0):.0f}%｜"
            f"稳健腿(ETF) {alloc.get('defense_etf_pct', 0):.0f}%｜"
            f"增强腿(做T) {alloc.get('t_enhance_pct', 0):.0f}%｜"
            f"现金留存 {alloc.get('cash_pct', 0):.0f}%"
        )
        checklist.append(f"【三腿配额】{alloc_line}")

    # 生成一页式 Markdown 简报（适合前端展示与微信推送）
    turnover_str = f"{tf['forecast_trillion']}万亿" if tf.get("forecast_trillion") is not None else "—"
    actual_str = f"{actual_pct:.1f}%" if actual_pct is not None else "待同步"
    lines = [
        f"【AiStock 盘前主线简报 · {d_str}】",
        f"■ 市场定位：{regime_label}｜仓位上限 ≤{cap_pct:.0f}%｜{'允许开新主升' if allow_new else '严禁开新主升'}",
        f"■ 当前水位：{actual_str}（{wl.get('status') or 'unknown'}）",
        f"■ 核心盘面：上证R51={idx.get('confirmed_regime') or '—'}（连续{idx.get('streak_days') or 0}日）｜情绪={mood.get('mood_label') or '—'}（涨停{mood.get('limit_up_count', '—')}/跌停{mood.get('limit_down_count', '—')}）｜成交额={turnover_str}",
    ]
    if alloc_line:
        lines.append(f"■ 三腿配额：{alloc_line}")
    if agent_thesis:
        lines.append(f"■ AI 主线研判：{agent_thesis.strip()}")
    if overnight_notes:
        lines.append("■ 隔夜要点：" + "；".join(str(x).strip() for x in overnight_notes if str(x).strip()))
    lines.append("■ 今日作战清单：")
    for idx_i, item in enumerate(checklist, 1):
        lines.append(f"  {idx_i}. {item}")

    markdown_text = "\n".join(lines)

    brief = {
        "schema": "g3_premarket_brief/1",
        "date": d_str,
        "generated_at": now_bj.isoformat(timespec="seconds"),
        "source": source,
        "market_stance": {
            "regime": regime,
            "regime_label": regime_label,
            "tone": market_gate.get("tone") or "red",
            "allow_new_mainwave": allow_new,
            "target_cap_pct": cap_pct,
            "actual_pct": actual_pct,
            "waterline_status": wl.get("status"),
            "waterline_advice": wl.get("advice"),
        },
        "core_themes": core_themes,
        "top_picks": top_picks,
        "overnight_notes": [str(x).strip() for x in (overnight_notes or []) if str(x).strip()],
        "agent_thesis": (agent_thesis or "").strip() or None,
        "action_checklist": checklist,
        "markdown_text": markdown_text,
    }

    store_dir = _brief_dir(base_dir)
    text = json.dumps(brief, ensure_ascii=False, indent=2)
    (store_dir / f"{ymd}.json").write_text(text, encoding="utf-8")
    (store_dir / "latest.json").write_text(text, encoding="utf-8")
    return brief


def load_latest_premarket_brief(
    trade_date: Optional[str] = None,
    *,
    base_dir: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    store_dir = base_dir or runtime_path("g3_premarket_brief")
    if not store_dir.exists():
        return None
    if trade_date:
        ymd = _norm_date(trade_date).replace("-", "")
        exact = _read_json_safe(store_dir / f"{ymd}.json")
        if exact:
            return exact
    return _read_json_safe(store_dir / "latest.json")


def push_premarket_brief_wechat(brief: Mapping[str, Any]) -> dict[str, Any]:
    """复用 t_notify 现有微信通道推送盘前简报。"""
    from services.trading.t_notify import send_wechat_notification

    title = f"AiStock 盘前主线简报 ({brief.get('date') or ''})"
    content = str(brief.get("markdown_text") or "")
    ok, channel, detail = send_wechat_notification(title=title, content=content)
    return {"ok": ok, "channel": channel, "detail": detail}

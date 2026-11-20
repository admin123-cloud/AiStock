# -*- coding: utf-8 -*-
"""G3 Phase 3A：盘后 AI 交易教练引擎（AI Trading Coach）。

职责：
1. 自动扫描近 N 自然日（默认 14 天）的双账号成交对账 ledger、coaching 违约记录与大盘总闸门状态；
2. 识别并量化高频行为出血点：
   - contract_ignored（扛单未止损）：按标的聚合连续违约天数与估算拖延风险；
   - off_plan_trade（计划外交易 / 追高情绪单）；
   - knife_catch（下降周期逆势接飞刀）；
   - overweight_in_defense（大盘防守期高水位硬扛）；
   - missed_signals（信号未跟单率偏高）；
3. 输出周度行为体检评分、Top 出血点与“下周必须守住的 1~2 条行为红线”；
4. 支持现有 Agent / 定时工作流注入深度自然语言复盘点评（runtime/g3_ai_coach/YYYYMMDD.json）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from services.g3.attribution import build_four_dim_attribution
from utils.common.paths import runtime_path

BJ_TZ = timezone(timedelta(hours=8))


def _coach_dir(base_dir: Optional[Path] = None) -> Path:
    p = base_dir or runtime_path("g3_ai_coach")
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


def save_agent_coach_notes(
    *,
    report_date: str,
    headline: str,
    weekly_diagnosis: str,
    red_lines: Sequence[str],
    agent_name: str = "antigravity_agent",
    base_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """落盘 Agent 生成的盘后/周度交易教练深度点评。"""
    d_str = _norm_date(report_date)
    ymd = d_str.replace("-", "")
    now_iso = datetime.now(BJ_TZ).isoformat(timespec="seconds")
    doc = {
        "schema": "g3_ai_coach_notes/1",
        "date": d_str,
        "generated_at": now_iso,
        "agent_name": agent_name,
        "headline": headline.strip(),
        "weekly_diagnosis": weekly_diagnosis.strip(),
        "red_lines": [str(x).strip() for x in red_lines if str(x).strip()][:5],
    }
    store = _coach_dir(base_dir)
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    (store / f"{ymd}.json").write_text(text, encoding="utf-8")
    (store / "latest.json").write_text(text, encoding="utf-8")
    return doc


def load_latest_agent_coach_notes(base_dir: Optional[Path] = None) -> Optional[dict[str, Any]]:
    store = base_dir or runtime_path("g3_ai_coach")
    return _read_json_safe(store / "latest.json")


def build_trading_coach_report(
    *,
    days: int = 14,
    market_gate: Optional[Mapping[str, Any]] = None,
    ths_dir: Optional[Path] = None,
    tracker_dir: Optional[Path] = None,
    desk_plan_dir: Optional[Path] = None,
    coach_dir: Optional[Path] = None,
    benchmark_fetcher=None,
    today_date: Optional[str] = None,
) -> dict[str, Any]:
    """汇总近 N 天真实成交、纪律评分、止损违约与水位状态，产出 Phase 3A 行为体检报告。"""
    now_bj = datetime.now(BJ_TZ)
    end_str = today_date or now_bj.strftime("%Y-%m-%d")

    attr = build_four_dim_attribution(
        days=days,
        ths_dir=ths_dir,
        tracker_dir=tracker_dir,
        desk_plan_dir=desk_plan_dir,
        benchmark_fetcher=benchmark_fetcher,
        today_date=end_str,
    )
    summary = attr["summary"]
    daily_rows = attr["daily_rows"]
    friction_events = attr["friction_events"]

    # 统计各类错误标签
    stop_ignored_by_code: dict[str, dict[str, Any]] = {}
    for ev in friction_events:
        if ev.get("type") == "contract_ignored":
            code = str(ev.get("code") or "")
            slot = stop_ignored_by_code.setdefault(
                code,
                {"code": code, "days": [], "max_est_drag": 0.0, "latest_label": ""},
            )
            slot["days"].append(ev.get("date"))
            drag = abs(float(ev.get("estimated_drag_pnl") or 0.0))
            if drag > slot["max_est_drag"]:
                slot["max_est_drag"] = drag
            if not slot["latest_label"]:
                slot["latest_label"] = str(ev.get("label") or "")

    # 读取 ledger 中额外的 knife_catch / off_plan 指标
    trk_root = tracker_dir or runtime_path("t_tracker")
    ledger_dir = trk_root / "ledger"
    knife_total = 0
    off_plan_total = 0
    if ledger_dir.exists():
        cutoff = attr["from_date"]
        for f in sorted(ledger_dir.glob("*_summary.json")):
            doc = _read_json_safe(f)
            if not doc:
                continue
            d = _norm_date(doc.get("date") or f.stem)
            if d < cutoff:
                continue
            off_plan_total += int(doc.get("off_plan_count") or 0)
            coaching = doc.get("coaching") if isinstance(doc.get("coaching"), Mapping) else {}
            knife_total += int(coaching.get("knife_catch_count") or 0)

    # 组装出血点 Top 清单
    bleeding_points: list[dict[str, Any]] = []
    for code, info in sorted(
        stop_ignored_by_code.items(),
        key=lambda kv: (-len(kv[1]["days"]), -kv[1]["max_est_drag"]),
    ):
        bleeding_points.append({
            "tag": "contract_ignored",
            "severity": "critical",
            "title": f"{code} 止损契约连续 {len(info['days'])} 日未执行",
            "detail": info["latest_label"],
            "impact_estimate": round(-info["max_est_drag"], 2) if info["max_est_drag"] > 0 else None,
            "dates": sorted(set(info["days"])),
        })

    if isinstance(market_gate, Mapping):
        wl = market_gate.get("r51_waterline") if isinstance(market_gate.get("r51_waterline"), Mapping) else {}
        if market_gate.get("regime") == "defensive" and wl.get("status") == "overweight":
            bleeding_points.append({
                "tag": "overweight_in_defense",
                "severity": "high",
                "title": f"防守期仓位严重超配（实际 {wl.get('actual_pct')}% vs 上限 {wl.get('target_cap_pct')}%）",
                "detail": str(wl.get("advice") or ""),
                "impact_estimate": None,
                "dates": [end_str],
            })

    if knife_total > 0:
        bleeding_points.append({
            "tag": "knife_catch",
            "severity": "high",
            "title": f"近 {days} 日出现 {knife_total} 次下降周期逆势抄底（接飞刀）",
            "detail": "下降周期优先反弹减仓与反T，避免急跌时盲目正T变补仓套牢。",
            "impact_estimate": None,
            "dates": [],
        })

    if off_plan_total > 0:
        bleeding_points.append({
            "tag": "off_plan_trade",
            "severity": "medium",
            "title": f"近 {days} 日存在 {off_plan_total} 笔计划外成交",
            "detail": "计划外交易往往由盘中情绪驱动，建议下单前先过 L2 风控预检。",
            "impact_estimate": None,
            "dates": [],
        })

    if (summary.get("t_trade_pnl") or 0) < 0 and (summary.get("t_round_trips") or 0) > 0:
        bleeding_points.append({
            "tag": "negative_t_pnl",
            "severity": "medium",
            "title": f"近 {days} 日做T配对 {summary['t_round_trips']} 轮净亏损 ¥{abs(summary['t_trade_pnl']):.2f}",
            "detail": "检查是否在弱势票或非价格带区域勉强做T；弱势期宁可不做也不硬做。",
            "impact_estimate": summary["t_trade_pnl"],
            "dates": [],
        })

    # 生成下周必须守住的 1~2 条行为红线
    red_lines: list[str] = []
    if stop_ignored_by_code:
        codes_txt = "、".join(sorted(stop_ignored_by_code.keys()))
        red_lines.append(f"【铁律1 · 止损零拖延】{codes_txt} 已跌破硬止损契约，下个交易日开盘/反抽必须按纪律执行，严禁继续扛单。")
    if isinstance(market_gate, Mapping) and market_gate.get("regime") == "defensive":
        wl = market_gate.get("r51_waterline") if isinstance(market_gate.get("r51_waterline"), Mapping) else {}
        if wl.get("status") == "overweight":
            red_lines.append(
                f"【铁律2 · 防守期降水位】大盘防守模式严禁开新主升仓；逢反弹卖带分步将水位向 ≤{wl.get('target_cap_pct')}% 靠拢（单次 ≤15%）。"
            )
    if not red_lines:
        red_lines.append("【铁律1 · 带位执行】所有加减仓与做T必须严格依托买带/卖带与 L2 风控预检，杜绝情绪追涨杀跌。")
        red_lines.append("【铁律2 · 日内归零】14:30 后严禁新开做T敞口，14:50 前确保当日 T 仓平仓不留隔夜加仓风险。")

    # 融合 Agent 深度点评（如有）
    agent_notes = load_latest_agent_coach_notes(base_dir=coach_dir)

    # 综合纪律评级
    avg_score = summary.get("avg_discipline_score")
    if avg_score is None:
        grade = "待积累"
    elif avg_score >= 90 and not stop_ignored_by_code:
        grade = "A · 纪律优良"
    elif avg_score >= 75:
        grade = "B · 存在执行漏点"
    else:
        grade = "C · 急需止血纠偏"

    return {
        "schema": "g3_trading_coach/1",
        "as_of": now_bj.isoformat(timespec="seconds"),
        "window_days": days,
        "from_date": attr["from_date"],
        "to_date": attr["to_date"],
        "grade": grade,
        "metrics": {
            "avg_discipline_score": avg_score,
            "violations_total": summary.get("violations_total", 0),
            "stop_ignored_codes": len(stop_ignored_by_code),
            "knife_catch_total": knife_total,
            "off_plan_total": off_plan_total,
            "unmatched_drafts_total": summary.get("unmatched_drafts_total", 0),
            "t_trade_pnl": summary.get("t_trade_pnl", 0.0),
            "estimated_stop_drag_pnl": summary.get("estimated_stop_drag_pnl", 0.0),
        },
        "bleeding_points": bleeding_points,
        "next_week_red_lines": (agent_notes.get("red_lines") if agent_notes and agent_notes.get("red_lines") else red_lines),
        "agent_notes": agent_notes,
        "recent_daily_discipline": [
            {
                "date": r["date"],
                "discipline_score": r["discipline_score"],
                "violations_total": r["violations_total"],
                "unmatched_drafts": r["unmatched_drafts"],
                "t_trade_pnl": r["t_trade_pnl"],
                "estimated_stop_drag": r["estimated_stop_drag"],
            }
            for r in daily_rows[:10]
        ],
    }

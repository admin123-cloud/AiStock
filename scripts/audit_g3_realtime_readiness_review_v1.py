from __future__ import annotations

import asyncio
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.gen3_state_alpha import PAPER_WATCH_REVIEWS_PATH, _broker_snapshot, get_gen3_state_alpha_current  # noqa: E402
from scripts.ptrade_bridge_readiness_audit import run_audit as run_ptrade_audit  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_realtime_readiness_review_v1")


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        x = float(value)
    except Exception:
        return default
    return x if math.isfinite(x) else default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "ok", "pass"}


def _date_text(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d")


def _pct(value: Any) -> str:
    x = _safe_float(value)
    return "--" if x is None else f"{x:.1%}"


def _money(value: Any) -> str:
    x = _safe_float(value)
    return "--" if x is None else f"{x:,.0f}"


def _md_table(df: pd.DataFrame, max_rows: int = 80) -> str:
    if df.empty:
        return "_无数据_"
    return df.head(max_rows).to_markdown(index=False)


def _risk_level(row: dict[str, Any]) -> str:
    blockers = int(row.get("blocker_count") or 0)
    warnings = int(row.get("warning_count") or 0)
    if blockers:
        return "block"
    if warnings >= 2:
        return "warn"
    if warnings:
        return "watch"
    return "pass"


def _route_label(row: dict[str, Any]) -> str:
    return str(row.get("trade_strategy_label") or row.get("route_label") or row.get("route") or "--")


def _text(value: Any, default: str = "--") -> str:
    text = str(value or "").strip()
    return text if text else default


def _join_unique(items: list[str]) -> str:
    return "；".join(dict.fromkeys([x for x in items if str(x or "").strip()]))


def _ticket_checklist_level(risks: list[str], review_action: str) -> str:
    if review_action in {"skip", "reject"}:
        return "block"
    if len(risks) >= 3:
        return "warn"
    if risks:
        return "watch"
    return "pass"


def _same_sector_pair_decision(rows: list[dict[str, Any]], row: dict[str, Any]) -> dict[str, Any]:
    sector = _text(row.get("sector_name") or row.get("l2_sector_name") or row.get("industry"), "")
    if not sector:
        return {
            "sector_pair_count": 1,
            "same_sector_codes": _text(row.get("code"), ""),
            "portfolio_decision_level": "pass",
            "portfolio_decision": "单票暴露",
            "portfolio_decision_reason": "未形成同板块双票暴露",
        }

    same_sector_rows = [
        item
        for item in rows or []
        if _text(item.get("sector_name") or item.get("l2_sector_name") or item.get("industry"), "") == sector
    ]
    codes = "、".join([_text(item.get("code"), "") for item in same_sector_rows if _text(item.get("code"), "")])
    if len(same_sector_rows) <= 1:
        return {
            "sector_pair_count": len(same_sector_rows),
            "same_sector_codes": codes or _text(row.get("code"), ""),
            "portfolio_decision_level": "pass",
            "portfolio_decision": "单票暴露",
            "portfolio_decision_reason": f"{sector} 当前仅 1 张下一交易日票据",
        }

    all_mainwave = all(str(item.get("route") or "") == "institutional_mainwave" for item in same_sector_rows)
    heat = _text(row.get("index_mom60_heat_state"), "")
    if all_mainwave and heat in {"high_heat_reduce_position", "high_heat_observe"}:
        return {
            "sector_pair_count": len(same_sector_rows),
            "same_sector_codes": codes,
            "portfolio_decision_level": "warn",
            "portfolio_decision": "允许主升共振，但按高热度观察复盘",
            "portfolio_decision_reason": f"{sector} 同板块 {len(same_sector_rows)} 张均为机构主升，符合 G3 二槽共振例外；但市场热度较高，正式放行前需确认不是拥挤追高。",
        }
    if all_mainwave:
        return {
            "sector_pair_count": len(same_sector_rows),
            "same_sector_codes": codes,
            "portfolio_decision_level": "watch",
            "portfolio_decision": "允许主升共振",
            "portfolio_decision_reason": f"{sector} 同板块 {len(same_sector_rows)} 张均为机构主升，符合 G3 同主线共振规则。",
        }
    return {
        "sector_pair_count": len(same_sector_rows),
        "same_sector_codes": codes,
        "portfolio_decision_level": "block",
        "portfolio_decision": "重复暴露应压缩到一张",
        "portfolio_decision_reason": f"{sector} 同板块 {len(same_sector_rows)} 张并非全部机构主升，不符合 G3 同板块共振例外。",
    }


def _build_ticket_review_checklist(rows: list[dict[str, Any]], ticket_review: pd.DataFrame) -> pd.DataFrame:
    review_by_code = {}
    if not ticket_review.empty and "code" in ticket_review.columns:
        review_by_code = {
            str(row.get("code") or "").strip(): row
            for row in ticket_review.to_dict("records")
            if str(row.get("code") or "").strip()
        }

    sectors: dict[str, int] = {}
    for row in rows or []:
        sector = _text(row.get("sector_name") or row.get("l2_sector_name") or row.get("industry"), "")
        if sector:
            sectors[sector] = sectors.get(sector, 0) + 1

    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows or [], start=1):
        code = _text(row.get("code"), "")
        review = review_by_code.get(code, {})
        route = str(row.get("route") or "")
        strategy = str(row.get("trade_strategy") or "")
        sector = _text(row.get("sector_name") or row.get("l2_sector_name") or row.get("industry"))
        heat = _text(row.get("index_mom60_heat_state"), "")
        natural_tag = _text(row.get("natural_context_tag"), "")
        natural_rules = _text(row.get("natural_rule_hits"), "")
        review_action = str(row.get("pretrade_review_action") or "").strip()
        entry_date = _date_text(row.get("entry_date") or row.get("planned_entry_ts"))
        source_entry_date = _date_text(row.get("source_entry_date") or row.get("source_planned_entry_ts"))
        pos = _safe_float(row.get("position_pct"))
        natural_pos = _safe_float(row.get("natural_position_pct"))
        ref = _safe_float(row.get("reference_close"))
        hard = _safe_float(row.get("hard_stop"))
        structure = _safe_float(row.get("structure_stop"))
        take = _safe_float(row.get("take_profit_1"))
        portfolio_decision = _same_sector_pair_decision(rows, row)
        risks: list[str] = []
        questions: list[str] = []

        if not review_action:
            risks.append("尚未人工逐票复盘")
            questions.append("是否只做纸面观察，还是人工放行进入下一交易日执行清单？")
        elif review_action == "paper_watch":
            risks.append("当前仅纸面观察，未正式放行")
            questions.append("观察结论是否足以升级为人工放行？")
        elif review_action == "wait_refresh":
            risks.append("等待刷新后再判断")
            questions.append("价格、持仓或信号刷新后，买点是否仍成立？")

        if heat in {"high_heat_reduce_position", "high_heat_observe"}:
            risks.append("市场热度较高，仅观察，不触发仓位减半")
            questions.append("指数/主线热度是否已经从动量变成拥挤？")
        elif heat and "block" in heat:
            risks.append(f"市场热度状态异常：{heat}")

        if sector != "--" and sectors.get(sector, 0) > 1:
            risks.append(f"下一交易日票据集中在同一板块：{sector}")
            if portfolio_decision["portfolio_decision_level"] == "block":
                risks.append(portfolio_decision["portfolio_decision_reason"])
            elif portfolio_decision["portfolio_decision_level"] in {"warn", "watch"}:
                risks.append(portfolio_decision["portfolio_decision"])
            questions.append("同板块双票是否属于机构主升共振，而不是单一板块过度暴露？")

        if natural_tag == "stock_leads_market_mainwave" or "N4" in natural_rules:
            risks.append("个股领先市场主升，需要人工确认不是脱离大盘的孤立强势")
            questions.append("个股强度是否有板块扩散或资金持续性支撑？")

        if source_entry_date and entry_date and source_entry_date != entry_date:
            risks.append(f"信号来源日 {source_entry_date} 顺延到入场日 {entry_date}")
            questions.append("顺延后是否仍保持 30m 结构和买点新鲜度？")

        if route == "g2_gap_supplement" or strategy == "volume_runup_supplement":
            risks.append("G2 空档补位只应填空槽，不能替代主路由")
            questions.append("当前是否确实存在 G3 主路由未占满槽位？")

        if ref is None or hard is None or take is None:
            risks.append("买卖点价格合同不完整")
        if pos is not None and natural_pos is not None and natural_pos < pos:
            risks.append(f"自然纪律建议仓位 {_pct(natural_pos)} 低于合同仓位 {_pct(pos)}")

        wave_score = _safe_float(row.get("wave_style_score"))
        diffusion_score = _safe_float(row.get("sector_diffusion_score"))
        buy_assessment = _join_unique(
            [
                f"{_route_label(row)}，{_text(row.get('route_strategy_family_label') or row.get('route_strategy_family'))}",
                "30m确认通过" if _truthy(row.get("m30_confirmed")) else "30m确认缺失",
                f"自然纪律：{_text(row.get('natural_action_label') or row.get('natural_action'))} / {natural_tag}",
                f"市场热度：{heat or '--'}，仓位缩放 {row.get('market_heat_position_scale') if row.get('market_heat_position_scale') is not None else '--'}",
            ]
        )
        exit_plan = _join_unique(
            [
                f"参考价 {ref:.2f}" if ref is not None else "参考价缺失",
                f"硬止损 {hard:.2f}" if hard is not None else "硬止损缺失",
                f"结构/前低保护 {structure:.2f}" if structure is not None else "结构保护缺失",
                f"第一止盈 {take:.2f}，卖出比例 {_pct(row.get('take_profit_1_sell_ratio'))}" if take is not None else "第一止盈缺失",
            ]
        )
        selection_pattern = _join_unique(
            [
                f"板块：{sector}",
                f"主升分 {wave_score:.2f}" if wave_score is not None else "",
                f"扩散分 {diffusion_score:.2f}" if diffusion_score is not None else "",
                f"策略：{_text(row.get('strategy_profile_name') or row.get('strategy_profile'))}",
            ]
        )
        model_switch = _join_unique(
            [
                f"当前路由：{_text(row.get('route_label') or route)}",
                f"父路由：{_text(row.get('route_parent_label') or row.get('route_parent'))}",
                "主路由进攻票" if route == "institutional_mainwave" else "补位/其他路线票",
                f"信号来源：{_text(row.get('source_strategy_label'))}",
            ]
        )
        approval_items = [
            "确认高热度仅作为观察项，不触发仓位减半",
            "确认同板块双票属于机构主升共振而非单一拥挤暴露",
            "确认顺延后 30m 结构和买点新鲜度仍成立",
            "确认真实持仓、可用现金和单槽仓位允许执行",
        ]
        if ref is not None and hard is not None and take is not None:
            approval_items.append("确认参考价、硬止损、第一止盈和剩余仓保护合同完整")
        else:
            approval_items.append("补齐参考价、硬止损、第一止盈和剩余仓保护合同")
        action = (
            "block：已跳过/否决，不进入执行"
            if review_action in {"skip", "reject"}
            else "仅纸面观察：人工放行并确认风险前不执行"
            if review_action != "manual_approved"
            else "待补风险确认：人工放行缺少 risk_acknowledged，不进入正式执行"
            if not _truthy(row.get("pretrade_review_risk_acknowledged"))
            else "可进入人工执行候选：仍需临盘价格与持仓检查"
        )
        risk_acknowledged = _truthy(row.get("pretrade_review_risk_acknowledged"))
        confirmation_complete = _truthy(row.get("pretrade_review_confirmation_complete"))
        missing_confirmation_items = row.get("pretrade_review_missing_confirmation_items")
        if isinstance(missing_confirmation_items, list):
            missing_confirmation_text = _join_unique([str(x) for x in missing_confirmation_items])
        else:
            missing_confirmation_text = str(missing_confirmation_items or "").strip()
        if review_action == "manual_approved" and not confirmation_complete:
            risks.append("人工放行缺少结构化确认项证据")
            questions.append("买点、热度、同板块、仓位、卖点和降级规则是否均已逐项确认？")
        formal_ready = review_action == "manual_approved" and risk_acknowledged and confirmation_complete
        formal_ready_reason = (
            "人工放行、风险确认和结构化确认项均已记录"
            if formal_ready
            else "已人工放行但缺少结构化确认项证据"
            if review_action == "manual_approved" and risk_acknowledged and not confirmation_complete
            else "已人工放行但缺少风险确认"
            if review_action == "manual_approved"
            else "纸面观察不等于正式就绪"
            if review_action == "paper_watch"
            else "等待刷新后再判断"
            if review_action == "wait_refresh"
            else "已跳过/否决"
            if review_action in {"skip", "reject"}
            else "尚未逐票复盘"
        )
        item = {
            "rank": idx,
            "code": row.get("code"),
            "name": row.get("name"),
            "entry_date": entry_date,
            "route": route,
            "strategy": _route_label(row),
            "sector": sector,
            "position_pct": pos,
            "review_action": review_action,
            "review_label": row.get("pretrade_review_label") or review.get("pretrade_review_label") or "未复盘",
            "review_risk_acknowledged": risk_acknowledged,
            "review_confirmation_complete": confirmation_complete,
            "review_missing_confirmation_items": missing_confirmation_text,
            "review_execution_posture": row.get("pretrade_review_execution_posture"),
            "review_decision_level": row.get("pretrade_review_decision_level"),
            "review_consistency_grade": row.get("pretrade_review_consistency_grade"),
            "review_consistency_score": row.get("pretrade_review_consistency_score"),
            "review_downgrade_rule": row.get("pretrade_review_downgrade_rule"),
            "review_decision_reason": row.get("pretrade_review_decision_reason"),
            "next_review_trigger": row.get("pretrade_review_next_review_trigger"),
            "formal_ready": formal_ready,
            "formal_ready_reason": formal_ready_reason,
            "buy_point_assessment": buy_assessment,
            "exit_plan": exit_plan,
            "selection_pattern": selection_pattern,
            "sector_pair_count": portfolio_decision["sector_pair_count"],
            "same_sector_codes": portfolio_decision["same_sector_codes"],
            "portfolio_decision_level": portfolio_decision["portfolio_decision_level"],
            "portfolio_decision": portfolio_decision["portfolio_decision"],
            "portfolio_decision_reason": portfolio_decision["portfolio_decision_reason"],
            "model_switch_assessment": model_switch,
            "manual_approval_checklist": _join_unique(approval_items),
            "hidden_risks": _join_unique(risks),
            "manual_questions": _join_unique(questions),
            "ticket_warnings": review.get("warnings"),
            "action_recommendation": action,
        }
        item["checklist_level"] = _ticket_checklist_level(risks, review_action)
        out.append(item)
    return pd.DataFrame(out)


def _ticket_checklist_markdown(checklist: pd.DataFrame) -> str:
    if checklist.empty:
        return "_暂无下一交易日票据复盘底稿_"
    parts: list[str] = []
    for _, row in checklist.iterrows():
        parts.extend(
            [
                f"### {row.get('rank')}. {row.get('code')} {row.get('name')}",
                "",
                f"- 复盘等级：`{row.get('checklist_level')}`；复盘状态：{row.get('review_label') or '--'}",
                f"- 正式就绪：{row.get('formal_ready')}；原因：{row.get('formal_ready_reason') or '--'}",
                f"- 买点判断：{row.get('buy_point_assessment') or '--'}",
                f"- 卖点合同：{row.get('exit_plan') or '--'}",
                f"- 选股模式：{row.get('selection_pattern') or '--'}",
                f"- 组合决策：`{row.get('portfolio_decision_level') or '--'}` {row.get('portfolio_decision') or '--'}；{row.get('portfolio_decision_reason') or '--'}",
                f"- 策略切换：{row.get('model_switch_assessment') or '--'}",
                f"- 人工放行确认：{row.get('manual_approval_checklist') or '--'}",
                f"- 隐藏风险：{row.get('hidden_risks') or '暂未发现新增隐患'}",
                f"- 人工问题：{row.get('manual_questions') or '无'}",
                f"- 动作建议：{row.get('action_recommendation') or '--'}",
                "",
            ]
        )
    return "\n".join(parts).strip()


def _holding_checklist_level(risks: list[str], action: str) -> str:
    if "卖" in action or "紧急" in action:
        return "block"
    if len(risks) >= 2:
        return "warn"
    if risks:
        return "watch"
    return "pass"


def _build_holding_exit_checklist(holding_review: pd.DataFrame) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    if holding_review.empty:
        return pd.DataFrame(out)
    for idx, row in enumerate(holding_review.to_dict("records"), start=1):
        source = str(row.get("source") or "")
        action = str(row.get("management_action") or "")
        warnings = str(row.get("warnings") or "").strip()
        blockers = str(row.get("blockers") or "").strip()
        pnl = _safe_float(row.get("pnl_ratio"))
        pos = _safe_float(row.get("position_pct"))
        risks: list[str] = []
        questions: list[str] = []

        if source == "real_account":
            risks.append("真实账户持仓，优先级高于新开仓票据")
        else:
            risks.append("影子持仓，仅用于策略复现和退出纪律校验")

        if warnings:
            risks.extend([part.strip() for part in warnings.split("；") if part.strip()])
        if blockers:
            risks.extend([part.strip() for part in blockers.split("；") if part.strip()])
        if "价格不新鲜" in warnings:
            questions.append("卖出/保护判断前是否已手动刷新最新价？")
        if "保护剩余仓" in action:
            questions.append("是否继续以前低/结构保护管理剩余仓，而不是因为新票挤占仓位被动卖出？")
        if "先刷新再判断" in action:
            questions.append("刷新后是否触发硬止损、第一止盈或结构保护？")
        if source == "real_account":
            questions.append("真实账户仓位占用是否允许继续开新仓？")

        status = _holding_checklist_level(risks, action)
        exit_state = _join_unique(
            [
                f"来源：{'真实账户' if source == 'real_account' else '影子台账'}",
                f"仓位：{_pct(pos)}",
                f"当前价：{_safe_float(row.get('current_price')):.2f}" if _safe_float(row.get("current_price")) is not None else "当前价缺失",
                f"浮盈浮亏：{_pct(pnl)}" if pnl is not None else "浮盈浮亏缺失",
            ]
        )
        item = {
            "rank": idx,
            "source": source,
            "code": row.get("code"),
            "name": row.get("name"),
            "entry_date": row.get("entry_date"),
            "checklist_level": status,
            "exit_state": exit_state,
            "management_action": action or "--",
            "exit_reason": row.get("exit_reason") or "--",
            "hidden_risks": _join_unique(risks),
            "manual_questions": _join_unique(questions),
            "action_recommendation": (
                "先处理退出动作，再允许新开仓"
                if status == "block"
                else "先刷新退出价格，再确认是否影响新开仓"
                if "价格不新鲜" in warnings or "先刷新再判断" in action
                else "按合同继续持有和观察"
            ),
        }
        out.append(item)
    return pd.DataFrame(out)


def _holding_checklist_markdown(checklist: pd.DataFrame) -> str:
    if checklist.empty:
        return "_暂无持仓退出复盘底稿_"
    parts: list[str] = []
    for _, row in checklist.iterrows():
        parts.extend(
            [
                f"### {row.get('rank')}. {row.get('code')} {row.get('name')}",
                "",
                f"- 复盘等级：`{row.get('checklist_level')}`；来源：{row.get('source') or '--'}",
                f"- 退出状态：{row.get('exit_state') or '--'}",
                f"- 管理动作：{row.get('management_action') or '--'}",
                f"- 退出原因：{row.get('exit_reason') or '--'}",
                f"- 隐藏风险：{row.get('hidden_risks') or '暂未发现新增隐患'}",
                f"- 人工问题：{row.get('manual_questions') or '无'}",
                f"- 动作建议：{row.get('action_recommendation') or '--'}",
                "",
            ]
        )
    return "\n".join(parts).strip()


def _build_holding_refresh_evidence(holding_review: pd.DataFrame, holding_checklist: pd.DataFrame) -> pd.DataFrame:
    if holding_review.empty and holding_checklist.empty:
        return pd.DataFrame([])
    review_by_key: dict[str, dict[str, Any]] = {}
    if not holding_review.empty:
        for row in holding_review.to_dict("records"):
            key = f"{row.get('source')}|{row.get('code')}"
            review_by_key[key] = row
    rows: list[dict[str, Any]] = []
    source_rows = holding_checklist.to_dict("records") if not holding_checklist.empty else holding_review.to_dict("records")
    for idx, row in enumerate(source_rows, start=1):
        source = str(row.get("source") or "")
        code = str(row.get("code") or "").strip()
        review = review_by_key.get(f"{source}|{code}", {})
        warnings = str(review.get("warnings") or row.get("hidden_risks") or "")
        stale = "价格不新鲜" in warnings or "先刷新" in str(row.get("action_recommendation") or "")
        formal_required = source == "real_account" and stale
        if formal_required:
            refresh_status = "formal_refresh_required"
            next_action = "先手动刷新真实账户持仓/最新价，再重跑实战前审计"
        elif stale:
            refresh_status = "observation_refresh"
            next_action = "影子持仓只进入观察复现，不阻断人工实盘放行"
        else:
            refresh_status = "fresh_enough"
            next_action = "按退出合同继续观察"
        rows.append(
            {
                "rank": idx,
                "source": source,
                "code": row.get("code"),
                "name": row.get("name"),
                "entry_date": row.get("entry_date") or review.get("entry_date"),
                "formal_trade_required": formal_required,
                "refresh_status": refresh_status,
                "current_price": review.get("current_price"),
                "position_pct": review.get("position_pct"),
                "pnl_ratio": review.get("pnl_ratio"),
                "management_action": row.get("management_action") or review.get("management_action"),
                "exit_reason": row.get("exit_reason") or review.get("exit_reason"),
                "staleness_evidence": warnings or "--",
                "pass_condition": "真实账户持仓价格新鲜，且退出动作仍为保护/继续持有或已完成卖出处理" if formal_required else "观察项已记录，不作为正式买入硬阻断",
                "fallback_if_fail": "保持 not_formal_ready，不开新仓" if formal_required else "保留影子观察，不影响真实账户执行入口",
                "next_action": next_action,
            }
        )
    return pd.DataFrame(rows)


def _candidate_checklist_level(risks: list[str], router_eligible: bool, selected: bool) -> str:
    if selected:
        return "pass"
    if router_eligible:
        return "warn"
    if risks:
        return "watch"
    return "pass"


def _build_candidate_omission_checklist(rows: list[dict[str, Any]], selected_rows: list[dict[str, Any]]) -> pd.DataFrame:
    selected_codes = {str(row.get("code") or "").strip() for row in selected_rows or [] if str(row.get("code") or "").strip()}
    selected_routes = {str(row.get("route") or "").strip() for row in selected_rows or [] if str(row.get("route") or "").strip()}
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows or [], start=1):
        code = str(row.get("code") or "").strip()
        selected = code in selected_codes
        route = str(row.get("route") or "")
        strategy = str(row.get("trade_strategy") or "")
        router_eligible = _truthy(row.get("router_eligible") or row.get("qualified_shadow_buy"))
        natural_tag = str(row.get("natural_context_tag") or "")
        block_reason = str(row.get("block_reason") or row.get("shadow_status") or "").strip()
        score = _safe_float(row.get("score"))
        wave_score = _safe_float(row.get("wave_style_score"))
        risks: list[str] = []
        questions: list[str] = []

        if selected:
            risks.append("已进入下一交易日票据，不属于遗漏")
        elif not router_eligible:
            risks.append(block_reason or "未通过路由准入")
            questions.append("阻断原因是否来自真实风险，还是盘中数据/确认链路缺口？")
        elif router_eligible:
            risks.append("候选已合格但未进入最终票据，需确认排序/槽位/板块约束")
            questions.append("是否存在被主路由或板块暴露规则误伤的可交易票？")

        if natural_tag in {"repair_against_downrisk", "range_repair", "weak_rebound_repair"} or route == "panic_repair":
            risks.append("修复类候选，不能在缺少盘中确认时替代主升票")
            questions.append("修复证据是否足以从观察池升级到补位/执行？")

        if route == "panic_repair" and "institutional_mainwave" in selected_routes:
            risks.append("当前最终票据以机构主升为主，修复候选仅作遗漏观察")
            questions.append("市场状态是否已经从主升转为修复，导致策略切换滞后？")

        if str(row.get("natural_action") or "") == "skip":
            risks.append("自然纪律跳过")

        score_text = f"候选分 {score:.4f}" if score is not None else "候选分缺失"
        if wave_score is not None:
            score_text += f"；主升分 {wave_score:.2f}"
        selection_state = _join_unique(
            [
                "已入选最终票据" if selected else "未入选最终票据",
                "路由准入通过" if router_eligible else "路由准入未通过",
                f"阻断/状态：{block_reason}" if block_reason else "",
                score_text,
            ]
        )
        strategy_switch = _join_unique(
            [
                f"候选路线：{_text(row.get('trade_strategy_label') or row.get('route_label') or route)}",
                f"路线类型：{_text(row.get('route_strategy_family_label') or row.get('route_strategy_family'))}",
                f"来源策略：{_text(row.get('source_strategy_label'))}",
                "与当前最终票据路线不同，属于策略切换观察" if selected_routes and route not in selected_routes else "",
            ]
        )
        action = (
            "已入选：按逐票复盘底稿处理"
            if selected
            else "保留为遗漏观察池：补齐盘中确认后再判断"
            if not router_eligible
            else "人工复核排序/槽位约束，确认是否被误排除"
        )
        item = {
            "rank": idx,
            "code": row.get("code"),
            "name": row.get("name"),
            "entry_date": _date_text(row.get("entry_date") or row.get("trade_date")),
            "route": route,
            "strategy": _route_label(row),
            "selected_next_trade": selected,
            "router_eligible": router_eligible,
            "natural_action": row.get("natural_action"),
            "natural_context_tag": natural_tag,
            "selection_state": selection_state,
            "strategy_switch_assessment": strategy_switch,
            "hidden_risks": _join_unique(risks),
            "manual_questions": _join_unique(questions),
            "action_recommendation": action,
        }
        item["checklist_level"] = _candidate_checklist_level(risks, router_eligible, selected)
        out.append(item)
    return pd.DataFrame(out)


def _build_natural_trade_consistency_review(
    ticket_checklist: pd.DataFrame,
    candidate_checklist: pd.DataFrame,
    holding_refresh_evidence: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if ticket_checklist.empty:
        return pd.DataFrame(rows)

    selected_candidate_by_code: dict[str, dict[str, Any]] = {}
    if not candidate_checklist.empty and "code" in candidate_checklist.columns:
        for item in candidate_checklist.to_dict("records"):
            code = str(item.get("code") or "").strip()
            if code and _truthy(item.get("selected_next_trade")):
                selected_candidate_by_code[code] = item

    formal_refresh_pending = 0
    if not holding_refresh_evidence.empty and "formal_trade_required" in holding_refresh_evidence.columns:
        formal_refresh_pending = int(holding_refresh_evidence["formal_trade_required"].map(_truthy).sum())

    for row in ticket_checklist.to_dict("records"):
        code = str(row.get("code") or "").strip()
        candidate = selected_candidate_by_code.get(code, {})
        logic_deductions: list[tuple[int, str]] = []
        readiness_deductions: list[tuple[int, str]] = []
        strengths: list[str] = []

        checklist_level = str(row.get("checklist_level") or "")
        portfolio_level = str(row.get("portfolio_decision_level") or "")
        review_action = str(row.get("review_action") or "").strip()
        route = str(row.get("route") or "")
        natural_action = str(candidate.get("natural_action") or "")
        natural_tag = str(candidate.get("natural_context_tag") or "")

        if checklist_level == "pass":
            strengths.append("逐票底稿无明显阻断")

        if portfolio_level == "block":
            logic_deductions.append((40, "组合暴露规则阻断"))
        elif portfolio_level == "warn":
            logic_deductions.append((15, "组合暴露需要降温确认"))
        elif portfolio_level == "watch":
            logic_deductions.append((8, "组合暴露可做但需关注"))
        else:
            strengths.append("组合暴露未触发额外压缩")

        if review_action != "manual_approved":
            readiness_deductions.append((18, "尚未人工放行"))
        elif not _truthy(row.get("review_risk_acknowledged")):
            readiness_deductions.append((16, "人工放行缺少风险确认"))
        else:
            strengths.append("人工复盘与风险确认已闭环")

        hidden_risks = str(row.get("hidden_risks") or "")
        buy_assessment = str(row.get("buy_point_assessment") or "")
        exit_plan = str(row.get("exit_plan") or "")
        selection_pattern = str(row.get("selection_pattern") or "")
        model_switch = str(row.get("model_switch_assessment") or "")

        if "市场热度较高" in hidden_risks or "high_heat_reduce_position" in buy_assessment or "high_heat_observe" in buy_assessment:
            logic_deductions.append((10, "热度偏高，买点需要避免追涨语义"))
        if "同一板块" in hidden_risks:
            logic_deductions.append((8, "双票集中于同一板块"))
        if "顺延" in hidden_risks:
            logic_deductions.append((10, "信号顺延，需要确认买点新鲜度"))
        if "价格合同不完整" in hidden_risks or "缺失" in exit_plan:
            logic_deductions.append((18, "卖点/价格合同不完整"))
        if "自然纪律建议仓位" in hidden_risks:
            logic_deductions.append((12, "自然纪律仓位低于合同仓位"))
        if route == "g2_gap_supplement":
            logic_deductions.append((8, "G2 补位只能补槽，不能替代主路由"))
        if natural_action == "skip":
            logic_deductions.append((25, "自然纪律已给出跳过"))
        if formal_refresh_pending:
            readiness_deductions.append((8, "正式持仓价格刷新未完成"))

        if route == "institutional_mainwave":
            strengths.append("路线属于 G3 主路由机构主升")
        if "30m确认通过" in buy_assessment:
            strengths.append("30m 结构确认通过")
        if "主升分" in selection_pattern:
            strengths.append("选股证据包含主升分")
        if "允许主升共振" in str(row.get("portfolio_decision") or ""):
            strengths.append("同板块暴露符合机构主升共振例外")

        score = max(0, 100 - sum(points for points, _ in logic_deductions))
        readiness_score = max(0, 100 - sum(points for points, _ in readiness_deductions))
        if score >= 82:
            grade = "smooth"
            action = "逻辑顺畅：可进入人工放行复核；仍需遵守持仓刷新和执行入口矩阵"
        elif score >= 65:
            grade = "watch"
            action = "基本可解释：先补齐复盘/刷新证据，再判断是否放行"
        elif score >= 45:
            grade = "strained"
            action = "交易语义偏拧：优先复核买点新鲜度、组合暴露或策略切换"
        else:
            grade = "incoherent"
            action = "逻辑不顺：不宜为了收益曲线强行放行"

        rows.append(
            {
                "rank": row.get("rank"),
                "code": row.get("code"),
                "name": row.get("name"),
                "route": route,
                "strategy": row.get("strategy"),
                "consistency_score": score,
                "consistency_grade": grade,
                "operational_readiness_score": readiness_score,
                "buy_point_logic": buy_assessment,
                "sell_point_logic": exit_plan,
                "selection_logic": selection_pattern,
                "model_switch_logic": model_switch,
                "natural_context": _join_unique([natural_action, natural_tag]),
                "strengths": _join_unique(strengths),
                "deductions": _join_unique([f"-{points} {reason}" for points, reason in logic_deductions]),
                "readiness_gaps": _join_unique([f"-{points} {reason}" for points, reason in readiness_deductions]),
                "recommended_action": action,
            }
        )

    return pd.DataFrame(rows)


def _candidate_checklist_markdown(checklist: pd.DataFrame) -> str:
    if checklist.empty:
        return "_暂无候选遗漏复盘底稿_"
    parts: list[str] = []
    for _, row in checklist.iterrows():
        parts.extend(
            [
                f"### {row.get('rank')}. {row.get('code')} {row.get('name')}",
                "",
                f"- 复盘等级：`{row.get('checklist_level')}`；入选状态：{'已入选' if _truthy(row.get('selected_next_trade')) else '未入选'}",
                f"- 选股状态：{row.get('selection_state') or '--'}",
                f"- 策略切换：{row.get('strategy_switch_assessment') or '--'}",
                f"- 隐藏风险：{row.get('hidden_risks') or '暂未发现新增隐患'}",
                f"- 人工问题：{row.get('manual_questions') or '无'}",
                f"- 动作建议：{row.get('action_recommendation') or '--'}",
                "",
            ]
        )
    return "\n".join(parts).strip()


def _build_natural_execution_decision_matrix(natural_consistency: pd.DataFrame, ticket_checklist: pd.DataFrame) -> pd.DataFrame:
    if natural_consistency.empty:
        return pd.DataFrame([])

    ticket_by_code: dict[str, dict[str, Any]] = {}
    if not ticket_checklist.empty and "code" in ticket_checklist.columns:
        ticket_by_code = {
            str(row.get("code") or "").strip(): row
            for row in ticket_checklist.to_dict("records")
            if str(row.get("code") or "").strip()
        }

    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(natural_consistency.to_dict("records"), start=1):
        code = str(row.get("code") or "").strip()
        ticket = ticket_by_code.get(code, {})
        grade = str(row.get("consistency_grade") or "")
        score = int(_safe_float(row.get("consistency_score"), 0) or 0)
        formal_ready = _truthy(ticket.get("formal_ready"))
        portfolio_decision = str(ticket.get("portfolio_decision") or "")
        deductions = str(row.get("deductions") or "")
        readiness_gaps = str(row.get("readiness_gaps") or "")

        if grade == "incoherent" or score < 45:
            posture = "paper_or_skip_only"
            manual_live_allowed = False
            decision_level = "block"
            required_confirmation = "交易逻辑不顺，禁止为了收益曲线强行放行；只能纸面观察或跳过。"
            downgrade_rule = "若盘前刷新后仍低于 45 分，直接跳过；若恢复到 watch 以上再重新逐票复盘。"
        elif grade == "strained":
            posture = "manual_review_required"
            manual_live_allowed = formal_ready
            decision_level = "warn" if not formal_ready else "watch"
            required_confirmation = "必须人工确认买点新鲜度、热度拥挤、同板块共振与仓位缩放；未确认前不进正式实盘。"
            downgrade_rule = "若开盘出现高开追涨、板块扩散不足或 30m 结构变弱，降级为纸面观察/跳过。"
        elif grade == "watch":
            posture = "paper_watch_or_manual_small"
            manual_live_allowed = formal_ready
            decision_level = "watch"
            required_confirmation = "允许小心人工复核；必须确认卖点合同和真实账户仓位仍匹配。"
            downgrade_rule = "若真实持仓刷新或盘口价格不支持，先纸面观察。"
        else:
            posture = "manual_live_candidate"
            manual_live_allowed = formal_ready
            decision_level = "pass" if formal_ready else "watch"
            required_confirmation = "逻辑顺畅，但仍需完成逐票人工放行、风险确认和真实持仓刷新。"
            downgrade_rule = "若执行入口矩阵未 ready，保持 dry-run 或纸面观察。"

        if "允许主升共振" in portfolio_decision and "热度偏高" in deductions:
            required_confirmation += " 当前属于高热度主升共振，仅作为观察项，不追加额外仓位。"
        if readiness_gaps:
            required_confirmation += f" 就绪缺口：{readiness_gaps}"

        rows.append(
            {
                "priority": idx,
                "code": row.get("code"),
                "name": row.get("name"),
                "strategy": row.get("strategy"),
                "consistency_score": score,
                "consistency_grade": grade,
                "decision_level": decision_level,
                "execution_posture": posture,
                "manual_live_allowed_after_review": manual_live_allowed,
                "required_confirmation": required_confirmation,
                "downgrade_rule": downgrade_rule,
                "source_deductions": deductions or "--",
            }
        )

    return pd.DataFrame(rows)


def _build_pretrade_action_checklist(
    gates: pd.DataFrame,
    ticket_checklist: pd.DataFrame,
    holding_checklist: pd.DataFrame,
    candidate_checklist: pd.DataFrame,
    natural_execution_matrix: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(
        phase: str,
        severity: str,
        obj: str,
        action: str,
        done: str,
        source: str,
        formal_required: bool = True,
    ) -> None:
        rows.append(
            {
                "priority": len(rows) + 1,
                "phase": phase,
                "severity": severity or "watch",
                "object": obj or "--",
                "action": action or "--",
                "done_condition": done or "--",
                "source": source,
                "formal_trade_required": formal_required,
                "action_type": "formal_required" if formal_required else "observation",
                "status": "待处理" if severity in {"block", "warn", "watch"} else "已通过",
            }
        )

    if not gates.empty:
        for row in gates[~gates["ok"]].to_dict("records"):
            status = str(row.get("status") or row.get("severity") or "warn")
            formal_required = True
            if str(row.get("gate") or "") == "holding_exit_price_fresh" and not holding_checklist.empty:
                real_holding_attention = holding_checklist[
                    (holding_checklist.get("source", pd.Series(dtype=str)).astype(str) == "real_account")
                    & holding_checklist.get("checklist_level", pd.Series(dtype=str)).isin(["block", "warn", "watch"])
                ]
                formal_required = not real_holding_attention.empty
            add(
                "1_Gate",
                "block" if status == "block" else "warn",
                str(row.get("gate") or "--"),
                str(row.get("message") or "处理实战 Gate 异常"),
                "该 Gate 重新审计为 pass/ok",
                "readiness_gates",
                formal_required=formal_required,
            )

    if not holding_checklist.empty:
        subset = holding_checklist[holding_checklist["checklist_level"].isin(["block", "warn", "watch"])]
        for row in subset.to_dict("records"):
            source = str(row.get("source") or "")
            add(
                "2_持仓退出",
                str(row.get("checklist_level") or "watch"),
                f"{row.get('code')} {row.get('name')}",
                str(row.get("action_recommendation") or "先完成持仓退出确认"),
                str(row.get("manual_questions") or "刷新退出价格并确认不影响新开仓"),
                "holding_exit_checklist",
                formal_required=source == "real_account",
            )

    if not ticket_checklist.empty:
        subset = ticket_checklist[ticket_checklist["checklist_level"].isin(["block", "warn", "watch"])]
        for row in subset.to_dict("records"):
            add(
                "3_逐票买入",
                str(row.get("checklist_level") or "watch"),
                f"{row.get('code')} {row.get('name')}",
                str(row.get("action_recommendation") or "逐票人工复盘"),
                str(row.get("manual_questions") or "记录纸面观察/人工放行/跳过"),
                "ticket_review_checklist",
            )

    if not candidate_checklist.empty:
        subset = candidate_checklist[candidate_checklist["checklist_level"].isin(["block", "warn", "watch"])]
        for row in subset.to_dict("records"):
            add(
                "4_候选遗漏",
                str(row.get("checklist_level") or "watch"),
                f"{row.get('code')} {row.get('name')}",
                str(row.get("action_recommendation") or "保留为遗漏观察池"),
                str(row.get("manual_questions") or "确认是否存在策略切换滞后或数据确认缺口"),
                "candidate_omission_checklist",
                formal_required=False,
            )

    if natural_execution_matrix is not None and not natural_execution_matrix.empty:
        subset = natural_execution_matrix[natural_execution_matrix["decision_level"].isin(["block", "warn", "watch"])]
        for row in subset.to_dict("records"):
            level = str(row.get("decision_level") or "watch")
            add(
                "5_自然交易一致性",
                level,
                f"{row.get('code')} {row.get('name')}",
                str(row.get("required_confirmation") or "按自然交易一致性矩阵完成人工确认"),
                str(row.get("downgrade_rule") or "若交易语义不能解释清楚，则降级为纸面观察或跳过"),
                "natural_execution_decision_matrix",
                formal_required=level == "block",
            )

    if not rows:
        add("0_完成", "pass", "G3 实战前审计", "无待处理动作", "所有 Gate/票据/持仓/候选底稿均无关注项", "summary", formal_required=False)
    return pd.DataFrame(rows)


def _build_formal_launch_checklist(summary: dict[str, Any]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(item: str, ok: bool, severity: str, evidence: str, required_action: str) -> None:
        rows.append(
            {
                "priority": len(rows) + 1,
                "item": item,
                "status": "pass" if ok else severity,
                "ok": bool(ok),
                "evidence": evidence,
                "required_action": required_action if not ok else "已满足",
            }
        )

    hard_blockers = int(summary.get("gate_block_count") or 0) + int(summary.get("ticket_block_count") or 0) + int(summary.get("holding_block_count") or 0)
    ticket_count = int(summary.get("next_trade_ticket_count") or 0)
    formal_ready = int(summary.get("pretrade_review_formal_ready_count") or 0)
    formal_pending = int(summary.get("pretrade_formal_action_pending_count") or 0)
    portfolio_blocks = int(summary.get("portfolio_decision_block_count") or 0)
    natural_blocks = int(summary.get("natural_execution_block_count") or 0)

    add(
        "hard_blockers_clear",
        hard_blockers == 0,
        "block",
        f"hard_blockers={hard_blockers}",
        "先处理 Gate、票据或持仓硬阻断",
    )
    add(
        "has_next_trade_tickets",
        ticket_count > 0,
        "block",
        f"next_trade_ticket_count={ticket_count}",
        "没有下一交易日票据时不进入实盘买入流程",
    )
    add(
        "all_tickets_formally_reviewed",
        ticket_count > 0 and formal_ready == ticket_count,
        "warn",
        f"formal_ready={formal_ready}, ticket_count={ticket_count}",
        "逐票点击人工放行并确认风险，纸面观察不等于正式就绪",
    )
    add(
        "formal_required_actions_clear",
        formal_pending == 0,
        "warn",
        f"pretrade_formal_action_pending_count={formal_pending}",
        "完成真实持仓退出刷新、逐票复盘和正式 Gate 处理",
    )
    add(
        "portfolio_no_block",
        portfolio_blocks == 0,
        "block",
        f"portfolio_decision_block_count={portfolio_blocks}",
        "组合层出现阻断时，压缩仓位或改为纸面观察",
    )
    add(
        "natural_execution_no_block",
        natural_blocks == 0,
        "block",
        f"natural_execution_block_count={natural_blocks}",
        "自然交易一致性出现 incoherent（不顺）时，禁止正式实盘，只能纸面观察、跳过或等待刷新后重审",
    )
    add(
        "auto_order_still_locked",
        True,
        "watch",
        "正式自动下单仍锁定",
        "如需自动下单，必须另行开启并重新审计",
    )
    return pd.DataFrame(rows)


def _build_execution_mode_matrix(
    summary: dict[str, Any],
    current: dict[str, Any],
    broker: dict[str, Any],
    ptrade: dict[str, Any],
    gates: pd.DataFrame,
) -> pd.DataFrame:
    capital = broker.get("capital") if isinstance(broker.get("capital"), dict) else {}
    ptrade_gates = ptrade.get("gates") if isinstance(ptrade.get("gates"), dict) else {}
    gate_map = {}
    if not gates.empty and "gate" in gates.columns:
        gate_map = {str(row.get("gate") or ""): row for row in gates.to_dict("records")}

    ticket_count = int(summary.get("next_trade_ticket_count") or 0)
    formal_ready = bool(summary.get("formal_launch_ready"))
    formal_pending = int(summary.get("pretrade_formal_action_pending_count") or 0)
    has_capital = _safe_float(capital.get("total_capital")) is not None and _safe_float(capital.get("available_cash")) is not None
    holdings_fresh = not bool(capital.get("holdings_stale"))
    holding_price_fresh = bool(gate_map.get("holding_exit_price_fresh", {}).get("ok"))
    order_locked = (
        current.get("formal_buy_signal") is False
        and current.get("auto_order_allowed") is False
        and current.get("order_path_enabled") is False
    )
    dry_run_ready = bool(ptrade_gates.get("dry_run_ready") or ptrade_gates.get("local_submit_ready") or ptrade_gates.get("dry_run_probe_ready"))
    live_submit_ready = bool(ptrade_gates.get("live_submit_ready"))

    rows: list[dict[str, Any]] = []

    def add(mode: str, status: str, allowed: bool, evidence: str, required_before_use: str, failure_mode: str) -> None:
        rows.append(
            {
                "priority": len(rows) + 1,
                "mode": mode,
                "status": status,
                "allowed_now": allowed,
                "evidence": evidence,
                "required_before_use": required_before_use,
                "failure_mode": failure_mode,
            }
        )

    add(
        "paper_watch_review",
        "ready" if ticket_count > 0 else "blocked",
        ticket_count > 0,
        f"ticket_count={ticket_count}, formal_launch_status={summary.get('formal_launch_status')}",
        "可纸面复盘，但不能把纸面观察当作正式买入",
        "没有票据时只保留观察，不生成买入动作",
    )
    add(
        "manual_live_ths",
        "ready" if formal_ready and has_capital and holdings_fresh and holding_price_fresh else "not_ready",
        formal_ready and has_capital and holdings_fresh and holding_price_fresh,
        f"formal_ready={formal_ready}, formal_pending={formal_pending}, capital={has_capital}, holdings_fresh={holdings_fresh}, holding_price_fresh={holding_price_fresh}",
        "完成真实持仓价格刷新、逐票人工放行、风险确认、临盘价格/现金/仓位复核",
        "任一条件未满足时只允许纸面观察或等待刷新",
    )
    add(
        "ptrade_dry_run",
        "ready" if dry_run_ready and ticket_count > 0 else "not_ready",
        dry_run_ready and ticket_count > 0,
        f"dry_run_ready={dry_run_ready}, ticket_count={ticket_count}",
        "仅用于文件桥/下单链路演练，不代表可真实成交",
        "dry-run 失败时先修 PTrade 桥，不改策略买点",
    )
    add(
        "ptrade_live_auto",
        "locked" if order_locked and not live_submit_ready else "requires_reaudit",
        False,
        f"formal_buy_signal={current.get('formal_buy_signal')}, auto_order_allowed={current.get('auto_order_allowed')}, order_path_enabled={current.get('order_path_enabled')}, live_submit_ready={live_submit_ready}",
        "当前合同要求自动实盘保持锁定；如需开启，必须单独授权并重跑全链路审计",
        "不能把 dry-run 可用误判为自动实盘可用",
    )
    return pd.DataFrame(rows)


def _build_formal_launch_action_queue(
    action_checklist: pd.DataFrame,
    ticket_checklist: pd.DataFrame,
    formal_launch_checklist: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(stage: str, obj: str, action: str, unlocks: str, source: str, severity: str = "warn") -> None:
        rows.append(
            {
                "priority": len(rows) + 1,
                "stage": stage,
                "severity": severity,
                "object": obj or "--",
                "action": action or "--",
                "unlocks": unlocks or "--",
                "source": source,
            }
        )

    if not action_checklist.empty:
        formal_pending = action_checklist[
            action_checklist.get("status", pd.Series(dtype=str)).astype(str).eq("待处理")
            & action_checklist.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy)
            & (action_checklist.get("source", pd.Series(dtype=str)).astype(str) != "ticket_review_checklist")
        ]
        for row in formal_pending.to_dict("records"):
            phase = str(row.get("phase") or "")
            stage = "1_真实持仓/价格刷新" if "持仓" in phase or str(row.get("object") or "") == "holding_exit_price_fresh" else "2_逐票人工复盘"
            add(
                stage,
                str(row.get("object") or "--"),
                str(row.get("action") or "--"),
                "清理正式必处理动作，推进 formal_required_actions_clear",
                str(row.get("source") or "pretrade_action_checklist"),
                str(row.get("severity") or "warn"),
            )

    if not ticket_checklist.empty:
        if "formal_ready" in ticket_checklist.columns:
            formal_ready_mask = ticket_checklist["formal_ready"].map(_truthy)
        else:
            review_action = ticket_checklist.get("review_action", pd.Series(dtype=str)).fillna("").astype(str).str.strip()
            risk_ack = ticket_checklist.get("review_risk_acknowledged", pd.Series(dtype=bool)).map(_truthy)
            formal_ready_mask = (review_action == "manual_approved") & risk_ack
        pending_tickets = ticket_checklist[~formal_ready_mask]
        for row in pending_tickets.to_dict("records"):
            add(
                "2_逐票人工复盘",
                f"{row.get('code')} {row.get('name')}",
                f"{row.get('formal_ready_reason') or '选择纸面观察、待刷新、跳过或人工放行'}；人工放行必须同时确认高热度、同板块共振、买点新鲜度和风险勾选",
                "推进 all_tickets_formally_reviewed；若只纸面观察则保持 not_formal_ready",
                "ticket_review_checklist",
                str(row.get("checklist_level") or "watch"),
            )

    if not formal_launch_checklist.empty:
        failed = formal_launch_checklist[formal_launch_checklist.get("status", pd.Series(dtype=str)).astype(str) != "pass"]
        if not failed.empty:
            add(
                "3_重跑审计",
                "G3 实战前审计",
                "完成前置动作后点击重跑实战前审计",
                "刷新 formal_launch_status，确认是否进入 manual_live_ready",
                "formal_launch_checklist",
                "watch",
            )

    if not rows:
        add("0_完成", "G3 正式放行", "无待处理动作", "manual_live_ready 可进入人工实盘执行确认", "summary", "pass")
    return pd.DataFrame(rows)


def _build_pretrade_review_evidence(ticket_review: pd.DataFrame, ticket_checklist: pd.DataFrame) -> pd.DataFrame:
    if ticket_review.empty and ticket_checklist.empty:
        return pd.DataFrame([])
    review_by_code = {}
    if not ticket_review.empty and "code" in ticket_review.columns:
        review_by_code = {
            str(row.get("code") or "").strip(): row
            for row in ticket_review.to_dict("records")
            if str(row.get("code") or "").strip()
        }
    rows: list[dict[str, Any]] = []
    source_rows = ticket_checklist.to_dict("records") if not ticket_checklist.empty else ticket_review.to_dict("records")
    for idx, row in enumerate(source_rows, start=1):
        code = str(row.get("code") or "").strip()
        review = review_by_code.get(code, {})
        review_action = str(row.get("review_action") or review.get("pretrade_review_action") or "").strip()
        risk_ack = _truthy(row.get("review_risk_acknowledged") if "review_risk_acknowledged" in row else review.get("pretrade_review_risk_acknowledged"))
        confirmation_complete = _truthy(row.get("review_confirmation_complete") if "review_confirmation_complete" in row else review.get("pretrade_review_confirmation_complete"))
        formal_ready = review_action == "manual_approved" and risk_ack and confirmation_complete
        if formal_ready:
            status = "formal_ready"
            next_action = "盘前临盘复核价格、仓位、止损、止盈后才允许人工执行"
        elif review_action == "manual_approved" and risk_ack:
            status = "missing_confirmation_evidence"
            next_action = "补齐结构化确认项证据；否则保持 not_formal_ready"
        elif review_action == "manual_approved":
            status = "missing_risk_ack"
            next_action = "补做风险确认；否则保持 not_formal_ready"
        elif review_action == "paper_watch":
            status = "paper_watch"
            next_action = "继续纸面观察，不能进入正式买入"
        elif review_action == "wait_refresh":
            status = "wait_refresh"
            next_action = "刷新价格、持仓和信号后重跑审计"
        elif review_action in {"skip", "reject"}:
            status = "skip_or_reject"
            next_action = "不进入本次执行"
        else:
            status = "unreviewed"
            next_action = "逐票记录纸面观察、待刷新、跳过或人工放行"
        rows.append(
            {
                "rank": idx,
                "code": row.get("code"),
                "name": row.get("name"),
                "entry_date": row.get("entry_date") or review.get("entry_date"),
                "review_action": review_action,
                "review_label": row.get("review_label") or review.get("pretrade_review_label") or "未复盘",
                "risk_acknowledged": risk_ack,
                "confirmation_complete": confirmation_complete,
                "missing_confirmation_items": row.get("review_missing_confirmation_items"),
                "formal_ready": formal_ready,
                "formal_ready_reason": row.get("formal_ready_reason") or status,
                "review_decision_reason": row.get("review_decision_reason") or review.get("pretrade_review_decision_reason"),
                "next_review_trigger": row.get("next_review_trigger") or review.get("pretrade_review_next_review_trigger"),
                "portfolio_decision_level": row.get("portfolio_decision_level"),
                "portfolio_decision": row.get("portfolio_decision"),
                "manual_approval_checklist": row.get("manual_approval_checklist"),
                "next_action": next_action,
                "ticket_key": review.get("ticket_key") or row.get("ticket_key"),
            }
        )
    return pd.DataFrame(rows)


def _read_paper_watch_review_map() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(PAPER_WATCH_REVIEWS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = data.get("reviews") if isinstance(data, dict) else {}
    if not isinstance(rows, dict):
        return {}
    return {str(key): value for key, value in rows.items() if isinstance(value, dict)}


def _ticket_key_from_row(row: dict[str, Any]) -> str:
    for key in ("ticket_key", "candidate_key", "trade_key"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "|".join(
        [
            "g3_state_alpha",
            str(row.get("entry_date") or "").strip(),
            str(row.get("route") or "").strip(),
            str(row.get("code") or "").strip(),
        ]
    )


def _build_paper_watch_followup(ticket_review: pd.DataFrame, pretrade_review_evidence: pd.DataFrame) -> pd.DataFrame:
    if ticket_review.empty and pretrade_review_evidence.empty:
        return pd.DataFrame([])
    review_map = _read_paper_watch_review_map()
    source = pretrade_review_evidence if not pretrade_review_evidence.empty else ticket_review
    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(source.to_dict("records"), start=1):
        review_action = str(row.get("review_action") or row.get("pretrade_review_action") or "").strip()
        if review_action != "paper_watch":
            continue
        key = _ticket_key_from_row(row)
        review = review_map.get(key, {})
        watch_result = str(review.get("watch_result") or "").strip()
        issue_area = str(review.get("issue_area") or "").strip()
        if not watch_result:
            followup_status = "pending_observation"
            next_action = "盘后或次日补充观察结果，把问题归因到买点、选股、策略切换或卖点合同"
        elif watch_result == "as_expected":
            followup_status = "validated"
            next_action = "保留当前逻辑，继续观察下一次同类信号是否稳定复现"
        elif watch_result == "continue_watch":
            followup_status = "continue_watch"
            next_action = "延长观察，不做收益化优化；等待更完整的走势证据"
        else:
            followup_status = "issue_found"
            next_action = "进入策略调优队列：先解释行为问题，再决定是否调整参数或规则"
        rows.append(
            {
                "rank": idx,
                "code": row.get("code") or review.get("code"),
                "name": row.get("name") or review.get("name"),
                "entry_date": row.get("entry_date") or review.get("entry_date"),
                "ticket_key": key,
                "pretrade_reason": row.get("review_decision_reason") or row.get("pretrade_review_decision_reason"),
                "watch_result": watch_result,
                "watch_result_label": review.get("watch_result_label") or ("待观察" if not watch_result else watch_result),
                "followup_status": followup_status,
                "issue_area": issue_area or "observation",
                "observed_date": review.get("observed_date"),
                "observed_price": review.get("observed_price"),
                "max_gain_pct": review.get("max_gain_pct"),
                "max_drawdown_pct": review.get("max_drawdown_pct"),
                "naturalness_score": review.get("naturalness_score"),
                "hidden_risk": review.get("hidden_risk"),
                "optimization_suggestion": review.get("optimization_suggestion"),
                "review_note": review.get("review_note"),
                "updated_at": review.get("updated_at"),
                "next_action": next_action,
            }
        )
    return pd.DataFrame(rows)


def _build_premarket_execution_playbook(
    summary: dict[str, Any],
    formal_launch_action_queue: pd.DataFrame,
    ticket_checklist: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(window: str, step: str, action: str, pass_condition: str, fallback: str, source: str) -> None:
        rows.append(
            {
                "priority": len(rows) + 1,
                "window": window,
                "step": step,
                "action": action,
                "pass_condition": pass_condition,
                "fallback_if_fail": fallback,
                "source": source,
            }
        )

    add(
        "盘前",
        "确认正式放行状态",
        f"查看 formal_launch_status={summary.get('formal_launch_status') or '--'}，缺口={summary.get('formal_launch_missing_count')}",
        "formal_launch_status 进入 manual_live_ready，或明确只做纸面观察",
        "保持 not_formal_ready，不进入真实买入",
        "summary",
    )
    add(
        "盘前",
        "区分正式动作与观察动作",
        f"正式必处理={summary.get('pretrade_formal_action_pending_count', '--')}，观察项={summary.get('pretrade_observation_action_pending_count', '--')}",
        "真实持仓、逐票放行、Gate 完成后再判断；影子持仓和候选遗漏只进入观察清单",
        "若正式动作未清零，保持人工复盘/纸面观察，不因历史收益压力强行放行",
        "pretrade_action_checklist",
    )
    if not formal_launch_action_queue.empty:
        for row in formal_launch_action_queue.to_dict("records"):
            add(
                "盘前",
                str(row.get("stage") or "--"),
                f"{row.get('object') or '--'}：{row.get('action') or '--'}",
                str(row.get("unlocks") or "对应缺口被清理"),
                "若不能完成，保持纸面观察或待刷新",
                "formal_launch_action_queue",
            )

    if not ticket_checklist.empty:
        for row in ticket_checklist.to_dict("records"):
            add(
                "盘前逐票",
                f"{row.get('code')} {row.get('name')}",
                str(row.get("manual_approval_checklist") or "完成逐票人工放行确认"),
                "选择人工放行且风险确认，或明确纸面观察/待刷新/跳过",
                "未确认则不进入正式买入；同板块高热度默认先纸面观察",
                "ticket_review_checklist",
            )

    add(
        "开盘后",
        "执行纪律",
        "自动下单仍锁定；任何真实买入必须重新核对价格、仓位、止损、止盈和账户现金",
        "人工确认真实账户约束与下单路径后才允许执行",
        "若价格跳空、买点失真或账户约束变化，回退纸面观察",
        "formal_launch_checklist",
    )
    return pd.DataFrame(rows)


def _action_checklist_markdown(checklist: pd.DataFrame) -> str:
    if checklist.empty:
        return "_暂无实战前动作清单_"
    parts: list[str] = []
    for _, row in checklist.iterrows():
        parts.extend(
            [
                f"### {row.get('priority')}. {row.get('object')}",
                "",
                f"- 阶段：{row.get('phase') or '--'}；级别：`{row.get('severity') or '--'}`；状态：{row.get('status') or '--'}；正式放行：{'必处理' if _truthy(row.get('formal_trade_required')) else '观察'}",
                f"- 动作：{row.get('action') or '--'}",
                f"- 完成条件：{row.get('done_condition') or '--'}",
                f"- 来源：{row.get('source') or '--'}",
                "",
            ]
        )
    return "\n".join(parts).strip()


def _ticket_issues(row: dict[str, Any], open_codes: set[str]) -> tuple[list[str], list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    code = str(row.get("code") or "").strip()
    entry_date = _date_text(row.get("entry_date") or row.get("planned_entry_ts"))
    route = str(row.get("route") or "")
    strategy = str(row.get("trade_strategy") or "")
    pos = _safe_float(row.get("position_pct"))
    natural_pos = _safe_float(row.get("natural_position_pct"))
    ref = _safe_float(row.get("reference_close"))
    hard = _safe_float(row.get("hard_stop"))
    structure = _safe_float(row.get("structure_stop"))
    take = _safe_float(row.get("take_profit_1"))
    score = _safe_float(row.get("score"))
    wave_score = _safe_float(row.get("wave_style_score"))

    if not code:
        blockers.append("缺少股票代码")
    if not entry_date:
        blockers.append("缺少入场日期")
    if not _truthy(row.get("qualified_shadow_buy")):
        blockers.append("不是合格影子买入票")
    if not _truthy(row.get("m30_confirmed")):
        blockers.append("30m 确认未通过或缺失")
    if str(row.get("natural_action") or "") == "skip":
        blockers.append("自然纪律 N1/Nx 标记为跳过")
    if code in open_codes:
        blockers.append("真实/影子持仓中已有同票，禁止伪装成独立二槽")

    if pos is None or pos <= 0:
        blockers.append("缺少正式合同仓位")
    if natural_pos is not None and pos is not None and natural_pos < pos:
        warnings.append(f"自然纪律建议仓位 {_pct(natural_pos)} 低于合同仓位 {_pct(pos)}")
    if ref is None or ref <= 0:
        blockers.append("缺少参考价")
    if hard is None or hard <= 0:
        blockers.append("缺少硬止损")
    if take is None or take <= 0:
        warnings.append("缺少第一止盈价")
    if structure is None or structure <= 0:
        warnings.append("缺少结构止损/前低保护")

    if route == "institutional_mainwave" or strategy == "institutional_score120_mainwave":
        if wave_score is None:
            warnings.append("机构主升缺少 wave_style_score，分数口径需人工确认")
        if score is not None and wave_score is not None and abs(score - wave_score) > 1e-6:
            warnings.append("score 与 wave_style_score 不一致，需确认主显示分口径")
    if route == "g2_gap_supplement" or strategy == "volume_runup_supplement":
        if pos is not None and pos > 0.25:
            warnings.append("G2 空档补位仓位高于半槽，需确认补位角色")

    heat_state = str(row.get("index_mom60_heat_state") or "")
    if heat_state:
        notes.append(f"市场热度状态：{heat_state}")
    natural_reason = str(row.get("natural_reason") or "").strip()
    if natural_reason:
        notes.append(f"自然纪律：{row.get('natural_action_label') or row.get('natural_action') or '观察'}；{natural_reason}")
    if row.get("block_reason"):
        warnings.append(f"票据说明/阻断原因：{row.get('block_reason')}")
    review_action = str(row.get("pretrade_review_action") or "").strip()
    if not review_action:
        warnings.append("尚未完成逐票人工复盘确认")
    elif review_action in {"skip", "reject"}:
        blockers.append(f"逐票人工复盘已标记为 {row.get('pretrade_review_label') or review_action}")
    elif review_action == "paper_watch":
        warnings.append("逐票复盘仅标记为纸面观察，尚未人工放行实盘")
    elif review_action == "wait_refresh":
        warnings.append("逐票人工复盘要求等待刷新")
    elif review_action == "manual_approved" and not _truthy(row.get("pretrade_review_risk_acknowledged")):
        warnings.append("逐票人工放行缺少风险确认记录")

    return blockers, warnings, notes


def _review_tickets(rows: list[dict[str, Any]], open_codes: set[str]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        blockers, warnings, notes = _ticket_issues(row, open_codes)
        item = {
            "rank": idx,
            "code": row.get("code"),
            "name": row.get("name"),
            "entry_date": _date_text(row.get("entry_date") or row.get("planned_entry_ts")),
            "route": row.get("route"),
            "strategy": _route_label(row),
            "position_pct": _safe_float(row.get("position_pct")),
            "natural_position_pct": _safe_float(row.get("natural_position_pct")),
            "reference_close": _safe_float(row.get("reference_close")),
            "hard_stop": _safe_float(row.get("hard_stop")),
            "structure_stop": _safe_float(row.get("structure_stop")),
            "take_profit_1": _safe_float(row.get("take_profit_1")),
            "m30_confirmed": _truthy(row.get("m30_confirmed")),
            "natural_action": row.get("natural_action"),
            "natural_action_label": row.get("natural_action_label"),
            "pretrade_review_action": row.get("pretrade_review_action"),
            "pretrade_review_label": row.get("pretrade_review_label"),
            "pretrade_review_note": row.get("pretrade_review_note"),
            "pretrade_review_risk_acknowledged": _truthy(row.get("pretrade_review_risk_acknowledged")),
            "pretrade_review_confirmation_complete": _truthy(row.get("pretrade_review_confirmation_complete")),
            "pretrade_review_missing_confirmation_items": row.get("pretrade_review_missing_confirmation_items"),
            "pretrade_review_execution_posture": row.get("pretrade_review_execution_posture"),
            "pretrade_review_decision_level": row.get("pretrade_review_decision_level"),
            "pretrade_review_consistency_grade": row.get("pretrade_review_consistency_grade"),
            "pretrade_review_consistency_score": _safe_float(row.get("pretrade_review_consistency_score")),
            "pretrade_review_downgrade_rule": row.get("pretrade_review_downgrade_rule"),
            "pretrade_review_decision_reason": row.get("pretrade_review_decision_reason"),
            "pretrade_review_next_review_trigger": row.get("pretrade_review_next_review_trigger"),
            "blocker_count": len(blockers),
            "warning_count": len(warnings),
            "blockers": "；".join(blockers),
            "warnings": "；".join(warnings),
            "notes": "；".join(notes),
            "ticket_key": row.get("ticket_key") or row.get("candidate_key") or row.get("trade_key"),
        }
        item["risk_level"] = _risk_level(item)
        out.append(item)
    return pd.DataFrame(out)


def _review_holdings(real_rows: list[dict[str, Any]], shadow_rows: list[dict[str, Any]]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for source, rows in [("real_account", real_rows), ("shadow_ledger", shadow_rows)]:
        for row in rows or []:
            warnings: list[str] = []
            blockers: list[str] = []
            price_fresh = row.get("price_data_fresh")
            stale_minutes = _safe_float(row.get("price_stale_minutes"))
            if price_fresh is False:
                warnings.append(f"退出判断价格不新鲜，滞后约 {stale_minutes:.0f} 分钟" if stale_minutes is not None else "退出判断价格不新鲜")
            if not row.get("hard_stop") and not row.get("hard_stop_for_exit"):
                warnings.append("缺少硬止损")
            if not row.get("take_profit_1") and not row.get("take_profit_1_for_exit"):
                warnings.append("缺少止盈一")
            action = str(row.get("management_action") or row.get("exit_action_label") or "")
            if "卖" in action or str(row.get("exit_priority") or "") == "urgent":
                blockers.append("退出管理出现卖出/紧急动作，需要先处理持仓再开新仓")
            item = {
                "source": source,
                "code": row.get("code"),
                "name": row.get("name"),
                "entry_date": _date_text(row.get("entry_date") or row.get("trade_date")),
                "position_pct": _safe_float(row.get("position_pct") or row.get("slot_pct")),
                "current_price": _safe_float(row.get("current_price_for_exit") or row.get("current_price") or row.get("reference_close")),
                "pnl_ratio": _safe_float(row.get("pnl_ratio_for_exit") or row.get("pnl_ratio")),
                "management_action": action or "--",
                "exit_reason": row.get("exit_reason_label") or row.get("exit_reason") or row.get("management_reason") or "",
                "blocker_count": len(blockers),
                "warning_count": len(warnings),
                "blockers": "；".join(blockers),
                "warnings": "；".join(warnings),
            }
            item["risk_level"] = _risk_level(item)
            out.append(item)
    return pd.DataFrame(out)


def _review_candidates(rows: list[dict[str, Any]]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        issues: list[str] = []
        if not _truthy(row.get("router_eligible") or row.get("qualified_shadow_buy")):
            issues.append(str(row.get("block_reason") or row.get("shadow_status") or "未通过路由准入"))
        if row.get("natural_context_tag") in {"repair_against_downrisk", "range_repair", "weak_rebound_repair"}:
            issues.append("修复类候选，需要确认市场压力与修复证据")
        if str(row.get("natural_action") or "") == "skip":
            issues.append("自然纪律跳过")
        out.append(
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "entry_date": _date_text(row.get("entry_date") or row.get("trade_date")),
                "route": row.get("route"),
                "strategy": _route_label(row),
                "router_eligible": _truthy(row.get("router_eligible") or row.get("qualified_shadow_buy")),
                "natural_action": row.get("natural_action"),
                "natural_context_tag": row.get("natural_context_tag"),
                "score": _safe_float(row.get("score")),
                "wave_style_score": _safe_float(row.get("wave_style_score")),
                "sector": row.get("sector_name") or row.get("l2_sector_name") or row.get("industry"),
                "issues": "；".join(dict.fromkeys([x for x in issues if x])),
            }
        )
    return pd.DataFrame(out)


def _gate_rows(current: dict[str, Any], broker: dict[str, Any], ptrade: dict[str, Any], ticket_review: pd.DataFrame, holding_review: pd.DataFrame) -> pd.DataFrame:
    summary = current.get("summary") or {}
    capital = broker.get("capital") or {}
    gates: list[dict[str, Any]] = []

    def add(name: str, ok: bool, severity: str, message: str) -> None:
        gates.append(
            {
                "gate": name,
                "status": "pass" if ok else ("block" if severity == "block" else "warn"),
                "ok": bool(ok),
                "severity": severity,
                "message": message,
            }
        )

    add("current_payload_ok", bool(current.get("ok")), "block", f"diagnosis={summary.get('diagnosis_code') or '--'}")
    add("has_next_trade_ticket", len(current.get("next_trade_buy_tickets") or []) > 0, "warn", f"next_trade_buy_tickets={len(current.get('next_trade_buy_tickets') or [])}")
    add("ticket_contract_complete", not (ticket_review.get("blocker_count", pd.Series(dtype=int)).fillna(0) > 0).any(), "block", "每张下一交易日票必须有代码、日期、30m、仓位、参考价、硬止损")
    add("holding_exit_clean", not (holding_review.get("blocker_count", pd.Series(dtype=int)).fillna(0) > 0).any(), "block", "真实/影子持仓不能存在未处理的紧急退出动作")
    add("holding_exit_price_fresh", not (holding_review.get("warning_count", pd.Series(dtype=int)).fillna(0) > 0).any(), "warn", "持仓退出判断应先刷新最新价，避免用过期价格决定卖出或新开仓")
    add("broker_capital_present", _safe_float(capital.get("total_capital")) is not None and _safe_float(capital.get("available_cash")) is not None, "block", f"total_capital={_money(capital.get('total_capital'))}, available_cash={_money(capital.get('available_cash'))}")
    add("broker_holdings_fresh", not bool(capital.get("holdings_stale")), "warn", f"holdings_stale={capital.get('holdings_stale')}")
    add("shadow_only_locked", current.get("formal_buy_signal") is False and current.get("auto_order_allowed") is False and current.get("order_path_enabled") is False, "warn", "正式自动买入仍锁定；适合人工/纸面实战，不适合自动实盘")
    ptrade_gates = ptrade.get("gates") if isinstance(ptrade.get("gates"), dict) else {}
    add("ptrade_dry_run_ready", bool(ptrade_gates.get("dry_run_ready") or ptrade_gates.get("local_submit_ready")), "warn", f"ptrade_gates={ptrade_gates}")
    add("ptrade_live_submit_locked", not bool(ptrade_gates.get("live_submit_ready")), "warn", "live_submit_ready 应保持 false，除非人工确认切实开启真实下单")
    return pd.DataFrame(gates)


def _hazard_register(ticket_review: pd.DataFrame, holding_review: pd.DataFrame, candidate_review: pd.DataFrame, gate_rows: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for _, row in gate_rows[~gate_rows["ok"]].iterrows():
        rows.append({"scope": "gate", "severity": row["severity"], "object": row["gate"], "hazard": row["message"], "action": "先处理该 Gate 后再进入实战执行"})
    for _, row in ticket_review[ticket_review["risk_level"].isin(["block", "warn", "watch"])].iterrows():
        text = "；".join([str(row.get("blockers") or ""), str(row.get("warnings") or "")]).strip("；")
        rows.append({"scope": "next_trade_ticket", "severity": row["risk_level"], "object": f"{row.get('code')} {row.get('name')}", "hazard": text, "action": "逐票人工复核；block 项未清除前不执行"})
    for _, row in holding_review[holding_review["risk_level"].isin(["block", "warn", "watch"])].iterrows():
        text = "；".join([str(row.get("blockers") or ""), str(row.get("warnings") or "")]).strip("；")
        rows.append({"scope": row.get("source"), "severity": row["risk_level"], "object": f"{row.get('code')} {row.get('name')}", "hazard": text, "action": "先完成退出/价格新鲜度确认，再考虑新开仓"})
    repair = candidate_review[candidate_review["issues"].astype(str).str.contains("修复类候选", na=False)].head(20)
    for _, row in repair.iterrows():
        rows.append({"scope": "candidate_pool", "severity": "watch", "object": f"{row.get('code')} {row.get('name')}", "hazard": row.get("issues"), "action": "作为遗漏观察池，不直接替代主升票"})
    return pd.DataFrame(rows)


def run() -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    current = asyncio.run(get_gen3_state_alpha_current(limit=200, refresh=False, entry_date=None))
    broker = _broker_snapshot()
    try:
        ptrade = run_ptrade_audit()
    except Exception as exc:
        ptrade = {"ok": False, "error": str(exc), "gates": {}}

    real_holdings = broker.get("holdings") if isinstance(broker.get("holdings"), list) else []
    shadow_holdings = current.get("shadow_ledger") if isinstance(current.get("shadow_ledger"), list) else []
    open_codes = {str(x.get("code") or "").strip() for x in [*real_holdings, *shadow_holdings] if str(x.get("code") or "").strip()}

    next_trade_tickets = current.get("next_trade_buy_tickets") or []
    ticket_review = _review_tickets(next_trade_tickets, open_codes)
    ticket_checklist = _build_ticket_review_checklist(next_trade_tickets, ticket_review)
    holding_review = _review_holdings(real_holdings, shadow_holdings)
    holding_checklist = _build_holding_exit_checklist(holding_review)
    holding_refresh_evidence = _build_holding_refresh_evidence(holding_review, holding_checklist)
    source_candidates = current.get("all_source_candidates") or []
    candidate_review = _review_candidates(source_candidates)
    candidate_checklist = _build_candidate_omission_checklist(source_candidates, next_trade_tickets)
    natural_consistency = _build_natural_trade_consistency_review(ticket_checklist, candidate_checklist, holding_refresh_evidence)
    natural_execution_matrix = _build_natural_execution_decision_matrix(natural_consistency, ticket_checklist)
    gates = _gate_rows(current, broker, ptrade, ticket_review, holding_review)
    hazards = _hazard_register(ticket_review, holding_review, candidate_review, gates)
    action_checklist = _build_pretrade_action_checklist(gates, ticket_checklist, holding_checklist, candidate_checklist, natural_execution_matrix)

    for name, df in [
        ("next_trade_ticket_review.csv", ticket_review),
        ("ticket_review_checklist.csv", ticket_checklist),
        ("holding_exit_review.csv", holding_review),
        ("holding_exit_checklist.csv", holding_checklist),
        ("holding_refresh_evidence.csv", holding_refresh_evidence),
        ("candidate_hidden_risk_review.csv", candidate_review),
        ("candidate_omission_checklist.csv", candidate_checklist),
        ("natural_trade_consistency_review.csv", natural_consistency),
        ("natural_execution_decision_matrix.csv", natural_execution_matrix),
        ("readiness_gates.csv", gates),
        ("hazard_register.csv", hazards),
        ("pretrade_action_checklist.csv", action_checklist),
    ]:
        df.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")

    block_count = int((gates["status"] == "block").sum()) if not gates.empty else 0
    warn_count = int((gates["status"] == "warn").sum()) if not gates.empty else 0
    ticket_blocks = int((ticket_review.get("risk_level", pd.Series(dtype=str)) == "block").sum()) if not ticket_review.empty else 0
    holding_blocks = int((holding_review.get("risk_level", pd.Series(dtype=str)) == "block").sum()) if not holding_review.empty else 0
    ticket_warnings = int((ticket_review.get("warning_count", pd.Series(dtype=int)).fillna(0) > 0).sum()) if not ticket_review.empty else 0
    holding_warnings = int((holding_review.get("warning_count", pd.Series(dtype=int)).fillna(0) > 0).sum()) if not holding_review.empty else 0
    if ticket_review.empty:
        pretrade_actions = pd.Series(dtype=str)
    else:
        pretrade_actions = ticket_review.get("pretrade_review_action", pd.Series(dtype=str)).fillna("").astype(str).str.strip()
    pretrade_unreviewed = int((pretrade_actions == "").sum()) if not pretrade_actions.empty else 0
    pretrade_manual_approved = int((pretrade_actions == "manual_approved").sum()) if not pretrade_actions.empty else 0
    pretrade_paper_watch = int((pretrade_actions == "paper_watch").sum()) if not pretrade_actions.empty else 0
    pretrade_wait_refresh = int((pretrade_actions == "wait_refresh").sum()) if not pretrade_actions.empty else 0
    pretrade_skip_reject = int(pretrade_actions.isin(["skip", "reject"]).sum()) if not pretrade_actions.empty else 0
    checklist_warn_count = int(ticket_checklist.get("checklist_level", pd.Series(dtype=str)).isin(["warn", "watch"]).sum()) if not ticket_checklist.empty else 0
    checklist_block_count = int((ticket_checklist.get("checklist_level", pd.Series(dtype=str)) == "block").sum()) if not ticket_checklist.empty else 0
    portfolio_decision_warn_count = int(ticket_checklist.get("portfolio_decision_level", pd.Series(dtype=str)).isin(["warn", "watch"]).sum()) if not ticket_checklist.empty else 0
    portfolio_decision_block_count = int((ticket_checklist.get("portfolio_decision_level", pd.Series(dtype=str)) == "block").sum()) if not ticket_checklist.empty else 0
    holding_checklist_warn_count = int(holding_checklist.get("checklist_level", pd.Series(dtype=str)).isin(["warn", "watch"]).sum()) if not holding_checklist.empty else 0
    holding_checklist_block_count = int((holding_checklist.get("checklist_level", pd.Series(dtype=str)) == "block").sum()) if not holding_checklist.empty else 0
    holding_formal_refresh_pending = int(holding_refresh_evidence.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy).sum()) if not holding_refresh_evidence.empty else 0
    holding_observation_refresh_count = int((~holding_refresh_evidence.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy)).sum()) if not holding_refresh_evidence.empty else 0
    candidate_checklist_warn_count = int(candidate_checklist.get("checklist_level", pd.Series(dtype=str)).isin(["warn", "watch"]).sum()) if not candidate_checklist.empty else 0
    candidate_checklist_block_count = int((candidate_checklist.get("checklist_level", pd.Series(dtype=str)) == "block").sum()) if not candidate_checklist.empty else 0
    natural_consistency_min_score = int(natural_consistency.get("consistency_score", pd.Series(dtype=int)).min()) if not natural_consistency.empty else None
    natural_consistency_strained_count = int(natural_consistency.get("consistency_grade", pd.Series(dtype=str)).isin(["strained", "incoherent"]).sum()) if not natural_consistency.empty else 0
    natural_execution_block_count = int((natural_execution_matrix.get("decision_level", pd.Series(dtype=str)) == "block").sum()) if not natural_execution_matrix.empty else 0
    natural_execution_warn_watch_count = int(natural_execution_matrix.get("decision_level", pd.Series(dtype=str)).isin(["warn", "watch"]).sum()) if not natural_execution_matrix.empty else 0
    action_pending_count = int(action_checklist.get("status", pd.Series(dtype=str)).astype(str).eq("待处理").sum()) if not action_checklist.empty else 0
    action_block_count = int((action_checklist.get("severity", pd.Series(dtype=str)) == "block").sum()) if not action_checklist.empty else 0
    if action_checklist.empty:
        formal_action_mask = pd.Series(dtype=bool)
        pending_action_mask = pd.Series(dtype=bool)
    else:
        formal_action_mask = action_checklist.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy)
        pending_action_mask = action_checklist.get("status", pd.Series(dtype=str)).astype(str).eq("待处理")
    formal_action_pending_count = int((pending_action_mask & formal_action_mask).sum()) if not action_checklist.empty else 0
    observation_action_pending_count = int((pending_action_mask & ~formal_action_mask).sum()) if not action_checklist.empty else 0
    if ticket_review.empty:
        pretrade_risk_ack = pd.Series(dtype=bool)
        pretrade_confirmation_complete = pd.Series(dtype=bool)
    else:
        pretrade_risk_ack = ticket_review.get("pretrade_review_risk_acknowledged", pd.Series(dtype=bool)).fillna(False).map(_truthy)
        pretrade_confirmation_complete = ticket_review.get("pretrade_review_confirmation_complete", pd.Series(dtype=bool)).fillna(False).map(_truthy)
    pretrade_formal_ready = int(((pretrade_actions == "manual_approved") & pretrade_risk_ack & pretrade_confirmation_complete).sum()) if not pretrade_actions.empty else 0
    verdict = (
        "manual_review_blocked"
        if block_count or ticket_blocks or holding_blocks
        else ("manual_shadow_ready_with_warnings" if warn_count or ticket_warnings or holding_warnings else "manual_shadow_ready")
    )

    summary = {
        "version": "g3_realtime_readiness_review_v1",
        "generated_at": _now_text(),
        "verdict": verdict,
        "entry_date": (current.get("summary") or {}).get("entry_date"),
        "next_trade_entry_date": (current.get("date_display_analysis") or {}).get("display_entry_date"),
        "next_trade_ticket_count": len(current.get("next_trade_buy_tickets") or []),
        "candidate_count": len(current.get("all_source_candidates") or []),
        "real_holding_count": len(real_holdings),
        "shadow_holding_count": len(shadow_holdings),
        "gate_block_count": block_count,
        "gate_warn_count": warn_count,
        "ticket_block_count": ticket_blocks,
        "holding_block_count": holding_blocks,
        "ticket_warning_count": ticket_warnings,
        "holding_warning_count": holding_warnings,
        "pretrade_review_unreviewed_count": pretrade_unreviewed,
        "pretrade_review_manual_approved_count": pretrade_manual_approved,
        "pretrade_review_formal_ready_count": pretrade_formal_ready,
        "pretrade_review_paper_watch_count": pretrade_paper_watch,
        "pretrade_review_wait_refresh_count": pretrade_wait_refresh,
        "pretrade_review_skip_reject_count": pretrade_skip_reject,
        "ticket_checklist_block_count": checklist_block_count,
        "ticket_checklist_warn_watch_count": checklist_warn_count,
        "portfolio_decision_block_count": portfolio_decision_block_count,
        "portfolio_decision_warn_watch_count": portfolio_decision_warn_count,
        "holding_checklist_block_count": holding_checklist_block_count,
        "holding_checklist_warn_watch_count": holding_checklist_warn_count,
        "holding_formal_refresh_pending_count": holding_formal_refresh_pending,
        "holding_observation_refresh_count": holding_observation_refresh_count,
        "candidate_checklist_block_count": candidate_checklist_block_count,
        "candidate_checklist_warn_watch_count": candidate_checklist_warn_count,
        "natural_consistency_min_score": natural_consistency_min_score,
        "natural_consistency_strained_count": natural_consistency_strained_count,
        "natural_execution_block_count": natural_execution_block_count,
        "natural_execution_warn_watch_count": natural_execution_warn_watch_count,
        "pretrade_action_pending_count": action_pending_count,
        "pretrade_formal_action_pending_count": formal_action_pending_count,
        "pretrade_observation_action_pending_count": observation_action_pending_count,
        "pretrade_action_block_count": action_block_count,
        "output_dir": str(OUT_DIR),
    }
    formal_launch_checklist = _build_formal_launch_checklist(summary)
    formal_launch_missing_count = int((formal_launch_checklist["status"] != "pass").sum()) if not formal_launch_checklist.empty else 0
    formal_launch_block_count = int((formal_launch_checklist["status"] == "block").sum()) if not formal_launch_checklist.empty else 0
    if formal_launch_block_count:
        formal_launch_status = "blocked"
        formal_launch_next_step = "先处理硬阻断，再回到逐票复盘"
    elif formal_launch_missing_count:
        formal_launch_status = "not_formal_ready"
        formal_launch_next_step = "完成真实持仓刷新和逐票人工放行后再判断"
    else:
        formal_launch_status = "manual_live_ready"
        formal_launch_next_step = "可进入人工实盘执行确认；自动下单仍保持锁定"
    summary.update(
        {
            "formal_launch_status": formal_launch_status,
            "formal_launch_ready": formal_launch_status == "manual_live_ready",
            "formal_launch_missing_count": formal_launch_missing_count,
            "formal_launch_block_count": formal_launch_block_count,
            "formal_launch_next_step": formal_launch_next_step,
        }
    )
    formal_launch_checklist.to_csv(OUT_DIR / "formal_launch_checklist.csv", index=False, encoding="utf-8-sig")
    execution_mode_matrix = _build_execution_mode_matrix(summary, current, broker, ptrade, gates)
    execution_mode_matrix.to_csv(OUT_DIR / "execution_mode_matrix.csv", index=False, encoding="utf-8-sig")
    summary["execution_mode_ready_count"] = int(execution_mode_matrix.get("allowed_now", pd.Series(dtype=bool)).map(_truthy).sum()) if not execution_mode_matrix.empty else 0
    summary["execution_mode_locked_count"] = int((execution_mode_matrix.get("status", pd.Series(dtype=str)).astype(str) == "locked").sum()) if not execution_mode_matrix.empty else 0
    formal_launch_action_queue = _build_formal_launch_action_queue(action_checklist, ticket_checklist, formal_launch_checklist)
    formal_launch_action_queue.to_csv(OUT_DIR / "formal_launch_action_queue.csv", index=False, encoding="utf-8-sig")
    summary["formal_launch_action_queue_count"] = len(formal_launch_action_queue)
    pretrade_review_evidence = _build_pretrade_review_evidence(ticket_review, ticket_checklist)
    pretrade_review_evidence.to_csv(OUT_DIR / "pretrade_review_evidence.csv", index=False, encoding="utf-8-sig")
    summary["pretrade_review_evidence_count"] = len(pretrade_review_evidence)
    paper_watch_followup = _build_paper_watch_followup(ticket_review, pretrade_review_evidence)
    paper_watch_followup.to_csv(OUT_DIR / "paper_watch_followup.csv", index=False, encoding="utf-8-sig")
    summary["paper_watch_followup_count"] = len(paper_watch_followup)
    summary["paper_watch_followup_pending_count"] = (
        int((paper_watch_followup.get("followup_status", pd.Series(dtype=str)).astype(str) == "pending_observation").sum())
        if not paper_watch_followup.empty
        else 0
    )
    summary["paper_watch_issue_found_count"] = (
        int((paper_watch_followup.get("followup_status", pd.Series(dtype=str)).astype(str) == "issue_found").sum())
        if not paper_watch_followup.empty
        else 0
    )
    premarket_playbook = _build_premarket_execution_playbook(summary, formal_launch_action_queue, ticket_checklist)
    premarket_playbook.to_csv(OUT_DIR / "premarket_execution_playbook.csv", index=False, encoding="utf-8-sig")
    summary["premarket_playbook_step_count"] = len(premarket_playbook)
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUT_DIR / "TICKET_REVIEW_BRIEF_CN.md").write_text(
        "\n".join(
            [
                "# G3 下一交易日逐票复盘底稿",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 下一交易日：{summary.get('next_trade_entry_date') or '--'}",
                f"- 票据数量：{summary['next_trade_ticket_count']}",
                f"- 底稿阻断/关注：{summary['ticket_checklist_block_count']} / {summary['ticket_checklist_warn_watch_count']}",
                "",
                _ticket_checklist_markdown(ticket_checklist),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "HOLDING_EXIT_BRIEF_CN.md").write_text(
        "\n".join(
            [
                "# G3 当前持仓退出复盘底稿",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 真实持仓：{summary['real_holding_count']}；影子持仓：{summary['shadow_holding_count']}",
                f"- 底稿阻断/关注：{summary['holding_checklist_block_count']} / {summary['holding_checklist_warn_watch_count']}",
                f"- 正式刷新待处理：{summary['holding_formal_refresh_pending_count']}；观察刷新：{summary['holding_observation_refresh_count']}",
                "",
                _holding_checklist_markdown(holding_checklist),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "CANDIDATE_OMISSION_BRIEF_CN.md").write_text(
        "\n".join(
            [
                "# G3 候选池遗漏复盘底稿",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 全量候选：{summary['candidate_count']}",
                f"- 底稿阻断/关注：{summary['candidate_checklist_block_count']} / {summary['candidate_checklist_warn_watch_count']}",
                "",
                _candidate_checklist_markdown(candidate_checklist),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "PRETRADE_ACTION_CHECKLIST_CN.md").write_text(
        "\n".join(
            [
                "# G3 实战前动作清单",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 待处理动作：{summary['pretrade_action_pending_count']}",
                f"- 正式放行必处理：{summary['pretrade_formal_action_pending_count']}",
                f"- 观察动作：{summary['pretrade_observation_action_pending_count']}",
                f"- 阻断动作：{summary['pretrade_action_block_count']}",
                f"- 正式实盘放行状态：`{summary['formal_launch_status']}`；缺口：{summary['formal_launch_missing_count']}；下一步：{summary['formal_launch_next_step']}",
                f"- 执行入口可用/锁定：{summary['execution_mode_ready_count']} / {summary['execution_mode_locked_count']}",
                f"- 放行优先队列：{summary['formal_launch_action_queue_count']} 项",
                f"- 逐票复盘证据：{summary['pretrade_review_evidence_count']} 条",
                f"- 纸面观察后评估：{summary['paper_watch_followup_count']} 条；待观察 {summary['paper_watch_followup_pending_count']}；发现隐患 {summary['paper_watch_issue_found_count']}",
                f"- 盘前执行剧本：{summary['premarket_playbook_step_count']} 步",
                "",
                _action_checklist_markdown(action_checklist),
                "",
            ]
        ),
        encoding="utf-8",
    )

    report = [
        "# G3 实战前逐票复盘与隐患审计 v1",
        "",
        "## 结论",
        "",
        f"- 生成时间：{summary['generated_at']}",
        f"- 当前结论：`{verdict}`",
        f"- 当前入场日：{summary.get('entry_date') or '--'}",
        f"- 下一交易日关注：{summary.get('next_trade_entry_date') or '--'}",
        f"- 下一交易日票据：{summary['next_trade_ticket_count']} 张",
        f"- 全量候选：{summary['candidate_count']} 条",
        f"- 真实持仓：{summary['real_holding_count']} 条；影子持仓：{summary['shadow_holding_count']} 条",
        f"- Gate 阻断/警告：{summary['gate_block_count']} / {summary['gate_warn_count']}",
        f"- 票据阻断/警告：{summary['ticket_block_count']} / {summary['ticket_warning_count']}",
        f"- 持仓阻断/警告：{summary['holding_block_count']} / {summary['holding_warning_count']}",
        f"- 逐票人工复盘：未复盘 {summary['pretrade_review_unreviewed_count']}；正式就绪 {summary['pretrade_review_formal_ready_count']}；人工放行 {summary['pretrade_review_manual_approved_count']}；纸面观察 {summary['pretrade_review_paper_watch_count']}；待刷新 {summary['pretrade_review_wait_refresh_count']}；跳过/拒绝 {summary['pretrade_review_skip_reject_count']}",
        f"- 逐票复盘底稿阻断/关注：{summary['ticket_checklist_block_count']} / {summary['ticket_checklist_warn_watch_count']}",
        f"- 组合决策阻断/关注：{summary['portfolio_decision_block_count']} / {summary['portfolio_decision_warn_watch_count']}",
        f"- 持仓退出底稿阻断/关注：{summary['holding_checklist_block_count']} / {summary['holding_checklist_warn_watch_count']}",
        f"- 持仓刷新正式待处理/观察：{summary['holding_formal_refresh_pending_count']} / {summary['holding_observation_refresh_count']}",
        f"- 候选遗漏底稿阻断/关注：{summary['candidate_checklist_block_count']} / {summary['candidate_checklist_warn_watch_count']}",
        f"- 自然交易一致性最低分/偏拧票数：{summary['natural_consistency_min_score']} / {summary['natural_consistency_strained_count']}",
        f"- 自然执行决策阻断/关注：{summary['natural_execution_block_count']} / {summary['natural_execution_warn_watch_count']}",
        f"- 实战前动作清单待处理/正式必处理/观察/阻断：{summary['pretrade_action_pending_count']} / {summary['pretrade_formal_action_pending_count']} / {summary['pretrade_observation_action_pending_count']} / {summary['pretrade_action_block_count']}",
        f"- 正式实盘放行：`{summary['formal_launch_status']}`；缺口 {summary['formal_launch_missing_count']}；下一步：{summary['formal_launch_next_step']}",
        f"- 执行入口可用/锁定：{summary['execution_mode_ready_count']} / {summary['execution_mode_locked_count']}",
        f"- 放行优先队列：{summary['formal_launch_action_queue_count']} 项",
        f"- 逐票复盘证据：{summary['pretrade_review_evidence_count']} 条",
        f"- 纸面观察后评估：{summary['paper_watch_followup_count']} 条；待观察 {summary['paper_watch_followup_pending_count']}；发现隐患 {summary['paper_watch_issue_found_count']}",
        f"- 盘前执行剧本：{summary['premarket_playbook_step_count']} 步",
        "",
        "解释：本报告是实战前只读审计，不触发刷新、不下单。`manual_shadow_ready` 只代表可进入人工/纸面实战复盘；正式自动下单仍必须另行确认。",
        "",
        "## 实战 Gate",
        "",
        _md_table(gates),
        "",
        "## 实战前动作清单",
        "",
        _action_checklist_markdown(action_checklist),
        "",
        "## 正式实盘放行清单",
        "",
        _md_table(formal_launch_checklist),
        "",
        "## 执行入口矩阵",
        "",
        _md_table(execution_mode_matrix),
        "",
        "## 正式放行优先队列",
        "",
        _md_table(formal_launch_action_queue),
        "",
        "## 逐票复盘证据台账",
        "",
        _md_table(pretrade_review_evidence),
        "",
        "## 纸面观察后评估",
        "",
        "这里记录纸面观察后的真实学习结果：不是按收益倒推调参，而是把问题归因到买点、选股、策略切换或卖点合同。",
        "",
        _md_table(paper_watch_followup),
        "",
        "## 盘前执行剧本",
        "",
        _md_table(premarket_playbook),
        "",
        "## 自然交易一致性审计",
        "",
        "这里把买点、卖点、选股模式和策略切换放在同一张表里检查；人工放行和持仓刷新只进入就绪缺口，不把手续未完成误判为交易逻辑不顺。",
        "",
        _md_table(natural_consistency),
        "",
        "## 自然执行决策矩阵",
        "",
        "这里把一致性审计翻译成实战动作：`incoherent` 才硬阻断；`strained` 需要人工确认买点新鲜度、热度拥挤、同板块共振和降级规则。",
        "",
        _md_table(natural_execution_matrix),
        "",
        "## 下一交易日逐票复盘",
        "",
        _md_table(ticket_review),
        "",
        "## 下一交易日逐票复盘底稿",
        "",
        _ticket_checklist_markdown(ticket_checklist),
        "",
        "## 当前持仓退出复盘",
        "",
        _md_table(holding_review),
        "",
        "## 真实/影子持仓刷新证明",
        "",
        _md_table(holding_refresh_evidence),
        "",
        "## 当前持仓退出复盘底稿",
        "",
        _holding_checklist_markdown(holding_checklist),
        "",
        "## 隐患登记",
        "",
        _md_table(hazards),
        "",
        "## 候选池遗漏观察",
        "",
        "这里不是让修复票替代主升票，而是把被路由阻断、修复语义较强、或自然纪律需要解释的候选列入观察池，防止策略遗漏长期不被看见。",
        "",
        _md_table(candidate_review[candidate_review["issues"].astype(str).ne("")].head(40) if not candidate_review.empty else candidate_review),
        "",
        "## 候选池遗漏复盘底稿",
        "",
        _candidate_checklist_markdown(candidate_checklist),
        "",
        "## 下一步",
        "",
        "1. 若 Gate 存在 `block`，先处理对应实盘风险，再考虑任何新开仓。",
        "2. 对每张下一交易日票据按 `blockers/warnings/notes` 做人工确认，确认后只记录纸面/人工实战结果。",
        "3. 连续积累 30 个交易日后，再判断 N1/N2/N3 是否升级为硬约束或仓位缩放。",
        "",
    ]
    (OUT_DIR / "REPORT_CN.md").write_text("\n".join(report), encoding="utf-8")
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2, default=str))

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

from api.gen3_state_alpha import CANDIDATE_OMISSION_REVIEWS_PATH, DAILY_REVIEW_CHECKLIST_REVIEWS_PATH, FORMAL_ACTION_REVIEWS_PATH, NO_TRADE_DAY_REVIEWS_PATH, PAPER_EXECUTIONS_PATH, PAPER_WATCH_REVIEWS_PATH, PREMARKET_ACTION_ATTEMPTS_PATH, _broker_snapshot, get_gen3_state_alpha_current  # noqa: E402
from utils.paths import report_path  # noqa: E402


OUT_DIR = report_path("g3_realtime_readiness_review_v1")

EMPTY_REPORT_COLUMNS = {
    "next_trade_ticket_review.csv": [
        "code",
        "name",
        "entry_date",
        "route",
        "strategy",
        "risk_level",
        "blocker_count",
        "warning_count",
        "blockers",
        "warnings",
        "notes",
    ],
    "ticket_review_checklist.csv": [
        "rank",
        "code",
        "name",
        "entry_date",
        "route",
        "strategy",
        "checklist_level",
        "formal_ready",
        "formal_ready_reason",
        "hidden_risks",
        "manual_questions",
        "action_recommendation",
    ],
    "natural_trade_consistency_review.csv": [
        "rank",
        "code",
        "name",
        "entry_date",
        "consistency_score",
        "consistency_grade",
        "buy_point_logic",
        "selection_logic",
        "model_switch_logic",
        "sell_point_logic",
        "deductions",
    ],
    "natural_execution_decision_matrix.csv": [
        "rank",
        "code",
        "name",
        "entry_date",
        "decision_level",
        "execution_posture",
        "source_deductions",
        "required_before_execution",
        "next_action",
    ],
    "pretrade_review_evidence.csv": [
        "rank",
        "code",
        "name",
        "entry_date",
        "status",
        "review_action",
        "formal_ready",
        "risk_acknowledged",
        "confirmation_complete",
        "next_action",
    ],
    "formal_launch_action_queue.csv": [
        "priority",
        "stage",
        "severity",
        "object",
        "action",
        "unlocks",
        "source",
        "review_key",
        "action_status",
        "review_result",
        "review_result_label",
        "issue_area",
        "evidence",
        "natural_decision",
        "optimization_suggestion",
        "review_note",
        "source",
        "updated_at",
    ],
    "paper_watch_followup.csv": [
        "rank",
        "code",
        "name",
        "entry_date",
        "watch_result",
        "watch_result_label",
        "issue_area",
        "hidden_risk",
        "optimization_suggestion",
        "next_action",
    ],
    "day1_paper_review_pack.csv": [
        "rank",
        "code",
        "name",
        "entry_date",
        "route",
        "strategy",
        "launch_posture",
        "priority",
        "buy_point_review",
        "selection_review",
        "model_switch_review",
        "exit_contract_review",
        "hidden_risk_focus",
        "next_action",
    ],
    "day1_after_close_review_queue.csv": [
        "rank",
        "code",
        "name",
        "entry_date",
        "launch_posture",
        "after_close_status",
        "watch_result",
        "issue_area",
        "hidden_risk_focus",
        "next_action",
    ],
    "daily_live_review_board.csv": [
        "priority",
        "stage",
        "review_scope",
        "object",
        "code",
        "name",
        "entry_date",
        "review_status",
        "formal_trade_required",
        "issue_area",
        "required_action",
        "evidence",
        "natural_decision",
        "optimization_focus",
        "source",
        "review_key",
        "ticket_key",
    ],
    "strategy_learning_backlog.csv": [
        "priority",
        "learning_status",
        "issue_area",
        "review_scope",
        "object",
        "code",
        "name",
        "entry_date",
        "problem_signal",
        "evidence",
        "suggested_learning",
        "source",
        "review_key",
        "ticket_key",
    ],
    "live_hidden_risk_watchlist.csv": [
        "priority",
        "watch_status",
        "risk_level",
        "issue_area",
        "review_scope",
        "object",
        "code",
        "name",
        "entry_date",
        "risk_signal",
        "evidence_gap",
        "next_review_action",
        "natural_trade_boundary",
        "source",
        "review_key",
        "ticket_key",
    ],
    "live_daily_review_execution_checklist.csv": [
        "priority",
        "review_window",
        "checkpoint_time",
        "review_axis",
        "review_scope",
        "object",
        "code",
        "name",
        "entry_date",
        "risk_level",
        "watch_status",
        "review_status",
        "review_result",
        "review_result_label",
        "review_note",
        "updated_at",
        "evidence_to_collect",
        "pass_condition",
        "fail_condition",
        "target_action",
        "natural_trade_boundary",
        "source",
        "review_key",
        "ticket_key",
        "duplicate_source_count",
    ],
    "live_daily_review_action_layers.csv": [
        "priority",
        "action_layer",
        "layer_label",
        "review_window",
        "checkpoint_time",
        "item_count",
        "pending_count",
        "validated_count",
        "issue_found_count",
        "continue_watch_count",
        "high_risk_count",
        "review_axes",
        "first_action",
        "evidence_focus",
        "pass_condition",
        "fail_condition",
        "buy_permission_effect",
        "operator_instruction",
    ],
    "daily_review_checklist_reviews.csv": [
        "priority",
        "review_key",
        "ticket_key",
        "review_axis",
        "review_scope",
        "object",
        "code",
        "name",
        "entry_date",
        "review_status",
        "review_result",
        "review_result_label",
        "issue_area",
        "evidence",
        "natural_decision",
        "optimization_suggestion",
        "review_note",
        "source",
        "updated_at",
    ],
    "formal_action_reviews.csv": [
        "priority",
        "review_key",
        "object",
        "action",
        "source",
        "review_status",
        "review_result",
        "review_result_label",
        "issue_area",
        "evidence",
        "natural_decision",
        "optimization_suggestion",
        "review_note",
        "updated_at",
    ],
    "first_live_decision_card.csv": [
        "generated_at",
        "entry_date",
        "next_trade_entry_date",
        "decision_status",
        "decision_label",
        "execution_posture",
        "live_buy_allowed",
        "paper_execution_allowed",
        "auto_order_allowed",
        "primary_reason",
        "risk_tone",
        "formal_launch_status",
        "ticket_count",
        "formal_pending_count",
        "board_pending_count",
        "issue_found_count",
        "first_required_action",
        "top_action_list",
        "review_focus",
        "locked_modes",
        "next_review_trigger",
    ],
    "live_review_task_queue.csv": [
        "priority",
        "window",
        "task_type",
        "review_scope",
        "object",
        "code",
        "name",
        "entry_date",
        "task_status",
        "formal_trade_required",
        "action",
        "acceptance",
        "fallback",
        "review_method",
        "source",
        "review_key",
        "ticket_key",
    ],
    "review_coverage_dashboard.csv": [
        "priority",
        "review_scope",
        "task_type",
        "total_count",
        "covered_count",
        "pending_count",
        "issue_found_count",
        "continue_watch_count",
        "formal_required_count",
        "coverage_pct",
        "coverage_status",
        "next_action",
    ],
    "live_review_evidence_rubric.csv": [
        "priority",
        "review_scope",
        "task_type",
        "review_axes",
        "task_count",
        "formal_required_count",
        "pending_count",
        "evidence_required",
        "decision_rule",
        "optimization_boundary",
        "promotion_rule",
        "next_action",
    ],
    "live_premarket_command_sheet.csv": [
        "priority",
        "command_window",
        "command_type",
        "decision_gate",
        "review_scope",
        "object",
        "code",
        "name",
        "entry_date",
        "current_status",
        "ledger_key",
        "formal_trade_required",
        "blocks_live_buy",
        "unlock_status",
        "action",
        "acceptance",
        "evidence_required",
        "optimization_boundary",
        "post_action_check",
        "fallback",
        "source",
        "review_key",
        "ticket_key",
    ],
    "live_admission_snapshot.csv": [
        "generated_at",
        "next_trade_entry_date",
        "admission_status",
        "admission_label",
        "live_buy_allowed",
        "paper_execution_allowed",
        "auto_order_allowed",
        "ticket_count",
        "blocking_command_count",
        "formal_command_count",
        "first_blocking_key",
        "first_blocking_scope",
        "first_required_action",
        "execution_posture",
        "primary_reason",
        "next_step",
        "verification_rule",
        "risk_tone",
    ],
    "live_blocker_resolution_plan.csv": [
        "priority",
        "blocking_key",
        "review_scope",
        "object",
        "entry_date",
        "action",
        "source",
        "current_status",
        "resolution_type",
        "execution_owner",
        "can_auto_trigger",
        "requires_manual_confirmation",
        "blocks_live_buy",
        "recommended_ui_action",
        "recommended_api_action",
        "evidence_required",
        "completion_check",
        "fallback",
        "natural_trade_boundary",
    ],
    "live_blocker_evidence_ledger.csv": [
        "priority",
        "review_key",
        "blocking_key",
        "review_scope",
        "object",
        "source",
        "action",
        "resolution_type",
        "evidence_status",
        "review_result",
        "review_result_label",
        "issue_area",
        "evidence",
        "evidence_required",
        "review_note",
        "updated_at",
        "recommended_ui_action",
        "next_action",
        "completion_check",
        "natural_trade_boundary",
    ],
    "live_premarket_action_sequence.csv": [
        "step",
        "action_group",
        "action_label",
        "blocker_count",
        "primary_blocking_key",
        "review_scope",
        "object",
        "entry_date",
        "source",
        "action",
        "resolution_types",
        "evidence_statuses",
        "can_execute_now",
        "requires_manual_confirmation",
        "recommended_ui_action",
        "recommended_api_action",
        "expected_effect",
        "stop_if_fail",
        "completion_check",
        "natural_trade_boundary",
    ],
    "live_premarket_execution_recheck.csv": [
        "generated_at",
        "recheck_status",
        "recheck_label",
        "live_buy_allowed",
        "blocking_command_count",
        "sequence_step_count",
        "ready_step_count",
        "pending_evidence_count",
        "validated_evidence_count",
        "issue_evidence_count",
        "current_step",
        "current_action_group",
        "current_action_label",
        "current_can_execute_now",
        "next_operator_action",
        "recheck_rule",
        "natural_trade_boundary",
    ],
    "live_premarket_action_attempts.csv": [
        "priority",
        "attempted_at",
        "action_group",
        "action_label",
        "source",
        "ok",
        "sync_ok",
        "review_ok",
        "sync_mode",
        "sync_message",
        "holdings_count",
        "review_key",
        "review_result",
        "review_status",
        "review_axis",
        "review_scope",
        "issue_area",
        "review_returncode",
        "live_admission_status",
        "live_admission_buy_allowed",
        "live_admission_blocking_command_count",
        "live_premarket_next_action",
        "formal_buy_signal",
        "auto_order_allowed",
        "order_path_enabled",
        "evidence_boundary",
    ],
    "live_manual_launch_acceptance.csv": [
        "priority",
        "acceptance_item",
        "acceptance_label",
        "status",
        "status_label",
        "is_hard_blocker",
        "must_pass_before_live",
        "current_value",
        "required_value",
        "evidence",
        "next_action",
        "natural_trade_boundary",
    ],
    "live_day1_review_journal.csv": [
        "priority",
        "journal_window",
        "checkpoint_time",
        "journal_type",
        "source_table",
        "object",
        "code",
        "name",
        "entry_date",
        "review_axis",
        "status",
        "is_blocking",
        "required_before_live",
        "action",
        "evidence_to_record",
        "pass_condition",
        "fail_condition",
        "next_action",
        "natural_trade_boundary",
        "review_key",
        "ticket_key",
    ],
    "no_trade_day_review.csv": [
        "rank",
        "review_key",
        "entry_date",
        "review_type",
        "posture",
        "formal_trade_required",
        "evidence",
        "hidden_risk",
        "natural_decision",
        "next_action",
        "source",
        "review_status",
        "review_result",
        "review_result_label",
        "issue_area",
        "review_note",
        "optimization_suggestion",
        "review_updated_at",
    ],
}


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


def _write_report_csv(name: str, df: pd.DataFrame) -> None:
    out = df
    if out.empty and len(out.columns) == 0:
        out = pd.DataFrame(columns=EMPTY_REPORT_COLUMNS.get(name, []))
    out.to_csv(OUT_DIR / name, index=False, encoding="utf-8-sig")


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


def _is_institutional_mainwave(row: dict[str, Any]) -> bool:
    route = str(row.get("route") or "")
    strategy = str(row.get("trade_strategy") or row.get("strategy") or "")
    return (
        route == "institutional_mainwave"
        or strategy in {"institutional_score120_mainwave", "机构主升Score120"}
        or "机构主升" in strategy
    )


def _institutional_mom60_contract_block(row: dict[str, Any]) -> str:
    if not _is_institutional_mainwave(row):
        return ""
    heat = str(row.get("index_mom60_heat_state") or "")
    block_reason = str(row.get("block_reason") or "")
    mom60 = _safe_float(row.get("index_mom60"), None)
    if (
        heat in {"high_heat_reduce_position", "high_heat_observe", "institutional_mom60_gt_5_block"}
        or "mom60_gt_5" in heat
        or "mom60_gt_5" in block_reason
        or (mom60 is not None and mom60 > 0.05)
    ):
        return "机构主升当前合同要求 index_mom60<=5%；>5% 只能观察/阻断，不进入买入或纸面执行"
    return ""


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
    contract_block = _institutional_mom60_contract_block(row)
    if all_mainwave and contract_block:
        return {
            "sector_pair_count": len(same_sector_rows),
            "same_sector_codes": codes,
            "portfolio_decision_level": "block",
            "portfolio_decision": "主升共振不放行高热度合同阻断",
            "portfolio_decision_reason": f"{sector} 同板块 {len(same_sector_rows)} 张虽均为机构主升，但 {contract_block}",
        }
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
        contract_block = _institutional_mom60_contract_block(row)
        if contract_block:
            risks.append(contract_block)
            questions.append("这张票只能进入观察和盘后归因，不能写入买入或 Day1 纸面执行。")

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
        formal_ready = review_action == "manual_approved" and risk_acknowledged and confirmation_complete and not contract_block
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
        if contract_block:
            action = "contract_block: institutional_mainwave index_mom60>5%, observe only; no buy or Day1 paper execution"
            formal_ready_reason = contract_block
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
            "contract_block": bool(contract_block),
            "contract_block_reason": contract_block,
            "index_mom60": row.get("index_mom60"),
            "index_mom60_heat_state": row.get("index_mom60_heat_state"),
            "block_reason": row.get("block_reason"),
            "model_switch_assessment": model_switch,
            "manual_approval_checklist": _join_unique(approval_items),
            "hidden_risks": _join_unique(risks),
            "manual_questions": _join_unique(questions),
            "ticket_warnings": review.get("warnings"),
            "action_recommendation": action,
        }
        item["checklist_level"] = "block" if contract_block else _ticket_checklist_level(risks, review_action)
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
    review_map = _read_candidate_omission_review_map()
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
        ticket_key = _ticket_key_from_row(
            {
                "ticket_key": row.get("ticket_key"),
                "candidate_key": row.get("candidate_key"),
                "trade_key": row.get("trade_key"),
                "entry_date": _date_text(row.get("entry_date") or row.get("trade_date")),
                "route": route,
                "code": code,
            }
        )
        review = review_map.get(ticket_key, {})
        watch_result = str(review.get("watch_result") or "").strip()
        item = {
            "rank": idx,
            "code": row.get("code"),
            "name": row.get("name"),
            "entry_date": _date_text(row.get("entry_date") or row.get("trade_date")),
            "route": route,
            "strategy": _route_label(row),
            "source": "candidate_omission_checklist",
            "ticket_key": ticket_key,
            "selected_next_trade": selected,
            "router_eligible": router_eligible,
            "natural_action": row.get("natural_action"),
            "natural_context_tag": natural_tag,
            "selection_state": selection_state,
            "strategy_switch_assessment": strategy_switch,
            "hidden_risks": _join_unique(risks),
            "manual_questions": _join_unique(questions),
            "action_recommendation": action,
            "omission_watch_status": "pending_observation"
            if not watch_result
            else "validated"
            if watch_result == "as_expected"
            else "continue_watch"
            if watch_result == "continue_watch"
            else "issue_found",
            "watch_result": watch_result,
            "watch_result_label": review.get("watch_result_label") or ("待观察" if not watch_result else watch_result),
            "issue_area": review.get("issue_area") or "",
            "review_context": review.get("review_context") or "",
            "review_source": review.get("source") or "",
            "review_note": review.get("review_note") or "",
            "optimization_suggestion": review.get("optimization_suggestion") or "",
            "review_selection_state": review.get("selection_state") or "",
            "review_strategy_switch_assessment": review.get("strategy_switch_assessment") or "",
            "review_candidate_block_reason": review.get("candidate_block_reason") or "",
            "review_action_recommendation": review.get("action_recommendation") or "",
            "review_updated_at": review.get("updated_at") or "",
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


def _build_no_trade_day_review(
    current: dict[str, Any],
    next_trade_tickets: list[dict[str, Any]],
    candidate_checklist: pd.DataFrame,
    gates: pd.DataFrame,
    holding_refresh_evidence: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    review_map = _read_no_trade_day_review_map()
    date_display = current.get("date_display_analysis") if isinstance(current.get("date_display_analysis"), dict) else {}
    entry_date = _date_text(
        current.get("next_trade_entry_date")
        or date_display.get("display_entry_date")
        or current.get("entry_date")
        or (current.get("summary") or {}).get("entry_date")
    )

    def add(
        review_type: str,
        posture: str,
        formal_required: bool,
        evidence: str,
        hidden_risk: str,
        natural_decision: str,
        next_action: str,
        source: str,
    ) -> None:
        base = {
            "entry_date": entry_date,
            "review_type": review_type,
            "source": source,
        }
        review_key = _no_trade_day_review_key(base)
        review = review_map.get(review_key, {})
        review_result = str(review.get("review_result") or "").strip()
        rows.append(
            {
                "rank": len(rows) + 1,
                "review_key": review_key,
                "entry_date": entry_date,
                "review_type": review_type,
                "posture": posture,
                "formal_trade_required": formal_required,
                "evidence": evidence,
                "hidden_risk": hidden_risk,
                "natural_decision": natural_decision,
                "next_action": next_action,
                "source": source,
                "review_status": review.get("review_status") or ("pending_review" if not review_result else "reviewed"),
                "review_result": review_result,
                "review_result_label": review.get("review_result_label") or ("待复盘" if not review_result else review_result),
                "issue_area": review.get("issue_area") or "",
                "review_note": review.get("review_note") or "",
                "optimization_suggestion": review.get("optimization_suggestion") or "",
                "review_updated_at": review.get("updated_at") or "",
            }
        )

    if not next_trade_tickets:
        diagnosis = str(current.get("diagnosis_code") or current.get("diagnosis") or "NO_TRADE_TICKET")
        candidate_count = len(current.get("all_source_candidates") or [])
        add(
            "no_final_ticket",
            "observe_no_buy",
            False,
            f"diagnosis={diagnosis}; source_candidates={candidate_count}; next_trade_tickets=0",
            "无票日不能被简单视为失败，需要区分自然空仓、盘中确认缺口、数据缺口和过热阻断。",
            "保持空仓/不新开仓；不为了补满二槽而降低确认门槛。",
            "盘前只处理持仓刷新和候选观察；若盘中确认补齐，重新跑 G3 审计后再判断。",
            "current_payload",
        )

    if not candidate_checklist.empty:
        router_eligible = candidate_checklist.get("router_eligible", pd.Series(dtype=bool)).map(_truthy)
        selected = candidate_checklist.get("selected_next_trade", pd.Series(dtype=bool)).map(_truthy)
        waiting = candidate_checklist[~router_eligible]
        repair_waiting = waiting[
            waiting.get("route", pd.Series(dtype=str)).astype(str).eq("panic_repair")
            | waiting.get("hidden_risks", pd.Series(dtype=str)).astype(str).str.contains("修复", na=False)
        ]
        if not repair_waiting.empty:
            codes = "、".join([f"{row.get('code')} {row.get('name')}" for row in repair_waiting.head(8).to_dict("records")])
            add(
                "repair_candidates_wait_intraday_confirm",
                "observation_pool",
                False,
                f"{len(repair_waiting)} 个修复候选未通过路由准入：{codes}",
                "修复票在未补齐盘中确认时容易把弱反弹误判成可交易补位。",
                "修复候选只做遗漏观察池，不替代机构主升，也不因为当日无票而强行补位。",
                "优先补齐盘中确认/分钟线证据；盘后复盘这些候选是否误杀，而不是盘前直接买入。",
                "candidate_omission_checklist",
            )
        eligible_unselected = candidate_checklist[router_eligible & ~selected]
        if not eligible_unselected.empty:
            codes = "、".join([f"{row.get('code')} {row.get('name')}" for row in eligible_unselected.head(8).to_dict("records")])
            add(
                "eligible_candidate_not_selected",
                "manual_omission_review",
                False,
                f"{len(eligible_unselected)} 个候选已过准入但未入最终票据：{codes}",
                "可能存在排序、槽位、板块暴露或策略切换规则误伤。",
                "先复核排序和组合约束，不直接放宽仓位或二槽规则。",
                "逐票检查未入选原因；只有确认规则误伤后才进入策略调优队列。",
                "candidate_omission_checklist",
            )

    if not holding_refresh_evidence.empty:
        formal_pending = holding_refresh_evidence[
            holding_refresh_evidence.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy)
        ]
        if not formal_pending.empty:
            codes = "、".join([f"{row.get('code')} {row.get('name')}" for row in formal_pending.head(8).to_dict("records")])
            add(
                "holding_refresh_before_new_open",
                "must_refresh_before_live",
                True,
                f"{len(formal_pending)} 个正式持仓刷新项：{codes}",
                "退出价格不新鲜时，新开仓会挤占真实账户风险预算，导致卖点/买点合同互相污染。",
                "先刷新真实持仓与退出判断，再考虑任何新开仓。",
                "执行手动真实持仓刷新；若刷新失败，保持 not_formal_ready。",
                "holding_refresh_evidence",
            )

    if not gates.empty:
        gate_ok = gates.get("ok", pd.Series(dtype=bool)).map(_truthy)
        gate_severity = gates.get("severity", pd.Series(dtype=str)).astype(str)
        gate_warn = gates[(~gate_ok) & gate_severity.eq("warn")]
        if not gate_warn.empty:
            evidence = "；".join([f"{row.get('gate')}={row.get('message')}" for row in gate_warn.to_dict("records")])
            formal_gate_warn = gate_warn[
                gate_warn.get("gate", pd.Series(dtype=str)).astype(str).ne("has_next_trade_ticket")
            ]
            add(
                "readiness_warning_context",
                "manual_check_required",
                not formal_gate_warn.empty,
                evidence,
                "warn 不是硬阻断，但实战第一天会放大操作噪声。",
                "先把正式动作类 warn 清掉，观察类 warn 保留复盘即可。",
                "盘前按 formal_trade_required 区分必须处理与只观察项。",
                "readiness_gates",
            )

    return pd.DataFrame(rows)


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
            gate_name = str(row.get("gate") or "")
            if gate_name == "has_next_trade_ticket":
                formal_required = False
            if gate_name == "holding_exit_price_fresh" and not holding_checklist.empty:
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
        True,
        "watch",
        f"next_trade_ticket_count={ticket_count}",
        "没有下一交易日票据时不进入实盘买入流程",
    )
    add(
        "all_tickets_formally_reviewed",
        ticket_count == 0 or formal_ready == ticket_count,
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
    gates: pd.DataFrame,
) -> pd.DataFrame:
    capital = broker.get("capital") if isinstance(broker.get("capital"), dict) else {}
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
    return pd.DataFrame(rows)


def _build_formal_launch_action_queue(
    action_checklist: pd.DataFrame,
    ticket_checklist: pd.DataFrame,
    formal_launch_checklist: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    review_map = _read_formal_action_review_map()

    def add(stage: str, obj: str, action: str, unlocks: str, source: str, severity: str = "warn") -> None:
        base = {
            "object": obj or "--",
            "action": action or "--",
            "source": source,
        }
        review_key = _formal_action_review_key(base)
        review = review_map.get(review_key, {})
        review_status = str(review.get("review_status") or "pending").strip()
        rows.append(
            {
                "priority": len(rows) + 1,
                "stage": stage,
                "severity": severity,
                "object": base["object"],
                "action": base["action"],
                "unlocks": unlocks or "--",
                "source": source,
                "review_key": review_key,
                "action_status": review_status,
                "review_result": review.get("review_result"),
                "review_result_label": review.get("review_result_label"),
                "issue_area": review.get("issue_area") or severity,
                "evidence": review.get("evidence"),
                "natural_decision": review.get("natural_decision"),
                "optimization_suggestion": review.get("optimization_suggestion"),
                "review_note": review.get("review_note"),
                "updated_at": review.get("updated_at"),
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


def _read_candidate_omission_review_map() -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for key, value in _read_paper_watch_review_map().items():
        if str(value.get("review_context") or "").strip() == "candidate_omission":
            merged[str(key)] = value
    try:
        data = json.loads(CANDIDATE_OMISSION_REVIEWS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return merged
    rows = data.get("reviews") if isinstance(data, dict) else {}
    if not isinstance(rows, dict):
        return merged
    for key, value in rows.items():
        if isinstance(value, dict):
            merged[str(key)] = value
    return merged


def _read_no_trade_day_review_map() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(NO_TRADE_DAY_REVIEWS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = data.get("reviews") if isinstance(data, dict) else {}
    if not isinstance(rows, dict):
        return {}
    return {str(key): value for key, value in rows.items() if isinstance(value, dict)}


def _read_formal_action_review_map() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(FORMAL_ACTION_REVIEWS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = data.get("reviews") if isinstance(data, dict) else {}
    if not isinstance(rows, dict):
        return {}
    return {str(key): value for key, value in rows.items() if isinstance(value, dict)}


def _read_daily_review_checklist_review_map() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = data.get("reviews") if isinstance(data, dict) else {}
    if not isinstance(rows, dict):
        return {}
    return {str(key): value for key, value in rows.items() if isinstance(value, dict)}


def _read_premarket_action_attempts() -> list[dict[str, Any]]:
    try:
        data = json.loads(PREMARKET_ACTION_ATTEMPTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.get("attempts") if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _formal_action_review_key(row: dict[str, Any]) -> str:
    key = str(row.get("review_key") or row.get("key") or "").strip()
    if key:
        return key
    return "|".join(
        [
            str(row.get("source") or "").strip(),
            str(row.get("object") or row.get("action_object") or "").strip(),
            str(row.get("action") or row.get("required_action") or "").strip(),
        ]
    )


def _no_trade_day_review_key(row: dict[str, Any]) -> str:
    key = str(row.get("review_key") or row.get("key") or "").strip()
    if key:
        return key
    return "|".join(
        [
            str(row.get("entry_date") or "").strip(),
            str(row.get("review_type") or "").strip(),
            str(row.get("source") or "").strip(),
        ]
    )


def _daily_review_checklist_review_key(row: dict[str, Any]) -> str:
    axis = str(row.get("review_axis") or "").strip()
    for key in ("review_key", "key", "ticket_key"):
        value = str(row.get(key) or "").strip()
        if value:
            return f"{axis}|{value}" if axis and not value.startswith(f"{axis}|") else value
    return "|".join(
        [
            axis,
            str(row.get("review_scope") or "").strip(),
            str(row.get("object") or "").strip(),
            str(row.get("code") or "").strip(),
            str(row.get("entry_date") or "").strip(),
        ]
    )


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


def _index_by_code(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    if df.empty or "code" not in df.columns:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in df.to_dict("records"):
        code = str(row.get("code") or "").strip()
        if code:
            out[code] = row
    return out


def _build_day1_paper_review_pack(
    ticket_review: pd.DataFrame,
    pretrade_review_evidence: pd.DataFrame,
    natural_consistency: pd.DataFrame,
    natural_execution_matrix: pd.DataFrame,
    ticket_checklist: pd.DataFrame,
) -> pd.DataFrame:
    if ticket_review.empty and pretrade_review_evidence.empty:
        return pd.DataFrame([])

    evidence_by_code = _index_by_code(pretrade_review_evidence)
    consistency_by_code = _index_by_code(natural_consistency)
    execution_by_code = _index_by_code(natural_execution_matrix)
    checklist_by_code = _index_by_code(ticket_checklist)
    source_rows = ticket_review.to_dict("records") if not ticket_review.empty else pretrade_review_evidence.to_dict("records")
    rows: list[dict[str, Any]] = []

    for idx, row in enumerate(source_rows, start=1):
        code = str(row.get("code") or "").strip()
        evidence = evidence_by_code.get(code, {})
        consistency = consistency_by_code.get(code, {})
        execution = execution_by_code.get(code, {})
        checklist = checklist_by_code.get(code, {})
        contract_source = {**row, **evidence, **checklist}
        contract_block = _institutional_mom60_contract_block(contract_source)
        if not contract_block and _truthy(checklist.get("contract_block")):
            contract_block = str(checklist.get("contract_block_reason") or "机构主升当前合同阻断：只观察，不进入买入或纸面执行")
        review_action = str(
            row.get("pretrade_review_action")
            or row.get("review_action")
            or evidence.get("review_action")
            or ""
        ).strip()
        if contract_block:
            posture = "contract_block_observation"
            action = "只做观察和盘后归因，不写 Day1 纸面执行；需等待 index_mom60<=5% 后重新出票"
        elif review_action == "manual_approved" and _truthy(evidence.get("formal_ready")):
            posture = "formal_candidate_after_manual_review"
            action = "盘前再次核对真实持仓、价格、止损、止盈和账户现金；仍由人工决定是否执行"
        elif review_action == "paper_watch":
            posture = "paper_watch"
            action = "按纸面观察记录开盘、盘中、收盘三段证据，盘后填写后评估"
        elif review_action == "wait_refresh":
            posture = "wait_refresh"
            action = "等待持仓、价格或信号刷新后重跑审计，不做买入动作"
        elif review_action in {"skip", "reject"}:
            posture = "skip"
            action = "本轮不跟踪买入，只保留是否误杀的盘后观察"
        else:
            posture = "unreviewed_to_paper_watch"
            action = "先转入纸面观察；不得因为历史收益压力直接实盘放行"

        hidden_focus = _join_unique(
            [
                str(row.get("warnings") or ""),
                str(row.get("notes") or ""),
                str(checklist.get("hidden_risks") or ""),
                str(consistency.get("deductions") or ""),
                str(execution.get("source_deductions") or ""),
            ]
        )
        rows.append(
            {
                "rank": idx,
                "code": row.get("code"),
                "name": row.get("name"),
                "entry_date": row.get("entry_date") or evidence.get("entry_date"),
                "route": row.get("route") or checklist.get("route"),
                "strategy": row.get("strategy") or checklist.get("strategy"),
                "pretrade_status": evidence.get("status") or row.get("pretrade_review_label") or "未复盘",
                "launch_posture": posture,
                "priority": "P0" if posture == "formal_candidate_after_manual_review" else "P2" if posture == "contract_block_observation" else "P1",
                "buy_point_review": consistency.get("buy_point_logic") or checklist.get("buy_point_state") or "观察买点是否新鲜、是否追高、是否脱离30m确认",
                "selection_review": consistency.get("selection_logic") or checklist.get("selection_state") or "观察选股是否来自主升核心，而非被热度或板块重复暴露牵引",
                "model_switch_review": consistency.get("model_switch_logic") or checklist.get("strategy_switch_assessment") or "观察路由切换是否顺畅，G2补位不得替代主升逻辑",
                "exit_contract_review": consistency.get("sell_point_logic") or row.get("exit_contract") or "记录硬止损、结构止损、第一止盈和前低保护是否可执行",
                "portfolio_review": row.get("portfolio_decision") or checklist.get("portfolio_decision") or "确认二槽、同板块共振和真实账户剩余仓位约束",
                "paper_watch_action": action,
                "contract_block_reason": contract_block,
                "after_close_required_note": "盘后归因到 buy_point / selection / model_switch / sell_exit / signal_validation，不按单日收益倒推调参",
                "hidden_risk_focus": hidden_focus or "重点观察买点自然度、同板块拥挤、热度追高和退出合同是否通畅",
                "next_action": action,
                "ticket_key": row.get("ticket_key") or evidence.get("ticket_key"),
            }
        )
    return pd.DataFrame(rows)


def _read_paper_execution_rows() -> list[dict[str, Any]]:
    try:
        data = json.loads(PAPER_EXECUTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = data.get("executions") if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def _execution_matches_review_row(execution: dict[str, Any], row: dict[str, Any]) -> bool:
    execution_key = str(execution.get("ticket_key") or "").strip()
    row_key = _ticket_key_from_row(row)
    if execution_key and row_key and execution_key == row_key:
        return True
    execution_code = str(execution.get("code") or "").strip().upper()
    row_code = str(row.get("code") or "").strip().upper()
    execution_date = str(execution.get("entry_date") or "")[:10]
    row_date = str(row.get("entry_date") or "")[:10]
    return bool(execution_code and row_code and execution_code == row_code and execution_date and row_date and execution_date == row_date)


def _build_day1_after_close_review_queue(day1_pack: pd.DataFrame, paper_watch_followup: pd.DataFrame) -> pd.DataFrame:
    execution_rows = _read_paper_execution_rows()
    review_map = _read_paper_watch_review_map()
    followup_by_code = _index_by_code(paper_watch_followup)
    source_rows = day1_pack.to_dict("records") if not day1_pack.empty else []
    if not source_rows:
        for execution in execution_rows:
            if str(execution.get("source") or "") != "g3_day1_paper_review_pack":
                continue
            source_rows.append(
                {
                    "code": execution.get("code"),
                    "name": execution.get("name"),
                    "entry_date": execution.get("entry_date"),
                    "ticket_key": execution.get("ticket_key"),
                    "route": execution.get("route"),
                    "strategy": execution.get("contract"),
                    "launch_posture": "paper_executed",
                }
            )

    rows: list[dict[str, Any]] = []
    for idx, row in enumerate(source_rows, start=1):
        key = _ticket_key_from_row(row)
        execution = next((item for item in execution_rows if _execution_matches_review_row(item, row)), {})
        review = review_map.get(key, {})
        code = str(row.get("code") or execution.get("code") or "").strip()
        followup = followup_by_code.get(code, {})
        watch_result = str(review.get("watch_result") or followup.get("watch_result") or "").strip()
        if str(row.get("launch_posture") or "") == "contract_block_observation":
            status = "contract_block_observation"
            next_action = "只做观察和盘后归因，不写纸面执行；等待 index_mom60<=5% 后由策略重新出票"
        elif not execution:
            status = "pending_paper_execution"
            next_action = "先在风控合同页写入Day1纸面执行，再进入盘后归因"
        elif not watch_result:
            status = "pending_after_close_review"
            next_action = "盘后填写观察结果：符合预期、买点偏急、选股隐患、策略切换隐患、卖点/风控隐患或继续观察"
        elif watch_result == "as_expected":
            status = "validated"
            next_action = "保留逻辑，不因单日波动调参，继续观察同类信号复现"
        elif watch_result == "continue_watch":
            status = "continue_watch"
            next_action = "延长观察窗口，等待更多价格、量能和退出合同证据"
        else:
            status = "issue_found"
            next_action = "进入调优队列：先解释行为问题，再决定是否改规则或参数"
        rows.append(
            {
                "rank": idx,
                "code": row.get("code") or execution.get("code"),
                "name": row.get("name") or execution.get("name"),
                "entry_date": row.get("entry_date") or execution.get("entry_date"),
                "ticket_key": key,
                "execution_id": execution.get("execution_id"),
                "paper_execution_status": execution.get("status") or ("missing" if not execution else ""),
                "execution_price": execution.get("execution_price"),
                "quantity": execution.get("quantity"),
                "notional": execution.get("notional"),
                "launch_posture": row.get("launch_posture"),
                "after_close_status": status,
                "watch_result": watch_result,
                "watch_result_label": review.get("watch_result_label") or followup.get("watch_result_label") or ("待盘后复盘" if not watch_result else watch_result),
                "issue_area": review.get("issue_area") or followup.get("issue_area") or "observation",
                "buy_point_review": row.get("buy_point_review"),
                "selection_review": row.get("selection_review"),
                "model_switch_review": row.get("model_switch_review"),
                "exit_contract_review": row.get("exit_contract_review"),
                "hidden_risk_focus": row.get("hidden_risk_focus") or review.get("hidden_risk") or followup.get("hidden_risk"),
                "review_note": review.get("review_note") or followup.get("review_note"),
                "optimization_suggestion": review.get("optimization_suggestion") or followup.get("optimization_suggestion"),
                "next_action": next_action,
            }
        )
    return pd.DataFrame(rows)


def _review_board_status_level(status: Any) -> int:
    text = str(status or "").strip()
    if text in {"block", "issue_found", "pending_execution_fix"}:
        return 0
    if text in {"pending_review", "pending_observation", "pending_after_close_review", "pending_paper_execution"}:
        return 1
    if text in {"continue_watch", "warn", "watch"}:
        return 2
    if text in {"validated", "pass", "ready"}:
        return 4
    return 3


def _build_daily_live_review_board(
    formal_launch_action_queue: pd.DataFrame,
    no_trade_day_review: pd.DataFrame,
    candidate_checklist: pd.DataFrame,
    day1_paper_review_pack: pd.DataFrame,
    day1_after_close_review_queue: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(
        stage: str,
        review_scope: str,
        obj: str,
        code: Any = "",
        name: Any = "",
        entry_date: Any = "",
        review_status: str = "",
        formal_trade_required: bool = False,
        issue_area: Any = "",
        required_action: Any = "",
        evidence: Any = "",
        natural_decision: Any = "",
        optimization_focus: Any = "",
        source: Any = "",
        review_key: Any = "",
        ticket_key: Any = "",
    ) -> None:
        rows.append(
            {
                "priority": 0,
                "stage": stage,
                "review_scope": review_scope,
                "object": obj,
                "code": code,
                "name": name,
                "entry_date": entry_date,
                "review_status": review_status,
                "formal_trade_required": bool(formal_trade_required),
                "issue_area": issue_area,
                "required_action": required_action,
                "evidence": evidence,
                "natural_decision": natural_decision,
                "optimization_focus": optimization_focus,
                "source": source,
                "review_key": review_key,
                "ticket_key": ticket_key,
            }
        )

    for row in formal_launch_action_queue.to_dict("records") if not formal_launch_action_queue.empty else []:
        add(
            "盘前正式动作",
            "formal_action",
            row.get("object"),
            review_status=row.get("action_status") or "pending",
            formal_trade_required=True,
            issue_area=row.get("issue_area") or row.get("severity"),
            required_action=row.get("action"),
            evidence=row.get("review_note") or row.get("evidence") or row.get("unlocks"),
            natural_decision=row.get("natural_decision") or row.get("review_result_label"),
            optimization_focus=row.get("optimization_suggestion") or "先处理运行/持仓/复盘手续，不把流程缺口误判为交易模型问题。",
            source=row.get("source") or "formal_launch_action_queue",
            review_key=row.get("review_key"),
        )

    for row in no_trade_day_review.to_dict("records") if not no_trade_day_review.empty else []:
        add(
            "无票日复盘",
            "no_trade_day",
            row.get("review_type"),
            entry_date=row.get("entry_date"),
            review_status=row.get("review_status") or "pending_review",
            formal_trade_required=_truthy(row.get("formal_trade_required")),
            issue_area=row.get("issue_area"),
            required_action=row.get("next_action"),
            evidence=row.get("evidence"),
            natural_decision=row.get("natural_decision"),
            optimization_focus=row.get("optimization_suggestion") or row.get("hidden_risk"),
            source=row.get("source"),
            review_key=row.get("review_key"),
        )

    for row in candidate_checklist.to_dict("records") if not candidate_checklist.empty else []:
        add(
            "候选遗漏",
            "candidate_omission",
            row.get("strategy") or row.get("route"),
            code=row.get("code"),
            name=row.get("name"),
            entry_date=row.get("entry_date"),
            review_status=row.get("omission_watch_status") or "pending_observation",
            formal_trade_required=False,
            issue_area=row.get("issue_area"),
            required_action=row.get("action_recommendation"),
            evidence=row.get("selection_state"),
            natural_decision=row.get("strategy_switch_assessment"),
            optimization_focus=row.get("optimization_suggestion") or row.get("hidden_risks"),
            source=row.get("source") or "candidate_omission_checklist",
            ticket_key=row.get("ticket_key"),
        )

    for row in day1_paper_review_pack.to_dict("records") if not day1_paper_review_pack.empty else []:
        add(
            "Day1纸面",
            "day1_paper_pack",
            row.get("launch_posture"),
            code=row.get("code"),
            name=row.get("name"),
            entry_date=row.get("entry_date"),
            review_status=row.get("launch_posture") or "pending",
            formal_trade_required=False,
            required_action=row.get("next_action"),
            evidence=row.get("buy_point_review"),
            natural_decision=row.get("model_switch_review"),
            optimization_focus=row.get("hidden_risk_focus") or row.get("selection_review"),
            source="day1_paper_review_pack",
            ticket_key=row.get("ticket_key"),
        )

    for row in day1_after_close_review_queue.to_dict("records") if not day1_after_close_review_queue.empty else []:
        add(
            "Day1盘后",
            "day1_after_close",
            row.get("after_close_status"),
            code=row.get("code"),
            name=row.get("name"),
            entry_date=row.get("entry_date"),
            review_status=row.get("after_close_status") or "pending_after_close_review",
            formal_trade_required=False,
            issue_area=row.get("issue_area"),
            required_action=row.get("next_action"),
            evidence=row.get("review_note") or row.get("after_close_required_note"),
            natural_decision=row.get("watch_result_label"),
            optimization_focus=row.get("hidden_risk_focus") or row.get("optimization_suggestion"),
            source="day1_after_close_review_queue",
            ticket_key=row.get("ticket_key"),
        )

    if not rows:
        return pd.DataFrame([])
    out = pd.DataFrame(rows)
    out["_status_level"] = out["review_status"].map(_review_board_status_level)
    out["_formal_sort"] = out["formal_trade_required"].map(lambda x: 0 if _truthy(x) else 1)
    out = out.sort_values(["_formal_sort", "_status_level", "stage", "code"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_status_level", "_formal_sort"])


def _learning_status_from_review(status: Any) -> str:
    text = str(status or "").strip()
    if text in {"issue_found", "block"}:
        return "needs_review"
    if text == "continue_watch":
        return "watch_more"
    if text == "validated":
        return "validated_rule"
    return ""


def _build_strategy_learning_backlog(
    no_trade_day_review: pd.DataFrame,
    candidate_checklist: pd.DataFrame,
    paper_watch_followup: pd.DataFrame,
    day1_after_close_review_queue: pd.DataFrame,
    formal_action_reviews_report: pd.DataFrame,
    daily_review_checklist_reviews_report: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(
        learning_status: str,
        issue_area: Any,
        review_scope: str,
        obj: Any,
        code: Any = "",
        name: Any = "",
        entry_date: Any = "",
        problem_signal: Any = "",
        evidence: Any = "",
        suggested_learning: Any = "",
        source: Any = "",
        review_key: Any = "",
        ticket_key: Any = "",
    ) -> None:
        if not learning_status:
            return
        rows.append(
            {
                "priority": 0,
                "learning_status": learning_status,
                "issue_area": issue_area or "observation",
                "review_scope": review_scope,
                "object": obj,
                "code": code,
                "name": name,
                "entry_date": entry_date,
                "problem_signal": problem_signal,
                "evidence": evidence,
                "suggested_learning": suggested_learning,
                "source": source,
                "review_key": review_key,
                "ticket_key": ticket_key,
            }
        )

    for row in no_trade_day_review.to_dict("records") if not no_trade_day_review.empty else []:
        status = _learning_status_from_review(row.get("review_status"))
        if status not in {"needs_review", "watch_more"}:
            continue
        add(
            status,
            row.get("issue_area"),
            "no_trade_day",
            row.get("review_type"),
            entry_date=row.get("entry_date"),
            problem_signal=row.get("review_result_label") or row.get("review_result"),
            evidence=row.get("review_note") or row.get("evidence"),
            suggested_learning=row.get("optimization_suggestion") or row.get("hidden_risk"),
            source=row.get("source") or "no_trade_day_review",
            review_key=row.get("review_key"),
        )

    for row in candidate_checklist.to_dict("records") if not candidate_checklist.empty else []:
        status = _learning_status_from_review(row.get("omission_watch_status"))
        if status not in {"needs_review", "watch_more"}:
            continue
        add(
            status,
            row.get("issue_area") or "selection_or_switch",
            "candidate_omission",
            row.get("strategy") or row.get("route"),
            code=row.get("code"),
            name=row.get("name"),
            entry_date=row.get("entry_date"),
            problem_signal=row.get("watch_result_label") or row.get("watch_result"),
            evidence=row.get("review_note") or row.get("selection_state"),
            suggested_learning=row.get("optimization_suggestion") or row.get("hidden_risks"),
            source=row.get("review_source") or row.get("source") or "candidate_omission_checklist",
            ticket_key=row.get("ticket_key"),
        )

    for row in paper_watch_followup.to_dict("records") if not paper_watch_followup.empty else []:
        status = _learning_status_from_review(row.get("followup_status"))
        if status not in {"needs_review", "watch_more"}:
            continue
        add(
            status,
            row.get("issue_area"),
            "paper_watch_followup",
            row.get("pretrade_reason"),
            code=row.get("code"),
            name=row.get("name"),
            entry_date=row.get("entry_date"),
            problem_signal=row.get("watch_result_label") or row.get("watch_result"),
            evidence=row.get("review_note"),
            suggested_learning=row.get("optimization_suggestion") or row.get("hidden_risk"),
            source="paper_watch_followup",
            ticket_key=row.get("ticket_key"),
        )

    for row in day1_after_close_review_queue.to_dict("records") if not day1_after_close_review_queue.empty else []:
        status = _learning_status_from_review(row.get("after_close_status"))
        if status not in {"needs_review", "watch_more"}:
            continue
        add(
            status,
            row.get("issue_area"),
            "day1_after_close",
            row.get("launch_posture"),
            code=row.get("code"),
            name=row.get("name"),
            entry_date=row.get("entry_date"),
            problem_signal=row.get("watch_result_label") or row.get("watch_result"),
            evidence=row.get("review_note") or row.get("after_close_required_note"),
            suggested_learning=row.get("optimization_suggestion") or row.get("hidden_risk_focus"),
            source="day1_after_close_review_queue",
            ticket_key=row.get("ticket_key"),
        )

    for row in formal_action_reviews_report.to_dict("records") if not formal_action_reviews_report.empty else []:
        status = _learning_status_from_review(row.get("review_status"))
        if status not in {"needs_review", "watch_more"}:
            continue
        add(
            status,
            row.get("issue_area") or "execution_process",
            "formal_action",
            row.get("object"),
            problem_signal=row.get("review_result_label") or row.get("review_result"),
            evidence=row.get("review_note") or row.get("evidence"),
            suggested_learning=row.get("optimization_suggestion") or "先修复实战前动作证据链，不把执行流程缺口误判为收益优化空间",
            source=row.get("source") or "formal_action_reviews",
            review_key=row.get("review_key"),
        )

    strategy_axes = {"buy_point", "sell_point", "selection", "model_switch", "natural_trade_consistency"}
    for row in daily_review_checklist_reviews_report.to_dict("records") if not daily_review_checklist_reviews_report.empty else []:
        axis = str(row.get("review_axis") or row.get("issue_area") or "").strip()
        if axis not in strategy_axes:
            continue
        status = _learning_status_from_review(row.get("review_status"))
        if status not in {"needs_review", "watch_more"}:
            continue
        add(
            status,
            row.get("issue_area") or axis,
            "daily_review_checklist",
            row.get("object") or row.get("review_scope"),
            code=row.get("code"),
            name=row.get("name"),
            entry_date=row.get("entry_date"),
            problem_signal=row.get("review_result_label") or row.get("review_result"),
            evidence=row.get("review_note") or row.get("evidence"),
            suggested_learning=(
                row.get("optimization_suggestion")
                or row.get("natural_decision")
                or "归入逐项复盘学习队列；先确认事实和合同边界，再决定是否调整买卖点、选股或策略切换。"
            ),
            source=row.get("source") or "daily_review_checklist_reviews",
            review_key=row.get("review_key"),
            ticket_key=row.get("ticket_key"),
        )

    if not rows:
        return pd.DataFrame([])
    out = pd.DataFrame(rows)
    status_rank = {"needs_review": 0, "watch_more": 1, "validated_rule": 2}
    out["_status_rank"] = out["learning_status"].map(lambda x: status_rank.get(str(x), 9))
    out = out.sort_values(["_status_rank", "issue_area", "entry_date", "code"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_status_rank"])


def _build_live_hidden_risk_watchlist(
    daily_live_review_board: pd.DataFrame,
    live_blocker_evidence_ledger: pd.DataFrame,
    strategy_learning_backlog: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(
        watch_status: str,
        risk_level: str,
        issue_area: Any,
        review_scope: str,
        obj: Any,
        code: Any = "",
        name: Any = "",
        entry_date: Any = "",
        risk_signal: Any = "",
        evidence_gap: Any = "",
        next_review_action: Any = "",
        natural_trade_boundary: Any = "",
        source: Any = "",
        review_key: Any = "",
        ticket_key: Any = "",
    ) -> None:
        rows.append(
            {
                "priority": 0,
                "watch_status": watch_status,
                "risk_level": risk_level,
                "issue_area": issue_area or "observation",
                "review_scope": review_scope,
                "object": obj,
                "code": code,
                "name": name,
                "entry_date": entry_date,
                "risk_signal": risk_signal,
                "evidence_gap": evidence_gap,
                "next_review_action": next_review_action,
                "natural_trade_boundary": natural_trade_boundary
                or "先记录事实和证据，再决定是否调策略；不按单日收益压力反推放宽规则。",
                "source": source,
                "review_key": review_key,
                "ticket_key": ticket_key,
            }
        )

    for row in daily_live_review_board.to_dict("records") if not daily_live_review_board.empty else []:
        status = str(row.get("review_status") or "").strip()
        formal_required = _truthy(row.get("formal_trade_required"))
        if status in {"validated", "pass", "ready"}:
            continue
        if status in {"issue_found", "block"}:
            watch_status = "issue_found"
            risk_level = "high"
        elif formal_required:
            watch_status = "pending_evidence"
            risk_level = "high"
        elif status in {"pending_review", "pending_observation", "pending_after_close_review", "pending_paper_execution", "pending"}:
            watch_status = "pending_review"
            risk_level = "medium"
        elif status == "continue_watch":
            watch_status = "continue_watch"
            risk_level = "medium"
        else:
            continue
        add(
            watch_status,
            risk_level,
            row.get("issue_area"),
            row.get("review_scope"),
            row.get("object"),
            code=row.get("code"),
            name=row.get("name"),
            entry_date=row.get("entry_date"),
            risk_signal=row.get("required_action") or row.get("review_status"),
            evidence_gap=row.get("evidence") or row.get("optimization_focus"),
            next_review_action=row.get("required_action") or "补充复盘证据后重跑审计。",
            source=row.get("source") or "daily_live_review_board",
            review_key=row.get("review_key"),
            ticket_key=row.get("ticket_key"),
        )

    for row in live_blocker_evidence_ledger.to_dict("records") if not live_blocker_evidence_ledger.empty else []:
        status = str(row.get("evidence_status") or "").strip()
        if status == "validated":
            continue
        risk_level = "high" if status in {"pending_evidence", "issue_found"} else "medium"
        add(
            status or "pending_evidence",
            risk_level,
            row.get("issue_area") or row.get("resolution_type") or "operation_evidence",
            "live_blocker_evidence",
            row.get("object"),
            risk_signal=row.get("next_action") or row.get("resolution_type"),
            evidence_gap=row.get("review_note") or row.get("evidence") or row.get("completion_check"),
            next_review_action=row.get("next_action") or "补齐阻断证据并重跑审计。",
            natural_trade_boundary=row.get("natural_trade_boundary"),
            source="live_blocker_evidence_ledger",
            review_key=row.get("blocking_key"),
        )

    for row in strategy_learning_backlog.to_dict("records") if not strategy_learning_backlog.empty else []:
        status = str(row.get("learning_status") or "").strip()
        risk_level = "high" if status == "needs_review" else "medium"
        add(
            status,
            risk_level,
            row.get("issue_area"),
            row.get("review_scope"),
            row.get("object"),
            code=row.get("code"),
            name=row.get("name"),
            entry_date=row.get("entry_date"),
            risk_signal=row.get("problem_signal"),
            evidence_gap=row.get("evidence"),
            next_review_action=row.get("suggested_learning") or "进入策略学习复盘。",
            source=row.get("source") or "strategy_learning_backlog",
            review_key=row.get("review_key"),
            ticket_key=row.get("ticket_key"),
        )

    if not rows:
        return pd.DataFrame([])
    out = pd.DataFrame(rows)
    out = out.drop_duplicates(
        subset=["review_scope", "object", "code", "entry_date", "review_key", "ticket_key", "source"],
        keep="first",
    )
    risk_rank = {"high": 0, "medium": 1, "low": 2}
    status_rank = {"issue_found": 0, "pending_evidence": 1, "needs_review": 2, "pending_review": 3, "continue_watch": 4, "watch_more": 5}
    out["_risk_rank"] = out["risk_level"].map(lambda x: risk_rank.get(str(x), 9))
    out["_status_rank"] = out["watch_status"].map(lambda x: status_rank.get(str(x), 9))
    out = out.sort_values(["_risk_rank", "_status_rank", "review_scope", "entry_date", "code"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_risk_rank", "_status_rank"])


def _review_axis_for_watch(row: dict[str, Any]) -> str:
    scope = str(row.get("review_scope") or "").strip()
    issue = str(row.get("issue_area") or "").strip()
    text = " ".join(str(row.get(key) or "") for key in ["risk_signal", "evidence_gap", "next_review_action", "object"])
    if scope in {"formal_action", "live_blocker_evidence"} or issue in {"operation_evidence", "execution_process"}:
        return "execution_evidence"
    if scope == "candidate_omission" or "selection" in issue or "候选" in text or "选股" in text:
        return "selection"
    if scope == "no_trade_day" or "switch" in issue or "策略" in text or "误杀" in text:
        return "model_switch"
    if "exit" in issue or "sell" in issue or "退出" in text or "卖点" in text:
        return "sell_point"
    if "buy" in issue or "买点" in text or "追高" in text:
        return "buy_point"
    return "natural_trade_consistency"


def _daily_review_window_for_axis(axis: str, risk_level: Any) -> tuple[str, str]:
    if axis == "execution_evidence":
        return "盘前", "09:15 前"
    if axis in {"buy_point", "selection", "model_switch"}:
        return "盘中/盘后", "盘中观察，15:10 后落账"
    if axis == "sell_point":
        return "盘中/盘后", "触发退出条件后即时记录，15:10 后复核"
    if str(risk_level) == "high":
        return "盘前/盘后", "盘前确认边界，盘后落账"
    return "盘后", "15:10 后"


def _build_live_daily_review_execution_checklist(
    live_hidden_risk_watchlist: pd.DataFrame,
    daily_review_checklist_reviews_report: pd.DataFrame,
) -> pd.DataFrame:
    if live_hidden_risk_watchlist.empty:
        return pd.DataFrame([])

    axis_labels = {
        "execution_evidence": "执行证据",
        "selection": "选股模式",
        "model_switch": "策略切换",
        "sell_point": "卖点/退出",
        "buy_point": "买点",
        "natural_trade_consistency": "自然交易一致性",
    }
    pass_condition = {
        "execution_evidence": "证据补齐并重跑审计后，阻断消失或明确降级为观察；未消失前不允许真实买入。",
        "selection": "候选被挡有清晰合同理由，且后续走势没有证明核心规则误杀；否则进入选股模式复盘。",
        "model_switch": "无票或切换结果符合市场状态和合同边界；若出现可解释的漏切换，进入策略切换复盘。",
        "sell_point": "退出判断基于刷新价格和既定止损/止盈/前低保护；若价格过期或规则冲突，进入卖点修正。",
        "buy_point": "买点符合 30m 确认、热度边界和追高约束；若纸面观察显示过早/过急，进入买点调优。",
        "natural_trade_consistency": "行为符合先事实、再决策、后调优；不因单日无票或收益压力放宽规则。",
    }
    fail_condition = {
        "execution_evidence": "同步失败、审计仍阻断、证据无法落账或真实持仓/价格不可信。",
        "selection": "被挡候选后续明显优于正式候选，且原阻断理由不充分。",
        "model_switch": "自然空仓被误判、策略切换迟钝，或补位逻辑与主策略角色冲突。",
        "sell_point": "退出价格过期、卖点合同缺口、止损止盈与前低保护冲突。",
        "buy_point": "买点追高、确认不足、过早入场或热度约束未被正确执行。",
        "natural_trade_consistency": "复盘结论依赖收益倒推，而不是合同、证据和自然交易边界。",
    }

    rows: list[dict[str, Any]] = []
    review_map: dict[str, dict[str, Any]] = {}
    for row in daily_review_checklist_reviews_report.to_dict("records") if not daily_review_checklist_reviews_report.empty else []:
        key = str(row.get("review_key") or row.get("key") or "").strip()
        if not key:
            continue
        review_map[key] = row
        axis_key = _daily_review_checklist_review_key(row)
        if axis_key:
            review_map[axis_key] = row
    for item in live_hidden_risk_watchlist.to_dict("records"):
        axis = _review_axis_for_watch(item)
        window, checkpoint = _daily_review_window_for_axis(axis, item.get("risk_level"))
        target = str(item.get("next_review_action") or "").strip()
        review_key = _daily_review_checklist_review_key({**item, "review_axis": axis})
        review = review_map.get(review_key, {})
        if not target:
            target = "补齐证据并在盘后写入复盘结论。"
        rows.append(
            {
                "priority": 0,
                "review_window": window,
                "checkpoint_time": checkpoint,
                "review_axis": axis,
                "review_scope": item.get("review_scope"),
                "object": item.get("object"),
                "code": item.get("code"),
                "name": item.get("name"),
                "entry_date": item.get("entry_date"),
                "risk_level": item.get("risk_level"),
                "watch_status": item.get("watch_status"),
                "review_status": review.get("review_status") or item.get("watch_status") or "pending_review",
                "review_result": review.get("review_result"),
                "review_result_label": review.get("review_result_label"),
                "review_note": review.get("review_note"),
                "updated_at": review.get("updated_at"),
                "evidence_to_collect": item.get("evidence_gap") or item.get("risk_signal"),
                "pass_condition": pass_condition.get(axis),
                "fail_condition": fail_condition.get(axis),
                "target_action": f"{axis_labels.get(axis, axis)}：{target}",
                "natural_trade_boundary": item.get("natural_trade_boundary"),
                "source": item.get("source"),
                "review_key": review_key,
                "ticket_key": item.get("ticket_key"),
                "duplicate_source_count": 1,
            }
        )

    out = pd.DataFrame(rows)
    window_rank = {"盘前": 0, "盘前/盘后": 1, "盘中/盘后": 2, "盘后": 3}
    risk_rank = {"high": 0, "medium": 1, "low": 2}
    out["_window_rank"] = out["review_window"].map(lambda x: window_rank.get(str(x), 9))
    out["_risk_rank"] = out["risk_level"].map(lambda x: risk_rank.get(str(x), 9))
    out = out.sort_values(["_window_rank", "_risk_rank", "review_axis", "priority"], kind="stable").reset_index(drop=True)
    out["duplicate_source_count"] = out.groupby("review_key")["review_key"].transform("size")
    out = out.drop_duplicates(["review_key"], keep="first").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_window_rank", "_risk_rank"])


def _build_live_daily_review_action_layers(live_daily_review_execution_checklist: pd.DataFrame) -> pd.DataFrame:
    if live_daily_review_execution_checklist.empty:
        return pd.DataFrame([])

    def status_count(df: pd.DataFrame, status: str) -> int:
        return int((df.get("review_status", pd.Series(dtype=str)).astype(str) == status).sum()) if not df.empty else 0

    def pending_count(df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        statuses = df.get("review_status", pd.Series(dtype=str)).astype(str)
        return int(statuses.isin({"pending", "pending_review", "pending_observation", "pending_evidence", "watch"}).sum())

    def axis_text(df: pd.DataFrame) -> str:
        axes = [x for x in df.get("review_axis", pd.Series(dtype=str)).astype(str).dropna().unique().tolist() if x]
        return " / ".join(axes) if axes else "--"

    def first_non_empty(df: pd.DataFrame, column: str) -> str:
        if df.empty or column not in df.columns:
            return "--"
        for value in df[column].tolist():
            text = str(value or "").strip()
            if text and text.lower() != "nan":
                return text
        return "--"

    def add_layer(
        rows: list[dict[str, Any]],
        priority: int,
        action_layer: str,
        layer_label: str,
        df: pd.DataFrame,
        review_window: str,
        checkpoint_time: str,
        buy_permission_effect: str,
        operator_instruction: str,
    ) -> None:
        if df.empty:
            return
        rows.append(
            {
                "priority": priority,
                "action_layer": action_layer,
                "layer_label": layer_label,
                "review_window": review_window,
                "checkpoint_time": checkpoint_time,
                "item_count": len(df),
                "pending_count": pending_count(df),
                "validated_count": status_count(df, "validated"),
                "issue_found_count": status_count(df, "issue_found"),
                "continue_watch_count": status_count(df, "continue_watch"),
                "high_risk_count": int((df.get("risk_level", pd.Series(dtype=str)).astype(str) == "high").sum()),
                "review_axes": axis_text(df),
                "first_action": first_non_empty(df, "target_action"),
                "evidence_focus": first_non_empty(df, "evidence_to_collect"),
                "pass_condition": first_non_empty(df, "pass_condition"),
                "fail_condition": first_non_empty(df, "fail_condition"),
                "buy_permission_effect": buy_permission_effect,
                "operator_instruction": operator_instruction,
            }
        )

    rows: list[dict[str, Any]] = []
    axes = live_daily_review_execution_checklist.get("review_axis", pd.Series(dtype=str)).astype(str)
    statuses = live_daily_review_execution_checklist.get("review_status", pd.Series(dtype=str)).astype(str)

    premarket_df = live_daily_review_execution_checklist[axes == "execution_evidence"].copy()
    strategy_df = live_daily_review_execution_checklist[axes.isin({"buy_point", "sell_point", "selection", "model_switch", "natural_trade_consistency"})].copy()
    issue_df = live_daily_review_execution_checklist[statuses.isin({"issue_found", "continue_watch"})].copy()

    add_layer(
        rows,
        1,
        "premarket_blocker_clear",
        "盘前阻断清理",
        premarket_df,
        "盘前",
        "09:15 前",
        "未清理前禁止真实买入；清理后仍需重跑审计。",
        "先同步真实持仓/价格、补齐执行证据，再重跑实战前审计。",
    )
    add_layer(
        rows,
        2,
        "intraday_strategy_watch",
        "盘中/盘后策略观察",
        strategy_df,
        "盘中/盘后",
        "盘中观察，15:10 后落账",
        "只产生学习证据，不直接放宽买入规则。",
        "围绕买点、卖点、选股和策略切换逐项记录证据，避免按收益倒推调参。",
    )
    add_layer(
        rows,
        3,
        "after_close_learning_attribution",
        "盘后学习归因",
        issue_df,
        "盘后",
        "15:10 后",
        "发现隐患后保持观察或阻断，必须先归因再改合同。",
        "把 issue_found/continue_watch 样本沉淀到学习队列，区分数据缺口、流程缺口和策略逻辑缺口。",
    )

    if not rows:
        return pd.DataFrame([])
    return pd.DataFrame(rows)


def _build_formal_action_reviews_report() -> pd.DataFrame:
    review_map = _read_formal_action_review_map()
    rows: list[dict[str, Any]] = []
    for key, row in review_map.items():
        rows.append(
            {
                "priority": 0,
                "review_key": row.get("review_key") or key,
                "object": row.get("object"),
                "action": row.get("action"),
                "source": row.get("source"),
                "review_status": row.get("review_status"),
                "review_result": row.get("review_result"),
                "review_result_label": row.get("review_result_label"),
                "issue_area": row.get("issue_area"),
                "evidence": row.get("evidence"),
                "natural_decision": row.get("natural_decision"),
                "optimization_suggestion": row.get("optimization_suggestion"),
                "review_note": row.get("review_note"),
                "source": row.get("source"),
                "updated_at": row.get("updated_at"),
            }
        )
    if not rows:
        return pd.DataFrame([])
    out = pd.DataFrame(rows)
    status_rank = {"issue_found": 0, "pending": 1, "continue_watch": 2, "validated": 3}
    out["_status_rank"] = out["review_status"].map(lambda x: status_rank.get(str(x), 9))
    out = out.sort_values(["_status_rank", "updated_at", "object"], ascending=[True, False, True], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_status_rank"])


def _build_daily_review_checklist_reviews_report() -> pd.DataFrame:
    review_map = _read_daily_review_checklist_review_map()
    rows: list[dict[str, Any]] = []
    for key, row in review_map.items():
        rows.append(
            {
                "priority": 0,
                "review_key": row.get("review_key") or key,
                "ticket_key": row.get("ticket_key"),
                "review_axis": row.get("review_axis"),
                "review_scope": row.get("review_scope"),
                "object": row.get("object"),
                "code": row.get("code"),
                "name": row.get("name"),
                "entry_date": row.get("entry_date"),
                "review_status": row.get("review_status"),
                "review_result": row.get("review_result"),
                "review_result_label": row.get("review_result_label"),
                "issue_area": row.get("issue_area"),
                "evidence": row.get("evidence"),
                "natural_decision": row.get("natural_decision"),
                "optimization_suggestion": row.get("optimization_suggestion"),
                "review_note": row.get("review_note"),
                "source": row.get("source"),
                "updated_at": row.get("updated_at"),
            }
        )
    if not rows:
        return pd.DataFrame([])
    out = pd.DataFrame(rows)
    status_rank = {"issue_found": 0, "pending_review": 1, "continue_watch": 2, "validated": 3}
    out["_status_rank"] = out["review_status"].map(lambda x: status_rank.get(str(x), 9))
    out = out.sort_values(["_status_rank", "review_axis", "entry_date", "review_key"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_status_rank"])


def _build_live_premarket_action_attempts_report() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for row in _read_premarket_action_attempts():
        rows.append(
            {
                "priority": 0,
                "attempted_at": row.get("attempted_at"),
                "action_group": row.get("action_group"),
                "action_label": row.get("action_label"),
                "source": row.get("source"),
                "ok": row.get("ok"),
                "sync_ok": row.get("sync_ok"),
                "review_ok": row.get("review_ok"),
                "sync_mode": row.get("sync_mode"),
                "sync_message": row.get("sync_message"),
                "holdings_count": row.get("holdings_count"),
                "review_key": row.get("review_key"),
                "review_result": row.get("review_result"),
                "review_status": row.get("review_status"),
                "review_axis": row.get("review_axis"),
                "review_scope": row.get("review_scope"),
                "issue_area": row.get("issue_area"),
                "review_returncode": row.get("review_returncode"),
                "live_admission_status": row.get("live_admission_status"),
                "live_admission_buy_allowed": row.get("live_admission_buy_allowed"),
                "live_admission_blocking_command_count": row.get("live_admission_blocking_command_count"),
                "live_premarket_next_action": row.get("live_premarket_next_action"),
                "formal_buy_signal": row.get("formal_buy_signal"),
                "auto_order_allowed": row.get("auto_order_allowed"),
                "order_path_enabled": row.get("order_path_enabled"),
                "evidence_boundary": "动作尝试只记录同步与复审事实；不替代 gate，不开启真实买入或自动下单。",
            }
        )
    if not rows:
        return pd.DataFrame([])
    out = pd.DataFrame(rows)
    out = out.sort_values(["attempted_at"], ascending=[False], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out


def _build_first_live_decision_card(
    summary: dict[str, Any],
    formal_launch_action_queue: pd.DataFrame,
    daily_live_review_board: pd.DataFrame,
    strategy_learning_backlog: pd.DataFrame,
) -> pd.DataFrame:
    ticket_count = int(summary.get("next_trade_ticket_count") or 0)
    formal_pending = int(summary.get("pretrade_formal_action_pending_count") or 0)
    board_pending = int(summary.get("daily_live_review_board_pending_count") or 0)
    issue_found = int(summary.get("daily_live_review_board_issue_found_count") or 0)
    formal_status = str(summary.get("formal_launch_status") or "").strip()
    formal_ready = _truthy(summary.get("formal_launch_ready"))
    block_count = int(summary.get("formal_launch_block_count") or 0) + int(summary.get("gate_block_count") or 0)

    if block_count or issue_found:
        decision_status = "blocked_review_required"
        decision_label = "阻断/隐患复盘"
        execution_posture = "不买入；先修复硬阻断或已发现隐患"
        risk_tone = "block"
        primary_reason = "存在硬阻断或复盘看板已发现隐患，不能进入真实买入或纸面执行放大。"
    elif ticket_count == 0 and formal_pending:
        decision_status = "no_buy_observe_pending"
        decision_label = "无票观察待处理"
        execution_posture = "不买入；先处理正式动作，再做无票日复盘"
        risk_tone = "watch"
        primary_reason = "下一交易日没有正式买入票，但真实持仓/价格刷新等正式动作仍未清理。"
    elif ticket_count == 0:
        decision_status = "no_buy_observe_ready"
        decision_label = "无票观察"
        execution_posture = "不买入；按无票日与候选遗漏复盘观察"
        risk_tone = "observe"
        primary_reason = "下一交易日没有正式买入票；这是自然空仓/观察日，不应为了凑槽位强行买入。"
    elif formal_ready:
        decision_status = "manual_live_ready"
        decision_label = "人工实盘待确认"
        execution_posture = "允许人工最终确认；自动下单仍锁定"
        risk_tone = "ready"
        primary_reason = "票据、正式动作和组合层审计已满足人工实盘确认条件，但自动下单未开启。"
    elif ticket_count and formal_pending:
        decision_status = "ticket_review_or_formal_action_pending"
        decision_label = "有票但未放行"
        execution_posture = "不进入真实买入；逐票复盘或纸面观察"
        risk_tone = "watch"
        primary_reason = "存在正式买入票，但逐票复盘、持仓刷新或正式动作尚未清理。"
    else:
        decision_status = "paper_or_manual_review_pending"
        decision_label = "复盘确认中"
        execution_posture = "先纸面/人工复核，不直接下单"
        risk_tone = "watch"
        primary_reason = str(summary.get("formal_launch_next_step") or "仍需补齐人工复盘证据。")

    action_rows = formal_launch_action_queue.to_dict("records") if not formal_launch_action_queue.empty else []
    pending_actions = [
        row
        for row in action_rows
        if str(row.get("action_status") or "pending").strip() in {"pending", "", "continue_watch", "issue_found"}
    ]
    top_actions = pending_actions[:5] if pending_actions else action_rows[:5]
    action_texts = [
        f"{row.get('object') or '--'}：{row.get('action') or '--'}"
        for row in top_actions
    ]
    first_required_action = action_texts[0] if action_texts else str(summary.get("formal_launch_next_step") or "重跑实战前审计并查看每日复盘看板")

    if not strategy_learning_backlog.empty:
        focus_rows = strategy_learning_backlog.head(3).to_dict("records")
        review_focus = "；".join(
            [
                f"{row.get('review_scope') or '--'}:{row.get('issue_area') or '--'}"
                for row in focus_rows
            ]
        )
    elif not daily_live_review_board.empty:
        focus_rows = daily_live_review_board.head(3).to_dict("records")
        review_focus = "；".join(
            [
                f"{row.get('stage') or '--'}:{row.get('object') or '--'}"
                for row in focus_rows
            ]
        )
    else:
        review_focus = "暂无复盘队列；等待下一轮审计或交易后样本。"

    return pd.DataFrame(
        [
            {
                "generated_at": summary.get("generated_at"),
                "entry_date": summary.get("entry_date"),
                "next_trade_entry_date": summary.get("next_trade_entry_date"),
                "decision_status": decision_status,
                "decision_label": decision_label,
                "execution_posture": execution_posture,
                "live_buy_allowed": bool(decision_status == "manual_live_ready"),
                "paper_execution_allowed": bool(ticket_count > 0 and decision_status not in {"blocked_review_required", "no_buy_observe_pending", "no_buy_observe_ready"}),
                "auto_order_allowed": False,
                "primary_reason": primary_reason,
                "risk_tone": risk_tone,
                "formal_launch_status": formal_status,
                "ticket_count": ticket_count,
                "formal_pending_count": formal_pending,
                "board_pending_count": board_pending,
                "issue_found_count": issue_found,
                "first_required_action": first_required_action,
                "top_action_list": "；".join(action_texts) if action_texts else "--",
                "review_focus": review_focus,
                "locked_modes": "自动下单锁定；无票日禁止凑槽；index_mom60>5% 机构主升只观察；真实买入必须受真实账户持仓/现金约束",
                "next_review_trigger": "盘前重跑审计、正式动作有证据后重跑、盘后补充无票/纸面/候选遗漏复盘",
            }
        ]
    )


def _task_window_for_scope(scope: str, formal_required: bool) -> str:
    if scope == "formal_action" or formal_required:
        return "盘前"
    if scope in {"day1_after_close", "no_trade_day"}:
        return "盘后"
    if scope in {"candidate_omission", "day1_paper_pack"}:
        return "盘中/盘后"
    return "盘前/盘后"


def _task_type_for_scope(scope: str) -> str:
    return {
        "formal_action": "正式动作",
        "no_trade_day": "无票复盘",
        "candidate_omission": "候选遗漏",
        "day1_paper_pack": "纸面执行准备",
        "day1_after_close": "盘后归因",
    }.get(scope, "复盘任务")


def _review_method_for_scope(scope: str) -> str:
    return {
        "formal_action": "记录已处理/卡住证据后重跑实战前审计",
        "no_trade_day": "选择空仓合理、可能误杀、数据缺口、流程缺口或继续观察",
        "candidate_omission": "选择挡得合理、可能误杀、信号失效或继续观察",
        "day1_paper_pack": "先记录纸面执行，再等待盘后归因",
        "day1_after_close": "归因到买点、选股、策略切换、卖点/风控或继续观察",
    }.get(scope, "按来源表格补充复盘证据")


def _build_live_review_task_queue(
    first_live_decision_card: pd.DataFrame,
    daily_live_review_board: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    decision = first_live_decision_card.iloc[0].to_dict() if not first_live_decision_card.empty else {}
    if decision:
        rows.append(
            {
                "priority": 0,
                "window": "盘前",
                "task_type": "首日决策确认",
                "review_scope": "first_live_decision",
                "object": decision.get("decision_label"),
                "code": "",
                "name": "",
                "entry_date": decision.get("next_trade_entry_date"),
                "task_status": decision.get("decision_status"),
                "formal_trade_required": False,
                "action": decision.get("first_required_action"),
                "acceptance": "确认真实买入、纸面执行、自动下单三类权限与决策卡一致",
                "fallback": decision.get("execution_posture"),
                "review_method": "先读决策卡，再按本任务队列逐项处理",
                "source": "first_live_decision_card",
                "review_key": "",
                "ticket_key": "",
            }
        )

    board_rows = daily_live_review_board.to_dict("records") if not daily_live_review_board.empty else []
    for row in board_rows:
        scope = str(row.get("review_scope") or "").strip()
        formal_required = _truthy(row.get("formal_trade_required"))
        status = str(row.get("review_status") or "").strip()
        if status in {"validated", "pass", "ready"}:
            continue
        action = row.get("required_action") or row.get("optimization_focus") or "补充复盘证据"
        if scope == "candidate_omission":
            acceptance = "候选走势已归因：挡得合理、可能误杀、信号失效或继续观察"
            fallback = "证据不足时继续观察，不放宽买点或选股规则"
        elif scope == "no_trade_day":
            acceptance = "无票日已归因：自然空仓、误杀、数据缺口、流程缺口或继续观察"
            fallback = "未复盘前不把无票当策略失败，也不为补二槽强行买入"
        elif scope == "formal_action":
            acceptance = "正式动作已有证据并重跑审计；底层 gate/action 真实清理才算完成"
            fallback = "若卡住，记录流程隐患，不归因为交易模型收益问题"
        elif scope == "day1_after_close":
            acceptance = "纸面执行或盘后走势已归因到买点、选股、策略切换或卖点合同"
            fallback = "未归因前不进入策略学习结论"
        else:
            acceptance = "补齐证据并刷新审计报告"
            fallback = "证据不足则保持观察"
        rows.append(
            {
                "priority": 0,
                "window": _task_window_for_scope(scope, formal_required),
                "task_type": _task_type_for_scope(scope),
                "review_scope": scope,
                "object": row.get("object"),
                "code": row.get("code"),
                "name": row.get("name"),
                "entry_date": row.get("entry_date"),
                "task_status": status,
                "formal_trade_required": formal_required,
                "action": action,
                "acceptance": acceptance,
                "fallback": fallback,
                "review_method": _review_method_for_scope(scope),
                "source": row.get("source"),
                "review_key": row.get("review_key"),
                "ticket_key": row.get("ticket_key"),
            }
        )

    if not rows:
        return pd.DataFrame([])
    out = pd.DataFrame(rows)
    window_rank = {"盘前": 0, "盘中/盘后": 1, "盘后": 2, "盘前/盘后": 3}
    status_rank = {"blocked_review_required": 0, "issue_found": 0, "pending": 1, "pending_review": 1, "pending_observation": 2, "continue_watch": 3}
    out["_decision_rank"] = out["review_scope"].map(lambda x: 0 if str(x) == "first_live_decision" else 1)
    out["_window_rank"] = out["window"].map(lambda x: window_rank.get(str(x), 9))
    out["_formal_rank"] = out["formal_trade_required"].map(lambda x: 0 if _truthy(x) else 1)
    out["_status_rank"] = out["task_status"].map(lambda x: status_rank.get(str(x), 5))
    out = out.sort_values(["_decision_rank", "_window_rank", "_formal_rank", "_status_rank", "task_type", "code"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_decision_rank", "_window_rank", "_formal_rank", "_status_rank"])


def _build_review_coverage_dashboard(live_review_task_queue: pd.DataFrame) -> pd.DataFrame:
    if live_review_task_queue.empty:
        return pd.DataFrame([])

    covered_statuses = {"validated", "pass", "ready", "issue_found", "no_buy_observe_ready", "manual_live_ready"}
    pending_statuses = {"pending", "pending_review", "pending_observation", "pending_after_close_review", "pending_paper_execution"}

    def _is_pending_review_status(status: str) -> bool:
        normalized = str(status or "").strip()
        return normalized in pending_statuses or normalized.startswith("pending_") or normalized.endswith("_pending")
    rows: list[dict[str, Any]] = []
    grouped = live_review_task_queue.groupby(["review_scope", "task_type"], dropna=False, sort=False)
    for (scope, task_type), group in grouped:
        statuses = group.get("task_status", pd.Series(dtype=str)).astype(str)
        total = int(len(group))
        covered = int(statuses.isin(covered_statuses).sum())
        pending = int(statuses.map(_is_pending_review_status).sum())
        issue_found = int((statuses == "issue_found").sum())
        continue_watch = int((statuses == "continue_watch").sum())
        formal_required = int(group.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy).sum())
        coverage_pct = round(covered / total, 4) if total else 0.0
        if issue_found:
            coverage_status = "issue_found"
            next_action = "优先复盘隐患样本，归因到买点、选股、策略切换、卖点合同或执行流程"
        elif pending:
            coverage_status = "pending_review"
            next_action = "继续处理待复盘任务，补齐证据后重跑审计"
        elif continue_watch:
            coverage_status = "continue_watch"
            next_action = "保留观察，不因单日收益倒推改规则"
        else:
            coverage_status = "covered"
            next_action = "保持当前规则，等待下一交易日或盘后新样本"
        rows.append(
            {
                "priority": 0,
                "review_scope": scope,
                "task_type": task_type,
                "total_count": total,
                "covered_count": covered,
                "pending_count": pending,
                "issue_found_count": issue_found,
                "continue_watch_count": continue_watch,
                "formal_required_count": formal_required,
                "coverage_pct": coverage_pct,
                "coverage_status": coverage_status,
                "next_action": next_action,
            }
        )

    out = pd.DataFrame(rows)
    status_rank = {"issue_found": 0, "pending_review": 1, "continue_watch": 2, "covered": 3}
    out["_status_rank"] = out["coverage_status"].map(lambda x: status_rank.get(str(x), 9))
    out["_formal_rank"] = out["formal_required_count"].map(lambda x: 0 if int(x or 0) else 1)
    out = out.sort_values(["_status_rank", "_formal_rank", "review_scope"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_status_rank", "_formal_rank"])


def _rubric_for_review_scope(scope: str) -> dict[str, str]:
    rules = {
        "first_live_decision": {
            "review_axes": "execution_process, model_switch",
            "evidence_required": "核对首日决策卡、正式放行状态、真实持仓刷新状态、下一交易日票据数量与自动下单锁定状态。",
            "decision_rule": "先判定是否允许真实买入，再判定是否只做纸面/观察；无票日不补票，不为填满二槽强行交易。",
            "optimization_boundary": "首日姿态只约束执行，不因当天无票或有票数量直接修改选股阈值。",
            "promotion_rule": "连续多个交易日出现同类执行阻塞，才进入流程修复；只有信号证据完整且重复遗漏，才进入策略学习。",
        },
        "formal_action": {
            "review_axes": "execution_process, sell_exit",
            "evidence_required": "保留真实持仓、最新价、退出判断、人工处理结果与重跑审计后的状态变化。",
            "decision_rule": "正式必处理项优先于新开仓；真实持仓退出必须先刷新价格，不能用过期价做卖点或买点判断。",
            "optimization_boundary": "流程卡住归为执行问题，不把未刷新、未同步、未确认误归因为交易模型收益问题。",
            "promotion_rule": "同类正式动作三次以上阻塞，升级为运行合同修复；若刷新后卖点合同仍不自然，再进入卖点优化。",
        },
        "no_trade_day": {
            "review_axes": "selection, model_switch",
            "evidence_required": "记录当天候选为空/无正式票的原因、市场状态、路由候选、G2补位可用性、真实账户约束与人工观察结果。",
            "decision_rule": "先判断自然空仓还是错杀机会；错杀必须能说明应由哪个路由、哪条证据链产生票。",
            "optimization_boundary": "无票日不是策略失败的直接证据，不为提高交易频率放宽核心准入。",
            "promotion_rule": "连续错杀且事后走势、信号新鲜度、风控门槛都支持时，才进入选股/策略切换规则修订。",
        },
        "candidate_omission": {
            "review_axes": "selection, buy_point, model_switch",
            "evidence_required": "跟踪候选后续走势、当日30m确认、主升分/路由分、板块暴露、index_mom60、是否被二槽或同板块规则过滤。",
            "decision_rule": "先判断过滤是否合理，再判断是否错过买点；被高热度或硬 gate 阻断的票只能观察，不能倒推买入。",
            "optimization_boundary": "单只候选走强不等于选股遗漏；必须证明当时已有可执行买点和合规仓位。",
            "promotion_rule": "同类候选多次被同一规则过滤且后续验证强，才进入选股模式或策略切换候选优化。",
        },
        "day1_paper_pack": {
            "review_axes": "buy_point, selection, model_switch, sell_exit",
            "evidence_required": "记录纸面买入价、信号时间、30m确认、路由来源、仓位约束、盘中最大回撤、第一止盈/止损触发情况。",
            "decision_rule": "Day1 先看买点是否自然、选股是否属于主路由、策略切换是否顺畅，再看收益。",
            "optimization_boundary": "纸面首日收益不能直接决定规则优劣；先判定行为是否符合合同。",
            "promotion_rule": "盘后归因稳定落到同一轴，且跨样本重复，才进入策略学习 backlog。",
        },
        "day1_after_close": {
            "review_axes": "buy_point, selection, model_switch, sell_exit",
            "evidence_required": "盘后补齐收盘价、浮盈亏、最大回撤、触发过的卖点合同、未触发但应关注的风险点。",
            "decision_rule": "归因到买点、选股、策略切换或卖点合同；继续观察样本不调参。",
            "optimization_boundary": "不以收盘涨跌单独定义成功/失败，必须结合入场位置、风险暴露和退出可执行性。",
            "promotion_rule": "同类归因至少形成可复核样本簇后，再进入参数或合同修订。",
        },
    }
    return rules.get(
        str(scope or "").strip(),
        {
            "review_axes": "execution_process",
            "evidence_required": "补齐来源表、任务状态、人工判断和重跑审计结果。",
            "decision_rule": "先判断证据是否完整，再判断是否进入策略学习。",
            "optimization_boundary": "证据不完整时只保留观察，不做收益导向调参。",
            "promotion_rule": "重复出现且可复核后再升级。",
        },
    )


def _build_live_review_evidence_rubric(live_review_task_queue: pd.DataFrame) -> pd.DataFrame:
    if live_review_task_queue.empty:
        scopes = [
            ("first_live_decision", "首日决策确认", 0, 0, 0),
            ("formal_action", "正式动作", 0, 0, 0),
            ("no_trade_day", "无票日复盘", 0, 0, 0),
            ("candidate_omission", "候选遗漏复盘", 0, 0, 0),
            ("day1_after_close", "盘后归因", 0, 0, 0),
        ]
    else:
        pending_statuses = {"pending", "pending_review", "pending_observation", "pending_after_close_review", "pending_paper_execution"}

        def is_pending(status: Any) -> bool:
            normalized = str(status or "").strip()
            return normalized in pending_statuses or normalized.startswith("pending_") or normalized.endswith("_pending")

        scopes = []
        grouped = live_review_task_queue.groupby(["review_scope", "task_type"], dropna=False, sort=False)
        for (scope, task_type), group in grouped:
            statuses = group.get("task_status", pd.Series(dtype=str))
            scopes.append(
                (
                    str(scope or ""),
                    str(task_type or ""),
                    int(len(group)),
                    int(group.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy).sum()),
                    int(statuses.map(is_pending).sum()),
                )
            )

    rows: list[dict[str, Any]] = []
    for scope, task_type, task_count, formal_count, pending_count in scopes:
        rule = _rubric_for_review_scope(scope)
        rows.append(
            {
                "priority": 0,
                "review_scope": scope,
                "task_type": task_type,
                "review_axes": rule["review_axes"],
                "task_count": task_count,
                "formal_required_count": formal_count,
                "pending_count": pending_count,
                "evidence_required": rule["evidence_required"],
                "decision_rule": rule["decision_rule"],
                "optimization_boundary": rule["optimization_boundary"],
                "promotion_rule": rule["promotion_rule"],
                "next_action": "按证据要求完成复盘后重跑审计；只有重复、可复核的同类问题才进入策略优化。",
            }
        )

    out = pd.DataFrame(rows)
    out["_formal_rank"] = out["formal_required_count"].map(lambda x: 0 if int(x or 0) else 1)
    out["_pending_rank"] = out["pending_count"].map(lambda x: 0 if int(x or 0) else 1)
    out = out.sort_values(["_formal_rank", "_pending_rank", "priority", "review_scope"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_formal_rank", "_pending_rank"])


def _build_live_premarket_command_sheet(
    first_live_decision_card: pd.DataFrame,
    live_review_task_queue: pd.DataFrame,
    live_review_evidence_rubric: pd.DataFrame,
) -> pd.DataFrame:
    if live_review_task_queue.empty:
        return pd.DataFrame([])

    rubric_map = {}
    if not live_review_evidence_rubric.empty:
        for item in live_review_evidence_rubric.to_dict("records"):
            rubric_map[str(item.get("review_scope") or "")] = item

    decision = first_live_decision_card.iloc[0].to_dict() if not first_live_decision_card.empty else {}
    decision_status = str(decision.get("decision_status") or "").strip()
    live_buy_allowed = _truthy(decision.get("live_buy_allowed"))
    rows: list[dict[str, Any]] = []

    def gate_for_task(row: dict[str, Any]) -> tuple[str, bool]:
        scope = str(row.get("review_scope") or "").strip()
        status = str(row.get("task_status") or "").strip()
        formal_required = _truthy(row.get("formal_trade_required"))
        if scope == "first_live_decision":
            if live_buy_allowed:
                return "live_buy_possible_manual_confirm", False
            if "no_buy" in status:
                return "no_buy_observe", False
            return "live_buy_blocked_by_decision", True
        if formal_required:
            return "blocks_live_buy_until_done", True
        if status == "issue_found":
            return "review_before_learning", False
        return "observe_or_after_close_review", False

    def unlock_status_for_task(row: dict[str, Any], blocks_live_buy: bool) -> str:
        status = str(row.get("task_status") or "").strip()
        if status in {"validated", "pass", "ready"}:
            return "unlocked"
        if status == "issue_found":
            return "blocked_issue_found" if blocks_live_buy else "issue_review_required"
        if status == "continue_watch":
            return "watch_more"
        if blocks_live_buy:
            return "waiting_resolution"
        return "observation_open"

    def post_action_check_for_task(row: dict[str, Any], blocks_live_buy: bool) -> str:
        scope = str(row.get("review_scope") or "").strip()
        if scope == "formal_action":
            return "保存处理证据后重跑审计；若底层事实已清理，该正式动作应从盘前指挥单消失，阻止真实买入数下降。"
        if scope == "first_live_decision":
            return "重跑审计并确认 live_buy_allowed、paper_execution_allowed、auto_order_allowed 三类权限仍与合同一致。"
        if scope == "no_trade_day":
            return "记录无票日归因后重跑审计；自然空仓不触发补票，误杀样本进入策略学习队列。"
        if scope == "candidate_omission":
            return "记录候选后续表现后重跑审计；只有重复可复核的遗漏才进入选股或买点优化。"
        if blocks_live_buy:
            return "处理后重跑审计；阻断仍存在则继续保留在指挥单。"
        return "记录观察证据后重跑审计；不把单日盈亏作为调参依据。"

    task_rows = live_review_task_queue.to_dict("records")
    for task in task_rows:
        scope = str(task.get("review_scope") or "").strip()
        command_window = str(task.get("window") or "").strip()
        if command_window not in {"盘前", "盘前/盘后"} and not _truthy(task.get("formal_trade_required")) and scope != "first_live_decision":
            continue
        rubric = rubric_map.get(scope, {})
        decision_gate, blocks_live_buy = gate_for_task(task)
        current_status = str(task.get("task_status") or "").strip()
        rows.append(
            {
                "priority": 0,
                "command_window": command_window or "盘前",
                "command_type": task.get("task_type") or "实战复盘动作",
                "decision_gate": decision_gate,
                "review_scope": scope,
                "object": task.get("object"),
                "code": task.get("code"),
                "name": task.get("name"),
                "entry_date": task.get("entry_date"),
                "current_status": current_status,
                "ledger_key": task.get("review_key") or task.get("ticket_key"),
                "formal_trade_required": _truthy(task.get("formal_trade_required")),
                "blocks_live_buy": blocks_live_buy,
                "unlock_status": unlock_status_for_task(task, blocks_live_buy),
                "action": task.get("action"),
                "acceptance": task.get("acceptance"),
                "evidence_required": rubric.get("evidence_required") or task.get("review_method"),
                "optimization_boundary": rubric.get("optimization_boundary") or "先补证据再归因，不按单日盈亏倒推调参。",
                "post_action_check": post_action_check_for_task(task, blocks_live_buy),
                "fallback": task.get("fallback"),
                "source": task.get("source"),
                "review_key": task.get("review_key"),
                "ticket_key": task.get("ticket_key"),
            }
        )

    if not rows:
        return pd.DataFrame([])

    out = pd.DataFrame(rows)
    window_rank = {"盘前": 0, "盘前/盘后": 1, "盘中/盘后": 2, "盘后": 3}
    gate_rank = {
        "live_buy_blocked_by_decision": 0,
        "blocks_live_buy_until_done": 1,
        "no_buy_observe": 2,
        "live_buy_possible_manual_confirm": 3,
        "review_before_learning": 4,
        "observe_or_after_close_review": 5,
    }
    out["_block_rank"] = out["blocks_live_buy"].map(lambda x: 0 if _truthy(x) else 1)
    out["_window_rank"] = out["command_window"].map(lambda x: window_rank.get(str(x), 9))
    out["_gate_rank"] = out["decision_gate"].map(lambda x: gate_rank.get(str(x), 9))
    out["_formal_rank"] = out["formal_trade_required"].map(lambda x: 0 if _truthy(x) else 1)
    out = out.sort_values(["_block_rank", "_window_rank", "_gate_rank", "_formal_rank", "review_scope", "code"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_block_rank", "_window_rank", "_gate_rank", "_formal_rank"])


def _build_live_admission_snapshot(
    summary: dict[str, Any],
    first_live_decision_card: pd.DataFrame,
    live_premarket_command_sheet: pd.DataFrame,
) -> pd.DataFrame:
    decision = first_live_decision_card.iloc[0].to_dict() if not first_live_decision_card.empty else {}
    command_rows = live_premarket_command_sheet.to_dict("records") if not live_premarket_command_sheet.empty else []
    blocking_rows = [row for row in command_rows if _truthy(row.get("blocks_live_buy"))]
    formal_rows = [row for row in command_rows if _truthy(row.get("formal_trade_required"))]
    first_block = blocking_rows[0] if blocking_rows else {}

    decision_status = str(decision.get("decision_status") or "").strip()
    ticket_count = int(summary.get("next_trade_ticket_count") or decision.get("ticket_count") or 0)
    paper_allowed_by_decision = _truthy(decision.get("paper_execution_allowed"))
    live_allowed_by_decision = _truthy(decision.get("live_buy_allowed"))
    auto_allowed_by_decision = _truthy(decision.get("auto_order_allowed"))

    if not decision:
        admission_status = "unknown"
        admission_label = "审计未生成"
        live_buy_allowed = False
        paper_execution_allowed = False
        risk_tone = "block"
        primary_reason = "未生成首日决策卡，不能判断真实买入准入。"
        next_step = "先重跑 G3 实战前审计，生成首日决策卡和盘前指挥单。"
    elif blocking_rows:
        admission_status = "blocked_by_premarket_commands"
        admission_label = "盘前动作未清"
        live_buy_allowed = False
        paper_execution_allowed = paper_allowed_by_decision and not formal_rows
        risk_tone = "block"
        primary_reason = (
            f"仍有 {len(blocking_rows)} 项盘前动作阻止真实买入；第一项是 "
            f"{first_block.get('review_scope') or '--'}：{first_block.get('action') or '--'}。"
        )
        next_step = "按盘前指挥单 priority 从小到大处理；处理后重跑审计，确认阻止真实买入数降为 0。"
    elif decision_status == "manual_live_ready" and live_allowed_by_decision:
        admission_status = "manual_live_ready"
        admission_label = "人工实盘可确认"
        live_buy_allowed = True
        paper_execution_allowed = bool(ticket_count > 0)
        risk_tone = "ready"
        primary_reason = decision.get("primary_reason") or "正式放行条件已满足，可进入人工最终确认。"
        next_step = "只允许人工最终确认；自动下单保持锁定，真实仓位仍受账户现金和持仓约束。"
    elif "no_buy" in decision_status or ticket_count == 0:
        admission_status = decision_status or "no_buy_observe_ready"
        admission_label = decision.get("decision_label") or "无票观察"
        live_buy_allowed = False
        paper_execution_allowed = False
        risk_tone = decision.get("risk_tone") or "observe"
        primary_reason = decision.get("primary_reason") or "下一交易日没有正式买入票；这是观察日，不为填仓位强行交易。"
        next_step = decision.get("execution_posture") or "不买入；盘后复盘无票日和候选遗漏。"
    else:
        admission_status = decision_status or "review_pending"
        admission_label = decision.get("decision_label") or "复盘确认中"
        live_buy_allowed = False
        paper_execution_allowed = paper_allowed_by_decision
        risk_tone = decision.get("risk_tone") or "watch"
        primary_reason = decision.get("primary_reason") or "仍需补齐复盘证据或人工确认。"
        next_step = decision.get("execution_posture") or "先纸面或人工复核，不直接真实下单。"

    verification_rule = (
        "处理正式动作后必须重跑审计；只有 live_premarket_blocking_command_count=0 且 "
        "first_live_decision_status=manual_live_ready 时，真实买入才进入人工最终确认。"
    )
    if admission_status.startswith("no_buy"):
        verification_rule = "无票日不要求补票；盘后只复盘自然空仓、候选遗漏和策略切换，不按单日收益倒推放宽规则。"

    return pd.DataFrame(
        [
            {
                "generated_at": summary.get("generated_at"),
                "next_trade_entry_date": summary.get("next_trade_entry_date") or decision.get("next_trade_entry_date"),
                "admission_status": admission_status,
                "admission_label": admission_label,
                "live_buy_allowed": bool(live_buy_allowed),
                "paper_execution_allowed": bool(paper_execution_allowed),
                "auto_order_allowed": bool(auto_allowed_by_decision and live_buy_allowed),
                "ticket_count": ticket_count,
                "blocking_command_count": len(blocking_rows),
                "formal_command_count": len(formal_rows),
                "first_blocking_key": first_block.get("ledger_key") or first_block.get("review_key") or "",
                "first_blocking_scope": first_block.get("review_scope") or "",
                "first_required_action": first_block.get("action") or decision.get("first_required_action") or "",
                "execution_posture": decision.get("execution_posture") or next_step,
                "primary_reason": primary_reason,
                "next_step": next_step,
                "verification_rule": verification_rule,
                "risk_tone": risk_tone,
            }
        ]
    )


def _build_live_blocker_resolution_plan(live_premarket_command_sheet: pd.DataFrame) -> pd.DataFrame:
    if live_premarket_command_sheet.empty:
        return pd.DataFrame([])

    rows: list[dict[str, Any]] = []
    for item in live_premarket_command_sheet.to_dict("records"):
        if not _truthy(item.get("blocks_live_buy")):
            continue
        scope = str(item.get("review_scope") or "").strip()
        source = str(item.get("source") or "").strip()
        obj = str(item.get("object") or "").strip()
        action = str(item.get("action") or "").strip()
        text = f"{scope} {source} {obj} {action}"

        if "holding" in text or "持仓" in text or "最新价" in text or "价格" in text:
            resolution_type = "broker_holding_price_refresh"
            execution_owner = "human_trigger_system_refresh"
            can_auto_trigger = True
            requires_manual_confirmation = True
            recommended_ui_action = "点击“同步真实持仓并复审”"
            recommended_api_action = "POST /gen3-state-alpha/broker/holdings/sync-ths-and-review"
            evidence_required = "真实持仓、最新价、退出判断和复审后的盘前指挥单变化"
            completion_check = "刷新并重跑审计后，对应持仓/价格阻断从 live_premarket_command_sheet 消失"
            fallback = "若同花顺窗口不可用或刷新失败，保持禁止真实买入，只记录卡点证据"
        elif source == "formal_launch_checklist" or "审计" in text or "重跑" in text:
            resolution_type = "rerun_readiness_audit"
            execution_owner = "system_rerun_after_previous_actions"
            can_auto_trigger = True
            requires_manual_confirmation = False
            recommended_ui_action = "完成前置动作后点击“重跑审计”"
            recommended_api_action = "POST /gen3-state-alpha/realtime-readiness-review/run"
            evidence_required = "重跑后的 summary、准入快照、阻断数量和第一动作变化"
            completion_check = "live_admission_status 不再停留在 blocked_by_premarket_commands，或阻断数下降"
            fallback = "若重跑后仍阻断，按新的第一阻断动作继续处理，不跳过 gate"
        elif scope == "no_trade_day":
            resolution_type = "no_trade_context_review"
            execution_owner = "human_review"
            can_auto_trigger = False
            requires_manual_confirmation = True
            recommended_ui_action = "在盘前指挥单中记录无票日归因"
            recommended_api_action = "POST /gen3-state-alpha/no-trade-day-review-and-run"
            evidence_required = "自然空仓、误杀、数据缺口或流程缺口的人工归因"
            completion_check = "无票日复盘落账；自然空仓不触发补票，问题样本进入学习队列"
            fallback = "证据不足时继续观察，不为补满二槽降低买点和选股门槛"
        else:
            resolution_type = "manual_formal_action_review"
            execution_owner = "human_review"
            can_auto_trigger = False
            requires_manual_confirmation = True
            recommended_ui_action = "在盘前指挥单记录已处理或卡住"
            recommended_api_action = "POST /gen3-state-alpha/formal-action-review-and-run"
            evidence_required = "处理证据、卡点说明和重跑审计结果"
            completion_check = "保存证据并重跑审计后，该阻断消失或变成明确继续观察"
            fallback = "无法确认时禁止真实买入，不把流程卡点误归因为策略收益问题"

        rows.append(
            {
                "priority": 0,
                "blocking_key": item.get("ledger_key") or item.get("review_key") or item.get("ticket_key") or "",
                "review_scope": scope,
                "object": item.get("object"),
                "entry_date": item.get("entry_date"),
                "action": item.get("action"),
                "source": source,
                "current_status": item.get("current_status"),
                "resolution_type": resolution_type,
                "execution_owner": execution_owner,
                "can_auto_trigger": bool(can_auto_trigger),
                "requires_manual_confirmation": bool(requires_manual_confirmation),
                "blocks_live_buy": True,
                "recommended_ui_action": recommended_ui_action,
                "recommended_api_action": recommended_api_action,
                "evidence_required": evidence_required,
                "completion_check": completion_check,
                "fallback": fallback,
                "natural_trade_boundary": "先清执行证据，再判断交易逻辑；无票日不补票，不按单日收益倒推放宽规则。",
            }
        )

    if not rows:
        return pd.DataFrame([])
    out = pd.DataFrame(rows)
    type_rank = {
        "broker_holding_price_refresh": 0,
        "rerun_readiness_audit": 1,
        "manual_formal_action_review": 2,
        "no_trade_context_review": 3,
    }
    out["_type_rank"] = out["resolution_type"].map(lambda x: type_rank.get(str(x), 9))
    out = out.sort_values(["_type_rank", "source", "object"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_type_rank"])


def _build_live_blocker_evidence_ledger(
    live_blocker_resolution_plan: pd.DataFrame,
    formal_action_reviews_report: pd.DataFrame,
    no_trade_day_review: pd.DataFrame,
) -> pd.DataFrame:
    if live_blocker_resolution_plan.empty and formal_action_reviews_report.empty:
        return pd.DataFrame([])

    formal_map = {
        str(row.get("review_key") or "").strip(): row
        for row in formal_action_reviews_report.to_dict("records")
        if str(row.get("review_key") or "").strip()
    } if not formal_action_reviews_report.empty else {}
    no_trade_map = {
        str(row.get("review_key") or "").strip(): row
        for row in no_trade_day_review.to_dict("records")
        if str(row.get("review_key") or "").strip()
    } if not no_trade_day_review.empty else {}

    rows: list[dict[str, Any]] = []
    active_keys: set[str] = set()
    for item in live_blocker_resolution_plan.to_dict("records"):
        key = str(item.get("blocking_key") or "").strip()
        if key:
            active_keys.add(key)
        scope = str(item.get("review_scope") or "").strip()
        review = no_trade_map.get(key) if scope == "no_trade_day" else formal_map.get(key)
        review = review or {}
        review_status = str(review.get("review_status") or "").strip()

        if review_status == "validated":
            evidence_status = "validated"
            next_action = "证据已落账；重跑审计确认该阻断是否真实消失。"
        elif review_status == "issue_found":
            evidence_status = "issue_found"
            next_action = "已发现流程或判断隐患；保持禁止真实买入，并进入策略/执行学习队列。"
        elif review_status == "continue_watch":
            evidence_status = "continue_watch"
            next_action = "证据仍不足；继续观察，不为了补票或收益放宽规则。"
        else:
            evidence_status = "pending_evidence"
            next_action = item.get("recommended_ui_action") or "补充处理证据后重跑审计。"

        rows.append(
            {
                "priority": 0,
                "review_key": key,
                "blocking_key": key,
                "review_scope": scope,
                "object": item.get("object"),
                "source": item.get("source"),
                "action": item.get("action"),
                "resolution_type": item.get("resolution_type"),
                "evidence_status": evidence_status,
                "review_result": review.get("review_result"),
                "review_result_label": review.get("review_result_label"),
                "issue_area": review.get("issue_area"),
                "evidence": review.get("evidence"),
                "evidence_required": item.get("evidence_required"),
                "review_note": review.get("review_note"),
                "updated_at": review.get("updated_at"),
                "recommended_ui_action": item.get("recommended_ui_action"),
                "next_action": next_action,
                "completion_check": item.get("completion_check"),
                "natural_trade_boundary": item.get("natural_trade_boundary"),
            }
        )

    for key, review in formal_map.items():
        if key in active_keys:
            continue
        review_status = str(review.get("review_status") or "").strip()
        if review_status not in {"validated", "issue_found", "continue_watch"}:
            continue
        if review_status == "validated":
            evidence_status = "validated"
            next_action = "该阻断已有处理证据且已从当前盘前阻断中消失；保留记录用于盘后复盘。"
        elif review_status == "issue_found":
            evidence_status = "issue_found"
            next_action = "已发现流程或判断隐患；保持禁止真实买入，并进入策略/执行学习队列。"
        else:
            evidence_status = "continue_watch"
            next_action = "证据不足但已有观察记录；不为了补票或收益放宽规则。"
        rows.append(
            {
                "priority": 0,
                "review_key": key,
                "blocking_key": key,
                "review_scope": "formal_action",
                "object": review.get("object"),
                "source": review.get("source") or "formal_action_reviews",
                "action": review.get("action"),
                "resolution_type": "resolved_formal_action_review",
                "evidence_status": evidence_status,
                "review_result": review.get("review_result"),
                "review_result_label": review.get("review_result_label"),
                "issue_area": review.get("issue_area"),
                "evidence": review.get("evidence"),
                "evidence_required": review.get("evidence") or review.get("review_note"),
                "review_note": review.get("review_note"),
                "updated_at": review.get("updated_at"),
                "recommended_ui_action": "已关闭；如状态异常，重新运行实战前审计。",
                "next_action": next_action,
                "completion_check": "复核当前盘前指挥单中不再出现该阻断；若再次出现，继续补证据而不跳过 gate。",
                "natural_trade_boundary": "保留已关闭证据用于追溯；不把流程清理结果误当成策略收益优化结论。",
            }
        )

    out = pd.DataFrame(rows)
    status_rank = {"issue_found": 0, "pending_evidence": 1, "continue_watch": 2, "validated": 3}
    out["_status_rank"] = out["evidence_status"].map(lambda x: status_rank.get(str(x), 9))
    out = out.sort_values(["_status_rank", "priority", "review_scope", "object"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_status_rank"])


def _build_live_premarket_action_sequence(
    live_blocker_resolution_plan: pd.DataFrame,
    live_blocker_evidence_ledger: pd.DataFrame,
) -> pd.DataFrame:
    if live_blocker_resolution_plan.empty:
        return pd.DataFrame([])

    evidence_by_key = {}
    if not live_blocker_evidence_ledger.empty:
        for row in live_blocker_evidence_ledger.to_dict("records"):
            key = str(row.get("blocking_key") or "").strip()
            if key:
                evidence_by_key[key] = str(row.get("evidence_status") or "").strip()

    group_map = {
        "broker_holding_price_refresh": "sync_broker_holding_price",
        "rerun_readiness_audit": "rerun_readiness_audit",
        "no_trade_context_review": "review_no_trade_context",
        "manual_formal_action_review": "manual_formal_action_review",
    }
    group_rank = {
        "sync_broker_holding_price": 1,
        "rerun_readiness_audit": 2,
        "review_no_trade_context": 3,
        "manual_formal_action_review": 4,
    }
    group_label = {
        "sync_broker_holding_price": "同步真实持仓/价格并复审",
        "rerun_readiness_audit": "重跑实战前审计",
        "review_no_trade_context": "记录无票日归因",
        "manual_formal_action_review": "人工确认正式动作",
    }
    expected_effect = {
        "sync_broker_holding_price": "刷新并复审后，持仓/价格阻断应消失或明显下降。",
        "rerun_readiness_audit": "把已处理事实重新写入准入快照，确认是否仍禁止真实买入。",
        "review_no_trade_context": "区分自然空仓、误杀、数据缺口和流程缺口，不为补满仓位强行交易。",
        "manual_formal_action_review": "补齐人工证据，避免把流程卡点误判为策略买卖点问题。",
    }
    stop_if_fail = {
        "sync_broker_holding_price": "如果同步失败或窗口不可用，禁止真实买入，只记录卡点证据。",
        "rerun_readiness_audit": "如果重跑后仍阻断，回到新的第一阻断动作继续处理，不跳过 gate。",
        "review_no_trade_context": "如果证据不足，继续观察，不放宽买点或选股门槛。",
        "manual_formal_action_review": "如果无法确认，保持 not_formal_ready。",
    }

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in live_blocker_resolution_plan.to_dict("records"):
        resolution_type = str(item.get("resolution_type") or "").strip()
        action_group = group_map.get(resolution_type, "manual_formal_action_review")
        grouped.setdefault(action_group, []).append(item)

    rows: list[dict[str, Any]] = []
    has_refresh_pending = False
    has_audit_pending = False

    def _date_from_key(key: str) -> str:
        first = str(key or "").split("|", 1)[0].strip()
        if len(first) == 10 and first[4:5] == "-" and first[7:8] == "-":
            return first
        return ""

    for action_group in sorted(grouped, key=lambda x: group_rank.get(x, 99)):
        items = grouped[action_group]
        keys = [str(item.get("blocking_key") or "").strip() for item in items if str(item.get("blocking_key") or "").strip()]
        statuses = [evidence_by_key.get(key, "pending_evidence") for key in keys]
        pending_statuses = {status for status in statuses if status not in {"validated"}}
        primary = items[0]
        entry_dates = [str(item.get("entry_date") or "").strip() for item in items if str(item.get("entry_date") or "").strip()]
        if not entry_dates:
            entry_dates = [_date_from_key(key) for key in keys if _date_from_key(key)]

        if action_group == "sync_broker_holding_price":
            can_execute_now = True
            has_refresh_pending = bool(pending_statuses)
        elif action_group == "rerun_readiness_audit":
            can_execute_now = not has_refresh_pending
            has_audit_pending = bool(pending_statuses)
        elif action_group == "review_no_trade_context":
            can_execute_now = not has_refresh_pending and not has_audit_pending
        else:
            can_execute_now = not has_refresh_pending

        rows.append(
            {
                "step": 0,
                "action_group": action_group,
                "action_label": group_label.get(action_group, action_group),
                "blocker_count": len(items),
                "primary_blocking_key": keys[0] if keys else "",
                "review_scope": " / ".join(sorted({str(item.get("review_scope") or "").strip() for item in items if str(item.get("review_scope") or "").strip()})),
                "object": " / ".join([str(item.get("object") or "").strip() for item in items if str(item.get("object") or "").strip()][:3]),
                "entry_date": entry_dates[0] if entry_dates else "",
                "source": " / ".join(sorted({str(item.get("source") or "").strip() for item in items if str(item.get("source") or "").strip()})),
                "action": primary.get("action"),
                "resolution_types": " / ".join(sorted({str(item.get("resolution_type") or "").strip() for item in items if str(item.get("resolution_type") or "").strip()})),
                "evidence_statuses": " / ".join(sorted({status for status in statuses if status})) or "pending_evidence",
                "can_execute_now": bool(can_execute_now),
                "requires_manual_confirmation": any(_truthy(item.get("requires_manual_confirmation")) for item in items),
                "recommended_ui_action": primary.get("recommended_ui_action"),
                "recommended_api_action": primary.get("recommended_api_action"),
                "expected_effect": expected_effect.get(action_group, ""),
                "stop_if_fail": stop_if_fail.get(action_group, ""),
                "completion_check": primary.get("completion_check"),
                "natural_trade_boundary": primary.get("natural_trade_boundary"),
            }
        )

    out = pd.DataFrame(rows).sort_values("action_group", key=lambda s: s.map(lambda x: group_rank.get(str(x), 99)), kind="stable")
    out = out.reset_index(drop=True)
    out["step"] = range(1, len(out) + 1)
    return out


def _build_live_premarket_execution_recheck(
    summary: dict[str, Any],
    live_admission_snapshot: pd.DataFrame,
    live_premarket_action_sequence: pd.DataFrame,
    live_blocker_evidence_ledger: pd.DataFrame,
) -> pd.DataFrame:
    admission = live_admission_snapshot.iloc[0].to_dict() if not live_admission_snapshot.empty else {}
    sequence_rows = live_premarket_action_sequence.to_dict("records") if not live_premarket_action_sequence.empty else []
    evidence_status = (
        live_blocker_evidence_ledger.get("evidence_status", pd.Series(dtype=str)).astype(str)
        if not live_blocker_evidence_ledger.empty
        else pd.Series(dtype=str)
    )
    pending_evidence_count = int((evidence_status == "pending_evidence").sum())
    validated_evidence_count = int((evidence_status == "validated").sum())
    issue_evidence_count = int((evidence_status == "issue_found").sum())
    ready_rows = [row for row in sequence_rows if _truthy(row.get("can_execute_now"))]
    current = ready_rows[0] if ready_rows else (sequence_rows[0] if sequence_rows else {})
    live_buy_allowed = _truthy(admission.get("live_buy_allowed"))
    blocking_count = int(_safe_float(admission.get("blocking_command_count"), 0) or 0)

    if live_buy_allowed and blocking_count == 0:
        recheck_status = "ready_for_manual_live_review"
        recheck_label = "可进入人工实盘复核"
        next_operator_action = "复核候选、真实仓位和现金约束后，人工决定是否实盘执行。"
    elif issue_evidence_count:
        recheck_status = "issue_found_hold_live"
        recheck_label = "发现隐患，禁止实盘"
        next_operator_action = "先处理已发现隐患；不要把流程或数据缺口当成策略收益优化问题。"
    elif current:
        recheck_status = "next_action_required"
        recheck_label = "继续处理下一动作"
        next_operator_action = current.get("action_label") or current.get("recommended_ui_action") or "继续按盘前顺序处理。"
    else:
        recheck_status = "observe_no_blocker"
        recheck_label = "无阻断，保持观察"
        next_operator_action = "没有正式票时保持自然空仓观察；不为补满仓位放宽规则。"

    return pd.DataFrame(
        [
            {
                "generated_at": summary.get("generated_at"),
                "recheck_status": recheck_status,
                "recheck_label": recheck_label,
                "live_buy_allowed": bool(live_buy_allowed),
                "blocking_command_count": blocking_count,
                "sequence_step_count": len(sequence_rows),
                "ready_step_count": len(ready_rows),
                "pending_evidence_count": pending_evidence_count,
                "validated_evidence_count": validated_evidence_count,
                "issue_evidence_count": issue_evidence_count,
                "current_step": current.get("step"),
                "current_action_group": current.get("action_group"),
                "current_action_label": current.get("action_label"),
                "current_can_execute_now": bool(_truthy(current.get("can_execute_now"))) if current else False,
                "next_operator_action": next_operator_action,
                "recheck_rule": "每次处理盘前动作后必须重跑审计；只有 admission 放行且阻断数为 0，才允许进入人工实盘复核。",
                "natural_trade_boundary": "先验证事实，再评价策略；无票日允许自然空仓，不用收益压力反推放宽买点、选股或策略切换规则。",
            }
        ]
    )


def _build_live_manual_launch_acceptance(
    summary: dict[str, Any],
    live_admission_snapshot: pd.DataFrame,
    live_premarket_execution_recheck: pd.DataFrame,
    live_daily_review_action_layers: pd.DataFrame,
) -> pd.DataFrame:
    admission = live_admission_snapshot.iloc[0].to_dict() if not live_admission_snapshot.empty else {}
    recheck = live_premarket_execution_recheck.iloc[0].to_dict() if not live_premarket_execution_recheck.empty else {}
    layers = live_daily_review_action_layers.to_dict("records") if not live_daily_review_action_layers.empty else []
    layer_by_key = {str(row.get("action_layer") or ""): row for row in layers}
    premarket_layer = layer_by_key.get("premarket_blocker_clear", {})
    strategy_layer = layer_by_key.get("intraday_strategy_watch", {})
    learning_layer = layer_by_key.get("after_close_learning_attribution", {})

    def add(
        rows: list[dict[str, Any]],
        item: str,
        label: str,
        passed: bool,
        hard: bool,
        current_value: Any,
        required_value: Any,
        evidence: Any,
        next_action: Any,
        boundary: Any,
    ) -> None:
        rows.append(
            {
                "priority": len(rows) + 1,
                "acceptance_item": item,
                "acceptance_label": label,
                "status": "pass" if passed else "block" if hard else "watch",
                "status_label": "通过" if passed else "阻断" if hard else "观察",
                "is_hard_blocker": bool(hard and not passed),
                "must_pass_before_live": bool(hard),
                "current_value": current_value,
                "required_value": required_value,
                "evidence": evidence or "--",
                "next_action": next_action or "--",
                "natural_trade_boundary": boundary or "--",
            }
        )

    rows: list[dict[str, Any]] = []
    admission_allowed = _truthy(admission.get("live_buy_allowed"))
    blocking_count = int(_safe_float(admission.get("blocking_command_count"), 0) or 0)
    pending_evidence = int(_safe_float(recheck.get("pending_evidence_count"), 0) or 0)
    issue_evidence = int(_safe_float(recheck.get("issue_evidence_count"), 0) or 0)
    premarket_pending = int(_safe_float(premarket_layer.get("pending_count"), 0) or 0)
    strategy_pending = int(_safe_float(strategy_layer.get("pending_count"), 0) or 0)
    learning_pending = int(_safe_float(learning_layer.get("pending_count"), 0) or 0)
    ticket_count = int(_safe_float(admission.get("ticket_count"), summary.get("next_trade_ticket_count") or 0) or 0)

    add(
        rows,
        "admission_live_buy_allowed",
        "准入快照允许真实买入",
        admission_allowed,
        True,
        admission.get("admission_status"),
        "manual_live_ready + live_buy_allowed=true",
        admission.get("primary_reason"),
        admission.get("next_step"),
        "准入只来自审计快照，不来自人工愿望或收益压力。",
    )
    add(
        rows,
        "premarket_blockers_cleared",
        "盘前硬阻断已清零",
        blocking_count == 0,
        True,
        blocking_count,
        0,
        admission.get("first_required_action"),
        "按盘前动作顺序处理并重跑审计。",
        "真实持仓/价格/执行证据未清前，不讨论买点优化。",
    )
    add(
        rows,
        "premarket_evidence_closed",
        "盘前证据闭环完成",
        pending_evidence == 0 and issue_evidence == 0,
        True,
        f"pending={pending_evidence}; issue={issue_evidence}",
        "pending=0; issue=0",
        recheck.get("current_action_label"),
        recheck.get("next_operator_action"),
        "有证据不等于放行；证据必须让审计阻断真实消失。",
    )
    add(
        rows,
        "premarket_action_layer_done",
        "盘前行动层已完成",
        premarket_pending == 0,
        True,
        premarket_pending,
        0,
        premarket_layer.get("first_action"),
        premarket_layer.get("operator_instruction"),
        premarket_layer.get("buy_permission_effect"),
    )
    add(
        rows,
        "ticket_context",
        "下一交易日票据上下文明确",
        ticket_count > 0 or str(admission.get("admission_status") or "").startswith("no_buy") or not admission_allowed,
        False,
        ticket_count,
        "有票则逐票复核；无票则自然空仓复盘",
        admission.get("execution_posture"),
        "有票只进入人工最终复核；无票不为补仓放宽规则。",
        "允许自然空仓，不用收益压力反推补票。",
    )
    add(
        rows,
        "strategy_watch_layer",
        "盘中/盘后策略观察已安排",
        strategy_pending == 0,
        False,
        strategy_pending,
        "可盘中/盘后落账，不作为盘前硬阻断",
        strategy_layer.get("first_action"),
        strategy_layer.get("operator_instruction"),
        strategy_layer.get("buy_permission_effect"),
    )
    add(
        rows,
        "learning_attribution_layer",
        "盘后学习归因有入口",
        learning_pending == 0,
        False,
        learning_pending,
        "仅对已发现隐患/继续观察样本归因",
        learning_layer.get("first_action"),
        learning_layer.get("operator_instruction") or "没有 issue_found/continue_watch 样本时不凭空生成优化结论。",
        "先归因，再改合同；不按单日收益倒推参数。",
    )
    out = pd.DataFrame(rows)
    out["_hard_rank"] = out["must_pass_before_live"].map(lambda x: 0 if _truthy(x) else 1)
    out["_status_rank"] = out["status"].map({"block": 0, "watch": 1, "pass": 2}).fillna(9)
    out = out.sort_values(["_hard_rank", "_status_rank", "priority"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_hard_rank", "_status_rank"])


def _build_live_day1_review_journal(
    live_premarket_action_sequence: pd.DataFrame,
    live_blocker_evidence_ledger: pd.DataFrame,
    live_manual_launch_acceptance: pd.DataFrame,
    live_daily_review_execution_checklist: pd.DataFrame,
    day1_paper_review_pack: pd.DataFrame,
    day1_after_close_review_queue: pd.DataFrame,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []

    def add(
        window: str,
        checkpoint_time: str,
        journal_type: str,
        source_table: str,
        row: dict[str, Any],
        *,
        review_axis: str = "",
        status: str = "",
        is_blocking: bool = False,
        required_before_live: bool = False,
        action: Any = "",
        evidence_to_record: Any = "",
        pass_condition: Any = "",
        fail_condition: Any = "",
        next_action: Any = "",
        boundary: Any = "",
        review_key: Any = "",
        ticket_key: Any = "",
    ) -> None:
        rows.append(
            {
                "priority": 0,
                "journal_window": window,
                "checkpoint_time": checkpoint_time,
                "journal_type": journal_type,
                "source_table": source_table,
                "object": row.get("object") or row.get("acceptance_label") or row.get("action_label") or row.get("code") or "",
                "code": row.get("code") or "",
                "name": row.get("name") or "",
                "entry_date": row.get("entry_date") or "",
                "review_axis": review_axis or row.get("review_axis") or "",
                "status": status or row.get("status") or row.get("evidence_status") or row.get("review_status") or row.get("after_close_status") or row.get("launch_posture") or "",
                "is_blocking": bool(is_blocking),
                "required_before_live": bool(required_before_live),
                "action": action or row.get("action") or row.get("target_action") or row.get("next_action") or "",
                "evidence_to_record": evidence_to_record or row.get("evidence_to_collect") or row.get("evidence_required") or row.get("evidence") or "",
                "pass_condition": pass_condition or row.get("pass_condition") or row.get("required_value") or "",
                "fail_condition": fail_condition or row.get("fail_condition") or "",
                "next_action": next_action or row.get("next_action") or row.get("operator_instruction") or "",
                "natural_trade_boundary": boundary or row.get("natural_trade_boundary") or "",
                "review_key": review_key or row.get("review_key") or row.get("blocking_key") or row.get("primary_blocking_key") or row.get("acceptance_item") or "",
                "ticket_key": ticket_key or row.get("ticket_key") or "",
            }
        )

    for row in live_premarket_action_sequence.to_dict("records") if not live_premarket_action_sequence.empty else []:
        add(
            "盘前",
            "09:00-09:25",
            "盘前清障顺序",
            "live_premarket_action_sequence",
            row,
            review_axis="execution_evidence",
            status="ready" if _truthy(row.get("can_execute_now")) else "waiting_previous_step",
            is_blocking=True,
            required_before_live=True,
            action=row.get("action_label") or row.get("recommended_ui_action"),
            evidence_to_record=row.get("expected_effect"),
            pass_condition=row.get("completion_check"),
            fail_condition=row.get("stop_if_fail"),
            next_action=row.get("recommended_ui_action") or row.get("action_label"),
        )

    for row in live_blocker_evidence_ledger.to_dict("records") if not live_blocker_evidence_ledger.empty else []:
        add(
            "盘前",
            "09:00-09:30",
            "阻断证据闭环",
            "live_blocker_evidence_ledger",
            row,
            review_axis="execution_evidence",
            status=row.get("evidence_status"),
            is_blocking=str(row.get("evidence_status") or "") in {"pending_evidence", "issue_found"},
            required_before_live=str(row.get("evidence_status") or "") in {"pending_evidence", "issue_found"},
            evidence_to_record=row.get("evidence_required") or row.get("review_note"),
            pass_condition=row.get("completion_check"),
            fail_condition=row.get("next_action"),
        )

    for row in live_manual_launch_acceptance.to_dict("records") if not live_manual_launch_acceptance.empty else []:
        add(
            "盘前",
            "09:25-09:30",
            "人工实战验收",
            "live_manual_launch_acceptance",
            row,
            review_axis="execution_evidence",
            status=row.get("status"),
            is_blocking=_truthy(row.get("is_hard_blocker")),
            required_before_live=_truthy(row.get("must_pass_before_live")),
            action=row.get("next_action"),
            evidence_to_record=row.get("evidence"),
            pass_condition=row.get("required_value"),
            fail_condition=row.get("natural_trade_boundary"),
            review_key=row.get("acceptance_item"),
        )

    for row in live_daily_review_execution_checklist.to_dict("records") if not live_daily_review_execution_checklist.empty else []:
        add(
            str(row.get("review_window") or "盘中/盘后"),
            str(row.get("checkpoint_time") or ""),
            "逐项复盘检查",
            "live_daily_review_execution_checklist",
            row,
            status=row.get("review_status"),
            is_blocking=str(row.get("review_status") or "") in {"issue_found", "blocked", "data_gap", "process_gap"},
            required_before_live=str(row.get("review_window") or "") in {"盘前", "盘前/盘后"} and str(row.get("review_status") or "") not in {"validated"},
            action=row.get("target_action"),
            evidence_to_record=row.get("evidence_to_collect"),
        )

    for row in day1_paper_review_pack.to_dict("records") if not day1_paper_review_pack.empty else []:
        add(
            "盘中/盘后",
            "交易时段/收盘后",
            "Day1纸面跟踪",
            "day1_paper_review_pack",
            row,
            review_axis="buy_point/selection/model_switch/sell_point",
            status=row.get("launch_posture"),
            is_blocking=False,
            required_before_live=False,
            action=row.get("next_action"),
            evidence_to_record=row.get("hidden_risk_focus"),
            pass_condition=row.get("buy_point_review"),
            fail_condition=row.get("selection_review"),
            boundary=row.get("exit_contract_review"),
            review_key=row.get("ticket_key") or row.get("code"),
            ticket_key=row.get("ticket_key"),
        )

    for row in day1_after_close_review_queue.to_dict("records") if not day1_after_close_review_queue.empty else []:
        add(
            "盘后",
            "15:10后",
            "Day1盘后归因",
            "day1_after_close_review_queue",
            row,
            review_axis=row.get("issue_area") or "after_close_attribution",
            status=row.get("after_close_status"),
            is_blocking=str(row.get("after_close_status") or "") == "issue_found",
            required_before_live=False,
            action=row.get("next_action"),
            evidence_to_record=row.get("hidden_risk_focus"),
            pass_condition="只归因，不按单日收益倒推改合同。",
            fail_condition="若发现流程/数据/逻辑缺口，进入学习队列，下一日不放宽准入。",
            review_key=row.get("ticket_key") or row.get("code"),
            ticket_key=row.get("ticket_key"),
        )

    if not rows:
        return pd.DataFrame([])
    out = pd.DataFrame(rows)
    window_rank = {"盘前": 0, "盘前/盘后": 1, "盘中": 2, "盘中/盘后": 3, "盘后": 4}
    type_rank = {
        "盘前清障顺序": 0,
        "阻断证据闭环": 1,
        "人工实战验收": 2,
        "逐项复盘检查": 3,
        "Day1纸面跟踪": 4,
        "Day1盘后归因": 5,
    }
    out["_window_rank"] = out["journal_window"].map(lambda x: window_rank.get(str(x), 9))
    out["_type_rank"] = out["journal_type"].map(lambda x: type_rank.get(str(x), 9))
    out["_block_rank"] = out["is_blocking"].map(lambda x: 0 if _truthy(x) else 1)
    out = out.sort_values(["_window_rank", "_type_rank", "_block_rank", "checkpoint_time", "object"], kind="stable").reset_index(drop=True)
    out["priority"] = range(1, len(out) + 1)
    return out.drop(columns=["_window_rank", "_type_rank", "_block_rank"])


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
    contract_block = _institutional_mom60_contract_block(row)

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

    if contract_block:
        blockers.append(contract_block)

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


def _gate_rows(current: dict[str, Any], broker: dict[str, Any], ticket_review: pd.DataFrame, holding_review: pd.DataFrame) -> pd.DataFrame:
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
    return pd.DataFrame(gates)


def _hazard_register(ticket_review: pd.DataFrame, holding_review: pd.DataFrame, candidate_review: pd.DataFrame, gate_rows: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    if "risk_level" not in ticket_review.columns:
        ticket_review = ticket_review.copy()
        ticket_review["risk_level"] = pd.Series(dtype=str)
    if "risk_level" not in holding_review.columns:
        holding_review = holding_review.copy()
        holding_review["risk_level"] = pd.Series(dtype=str)
    if "issues" not in candidate_review.columns:
        candidate_review = candidate_review.copy()
        candidate_review["issues"] = pd.Series(dtype=str)
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
    gates = _gate_rows(current, broker, ticket_review, holding_review)
    hazards = _hazard_register(ticket_review, holding_review, candidate_review, gates)
    action_checklist = _build_pretrade_action_checklist(gates, ticket_checklist, holding_checklist, candidate_checklist, natural_execution_matrix)
    no_trade_day_review = _build_no_trade_day_review(current, next_trade_tickets, candidate_checklist, gates, holding_refresh_evidence)

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
        ("no_trade_day_review.csv", no_trade_day_review),
    ]:
        _write_report_csv(name, df)

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
    candidate_omission_pending_count = (
        int((candidate_checklist.get("omission_watch_status", pd.Series(dtype=str)).astype(str) == "pending_observation").sum())
        if not candidate_checklist.empty
        else 0
    )
    candidate_omission_issue_found_count = (
        int((candidate_checklist.get("omission_watch_status", pd.Series(dtype=str)).astype(str) == "issue_found").sum())
        if not candidate_checklist.empty
        else 0
    )
    candidate_omission_validated_count = (
        int((candidate_checklist.get("omission_watch_status", pd.Series(dtype=str)).astype(str) == "validated").sum())
        if not candidate_checklist.empty
        else 0
    )
    candidate_omission_continue_watch_count = (
        int((candidate_checklist.get("omission_watch_status", pd.Series(dtype=str)).astype(str) == "continue_watch").sum())
        if not candidate_checklist.empty
        else 0
    )
    no_trade_day_review_count = len(no_trade_day_review)
    no_trade_day_formal_required_count = (
        int(no_trade_day_review.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy).sum())
        if not no_trade_day_review.empty
        else 0
    )
    no_trade_day_pending_review_count = (
        int((no_trade_day_review.get("review_status", pd.Series(dtype=str)).astype(str) == "pending_review").sum())
        if not no_trade_day_review.empty
        else 0
    )
    no_trade_day_issue_found_count = (
        int((no_trade_day_review.get("review_status", pd.Series(dtype=str)).astype(str) == "issue_found").sum())
        if not no_trade_day_review.empty
        else 0
    )
    no_trade_day_validated_count = (
        int((no_trade_day_review.get("review_status", pd.Series(dtype=str)).astype(str) == "validated").sum())
        if not no_trade_day_review.empty
        else 0
    )
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
        "candidate_omission_pending_count": candidate_omission_pending_count,
        "candidate_omission_issue_found_count": candidate_omission_issue_found_count,
        "candidate_omission_validated_count": candidate_omission_validated_count,
        "candidate_omission_continue_watch_count": candidate_omission_continue_watch_count,
        "no_trade_day_review_count": no_trade_day_review_count,
        "no_trade_day_formal_required_count": no_trade_day_formal_required_count,
        "no_trade_day_pending_review_count": no_trade_day_pending_review_count,
        "no_trade_day_issue_found_count": no_trade_day_issue_found_count,
        "no_trade_day_validated_count": no_trade_day_validated_count,
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
    if summary["next_trade_ticket_count"] == 0:
        if formal_launch_block_count:
            formal_launch_status = "no_trade_observe_blocked"
            formal_launch_next_step = "不买入；先处理硬阻断，再保留无票日观察复盘"
        elif summary["pretrade_formal_action_pending_count"]:
            formal_launch_status = "no_trade_observe_pending"
            formal_launch_next_step = "不买入；先处理真实持仓/价格刷新等正式必处理项，再保留候选观察"
        else:
            formal_launch_status = "no_trade_observe_ready"
            formal_launch_next_step = "不买入；按无票日复盘观察候选是否盘后误杀"
    elif formal_launch_block_count:
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
    _write_report_csv("formal_launch_checklist.csv", formal_launch_checklist)
    execution_mode_matrix = _build_execution_mode_matrix(summary, current, broker, gates)
    _write_report_csv("execution_mode_matrix.csv", execution_mode_matrix)
    summary["execution_mode_ready_count"] = int(execution_mode_matrix.get("allowed_now", pd.Series(dtype=bool)).map(_truthy).sum()) if not execution_mode_matrix.empty else 0
    summary["execution_mode_locked_count"] = int((execution_mode_matrix.get("status", pd.Series(dtype=str)).astype(str) == "locked").sum()) if not execution_mode_matrix.empty else 0
    formal_launch_action_queue = _build_formal_launch_action_queue(action_checklist, ticket_checklist, formal_launch_checklist)
    _write_report_csv("formal_launch_action_queue.csv", formal_launch_action_queue)
    summary["formal_launch_action_queue_count"] = len(formal_launch_action_queue)
    formal_action_reviews_report = _build_formal_action_reviews_report()
    _write_report_csv("formal_action_reviews.csv", formal_action_reviews_report)
    formal_action_review_status = formal_action_reviews_report.get("review_status", pd.Series(dtype=str)).astype(str) if not formal_action_reviews_report.empty else pd.Series(dtype=str)
    summary["formal_action_review_count"] = len(formal_action_reviews_report)
    summary["formal_action_review_validated_count"] = int((formal_action_review_status == "validated").sum())
    summary["formal_action_review_issue_found_count"] = int((formal_action_review_status == "issue_found").sum())
    summary["formal_action_review_continue_watch_count"] = int((formal_action_review_status == "continue_watch").sum())
    daily_review_checklist_reviews_report = _build_daily_review_checklist_reviews_report()
    _write_report_csv("daily_review_checklist_reviews.csv", daily_review_checklist_reviews_report)
    daily_checklist_review_status = daily_review_checklist_reviews_report.get("review_status", pd.Series(dtype=str)).astype(str) if not daily_review_checklist_reviews_report.empty else pd.Series(dtype=str)
    summary["daily_review_checklist_review_count"] = len(daily_review_checklist_reviews_report)
    summary["daily_review_checklist_validated_count"] = int((daily_checklist_review_status == "validated").sum())
    summary["daily_review_checklist_issue_found_count"] = int((daily_checklist_review_status == "issue_found").sum())
    summary["daily_review_checklist_continue_watch_count"] = int((daily_checklist_review_status == "continue_watch").sum())
    pretrade_review_evidence = _build_pretrade_review_evidence(ticket_review, ticket_checklist)
    _write_report_csv("pretrade_review_evidence.csv", pretrade_review_evidence)
    summary["pretrade_review_evidence_count"] = len(pretrade_review_evidence)
    paper_watch_followup = _build_paper_watch_followup(ticket_review, pretrade_review_evidence)
    _write_report_csv("paper_watch_followup.csv", paper_watch_followup)
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
    _write_report_csv("premarket_execution_playbook.csv", premarket_playbook)
    summary["premarket_playbook_step_count"] = len(premarket_playbook)
    day1_paper_review_pack = _build_day1_paper_review_pack(
        ticket_review,
        pretrade_review_evidence,
        natural_consistency,
        natural_execution_matrix,
        ticket_checklist,
    )
    _write_report_csv("day1_paper_review_pack.csv", day1_paper_review_pack)
    summary["day1_paper_review_ticket_count"] = len(day1_paper_review_pack)
    summary["day1_paper_review_pending_count"] = (
        int(day1_paper_review_pack.get("launch_posture", pd.Series(dtype=str)).astype(str).isin({"paper_watch", "unreviewed_to_paper_watch", "wait_refresh"}).sum())
        if not day1_paper_review_pack.empty
        else 0
    )
    day1_after_close_review_queue = _build_day1_after_close_review_queue(day1_paper_review_pack, paper_watch_followup)
    _write_report_csv("day1_after_close_review_queue.csv", day1_after_close_review_queue)
    summary["day1_after_close_review_count"] = len(day1_after_close_review_queue)
    summary["day1_after_close_pending_execution_count"] = (
        int((day1_after_close_review_queue.get("after_close_status", pd.Series(dtype=str)).astype(str) == "pending_paper_execution").sum())
        if not day1_after_close_review_queue.empty
        else 0
    )
    summary["day1_after_close_pending_review_count"] = (
        int((day1_after_close_review_queue.get("after_close_status", pd.Series(dtype=str)).astype(str) == "pending_after_close_review").sum())
        if not day1_after_close_review_queue.empty
        else 0
    )
    summary["day1_after_close_issue_found_count"] = (
        int((day1_after_close_review_queue.get("after_close_status", pd.Series(dtype=str)).astype(str) == "issue_found").sum())
        if not day1_after_close_review_queue.empty
        else 0
    )
    daily_live_review_board = _build_daily_live_review_board(
        formal_launch_action_queue,
        no_trade_day_review,
        candidate_checklist,
        day1_paper_review_pack,
        day1_after_close_review_queue,
    )
    _write_report_csv("daily_live_review_board.csv", daily_live_review_board)
    board_status = daily_live_review_board.get("review_status", pd.Series(dtype=str)).astype(str) if not daily_live_review_board.empty else pd.Series(dtype=str)
    summary["daily_live_review_board_count"] = len(daily_live_review_board)
    summary["daily_live_review_board_pending_count"] = int(
        board_status.isin({"pending", "pending_review", "pending_observation", "pending_after_close_review", "pending_paper_execution"}).sum()
    )
    summary["daily_live_review_board_formal_required_count"] = (
        int(daily_live_review_board.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy).sum())
        if not daily_live_review_board.empty
        else 0
    )
    summary["daily_live_review_board_issue_found_count"] = int(board_status.isin({"block", "issue_found"}).sum())
    strategy_learning_backlog = _build_strategy_learning_backlog(
        no_trade_day_review,
        candidate_checklist,
        paper_watch_followup,
        day1_after_close_review_queue,
        formal_action_reviews_report,
        daily_review_checklist_reviews_report,
    )
    _write_report_csv("strategy_learning_backlog.csv", strategy_learning_backlog)
    learning_status = strategy_learning_backlog.get("learning_status", pd.Series(dtype=str)).astype(str) if not strategy_learning_backlog.empty else pd.Series(dtype=str)
    summary["strategy_learning_backlog_count"] = len(strategy_learning_backlog)
    summary["strategy_learning_needs_review_count"] = int((learning_status == "needs_review").sum())
    summary["strategy_learning_watch_more_count"] = int((learning_status == "watch_more").sum())
    first_live_decision_card = _build_first_live_decision_card(
        summary,
        formal_launch_action_queue,
        daily_live_review_board,
        strategy_learning_backlog,
    )
    _write_report_csv("first_live_decision_card.csv", first_live_decision_card)
    first_live_decision = first_live_decision_card.iloc[0].to_dict() if not first_live_decision_card.empty else {}
    summary["first_live_decision_status"] = first_live_decision.get("decision_status")
    summary["first_live_decision_label"] = first_live_decision.get("decision_label")
    summary["first_live_execution_posture"] = first_live_decision.get("execution_posture")
    summary["first_live_primary_reason"] = first_live_decision.get("primary_reason")
    summary["first_live_first_required_action"] = first_live_decision.get("first_required_action")
    live_review_task_queue = _build_live_review_task_queue(first_live_decision_card, daily_live_review_board)
    _write_report_csv("live_review_task_queue.csv", live_review_task_queue)
    task_windows = live_review_task_queue.get("window", pd.Series(dtype=str)).astype(str) if not live_review_task_queue.empty else pd.Series(dtype=str)
    summary["live_review_task_count"] = len(live_review_task_queue)
    summary["live_review_premarket_task_count"] = int((task_windows == "盘前").sum())
    summary["live_review_intraday_after_task_count"] = int((task_windows == "盘中/盘后").sum())
    summary["live_review_after_close_task_count"] = int((task_windows == "盘后").sum())
    summary["live_review_formal_required_task_count"] = (
        int(live_review_task_queue.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy).sum())
        if not live_review_task_queue.empty
        else 0
    )
    review_coverage_dashboard = _build_review_coverage_dashboard(live_review_task_queue)
    _write_report_csv("review_coverage_dashboard.csv", review_coverage_dashboard)
    coverage_status = review_coverage_dashboard.get("coverage_status", pd.Series(dtype=str)).astype(str) if not review_coverage_dashboard.empty else pd.Series(dtype=str)
    summary["review_coverage_scope_count"] = len(review_coverage_dashboard)
    summary["review_coverage_pending_scope_count"] = int((coverage_status == "pending_review").sum())
    summary["review_coverage_issue_scope_count"] = int((coverage_status == "issue_found").sum())
    summary["review_coverage_continue_watch_scope_count"] = int((coverage_status == "continue_watch").sum())
    live_review_evidence_rubric = _build_live_review_evidence_rubric(live_review_task_queue)
    _write_report_csv("live_review_evidence_rubric.csv", live_review_evidence_rubric)
    summary["live_review_rubric_scope_count"] = len(live_review_evidence_rubric)
    summary["live_review_rubric_pending_scope_count"] = (
        int((live_review_evidence_rubric.get("pending_count", pd.Series(dtype=int)).fillna(0).astype(int) > 0).sum())
        if not live_review_evidence_rubric.empty
        else 0
    )
    summary["live_review_rubric_formal_scope_count"] = (
        int((live_review_evidence_rubric.get("formal_required_count", pd.Series(dtype=int)).fillna(0).astype(int) > 0).sum())
        if not live_review_evidence_rubric.empty
        else 0
    )
    live_premarket_command_sheet = _build_live_premarket_command_sheet(
        first_live_decision_card,
        live_review_task_queue,
        live_review_evidence_rubric,
    )
    _write_report_csv("live_premarket_command_sheet.csv", live_premarket_command_sheet)
    summary["live_premarket_command_count"] = len(live_premarket_command_sheet)
    summary["live_premarket_blocking_command_count"] = (
        int(live_premarket_command_sheet.get("blocks_live_buy", pd.Series(dtype=bool)).map(_truthy).sum())
        if not live_premarket_command_sheet.empty
        else 0
    )
    summary["live_premarket_formal_command_count"] = (
        int(live_premarket_command_sheet.get("formal_trade_required", pd.Series(dtype=bool)).map(_truthy).sum())
        if not live_premarket_command_sheet.empty
        else 0
    )
    live_blocker_resolution_plan = _build_live_blocker_resolution_plan(live_premarket_command_sheet)
    _write_report_csv("live_blocker_resolution_plan.csv", live_blocker_resolution_plan)
    resolution_types = live_blocker_resolution_plan.get("resolution_type", pd.Series(dtype=str)).astype(str) if not live_blocker_resolution_plan.empty else pd.Series(dtype=str)
    summary["live_blocker_resolution_count"] = len(live_blocker_resolution_plan)
    summary["live_blocker_refresh_count"] = int((resolution_types == "broker_holding_price_refresh").sum())
    summary["live_blocker_rerun_audit_count"] = int((resolution_types == "rerun_readiness_audit").sum())
    summary["live_blocker_manual_review_count"] = int(resolution_types.isin({"manual_formal_action_review", "no_trade_context_review"}).sum())
    live_blocker_evidence_ledger = _build_live_blocker_evidence_ledger(
        live_blocker_resolution_plan,
        formal_action_reviews_report,
        no_trade_day_review,
    )
    _write_report_csv("live_blocker_evidence_ledger.csv", live_blocker_evidence_ledger)
    blocker_evidence_status = live_blocker_evidence_ledger.get("evidence_status", pd.Series(dtype=str)).astype(str) if not live_blocker_evidence_ledger.empty else pd.Series(dtype=str)
    summary["live_blocker_evidence_count"] = len(live_blocker_evidence_ledger)
    summary["live_blocker_pending_evidence_count"] = int((blocker_evidence_status == "pending_evidence").sum())
    summary["live_blocker_validated_evidence_count"] = int((blocker_evidence_status == "validated").sum())
    summary["live_blocker_issue_evidence_count"] = int((blocker_evidence_status == "issue_found").sum())
    live_hidden_risk_watchlist = _build_live_hidden_risk_watchlist(
        daily_live_review_board,
        live_blocker_evidence_ledger,
        strategy_learning_backlog,
    )
    _write_report_csv("live_hidden_risk_watchlist.csv", live_hidden_risk_watchlist)
    watch_status = live_hidden_risk_watchlist.get("watch_status", pd.Series(dtype=str)).astype(str) if not live_hidden_risk_watchlist.empty else pd.Series(dtype=str)
    watch_risk_level = live_hidden_risk_watchlist.get("risk_level", pd.Series(dtype=str)).astype(str) if not live_hidden_risk_watchlist.empty else pd.Series(dtype=str)
    summary["live_hidden_risk_watch_count"] = len(live_hidden_risk_watchlist)
    summary["live_hidden_risk_high_count"] = int((watch_risk_level == "high").sum())
    summary["live_hidden_risk_pending_evidence_count"] = int((watch_status == "pending_evidence").sum())
    summary["live_hidden_risk_issue_found_count"] = int((watch_status == "issue_found").sum())
    live_daily_review_execution_checklist = _build_live_daily_review_execution_checklist(
        live_hidden_risk_watchlist,
        daily_review_checklist_reviews_report,
    )
    _write_report_csv("live_daily_review_execution_checklist.csv", live_daily_review_execution_checklist)
    review_axes = live_daily_review_execution_checklist.get("review_axis", pd.Series(dtype=str)).astype(str) if not live_daily_review_execution_checklist.empty else pd.Series(dtype=str)
    review_windows = live_daily_review_execution_checklist.get("review_window", pd.Series(dtype=str)).astype(str) if not live_daily_review_execution_checklist.empty else pd.Series(dtype=str)
    daily_review_status = live_daily_review_execution_checklist.get("review_status", pd.Series(dtype=str)).astype(str) if not live_daily_review_execution_checklist.empty else pd.Series(dtype=str)
    duplicate_source_counts = live_daily_review_execution_checklist.get("duplicate_source_count", pd.Series(dtype=int)).fillna(1).astype(int) if not live_daily_review_execution_checklist.empty else pd.Series(dtype=int)
    summary["live_daily_review_check_count"] = len(live_daily_review_execution_checklist)
    summary["live_daily_review_source_signal_count"] = int(duplicate_source_counts.sum()) if not duplicate_source_counts.empty else 0
    summary["live_daily_review_duplicate_source_count"] = max(0, int(duplicate_source_counts.sum()) - len(live_daily_review_execution_checklist)) if not duplicate_source_counts.empty else 0
    summary["live_daily_review_execution_evidence_count"] = int((review_axes == "execution_evidence").sum())
    summary["live_daily_review_strategy_logic_count"] = int(review_axes.isin({"buy_point", "sell_point", "selection", "model_switch"}).sum())
    summary["live_daily_review_validated_count"] = int((daily_review_status == "validated").sum())
    summary["live_daily_review_issue_found_count"] = int((daily_review_status == "issue_found").sum())
    summary["live_daily_review_continue_watch_count"] = int((daily_review_status == "continue_watch").sum())
    live_daily_review_action_layers = _build_live_daily_review_action_layers(live_daily_review_execution_checklist)
    _write_report_csv("live_daily_review_action_layers.csv", live_daily_review_action_layers)
    summary["live_daily_review_action_layer_count"] = len(live_daily_review_action_layers)
    summary["live_daily_review_action_layer_pending_count"] = (
        int((live_daily_review_action_layers.get("pending_count", pd.Series(dtype=int)).fillna(0).astype(int) > 0).sum())
        if not live_daily_review_action_layers.empty
        else 0
    )
    summary["live_daily_review_premarket_count"] = int(review_windows.isin({"盘前", "盘前/盘后"}).sum())
    live_premarket_action_sequence = _build_live_premarket_action_sequence(
        live_blocker_resolution_plan,
        live_blocker_evidence_ledger,
    )
    _write_report_csv("live_premarket_action_sequence.csv", live_premarket_action_sequence)
    summary["live_premarket_sequence_step_count"] = len(live_premarket_action_sequence)
    summary["live_premarket_sequence_ready_step_count"] = (
        int(live_premarket_action_sequence.get("can_execute_now", pd.Series(dtype=bool)).map(_truthy).sum())
        if not live_premarket_action_sequence.empty
        else 0
    )
    ready_sequence = (
        live_premarket_action_sequence[live_premarket_action_sequence.get("can_execute_now", pd.Series(dtype=bool)).map(_truthy)]
        if not live_premarket_action_sequence.empty
        else pd.DataFrame([])
    )
    summary["live_premarket_next_action"] = (
        ready_sequence.iloc[0].get("action_label")
        if not ready_sequence.empty
        else (live_premarket_action_sequence.iloc[0].get("action_label") if not live_premarket_action_sequence.empty else "")
    )
    live_admission_snapshot = _build_live_admission_snapshot(
        summary,
        first_live_decision_card,
        live_premarket_command_sheet,
    )
    _write_report_csv("live_admission_snapshot.csv", live_admission_snapshot)
    live_admission = live_admission_snapshot.iloc[0].to_dict() if not live_admission_snapshot.empty else {}
    summary["live_admission_status"] = live_admission.get("admission_status")
    summary["live_admission_label"] = live_admission.get("admission_label")
    summary["live_admission_buy_allowed"] = live_admission.get("live_buy_allowed")
    summary["live_admission_blocking_command_count"] = live_admission.get("blocking_command_count")
    summary["live_admission_next_step"] = live_admission.get("next_step")
    live_premarket_execution_recheck = _build_live_premarket_execution_recheck(
        summary,
        live_admission_snapshot,
        live_premarket_action_sequence,
        live_blocker_evidence_ledger,
    )
    _write_report_csv("live_premarket_execution_recheck.csv", live_premarket_execution_recheck)
    live_recheck = live_premarket_execution_recheck.iloc[0].to_dict() if not live_premarket_execution_recheck.empty else {}
    summary["live_premarket_recheck_status"] = live_recheck.get("recheck_status")
    summary["live_premarket_recheck_label"] = live_recheck.get("recheck_label")
    summary["live_premarket_recheck_next_action"] = live_recheck.get("next_operator_action")
    live_premarket_action_attempts = _build_live_premarket_action_attempts_report()
    _write_report_csv("live_premarket_action_attempts.csv", live_premarket_action_attempts)
    attempt_status = live_premarket_action_attempts.get("ok", pd.Series(dtype=bool)).map(_truthy) if not live_premarket_action_attempts.empty else pd.Series(dtype=bool)
    latest_attempt = live_premarket_action_attempts.iloc[0].to_dict() if not live_premarket_action_attempts.empty else {}
    summary["live_premarket_action_attempt_count"] = len(live_premarket_action_attempts)
    summary["live_premarket_action_attempt_success_count"] = int(attempt_status.sum()) if not attempt_status.empty else 0
    summary["live_premarket_latest_attempt_at"] = latest_attempt.get("attempted_at")
    summary["live_premarket_latest_attempt_blocking_count"] = latest_attempt.get("live_admission_blocking_command_count")
    live_manual_launch_acceptance = _build_live_manual_launch_acceptance(
        summary,
        live_admission_snapshot,
        live_premarket_execution_recheck,
        live_daily_review_action_layers,
    )
    _write_report_csv("live_manual_launch_acceptance.csv", live_manual_launch_acceptance)
    acceptance_status = live_manual_launch_acceptance.get("status", pd.Series(dtype=str)).astype(str) if not live_manual_launch_acceptance.empty else pd.Series(dtype=str)
    acceptance_hard = live_manual_launch_acceptance.get("is_hard_blocker", pd.Series(dtype=bool)).map(_truthy) if not live_manual_launch_acceptance.empty else pd.Series(dtype=bool)
    summary["live_manual_launch_acceptance_count"] = len(live_manual_launch_acceptance)
    summary["live_manual_launch_hard_blocker_count"] = int(acceptance_hard.sum()) if not acceptance_hard.empty else 0
    summary["live_manual_launch_watch_count"] = int((acceptance_status == "watch").sum())
    summary["live_manual_launch_ready"] = summary["live_manual_launch_hard_blocker_count"] == 0 and _truthy(summary.get("live_admission_buy_allowed"))
    live_day1_review_journal = _build_live_day1_review_journal(
        live_premarket_action_sequence,
        live_blocker_evidence_ledger,
        live_manual_launch_acceptance,
        live_daily_review_execution_checklist,
        day1_paper_review_pack,
        day1_after_close_review_queue,
    )
    _write_report_csv("live_day1_review_journal.csv", live_day1_review_journal)
    journal_status = live_day1_review_journal.get("status", pd.Series(dtype=str)).astype(str) if not live_day1_review_journal.empty else pd.Series(dtype=str)
    journal_windows = live_day1_review_journal.get("journal_window", pd.Series(dtype=str)).astype(str) if not live_day1_review_journal.empty else pd.Series(dtype=str)
    summary["live_day1_review_journal_count"] = len(live_day1_review_journal)
    summary["live_day1_review_journal_blocking_count"] = (
        int(live_day1_review_journal.get("is_blocking", pd.Series(dtype=bool)).map(_truthy).sum())
        if not live_day1_review_journal.empty
        else 0
    )
    summary["live_day1_review_journal_premarket_count"] = int((journal_windows == "盘前").sum())
    summary["live_day1_review_journal_after_close_count"] = int((journal_windows == "盘后").sum())
    summary["live_day1_review_journal_issue_count"] = int(journal_status.isin({"issue_found", "block", "blocked", "data_gap", "process_gap"}).sum())
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (OUT_DIR / "LIVE_ADMISSION_SNAPSHOT_CN.md").write_text(
        "\n".join(
            [
                "# G3 实盘准入快照",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 下一交易日：{summary.get('next_trade_entry_date') or '--'}",
                f"- 准入结论：{live_admission.get('admission_label') or '--'} (`{live_admission.get('admission_status') or '--'}`)",
                f"- 真实买入允许：{live_admission.get('live_buy_allowed')}",
                f"- 纸面执行允许：{live_admission.get('paper_execution_allowed')}",
                f"- 自动下单允许：{live_admission.get('auto_order_allowed')}",
                f"- 阻止真实买入动作：{live_admission.get('blocking_command_count')}",
                f"- 第一动作：{live_admission.get('first_required_action') or '--'}",
                f"- 下一步：{live_admission.get('next_step') or '--'}",
                f"- 复核规则：{live_admission.get('verification_rule') or '--'}",
                "",
                "这张快照只回答盘前总闸门：今天是否能真实买入、为什么不能、处理后如何验证。无票日不是失败，不为填满仓位强行交易。",
                "",
                _md_table(live_admission_snapshot),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_BLOCKER_RESOLUTION_PLAN_CN.md").write_text(
        "\n".join(
            [
                "# G3 实盘阻断处理路线图",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 阻断处理项：{summary['live_blocker_resolution_count']}",
                f"- 持仓/价格刷新：{summary['live_blocker_refresh_count']}",
                f"- 重跑审计：{summary['live_blocker_rerun_audit_count']}",
                f"- 人工复盘/确认：{summary['live_blocker_manual_review_count']}",
                "",
                "路线图只负责拆解盘前动作，不直接下单。先处理真实持仓/价格与重跑审计，再做无票日归因；证据未闭环前禁止真实买入。",
                "",
                _md_table(live_blocker_resolution_plan),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_BLOCKER_EVIDENCE_LEDGER_CN.md").write_text(
        "\n".join(
            [
                "# G3 实盘阻断证据闭环表",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 阻断证据项：{summary['live_blocker_evidence_count']}",
                f"- 待证据：{summary['live_blocker_pending_evidence_count']}",
                f"- 已验证：{summary['live_blocker_validated_evidence_count']}",
                f"- 已发现隐患：{summary['live_blocker_issue_evidence_count']}",
                "",
                "这张表检查盘前阻断处理是否真正留下证据。证据落账不等于自动放行；仍必须重跑审计，确认底层阻断真实消失。",
                "",
                _md_table(live_blocker_evidence_ledger),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_PREMARKET_ACTION_SEQUENCE_CN.md").write_text(
        "\n".join(
            [
                "# G3 盘前执行顺序",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 执行步骤：{summary['live_premarket_sequence_step_count']} 步",
                f"- 当前可执行：{summary['live_premarket_sequence_ready_step_count']} 步",
                f"- 下一动作：{summary.get('live_premarket_next_action') or '--'}",
                "",
                "这张表把重复阻断合并成自然交易顺序：先刷新真实持仓/价格，再重跑审计，最后记录无票日归因。未完成前置事实前，不用无票归因反推放宽买点或选股规则。",
                "",
                _md_table(live_premarket_action_sequence),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_PREMARKET_EXECUTION_RECHECK_CN.md").write_text(
        "\n".join(
            [
                "# G3 盘前执行后复查",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 复查结论：{live_recheck.get('recheck_label') or '--'} (`{live_recheck.get('recheck_status') or '--'}`)",
                f"- 真实买入允许：{live_recheck.get('live_buy_allowed')}",
                f"- 仍阻断真实买入动作：{live_recheck.get('blocking_command_count')}",
                f"- 当前步骤：{live_recheck.get('current_action_label') or '--'}",
                f"- 下一动作：{live_recheck.get('next_operator_action') or '--'}",
                "",
                "这张表用于每次点击同步、重跑审计或记录归因后复查：动作有证据不等于可以买；必须重新审计确认准入快照真实放行。",
                "",
                _md_table(live_premarket_execution_recheck),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_MANUAL_LAUNCH_ACCEPTANCE_CN.md").write_text(
        "\n".join(
            [
                "# G3 人工实战验收单",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 验收项：{summary.get('live_manual_launch_acceptance_count', 0)}",
                f"- 硬阻断：{summary.get('live_manual_launch_hard_blocker_count', 0)}",
                f"- 观察项：{summary.get('live_manual_launch_watch_count', 0)}",
                f"- 人工实战复核就绪：{summary.get('live_manual_launch_ready')}",
                "",
                "这张表只回答一个问题：盘前动作处理后，是否可以进入人工实战最终复核。它不触发自动下单；硬阻断未清零前，策略观察和收益压力都不能替代准入。",
                "",
                _md_table(live_manual_launch_acceptance),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_DAY1_REVIEW_JOURNAL_CN.md").write_text(
        "\n".join(
            [
                "# G3 Day1 实战复盘日志",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 日志项：{summary.get('live_day1_review_journal_count', 0)}",
                f"- 当前阻断/隐患项：{summary.get('live_day1_review_journal_blocking_count', 0)}",
                f"- 盘前项：{summary.get('live_day1_review_journal_premarket_count', 0)}",
                f"- 盘后项：{summary.get('live_day1_review_journal_after_close_count', 0)}",
                f"- 已发现问题项：{summary.get('live_day1_review_journal_issue_count', 0)}",
                "",
                "这张表把第一天实战要做的事按时间线合并：先清盘前阻断，再看人工验收，盘中只记录观察证据，盘后再做买点、卖点、选股和策略切换归因。它不放宽买入规则，也不触发下单。",
                "",
                _md_table(live_day1_review_journal),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_HIDDEN_RISK_WATCHLIST_CN.md").write_text(
        "\n".join(
            [
                "# G3 实战隐患观察登记表",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 观察样本：{summary['live_hidden_risk_watch_count']} 条",
                f"- 高风险：{summary['live_hidden_risk_high_count']} 条",
                f"- 待证据：{summary['live_hidden_risk_pending_evidence_count']} 条",
                f"- 已发现隐患：{summary['live_hidden_risk_issue_found_count']} 条",
                "",
                "这张表承接尚未闭环的复盘样本：正式阻断、无票日、候选遗漏、纸面观察和已发现问题。它只用于复盘与学习，不直接放行买入。",
                "",
                _md_table(live_hidden_risk_watchlist),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_DAILY_REVIEW_EXECUTION_CHECKLIST_CN.md").write_text(
        "\n".join(
            [
                "# G3 实战逐项复盘执行清单",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 复盘检查项：{summary['live_daily_review_check_count']} 条",
                f"- 来源信号/重复合并：{summary.get('live_daily_review_source_signal_count', 0)} / {summary.get('live_daily_review_duplicate_source_count', 0)}",
                f"- 行动层/仍有待处理层：{summary.get('live_daily_review_action_layer_count', 0)} / {summary.get('live_daily_review_action_layer_pending_count', 0)}",
                f"- 已验证/发现隐患/继续观察：{summary.get('live_daily_review_validated_count', 0)} / {summary.get('live_daily_review_issue_found_count', 0)} / {summary.get('live_daily_review_continue_watch_count', 0)}",
                f"- 执行证据项：{summary['live_daily_review_execution_evidence_count']} 条",
                f"- 策略逻辑项：{summary['live_daily_review_strategy_logic_count']} 条",
                f"- 盘前必须看：{summary['live_daily_review_premarket_count']} 条",
                "",
                "这张表把隐患观察样本转成当天可执行的复盘动作，覆盖买点、卖点、选股、策略切换和执行证据。它不改变交易合同，只决定今天要收集什么证据、什么条件进入调优。",
                "",
                "## 行动层",
                "",
                _md_table(live_daily_review_action_layers),
                "",
                "## 逐项明细",
                "",
                _md_table(live_daily_review_execution_checklist),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "FIRST_LIVE_DECISION_CARD_CN.md").write_text(
        "\n".join(
            [
                "# G3 首日实战决策卡",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 下一交易日：{summary.get('next_trade_entry_date') or '--'}",
                f"- 决策：{first_live_decision.get('decision_label') or '--'} (`{first_live_decision.get('decision_status') or '--'}`)",
                f"- 执行动作：{first_live_decision.get('execution_posture') or '--'}",
                f"- 主因：{first_live_decision.get('primary_reason') or '--'}",
                f"- 第一动作：{first_live_decision.get('first_required_action') or '--'}",
                f"- 复盘焦点：{first_live_decision.get('review_focus') or '--'}",
                f"- 锁定模式：{first_live_decision.get('locked_modes') or '--'}",
                "",
                _md_table(first_live_decision_card),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_REVIEW_TASK_QUEUE_CN.md").write_text(
        "\n".join(
            [
                "# G3 实战复盘任务队列",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 任务总数：{summary['live_review_task_count']} 项",
                f"- 盘前/盘中盘后/盘后：{summary['live_review_premarket_task_count']} / {summary['live_review_intraday_after_task_count']} / {summary['live_review_after_close_task_count']}",
                f"- 正式必处理：{summary['live_review_formal_required_task_count']} 项",
                "",
                "按 priority 从小到大执行。正式动作只记录证据，不替代底层 gate 清理；候选遗漏和无票日复盘只做行为归因，不按单日收益倒推调参。",
                "",
                _md_table(live_review_task_queue),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "REVIEW_COVERAGE_DASHBOARD_CN.md").write_text(
        "\n".join(
            [
                "# G3 复盘覆盖率看板",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 复盘范围：{summary['review_coverage_scope_count']} 类",
                f"- 待复盘范围：{summary['review_coverage_pending_scope_count']} 类",
                f"- 发现隐患范围：{summary['review_coverage_issue_scope_count']} 类",
                f"- 继续观察范围：{summary['review_coverage_continue_watch_scope_count']} 类",
                "",
                "覆盖率只表示复盘证据是否落地，不代表收益优化完成；发现隐患后必须先做行为归因，再决定是否调整规则。",
                "",
                _md_table(review_coverage_dashboard),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_REVIEW_EVIDENCE_RUBRIC_CN.md").write_text(
        "\n".join(
            [
                "# G3 实战复盘证据矩阵",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 复盘规则范围：{summary['live_review_rubric_scope_count']} 类",
                f"- 仍有待复盘任务的范围：{summary['live_review_rubric_pending_scope_count']} 类",
                f"- 正式实战前必须处理的范围：{summary['live_review_rubric_formal_scope_count']} 类",
                "",
                "矩阵用于约束逐项复盘：先补证据，再做行为归因；只有重复、可复核、同类的问题才进入策略优化，不按单日盈亏倒推规则。",
                "",
                _md_table(live_review_evidence_rubric),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "LIVE_PREMARKET_COMMAND_SHEET_CN.md").write_text(
        "\n".join(
            [
                "# G3 实战盘前指挥单",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 下一交易日：{summary.get('next_trade_entry_date') or '--'}",
                f"- 指挥动作：{summary['live_premarket_command_count']} 项",
                f"- 阻止真实买入的动作：{summary['live_premarket_blocking_command_count']} 项",
                f"- 正式必处理动作：{summary['live_premarket_formal_command_count']} 项",
                "",
                "按 priority 从小到大执行。`blocks_live_buy=True` 的动作未完成前，不进入真实买入；观察项只进入复盘，不为填仓位强行交易。",
                "",
                _md_table(live_premarket_command_sheet),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "STRATEGY_LEARNING_BACKLOG_CN.md").write_text(
        "\n".join(
            [
                "# G3 策略学习隐患队列",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 待学习样本：{summary['strategy_learning_backlog_count']} 条",
                f"- 需调优复盘：{summary['strategy_learning_needs_review_count']} 条",
                f"- 继续观察：{summary['strategy_learning_watch_more_count']} 条",
                "",
                "这张表只收集人工复盘后的问题样本，用于后续优化买点、选股模式、策略切换和卖点合同；未复盘样本不会被自动当成问题。",
                "",
                _md_table(strategy_learning_backlog),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "DAILY_LIVE_REVIEW_BOARD_CN.md").write_text(
        "\n".join(
            [
                "# G3 每日实战复盘看板",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 下一交易日：{summary.get('next_trade_entry_date') or '--'}",
                f"- 复盘事项：{summary['daily_live_review_board_count']} 项",
                f"- 待处理/待复盘：{summary['daily_live_review_board_pending_count']} 项",
                f"- 正式实战前必须处理：{summary['daily_live_review_board_formal_required_count']} 项",
                f"- 已发现隐患/阻断：{summary['daily_live_review_board_issue_found_count']} 项",
                "",
                "使用方法：按 priority 从小到大处理。先处理正式实战前必须处理项，再记录无票日、候选遗漏和 Day1 纸面/盘后观察；不要用单日收益倒推改规则。",
                "",
                _md_table(daily_live_review_board),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "DAY1_PAPER_REVIEW_PACK_CN.md").write_text(
        "\n".join(
            [
                "# G3 第1天纸面实战复盘包",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 下一交易日：{summary.get('next_trade_entry_date') or '--'}",
                f"- 复盘票据：{summary['day1_paper_review_ticket_count']} 张",
                f"- 待纸面/待刷新：{summary['day1_paper_review_pending_count']} 张",
                "",
                "这份复盘包只用于学习和验证交易逻辑，不代表正式买入放行。盘后反馈必须归因到买点、选股、策略切换、卖点合同或信号有效性，不按单日收益倒推调参。",
                "",
                _md_table(day1_paper_review_pack),
                "",
            ]
        ),
        encoding="utf-8",
    )
    (OUT_DIR / "DAY1_AFTER_CLOSE_REVIEW_QUEUE_CN.md").write_text(
        "\n".join(
            [
                "# G3 Day1 盘后逐票复盘队列",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 队列票据：{summary['day1_after_close_review_count']} 张",
                f"- 待写纸面执行：{summary['day1_after_close_pending_execution_count']} 张",
                f"- 待盘后复盘：{summary['day1_after_close_pending_review_count']} 张",
                f"- 已发现隐患：{summary['day1_after_close_issue_found_count']} 张",
                "",
                "盘后复盘只做行为归因：买点、选股、策略切换、卖点合同、信号有效性。不要用单日收益倒推参数。",
                "",
                _md_table(day1_after_close_review_queue),
                "",
            ]
        ),
        encoding="utf-8",
    )
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
    (OUT_DIR / "NO_TRADE_DAY_REVIEW_CN.md").write_text(
        "\n".join(
            [
                "# G3 无票日实战复盘",
                "",
                f"- 生成时间：{summary['generated_at']}",
                f"- 下一交易日：{summary.get('next_trade_entry_date') or '--'}",
                f"- 无票日复盘项：{summary['no_trade_day_review_count']}",
                f"- 正式实战前必须处理：{summary['no_trade_day_formal_required_count']}",
                "",
                "无票日也要复盘：判断是自然空仓、候选确认缺口、数据/执行缺口，还是策略规则误伤；不要为了补满二槽而降低买点和盘中确认标准。",
                "",
                _md_table(no_trade_day_review),
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
                f"- 每日实战复盘看板：{summary['daily_live_review_board_count']} 项；待处理 {summary['daily_live_review_board_pending_count']}；正式必处理 {summary['daily_live_review_board_formal_required_count']}；隐患/阻断 {summary['daily_live_review_board_issue_found_count']}",
                f"- 策略学习隐患队列：{summary['strategy_learning_backlog_count']} 条；需调优复盘 {summary['strategy_learning_needs_review_count']}；继续观察 {summary['strategy_learning_watch_more_count']}",
                f"- Day1 纸面复盘包：{summary['day1_paper_review_ticket_count']} 张；待纸面/待刷新 {summary['day1_paper_review_pending_count']}",
                f"- Day1 盘后复盘队列：{summary['day1_after_close_review_count']} 张；待写执行 {summary['day1_after_close_pending_execution_count']}；待复盘 {summary['day1_after_close_pending_review_count']}；发现隐患 {summary['day1_after_close_issue_found_count']}",
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
        f"- 无票日复盘项/正式必处理：{summary['no_trade_day_review_count']} / {summary['no_trade_day_formal_required_count']}",
        f"- 自然交易一致性最低分/偏拧票数：{summary['natural_consistency_min_score']} / {summary['natural_consistency_strained_count']}",
        f"- 自然执行决策阻断/关注：{summary['natural_execution_block_count']} / {summary['natural_execution_warn_watch_count']}",
        f"- 实战前动作清单待处理/正式必处理/观察/阻断：{summary['pretrade_action_pending_count']} / {summary['pretrade_formal_action_pending_count']} / {summary['pretrade_observation_action_pending_count']} / {summary['pretrade_action_block_count']}",
        f"- 正式实盘放行：`{summary['formal_launch_status']}`；缺口 {summary['formal_launch_missing_count']}；下一步：{summary['formal_launch_next_step']}",
        f"- 首日实战决策：{summary.get('first_live_decision_label') or '--'} (`{summary.get('first_live_decision_status') or '--'}`)；动作：{summary.get('first_live_execution_posture') or '--'}",
        f"- 首日主因：{summary.get('first_live_primary_reason') or '--'}",
        f"- 首日第一动作：{summary.get('first_live_first_required_action') or '--'}",
        f"- 实战复盘任务队列：{summary.get('live_review_task_count', 0)} 项；盘前 {summary.get('live_review_premarket_task_count', 0)}；盘中/盘后 {summary.get('live_review_intraday_after_task_count', 0)}；盘后 {summary.get('live_review_after_close_task_count', 0)}；正式必处理 {summary.get('live_review_formal_required_task_count', 0)}",
        f"- 执行入口可用/锁定：{summary['execution_mode_ready_count']} / {summary['execution_mode_locked_count']}",
        f"- 放行优先队列：{summary['formal_launch_action_queue_count']} 项",
        f"- 逐票复盘证据：{summary['pretrade_review_evidence_count']} 条",
        f"- 纸面观察后评估：{summary['paper_watch_followup_count']} 条；待观察 {summary['paper_watch_followup_pending_count']}；发现隐患 {summary['paper_watch_issue_found_count']}",
        f"- 盘前执行剧本：{summary['premarket_playbook_step_count']} 步",
        f"- 每日实战复盘看板：{summary['daily_live_review_board_count']} 项；待处理 {summary['daily_live_review_board_pending_count']}；正式必处理 {summary['daily_live_review_board_formal_required_count']}；隐患/阻断 {summary['daily_live_review_board_issue_found_count']}",
        f"- 实战逐项复盘执行清单：{summary.get('live_daily_review_check_count', 0)} 个处理项；来源信号 {summary.get('live_daily_review_source_signal_count', 0)}；重复来源已合并 {summary.get('live_daily_review_duplicate_source_count', 0)}；行动层 {summary.get('live_daily_review_action_layer_count', 0)}；仍有待处理层 {summary.get('live_daily_review_action_layer_pending_count', 0)}；已验证 {summary.get('live_daily_review_validated_count', 0)}；发现隐患 {summary.get('live_daily_review_issue_found_count', 0)}；继续观察 {summary.get('live_daily_review_continue_watch_count', 0)}",
        f"- 人工实战验收：就绪 {summary.get('live_manual_launch_ready')}；硬阻断 {summary.get('live_manual_launch_hard_blocker_count', 0)}；观察项 {summary.get('live_manual_launch_watch_count', 0)}",
        f"- 策略学习隐患队列：{summary['strategy_learning_backlog_count']} 条；需调优复盘 {summary['strategy_learning_needs_review_count']}；继续观察 {summary['strategy_learning_watch_more_count']}",
        f"- Day1 纸面复盘包：{summary['day1_paper_review_ticket_count']} 张；待纸面/待刷新 {summary['day1_paper_review_pending_count']}",
        f"- Day1 盘后复盘队列：{summary['day1_after_close_review_count']} 张；待写执行 {summary['day1_after_close_pending_execution_count']}；待复盘 {summary['day1_after_close_pending_review_count']}；发现隐患 {summary['day1_after_close_issue_found_count']}",
        "",
        "解释：本报告是实战前只读审计，不触发刷新、不下单。`manual_shadow_ready` 只代表可进入人工/纸面实战复盘；正式自动下单仍必须另行确认。",
        "",
        "## 首日实战决策卡",
        "",
        _md_table(first_live_decision_card),
        "",
        "## 实战复盘任务队列",
        "",
        "按这张表执行日内动作和盘后复盘，所有任务都回到看板/台账记录证据。",
        "",
        _md_table(live_review_task_queue),
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
        "## 每日实战复盘看板",
        "",
        "这张表是实战处理顺序：先处理正式必处理项，再逐项复盘无票日、候选遗漏和 Day1 纸面/盘后观察。",
        "",
        _md_table(daily_live_review_board),
        "",
        "## 策略学习隐患队列",
        "",
        "这里只收集已经人工复盘出问题或仍需观察的样本；它是后续优化入口，不把未复盘事项自动当成策略缺陷。",
        "",
        _md_table(strategy_learning_backlog),
        "",
        "## 无票日实战复盘",
        "",
        "无票日不是空白日：这里记录为什么不买、哪些候选只观察、哪些正式动作必须先处理。",
        "",
        _md_table(no_trade_day_review),
        "",
        "## Day1 纸面实战复盘包",
        "",
        "这张表把下一交易日票据拆成买点、选股、策略切换、卖点合同和组合暴露五类观察任务；它是学习闭环，不是正式买入放行。",
        "",
        _md_table(day1_paper_review_pack),
        "",
        "## Day1 盘后逐票复盘队列",
        "",
        "这张表把纸面执行和盘后归因接起来：未写纸面执行的先补执行，已执行但未归因的必须盘后记录观察结果。",
        "",
        _md_table(day1_after_close_review_queue),
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

"""
Canonical G3 State Alpha read-only API.

This router wraps the current G3 state-router shadow artifacts under the new
strategy contract. It does not enable formal buy signals or order routing.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, Body, Query

from execution.ptrade_bridge import PTradeFileBridge
from scheduler.trading_calendar import TradingCalendar
from utils.config import config as app_config
from utils.logger import get_logger
from utils.paths import report_path, runtime_path


router = APIRouter(prefix="/gen3-state-alpha", tags=["G3 State Alpha"])
logger = get_logger("gen3_state_alpha")

STRATEGY_ID = "g3_final_with_g2_gap_supplement"
STRATEGY_NAME = "G3 Final With G2 Gap Supplement"
STRATEGY_NAME_CN = "G3最终版"
FINAL_G3_PROFILE = "g3_final_with_g2_gap_supplement"
FINAL_G3_PROFILE_NAME = "G3最终版：二槽主升 + G2空档补位"
FINAL_G3_LEGACY_BASE_PROFILE = "g3_final_top2_mainwave_sector_exempt_v1"

BLUEPRINT_DIR = report_path("gen3_strategy_rebuild_blueprint_v1")
STATE_ALPHA_RUNTIME_DIR = runtime_path("gen3_state_alpha")
STATE_ROUTER_RUNTIME_DIR = runtime_path("gen3_state_router_shadow")
STATE_ROUTER_REPORT_DIR = report_path("gen3_state_router_shadow_daily_v1")
MAINWAVE_RUNTIME_DIR = runtime_path("gen3_institutional_mainwave_current")
MAINWAVE_REPORT_DIR = report_path("gen3_institutional_mainwave_current_v1")
PROMOTION_REPORT_DIR = report_path("gen3_promotion_self_test_v1")
FINAL_G3_BACKTEST_DIR = report_path("g2_g3_market_style_router_v1")
LATEST_G3_PROFILE = FINAL_G3_PROFILE
HISTORICAL_TRADES_PATH = FINAL_G3_BACKTEST_DIR / f"{LATEST_G3_PROFILE}_closed_trades.csv"
SCORE120_CORE_TRADES_PATH = report_path("gen3_score120_core_strategy_v1", "g3_route_execution_mandate_candidate_closed_trades.csv")
EQUITY_CURVE_PATH = FINAL_G3_BACKTEST_DIR / f"{LATEST_G3_PROFILE}_equity_curve.csv"
MTM_EQUITY_CURVE_PATH = FINAL_G3_BACKTEST_DIR / f"{LATEST_G3_PROFILE}_mtm_equity_curve.csv"
PROMOTION_SUMMARY_PATH = FINAL_G3_BACKTEST_DIR / "summary.json"
PROMOTION_SUMMARY_CSV_PATH = FINAL_G3_BACKTEST_DIR / "summary.csv"
FINAL_G3_WINDOW_SUMMARY_PATH = FINAL_G3_BACKTEST_DIR / "window_summary.csv"
PROMOTION_GATES_PATH = PROMOTION_REPORT_DIR / "gates.csv"
STRATEGY_REDUCTION_RECOVERY_DIR = report_path("g3_strategy_reduction_recovery_v1")
NATIVE_BRIDGE_BACKTEST_DIR = report_path("g3_five_strategies_native_bridge_v1")
FORMAL_UNIFIED_CONTRACT_DIR = report_path("g3_formal_unified_five_strategy_contract_v1")
PRACTICAL_FUSION_CONTRACT_DIR = report_path("g3_practical_fusion_contract_v1")
FIVE_STRATEGY_DISPATCH_CONTRACT_DIR = report_path("g3_formal_five_strategy_dispatch_contract_v1")
FIVE_STRATEGY_OVERFIT_AUDIT_DIR = report_path("g3_formal_five_strategy_overfit_audit_v1")
UNIFIED_CONTRACT_GAP_DIR = report_path("g3_unified_contract_return_gap_v1")
PROFITABLE_RECALL_DIR = report_path("g3_profitable_signal_recall_v1")
NATIVE_BRIDGE_RECALL_DIR = report_path("g3_native_source_bridge_recall_v1")
NATIVE_BRIDGE_M30_DIR = report_path("g3_native_bridge_30m_integrity_v1")
NATIVE_BRIDGE_NATIVE_M30_DIR = report_path("g3_native_bridge_native_m30_semantics_v1")
STRATEGY_FUSION_GUARDRAILS_DIR = report_path("g3_strategy_fusion_guardrails_v1")
NATURAL_POLICY_SHADOW_DIR = report_path("g3_natural_policy_shadow_v1")
GATE_LAYER_AUDIT_DIR = report_path("gen3_gate_layer_policy_audit_v1")
GATE_LAYER_AUDIT_SUMMARY_PATH = GATE_LAYER_AUDIT_DIR / "summary.json"
GATE_LAYER_AUDIT_REPORT_PATH = GATE_LAYER_AUDIT_DIR / "REPORT_CN.md"
OPERATIONS_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "operations_state.json"
MONITOR_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "shadow_monitor_state.json"
OBSERVATION_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "observation_scheduler_state.json"
VERIFICATION_PATH = STATE_ALPHA_RUNTIME_DIR / "shadow_verifications.json"
MONITOR_EVENTS_PATH = STATE_ALPHA_RUNTIME_DIR / "shadow_monitor_events.json"
REPLACEMENT_ASSESSMENT_PATH = STATE_ALPHA_RUNTIME_DIR / "replacement_assessment.json"
PAPER_EXECUTIONS_PATH = STATE_ALPHA_RUNTIME_DIR / "paper_executions.json"
OBSERVATION_SNAPSHOTS_PATH = STATE_ALPHA_RUNTIME_DIR / "observation_snapshots.json"
BROKER_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "broker_state.json"
BROKER_SYNC_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "broker_sync_state.json"
PRETRADE_SMOKE_DIR = report_path("gen3_pretrade_smoke_test_v1")
PRETRADE_SMOKE_PATH = PRETRADE_SMOKE_DIR / "latest_pretrade_smoke.json"
PRETRADE_SMOKE_GATES_PATH = PRETRADE_SMOKE_DIR / "latest_pretrade_gates.csv"
PTRADE_E2E_ACCEPTANCE_PATH = report_path("gen3_ptrade_e2e_acceptance") / "latest.json"
PTRADE_INTERNAL_ACCEPTANCE_PATH = report_path("gen3_ptrade_internal_strategy_acceptance") / "latest.json"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_ROUTER_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_state_router_shadow_daily_v1.py"
STATE_ROUTER_RESEARCH_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_market_state_router_v1.py"
PRETRADE_SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_pretrade_smoke_test_v1.py"
REFRESH_TASKS: dict[str, dict[str, Any]] = {}
REFRESH_TASK_LOCK = threading.Lock()
_monitor_scheduler: BackgroundScheduler | None = None
_monitor_job_id = "g3_state_alpha_shadow_monitor"
_observation_job_id = "g3_state_alpha_observation_snapshot"
_broker_sync_job_id = "g3_state_alpha_broker_sync"
PTRADE_BRIDGE = PTradeFileBridge()


def _path_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "modified_at": None,
            "size_bytes": None,
        }
    stat = path.stat()
    return {
        "exists": True,
        "path": str(path),
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
        "size_bytes": stat.st_size,
    }


def _date_text(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%Y-%m-%d")


def _is_trading_day_text(value: Any) -> bool:
    date_text = _date_text(value)
    if not date_text:
        return False
    try:
        return bool(TradingCalendar.is_trading_day(datetime.strptime(date_text, "%Y-%m-%d")))
    except Exception:
        return True


def _filter_next_trade_buy_tickets(
    tickets: list[dict[str, Any]],
    current_summary: dict[str, Any],
    current_tickets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_entry = _date_text(
        current_tickets[0].get("entry_date") if current_tickets else current_summary.get("entry_date")
    )
    out: list[dict[str, Any]] = []
    for item in tickets:
        if not isinstance(item, dict):
            continue
        entry_date = _date_text(item.get("entry_date") or item.get("planned_entry_ts"))
        if not entry_date or not _is_trading_day_text(entry_date):
            continue
        if current_entry and entry_date <= current_entry:
            continue
        out.append(item)
    return out


def _next_trading_day_text(value: Any) -> str:
    date_text = _date_text(value)
    if not date_text:
        return ""
    try:
        return TradingCalendar.get_next_trading_day(datetime.strptime(date_text, "%Y-%m-%d")).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _build_date_display_analysis(
    current_summary: dict[str, Any],
    current_tickets: list[dict[str, Any]],
    afterhours_summary: dict[str, Any] | None,
    afterhours_tickets: list[dict[str, Any]],
    next_trade_tickets: list[dict[str, Any]],
) -> dict[str, Any]:
    current_entry = _date_text(
        current_tickets[0].get("entry_date") if current_tickets else current_summary.get("entry_date")
    )
    afterhours_entry = _date_text((afterhours_summary or {}).get("entry_date"))
    if not afterhours_entry and afterhours_tickets:
        afterhours_entry = _date_text(afterhours_tickets[0].get("entry_date") or afterhours_tickets[0].get("planned_entry_ts"))
    expected_entry = _next_trading_day_text(current_entry or (afterhours_summary or {}).get("decision_date"))
    display_entry = _date_text(next_trade_tickets[0].get("entry_date")) if next_trade_tickets else (afterhours_entry or expected_entry)

    raw_count = len([item for item in afterhours_tickets if isinstance(item, dict)])
    non_trading_count = 0
    stale_or_current_count = 0
    for item in afterhours_tickets:
        if not isinstance(item, dict):
            continue
        entry_date = _date_text(item.get("entry_date") or item.get("planned_entry_ts"))
        if entry_date and not _is_trading_day_text(entry_date):
            non_trading_count += 1
        elif current_entry and entry_date and entry_date <= current_entry:
            stale_or_current_count += 1

    if next_trade_tickets:
        status = "ready"
        message = f"已识别 {display_entry} 的下一交易日买入候选。"
    elif non_trading_count:
        status = "filtered_non_trading"
        message = f"已过滤 {non_trading_count} 条非交易日候选；当前按 {display_entry or expected_entry or '--'} 口径等待有效买入票。"
    elif afterhours_entry and expected_entry and afterhours_entry != expected_entry:
        status = "date_mismatch"
        message = f"盘后候选日 {afterhours_entry} 与下一交易日 {expected_entry} 不一致，页面不直接展示为买入建议。"
    else:
        status = "no_candidate"
        message = f"{display_entry or expected_entry or '下一交易日'} 暂无合格买入候选。"

    return {
        "status": status,
        "message": message,
        "current_holding_entry_date": current_entry,
        "afterhours_entry_date": afterhours_entry,
        "expected_next_trade_date": expected_entry,
        "display_entry_date": display_entry,
        "raw_afterhours_ticket_count": raw_count,
        "next_trade_ticket_count": len(next_trade_tickets),
        "filtered_non_trading_ticket_count": non_trading_count,
        "filtered_stale_or_current_ticket_count": stale_or_current_count,
        "display_label": "下一交易日买入关注",
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read json artifact: %s", path)
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _read_csv_records(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return []
    except Exception:
        logger.exception("Failed to read csv artifact: %s", path)
        return []
    if limit is not None:
        df = df.head(limit)
    df = df.astype(object).where(pd.notna(df), None)
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _read_csv_df(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()
    except Exception:
        logger.exception("Failed to read csv artifact: %s", path)
        return pd.DataFrame()


def _to_float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        if pd.isna(number):
            return None
        return number
    except Exception:
        return None


def _to_int_or_zero(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        number = float(value)
        if pd.isna(number):
            return 0
        return int(number)
    except Exception:
        return 0


def _find_institutional_mainwave_source(summary: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    for item in summary.get("sources") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("source") or "") == "institutional_mainwave_current_builder_v1":
            return item
    return {}


def _mainwave_summary_candidates() -> list[tuple[str, Path, dict[str, Any]]]:
    paths = [
        ("state_alpha_afterhours", STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_summary.json"),
        ("state_alpha_current", STATE_ALPHA_RUNTIME_DIR / "latest_summary.json"),
        ("state_router_current", STATE_ROUTER_RUNTIME_DIR / "latest_summary.json"),
        ("mainwave_runtime", MAINWAVE_RUNTIME_DIR / "latest_summary.json"),
    ]
    out: list[tuple[str, Path, dict[str, Any]]] = []
    for label, path in paths:
        payload = _read_json(path)
        if payload:
            out.append((label, path, payload))
    return out


def _select_mainwave_summary_source() -> tuple[str, Path | None, dict[str, Any], dict[str, Any]]:
    fallback: tuple[str, Path | None, dict[str, Any], dict[str, Any]] = ("", None, {}, {})
    for label, path, summary in _mainwave_summary_candidates():
        source = _find_institutional_mainwave_source(summary)
        if source and (source.get("pre_confirm_preview") or source.get("rows")):
            return label, path, summary, source
        if not fallback[2]:
            fallback = (label, path, summary, source)
    return fallback


def _row_code(value: Any, fallback: Any = None) -> str:
    raw = str(value or fallback or "").strip().upper()
    if re.search(r"\d{6}\.(SZ|SH|BJ)$", raw):
        return raw
    code6 = _normalize_code6(raw)
    if not code6:
        return raw
    suffix = ".SH" if code6.startswith(("6", "9")) else ".SZ"
    return f"{code6}{suffix}"


def _mainwave_candidate_from_row(row: dict[str, Any], source: str, recommended_codes: set[str]) -> dict[str, Any]:
    code = _row_code(row.get("code") or row.get("code_raw"), row.get("code6"))
    sector = row.get("sector_name") or row.get("l2_sector_name") or row.get("industry") or "未识别行业"
    score = _to_float_or_none(row.get("wave_style_score") or row.get("score"))
    diffusion = _to_float_or_none(row.get("sector_diffusion_score"))
    m30_status = str(row.get("m30_status") or ("ok" if _truthy(row.get("m30_confirmed") or row.get("m30_ok")) else "unknown"))
    is_recommended = code in recommended_codes or _truthy(row.get("qualified_shadow_buy"))
    entry_date = _date_text(row.get("entry_date") or row.get("planned_entry_ts") or row.get("entry_ts"))
    decision_date = _date_text(row.get("decision_date") or row.get("confirm_datetime") or row.get("trade_date"))
    return {
        "code": code,
        "name": row.get("name") or row.get("stock_name") or "",
        "sector_name": sector,
        "route": row.get("route") or "institutional_mainwave",
        "route_label": row.get("route_label") or "机构主升浪",
        "trade_strategy": row.get("trade_strategy") or "institutional_score120_mainwave",
        "trade_strategy_label": row.get("trade_strategy_label") or "机构主升Score120",
        "template_label": row.get("template_label") or row.get("source_strategy_label") or "",
        "wave_style_score": score,
        "score": score,
        "sector_diffusion_score": diffusion,
        "m30_status": m30_status,
        "m30_confirmed": _truthy(row.get("m30_confirmed") or row.get("m30_ok")) or m30_status == "ok",
        "is_recommended": is_recommended,
        "entry_date": entry_date,
        "decision_date": decision_date,
        "planned_entry_ts": str(row.get("planned_entry_ts") or row.get("entry_ts") or ""),
        "reference_close": _to_float_or_none(row.get("reference_close") or row.get("close")),
        "position_pct": _to_float_or_none(row.get("position_pct") or row.get("max_position_pct")),
        "block_reason": row.get("block_reason") or "",
        "source": source,
    }


def _build_mainwave_candidates(source_meta: dict[str, Any], ticket_rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    recommended_codes = {
        _row_code(item.get("code") or item.get("code_raw"))
        for item in ticket_rows
        if str(item.get("route") or "") == "institutional_mainwave" or str(item.get("route_label") or "").startswith("机构主升")
    }
    rows: list[dict[str, Any]] = []
    for item in ticket_rows:
        if str(item.get("route") or "") == "institutional_mainwave" or str(item.get("route_label") or "").startswith("机构主升"):
            rows.append(_mainwave_candidate_from_row(item, "next_trade_ticket", recommended_codes))
    for item in source_meta.get("pre_confirm_preview") or []:
        if isinstance(item, dict):
            rows.append(_mainwave_candidate_from_row(item, "pre_confirm_preview", recommended_codes))
    if not rows:
        for item in _read_csv_records(MAINWAVE_RUNTIME_DIR / "latest_candidates.csv", limit=limit):
            if isinstance(item, dict):
                rows.append(_mainwave_candidate_from_row(item, "mainwave_latest_candidates", recommended_codes))
    by_code: dict[str, dict[str, Any]] = {}
    for item in rows:
        code = str(item.get("code") or "")
        if not code:
            continue
        existing = by_code.get(code)
        if not existing or (item.get("is_recommended") and not existing.get("is_recommended")):
            by_code[code] = item
    out = list(by_code.values())
    out.sort(
        key=lambda item: (
            1 if item.get("is_recommended") else 0,
            _to_float_or_none(item.get("sector_diffusion_score")) or 0,
            _to_float_or_none(item.get("wave_style_score")) or 0,
        ),
        reverse=True,
    )
    return out[:limit]


def _mainwave_sector_state(avg_diffusion: float, max_score: float, candidate_count: int, recommended_count: int) -> tuple[str, str]:
    if avg_diffusion >= 85 and max_score >= 120 and (candidate_count >= 2 or recommended_count > 0):
        return "strong_mainwave", "强主升机会"
    if avg_diffusion >= 75 and max_score >= 118:
        return "important_industry", "重点行业机会"
    return "watch", "观察机会"


def _build_mainwave_sector_opportunities(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        groups.setdefault(str(item.get("sector_name") or "未识别行业"), []).append(item)
    out: list[dict[str, Any]] = []
    for sector, rows in groups.items():
        scores = [_to_float_or_none(item.get("wave_style_score")) for item in rows]
        scores = [value for value in scores if value is not None]
        diffusions = [_to_float_or_none(item.get("sector_diffusion_score")) for item in rows]
        diffusions = [value for value in diffusions if value is not None]
        max_score = max(scores) if scores else 0.0
        avg_score = sum(scores) / len(scores) if scores else 0.0
        avg_diffusion = sum(diffusions) / len(diffusions) if diffusions else 0.0
        recommended = [item for item in rows if item.get("is_recommended")]
        m30_ok = [item for item in rows if item.get("m30_confirmed") or str(item.get("m30_status") or "") == "ok"]
        state, state_label = _mainwave_sector_state(avg_diffusion, max_score, len(rows), len(recommended))
        top_rows = sorted(rows, key=lambda item: _to_float_or_none(item.get("wave_style_score")) or 0, reverse=True)[:5]
        templates = sorted({str(item.get("template_label") or "") for item in rows if item.get("template_label")})
        out.append(
            {
                "sector_name": sector,
                "state": state,
                "state_label": state_label,
                "candidate_count": len(rows),
                "recommended_count": len(recommended),
                "m30_ok_count": len(m30_ok),
                "max_wave_style_score": max_score,
                "avg_wave_style_score": avg_score,
                "avg_sector_diffusion_score": avg_diffusion,
                "top_candidates": top_rows,
                "top_candidate_names": " / ".join([str(item.get("name") or item.get("code") or "") for item in top_rows[:3]]),
                "template_labels": templates,
                "rank_score": avg_diffusion * 0.55 + max_score * 0.35 + min(len(rows), 8) * 2.0 + len(recommended) * 5.0,
            }
        )
    out.sort(key=lambda item: _to_float_or_none(item.get("rank_score")) or 0, reverse=True)
    return out


def _read_strategy_reduction_recovery() -> dict[str, Any]:
    """Read the latest evidence that strategy consolidation keeps native winners."""
    native_summary = _read_json(NATIVE_BRIDGE_BACKTEST_DIR / "summary.json")
    total_return = native_summary.get("total_return")
    max_drawdown = native_summary.get("max_drawdown")
    include_range_v3 = bool(native_summary.get("include_range_v3"))
    gap_summary = _read_json(UNIFIED_CONTRACT_GAP_DIR / "summary.json")
    formal_unified_summary = _read_json(FORMAL_UNIFIED_CONTRACT_DIR / "summary.json")
    formal_unified_ok = bool(formal_unified_summary.get("ok"))
    practical_fusion_summary = _read_json(PRACTICAL_FUSION_CONTRACT_DIR / "summary.json")
    practical_fusion_ok = bool(practical_fusion_summary.get("ok"))
    ok = False
    try:
        ok = formal_unified_ok and practical_fusion_ok and float(total_return) > 0 and not include_range_v3
    except Exception:
        ok = formal_unified_ok and practical_fusion_ok
    return {
        "ok": ok,
        "status": "pass" if ok else "needs_attention",
        "mode": "native_source_bridge_five_strategy",
        "principle": "5个交易策略主体保持统一；原生信号源作为子来源挂回，避免把原来赚钱的买点跑丢。",
        "default_admission": {
            "include_range_v3": include_range_v3,
            "excluded_sources": [] if include_range_v3 else ["Range V3弱势低吸原生源"],
            "reason": "Range V3在统一卖出合同下形成主要回撤，暂列研究源，不进默认正式合同。",
        },
        "summary": native_summary,
        "formal_profile": _latest_profile(),
        "formal_unified_contract": {
            "summary": formal_unified_summary,
            "strategy_metrics": _read_csv_records(FORMAL_UNIFIED_CONTRACT_DIR / "strategy_metrics.csv"),
            "route_parent_metrics": _read_csv_records(FORMAL_UNIFIED_CONTRACT_DIR / "route_parent_metrics.csv"),
            "selected_strategy_metrics": _read_csv_records(FORMAL_UNIFIED_CONTRACT_DIR / "selected_strategy_metrics.csv"),
        },
        "practical_fusion_contract": {
            "summary": practical_fusion_summary,
            "named_scenarios": _read_csv_records(PRACTICAL_FUSION_CONTRACT_DIR / "named_scenario_matrix.csv"),
            "strategy_admission": _read_csv_records(PRACTICAL_FUSION_CONTRACT_DIR / "strategy_admission_policy.csv"),
            "subset_matrix": _read_csv_records(PRACTICAL_FUSION_CONTRACT_DIR / "strategy_subset_matrix.csv", limit=50),
        },
        "five_strategy_dispatch_contract": {
            "summary": _read_json(FIVE_STRATEGY_DISPATCH_CONTRACT_DIR / "summary.json"),
            "contract": _read_json(FIVE_STRATEGY_DISPATCH_CONTRACT_DIR / "dispatch_contract.json"),
            "strategy_contract": _read_csv_records(FIVE_STRATEGY_DISPATCH_CONTRACT_DIR / "strategy_dispatch_contract.csv"),
            "state_contract": _read_csv_records(FIVE_STRATEGY_DISPATCH_CONTRACT_DIR / "state_dispatch_contract.csv"),
        },
        "five_strategy_overfit_audit": {
            "summary": _read_json(FIVE_STRATEGY_OVERFIT_AUDIT_DIR / "summary.json"),
            "checks": _read_csv_records(FIVE_STRATEGY_OVERFIT_AUDIT_DIR / "overfit_checks.csv"),
            "strategy_metrics": _read_csv_records(FIVE_STRATEGY_OVERFIT_AUDIT_DIR / "strategy_overfit_metrics.csv"),
            "window_summary": _read_csv_records(FIVE_STRATEGY_OVERFIT_AUDIT_DIR / "window_summary.csv"),
            "top_contribution_trades": _read_csv_records(FIVE_STRATEGY_OVERFIT_AUDIT_DIR / "top_contribution_trades.csv", limit=50),
        },
        "return_gap_audit": {
            "summary": gap_summary,
            "strategy_gap": _read_csv_records(UNIFIED_CONTRACT_GAP_DIR / "strategy_return_gap.csv"),
            "formal_only_samples": _read_csv_records(UNIFIED_CONTRACT_GAP_DIR / "formal_only_trades.csv", limit=50),
        },
        "headline": {
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "candidate_rows": native_summary.get("candidate_rows"),
            "selected_rows": native_summary.get("selected_rows"),
            "closed_trades": native_summary.get("closed_trades"),
            "win_rate": native_summary.get("win_rate"),
            "avg_ret": native_summary.get("avg_ret"),
        },
        "overall_compare": _read_csv_records(STRATEGY_REDUCTION_RECOVERY_DIR / "overall_compare.csv"),
        "strategy_compare": _read_csv_records(STRATEGY_REDUCTION_RECOVERY_DIR / "strategy_compare_wide.csv"),
        "scenario_matrix": _read_csv_records(STRATEGY_REDUCTION_RECOVERY_DIR / "scenario_matrix.csv"),
        "daily_proxy_recall": _read_csv_records(PROFITABLE_RECALL_DIR / "profitable_recall_by_strategy.csv"),
        "native_bridge_recall": _read_csv_records(NATIVE_BRIDGE_RECALL_DIR / "native_bridge_recall_by_strategy.csv"),
        "fusion_guardrails": {
            "summary": _read_json(STRATEGY_FUSION_GUARDRAILS_DIR / "summary.json"),
            "checks": _read_csv_records(STRATEGY_FUSION_GUARDRAILS_DIR / "guardrail_checks.csv"),
            "recall_compare": _read_csv_records(STRATEGY_FUSION_GUARDRAILS_DIR / "recall_compare.csv"),
        },
        "m30_integrity": {
            "summary": _read_json(NATIVE_BRIDGE_M30_DIR / "summary.json"),
            "by_strategy": _read_csv_records(NATIVE_BRIDGE_M30_DIR / "m30_summary_by_strategy.csv"),
            "by_source": _read_csv_records(NATIVE_BRIDGE_M30_DIR / "m30_summary_by_source.csv"),
            "unconfirmed_samples": _read_csv_records(NATIVE_BRIDGE_M30_DIR / "m30_unconfirmed_samples.csv", limit=50),
            "native_semantics": {
                "summary": _read_json(NATIVE_BRIDGE_NATIVE_M30_DIR / "summary.json"),
                "by_strategy": _read_csv_records(NATIVE_BRIDGE_NATIVE_M30_DIR / "native_m30_by_strategy.csv"),
                "by_source": _read_csv_records(NATIVE_BRIDGE_NATIVE_M30_DIR / "native_m30_by_source.csv"),
                "scenario_matrix": _read_csv_records(NATIVE_BRIDGE_NATIVE_M30_DIR / "native_m30_scenario_matrix.csv"),
                "proxy_killed_samples": _read_csv_records(NATIVE_BRIDGE_NATIVE_M30_DIR / "proxy_killed_but_native_confirmed.csv", limit=50),
            },
            "interpretation": "30m 数据覆盖用于证明执行链路是否具备验收基础；统一代理确认通过率低时，应保留各原生策略自己的 30m 确认语义，而不是误判策略融合失败。",
        },
        "artifacts": {
            "strategy_reduction_report": _path_status(STRATEGY_REDUCTION_RECOVERY_DIR / "REPORT_CN.md"),
            "strategy_reduction_overall": _path_status(STRATEGY_REDUCTION_RECOVERY_DIR / "overall_compare.csv"),
            "strategy_reduction_strategy_compare": _path_status(STRATEGY_REDUCTION_RECOVERY_DIR / "strategy_compare_wide.csv"),
            "native_bridge_summary": _path_status(NATIVE_BRIDGE_BACKTEST_DIR / "summary.json"),
            "native_bridge_closed_trades": _path_status(NATIVE_BRIDGE_BACKTEST_DIR / "closed_trades.csv"),
            "native_bridge_scenario_matrix": _path_status(NATIVE_BRIDGE_BACKTEST_DIR / "scenario_matrix.csv"),
            "formal_unified_summary": _path_status(FORMAL_UNIFIED_CONTRACT_DIR / "summary.json"),
            "formal_unified_report": _path_status(FORMAL_UNIFIED_CONTRACT_DIR / "REPORT_CN.md"),
            "formal_unified_closed_trades": _path_status(FORMAL_UNIFIED_CONTRACT_DIR / "formal_unified_closed_trades.csv"),
            "practical_fusion_summary": _path_status(PRACTICAL_FUSION_CONTRACT_DIR / "summary.json"),
            "practical_fusion_report": _path_status(PRACTICAL_FUSION_CONTRACT_DIR / "REPORT_CN.md"),
            "five_strategy_dispatch_summary": _path_status(FIVE_STRATEGY_DISPATCH_CONTRACT_DIR / "summary.json"),
            "five_strategy_dispatch_report": _path_status(FIVE_STRATEGY_DISPATCH_CONTRACT_DIR / "REPORT_CN.md"),
            "five_strategy_dispatch_contract": _path_status(FIVE_STRATEGY_DISPATCH_CONTRACT_DIR / "dispatch_contract.json"),
            "five_strategy_overfit_summary": _path_status(FIVE_STRATEGY_OVERFIT_AUDIT_DIR / "summary.json"),
            "five_strategy_overfit_report": _path_status(FIVE_STRATEGY_OVERFIT_AUDIT_DIR / "REPORT_CN.md"),
            "daily_proxy_recall": _path_status(PROFITABLE_RECALL_DIR / "profitable_recall_by_strategy.csv"),
            "native_bridge_recall": _path_status(NATIVE_BRIDGE_RECALL_DIR / "native_bridge_recall_by_strategy.csv"),
            "native_bridge_m30_summary": _path_status(NATIVE_BRIDGE_M30_DIR / "summary.json"),
            "native_bridge_m30_report": _path_status(NATIVE_BRIDGE_M30_DIR / "REPORT_CN.md"),
            "native_bridge_native_m30_summary": _path_status(NATIVE_BRIDGE_NATIVE_M30_DIR / "summary.json"),
            "native_bridge_native_m30_report": _path_status(NATIVE_BRIDGE_NATIVE_M30_DIR / "REPORT_CN.md"),
            "strategy_fusion_guardrails_summary": _path_status(STRATEGY_FUSION_GUARDRAILS_DIR / "summary.json"),
            "strategy_fusion_guardrails_report": _path_status(STRATEGY_FUSION_GUARDRAILS_DIR / "REPORT_CN.md"),
            "unified_contract_gap_summary": _path_status(UNIFIED_CONTRACT_GAP_DIR / "summary.json"),
            "unified_contract_gap_report": _path_status(UNIFIED_CONTRACT_GAP_DIR / "REPORT_CN.md"),
        },
    }


def _read_shadow_ledger_records(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        df = pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return []
    except Exception:
        logger.exception("Failed to read shadow ledger artifact: %s", path)
        return []
    logical_key = [col for col in ["entry_date", "code", "route"] if col in df.columns]
    if len(logical_key) == 3:
        df = df.drop_duplicates(subset=logical_key, keep="last")
    elif "ticket_key" in df.columns:
        df = df.drop_duplicates(subset=["ticket_key"], keep="last")
    if limit is not None:
        df = df.head(limit)
    df = df.astype(object).where(pd.notna(df), None)
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _enrich_trade_strategy_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    try:
        df = pd.DataFrame(records)
        df = _with_route_strategy_fields(df)
        df = df.astype(object).where(pd.notna(df), None)
        return json.loads(df.to_json(orient="records", force_ascii=False))
    except Exception:
        logger.exception("Failed to enrich records with trade strategy fields")
        return records


def _first_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    return records[0] if records else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ok"}
    return False


def _source_count(summary: dict[str, Any], source_name: str) -> int:
    for item in summary.get("sources") or []:
        if item.get("source") == source_name:
            return int(item.get("rows") or item.get("final_candidates") or item.get("confirmed") or 0)
    return 0


def _extract_g2_gap_supplement_status(summary: dict[str, Any]) -> dict[str, Any]:
    sources = summary.get("sources") if isinstance(summary, dict) else []
    source = next(
        (
            item
            for item in sources or []
            if isinstance(item, dict) and item.get("source") == "g2_gap_supplement_current_builder_v1"
        ),
        None,
    )
    if not source:
        return {
            "ok": False,
            "fresh_for_entry_date": False,
            "status": "missing_g2_source_diagnostic",
            "message": "未找到 G2 空档补位源诊断，无法确认是否因源过期导致无候选。",
        }

    rows = int(float(source.get("rows") or 0))
    fresh_for_entry_date = _truthy(source.get("fresh_for_entry_date"))
    requested_entry_date = source.get("requested_entry_date") or source.get("entry_date") or summary.get("entry_date")
    latest_dates = [
        source.get("latest_live_update_date"),
        source.get("latest_ledger_entry_date"),
        source.get("latest_v2_complete_entry_date"),
        source.get("latest_volume5_alpha191_entry_date"),
    ]
    latest_source_date = max([str(item) for item in latest_dates if item] or [""])
    stale_reason = source.get("stale_reason") or ""
    status = "fresh" if fresh_for_entry_date else ("stale" if stale_reason else str(source.get("status") or "no_candidate"))
    if fresh_for_entry_date:
        message = f"G2 空档补位源已覆盖 {requested_entry_date}，当前源候选 {rows} 条。"
    elif stale_reason:
        message = f"G2 空档补位源未覆盖 {requested_entry_date}，最新源日期 {latest_source_date or '--'}。"
    else:
        message = f"G2 空档补位源已读取，但当前无候选，状态 {status}。"

    return {
        "ok": fresh_for_entry_date,
        "fresh_for_entry_date": fresh_for_entry_date,
        "status": status,
        "message": message,
        "requested_entry_date": requested_entry_date,
        "latest_source_date": latest_source_date or None,
        "latest_live_update_date": source.get("latest_live_update_date"),
        "latest_ledger_entry_date": source.get("latest_ledger_entry_date"),
        "latest_v2_complete_entry_date": source.get("latest_v2_complete_entry_date"),
        "latest_volume5_alpha191_entry_date": source.get("latest_volume5_alpha191_entry_date"),
        "source_freshness": source.get("source_freshness"),
        "stale_reason": stale_reason or None,
        "rows": rows,
    }


def _build_pipeline(
    summary: dict[str, Any],
    tickets: list[dict[str, Any]],
    route_diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    market_context = summary.get("market_context") or {}
    selected_rows = int(summary.get("selected_rows") or 0)
    qualified_rows = int(summary.get("qualified_shadow_buy_rows") or 0)
    source_rows = int(summary.get("source_rows") or 0)
    ticket_rows = len(tickets)
    diagnostic_map = {item.get("route"): item for item in route_diagnostics}
    inst = diagnostic_map.get("institutional_mainwave") or {}
    panic = diagnostic_map.get("panic_repair") or {}
    old_g3_rows = _source_count(summary, "old_g3_current_builder_v1")

    def stage(key: str, title: str, status: str, detail: str, count: int | None = None) -> dict[str, Any]:
        return {"key": key, "title": title, "status": status, "detail": detail, "count": count}

    return [
        stage(
            "market_context",
            "市场状态",
            "pass" if market_context.get("ok") else "warn",
            f"{market_context.get('source') or 'unknown'} / rows={market_context.get('rows') or 0}",
            int(market_context.get("rows") or 0),
        ),
        stage(
            "panic_repair",
            "恐慌修复",
            "pass" if int(panic.get("eligible_rows") or 0) > 0 else "blocked",
            panic.get("top_block_reason") or "无阻断",
            int(panic.get("eligible_rows") or 0),
        ),
        stage(
            "institutional_mainwave",
            "机构主升",
            "pass" if int(inst.get("eligible_rows") or 0) > 0 else "blocked",
            summary.get("route_reason") or inst.get("top_block_reason") or "等待候选",
            int(inst.get("eligible_rows") or 0),
        ),
        stage(
            "old_g3_route",
            "旧G3兼容",
            "pass" if old_g3_rows > 0 else "idle",
            f"当前候选 {old_g3_rows}",
            old_g3_rows,
        ),
        stage(
            "router_selection",
            "路由选择",
            "pass" if selected_rows > 0 else "blocked",
            summary.get("selected_route") or summary.get("diagnosis_code") or "未选中",
            selected_rows,
        ),
        stage(
            "shadow_ticket",
            "买入候选票据",
            "pass" if qualified_rows > 0 and ticket_rows > 0 else "blocked",
            f"合格买入候选 {qualified_rows} / 票据 {ticket_rows}",
            ticket_rows,
        ),
        stage(
            "formal_guardrail",
            "正式交易闸门",
            "locked",
            "formal_buy_signal=false, auto_order_allowed=false",
            0,
        ),
        stage(
            "source_pool",
            "候选池",
            "pass" if source_rows > 0 else "blocked",
            f"来源候选 {source_rows}",
            source_rows,
        ),
    ]


def _build_readiness(
    summary: dict[str, Any],
    tickets: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    qualified_rows = int(summary.get("qualified_shadow_buy_rows") or 0)
    selected_rows = int(summary.get("selected_rows") or 0)
    all_m30_ok = bool(tickets) and all(_truthy(item.get("m30_confirmed")) for item in tickets)
    return [
        {
            "key": "candidate_selected",
            "label": "今日路由选中",
            "status": "pass" if selected_rows > 0 else "blocked",
            "detail": f"selected_rows={selected_rows}",
        },
        {
            "key": "shadow_ticket_ready",
            "label": "影子票据生成",
            "status": "pass" if qualified_rows > 0 and tickets else "blocked",
            "detail": f"qualified_shadow_buy_rows={qualified_rows}, tickets={len(tickets)}",
        },
        {
            "key": "m30_confirmed",
            "label": "30m确认",
            "status": "pass" if all_m30_ok else "blocked",
            "detail": "全部票据已通过30m确认" if all_m30_ok else "仍有票据缺少30m确认",
        },
        {
            "key": "risk_contract",
            "label": "风控合同",
            "status": "pass" if all(item.get("structure_stop") and item.get("hard_stop") for item in tickets) else "blocked",
            "detail": "每张票据必须有结构止损与硬止损",
        },
        {
            "key": "shadow_ledger",
            "label": "影子台账",
            "status": "pass" if ledger else "warn",
            "detail": f"ledger_rows={len(ledger)}",
        },
        {
            "key": "formal_gate",
            "label": "正式交易闸门",
            "status": "locked",
            "detail": "当前只允许观察和影子交易，不允许真实下单",
        },
    ]


def _read_backtest_snapshot() -> dict[str, Any]:
    summary = _read_json(PROMOTION_SUMMARY_PATH)
    return {
        "summary": summary,
        "profile": _latest_profile(),
        "windows": _latest_window_metrics(),
        "route_attribution": _read_route_attribution_records(),
        "strategy_reduction_recovery": _read_strategy_reduction_recovery(),
        "artifacts": {
            "report_dir": _path_status(FINAL_G3_BACKTEST_DIR),
            "summary": _path_status(PROMOTION_SUMMARY_PATH),
            "summary_csv": _path_status(PROMOTION_SUMMARY_CSV_PATH),
            "gates": _path_status(PROMOTION_GATES_PATH),
            "closed_trades": _path_status(HISTORICAL_TRADES_PATH),
            "equity_curve": _path_status(EQUITY_CURVE_PATH),
            "mtm_equity_curve": _path_status(MTM_EQUITY_CURVE_PATH),
            "strategy_reduction_recovery": _path_status(STRATEGY_REDUCTION_RECOVERY_DIR / "REPORT_CN.md"),
            "native_bridge_backtest": _path_status(NATIVE_BRIDGE_BACKTEST_DIR / "summary.json"),
        },
    }


def _default_monitor_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "interval_seconds": 120,
        "trading_hours_only": True,
        "email_enabled": True,
        "heartbeat_enabled": True,
        "heartbeat_minutes": 30,
        "recipient_email": "",
        "run_current_refresh": True,
        "last_run_at": None,
        "last_success_at": None,
        "last_error": None,
        "last_result": None,
        "last_email_sent_at": None,
        "last_heartbeat_sent_at": None,
        "last_blocker_alert_at": None,
        "last_refresh_task_id": None,
        "last_alert_keys": {},
        "last_email_error": None,
    }


def _load_monitor_state() -> dict[str, Any]:
    state = _read_json(MONITOR_STATE_PATH)
    base = _default_monitor_state()
    if isinstance(state, dict):
        base.update(state)
    base["enabled"] = bool(base.get("enabled"))
    base["interval_seconds"] = max(120, int(base.get("interval_seconds") or 120))
    base["trading_hours_only"] = bool(base.get("trading_hours_only", True))
    base["email_enabled"] = bool(base.get("email_enabled", True))
    base["heartbeat_enabled"] = bool(base.get("heartbeat_enabled", True))
    base["heartbeat_minutes"] = max(30, int(base.get("heartbeat_minutes") or 30))
    base["recipient_email"] = str(base.get("recipient_email") or "").strip()
    base["run_current_refresh"] = bool(base.get("run_current_refresh", True))
    if not isinstance(base.get("last_alert_keys"), dict):
        base["last_alert_keys"] = {}
    return base


def _save_monitor_state(state: dict[str, Any]) -> None:
    _write_json(MONITOR_STATE_PATH, state)


def _default_observation_scheduler_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "hour": 15,
        "minute": 40,
        "trading_days_only": True,
        "run_smoke": True,
        "refresh_smoke": True,
        "auto_repair_minute30": True,
        "last_run_at": None,
        "last_success_at": None,
        "last_error": None,
        "last_result": None,
    }


def _load_observation_scheduler_state() -> dict[str, Any]:
    state = _read_json(OBSERVATION_STATE_PATH)
    base = _default_observation_scheduler_state()
    if isinstance(state, dict):
        base.update(state)
    base["enabled"] = bool(base.get("enabled"))
    base["hour"] = min(23, max(0, int(base.get("hour") or 15)))
    base["minute"] = min(59, max(0, int(base.get("minute") or 40)))
    base["trading_days_only"] = bool(base.get("trading_days_only", True))
    base["run_smoke"] = bool(base.get("run_smoke", True))
    base["refresh_smoke"] = bool(base.get("refresh_smoke", True))
    base["auto_repair_minute30"] = bool(base.get("auto_repair_minute30", True))
    return base


def _save_observation_scheduler_state(state: dict[str, Any]) -> None:
    _write_json(OBSERVATION_STATE_PATH, state)


def _default_broker_sync_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "interval_seconds": None,
        "trading_hours_only": False,
        "trading_days_only": True,
        "hour": 17,
        "minute": 30,
        "sync_holdings": True,
        "sync_trades": True,
        "last_run_at": None,
        "last_success_at": None,
        "last_holdings_success_at": None,
        "last_trades_success_at": None,
        "last_error": None,
        "last_result": None,
    }


def _load_broker_sync_state() -> dict[str, Any]:
    state = _read_json(BROKER_SYNC_STATE_PATH)
    base = _default_broker_sync_state()
    if isinstance(state, dict):
        base.update(state)
    base["enabled"] = bool(base.get("enabled"))
    interval = base.get("interval_seconds")
    base["interval_seconds"] = max(60, int(interval)) if interval not in (None, "") else None
    base["trading_hours_only"] = bool(base.get("trading_hours_only", False))
    base["trading_days_only"] = bool(base.get("trading_days_only", True))
    base["hour"] = min(23, max(0, int(base.get("hour") or 17)))
    base["minute"] = min(59, max(0, int(base.get("minute") if base.get("minute") is not None else 30)))
    base["sync_holdings"] = bool(base.get("sync_holdings", True))
    base["sync_trades"] = bool(base.get("sync_trades", True))
    return base


def _save_broker_sync_state(state: dict[str, Any]) -> None:
    _write_json(BROKER_SYNC_STATE_PATH, state)


def _append_monitor_event(event: dict[str, Any]) -> None:
    existing = _read_json(MONITOR_EVENTS_PATH)
    rows = existing.get("events") if isinstance(existing, dict) else []
    if not isinstance(rows, list):
        rows = []
    item = {
        "created_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        **(event if isinstance(event, dict) else {}),
    }
    rows.append(item)
    _write_json(MONITOR_EVENTS_PATH, {"events": rows[-300:]})


def _recent_monitor_events(limit: int = 20) -> list[dict[str, Any]]:
    existing = _read_json(MONITOR_EVENTS_PATH)
    rows = existing.get("events") if isinstance(existing, dict) else []
    if not isinstance(rows, list):
        return []
    return rows[-limit:][::-1]


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _fmt_num(value: Any, digits: int = 2, suffix: str = "") -> str:
    val = _to_float(value)
    if val is None:
        return "--"
    return f"{val:.{digits}f}{suffix}"


def _default_monitor_recipient_email() -> str:
    email_cfg = app_config.get("email", default=None, config_file="settings.yaml") or {}
    to_emails = email_cfg.get("to_emails") or []
    if isinstance(to_emails, str):
        to_emails = [to_emails]
    for item in to_emails:
        text = str(item or "").strip()
        if text:
            return text
    return str(email_cfg.get("from_email") or email_cfg.get("smtp_user") or "").strip()


def _load_smtp_config(recipient_override: str | None = None) -> dict[str, Any]:
    email_cfg = app_config.get("email", default=None, config_file="settings.yaml") or {}
    if email_cfg.get("enabled", True) is False:
        raise RuntimeError("email.enabled is disabled")
    smtp_server = str(email_cfg.get("smtp_server") or "").strip()
    smtp_port = int(email_cfg.get("smtp_port") or 0)
    smtp_user = str(email_cfg.get("smtp_user") or "").strip()
    smtp_password = str(email_cfg.get("smtp_password") or "").strip()
    from_email = str(email_cfg.get("from_email") or "").strip()
    to_emails = email_cfg.get("to_emails") or []
    if isinstance(to_emails, str):
        to_emails = [to_emails]
    to_emails = [str(item).strip() for item in to_emails if str(item).strip()]
    if recipient_override and "@" in recipient_override:
        to_emails = [recipient_override.strip()]
    if not all([smtp_server, smtp_port, smtp_user, smtp_password, from_email, to_emails]):
        raise RuntimeError("SMTP config is incomplete")
    return {
        "server": smtp_server,
        "port": smtp_port,
        "username": smtp_user,
        "password": smtp_password,
        "from_email": from_email,
        "to_emails": to_emails,
        "use_ssl": bool(email_cfg.get("use_ssl")) or smtp_port == 465,
    }


def _smtp_attempt_plans(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    port = int(cfg.get("port") or 0)
    plans: list[dict[str, Any]] = []
    if cfg.get("use_ssl"):
        plans.append({"mode": "ssl", "port": port})
        if port == 465:
            plans.append({"mode": "starttls", "port": 587})
    else:
        if port == 587:
            plans.append({"mode": "starttls", "port": port})
        plans.append({"mode": "plain", "port": port})
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in plans:
        key = (str(item.get("mode") or ""), int(item.get("port") or 0))
        if key not in seen:
            deduped.append(item)
            seen.add(key)
    return deduped


def _open_smtp_server(cfg: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    errors: list[str] = []
    for plan in _smtp_attempt_plans(cfg):
        server = None
        mode = str(plan.get("mode") or "")
        port = int(plan.get("port") or 0)
        try:
            if mode == "ssl":
                server = smtplib.SMTP_SSL(cfg["server"], port, timeout=30)
            else:
                server = smtplib.SMTP(cfg["server"], port, timeout=30)
                server.ehlo()
                if mode == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            server.login(cfg["username"], cfg["password"])
            return server, {"mode": mode, "port": port}
        except Exception as exc:
            errors.append(f"{mode}:{port} -> {exc}")
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
    raise RuntimeError("SMTP send failed: " + " | ".join(errors))


def _send_shadow_email(subject: str, body: str, recipient_override: str | None = None) -> dict[str, Any]:
    cfg = _load_smtp_config(recipient_override)
    msg = MIMEMultipart()
    msg["From"] = cfg["from_email"]
    msg["To"] = ",".join(cfg["to_emails"])
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    server, send_meta = _open_smtp_server(cfg)
    try:
        server.sendmail(cfg["from_email"], cfg["to_emails"], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return {"sent": True, "to": cfg["to_emails"], "subject": subject, **send_meta}


def _notification_config_check(recipient_override: str | None = None) -> dict[str, Any]:
    try:
        cfg = _load_smtp_config(recipient_override)
        return {
            "name": "email_notification",
            "ok": True,
            "status": "pass",
            "blocking": False,
            "message": f"SMTP configured, recipients={len(cfg.get('to_emails') or [])}",
        }
    except Exception as exc:
        return {
            "name": "email_notification",
            "ok": False,
            "status": "warn",
            "blocking": False,
            "message": str(exc),
        }


def _ticket_alert_key(ticket: dict[str, Any], entry_date: str) -> str:
    return "|".join(
        [
            STRATEGY_ID,
            str(entry_date or ""),
            str(ticket.get("route") or ""),
            str(ticket.get("code") or ticket.get("code_raw") or ""),
            str(ticket.get("planned_entry_ts") or ticket.get("confirm_datetime") or ""),
        ]
    )


def _should_send_heartbeat(state: dict[str, Any], now: datetime, force_send: bool = False) -> bool:
    if force_send:
        return True
    if not state.get("heartbeat_enabled", True):
        return False
    last_text = str(state.get("last_heartbeat_sent_at") or "").strip()
    if not last_text:
        return True
    try:
        last_dt = datetime.fromisoformat(last_text)
    except Exception:
        return True
    if last_dt.date() != now.date():
        return True
    return (now - last_dt).total_seconds() >= int(state.get("heartbeat_minutes") or 30) * 60


def _build_shadow_buy_email_body(now_text: str, workflow: dict[str, Any], tickets: list[dict[str, Any]]) -> str:
    summary_lines = [
        f"Generated at: {now_text}",
        f"Strategy: {STRATEGY_NAME}",
        f"Entry date: {workflow.get('entry_date') or '--'}",
        f"Selected route: {workflow.get('selected_route') or '--'}",
        f"Diagnosis: {workflow.get('diagnosis_code') or '--'}",
        "",
        "Shadow buy tickets:",
    ]
    for idx, item in enumerate(tickets, start=1):
        summary_lines.append(
            (
                f"{idx}. {item.get('code') or item.get('code_raw') or '--'} "
                f"{item.get('name') or item.get('stock_name') or ''} | "
                f"route={item.get('route_label') or item.get('route') or '--'} | "
                f"planned={item.get('planned_entry_ts') or '--'} | "
                f"confirm={item.get('confirm_datetime') or '--'} | "
                f"position={_fmt_num(float(item.get('position_pct') or 0) * 100, 1, '%')} | "
                f"ref={_fmt_num(item.get('reference_close'), 2)} | "
                f"stop={_fmt_num(item.get('structure_stop'), 2)} / hard={_fmt_num(item.get('hard_stop'), 2)} | "
                f"30m={item.get('m30_status') or item.get('m30_confirmed') or '--'}"
            )
        )
        exit_contract = str(item.get("exit_contract") or "").strip()
        if exit_contract:
            summary_lines.append(f"   Exit: {exit_contract[:180]}")
    summary_lines.extend(["", "Guardrail: shadow-only observation. No formal buy signal, no auto order."])
    return "\n".join(summary_lines).strip() + "\n"


def _build_blocker_email_body(now_text: str, workflow: dict[str, Any]) -> str:
    blockers = workflow.get("blockers") if isinstance(workflow.get("blockers"), list) else []
    lines = [
        f"Generated at: {now_text}",
        f"Strategy: {STRATEGY_NAME}",
        f"Entry date: {workflow.get('entry_date') or '--'}",
        f"Selected route: {workflow.get('selected_route') or '--'}",
        f"Diagnosis: {workflow.get('diagnosis_code') or '--'}",
        f"Workflow ok: {bool(workflow.get('ok'))}",
        "",
        "Blockers:",
    ]
    if blockers:
        for idx, item in enumerate(blockers[:10], start=1):
            lines.append(f"{idx}. {item.get('name') or '--'} | {item.get('message') or '--'}")
    else:
        lines.append("- no blocker, heartbeat only")
    lines.extend(["", "Guardrail: shadow-only observation. No formal buy signal, no auto order."])
    return "\n".join(lines).strip() + "\n"


def _run_shadow_notifications(
    workflow: dict[str, Any],
    state: dict[str, Any],
    force_send: bool = False,
) -> dict[str, Any]:
    if not state.get("email_enabled", True):
        return {"ok": True, "email_sent": False, "reason": "email_disabled"}
    now = datetime.now()
    now_text = now.isoformat(sep=" ", timespec="seconds")
    if state.get("trading_hours_only", True) and not force_send and not _in_trading_window(now):
        result = {
            "ok": True,
            "email_sent": False,
            "reason": "not_trading_window",
            "checked_at": now_text,
        }
        _append_monitor_event({"type": "email_skipped", **result})
        return result
    runtime = _load_current_runtime(limit=50)
    tickets = runtime.get("tickets") if isinstance(runtime.get("tickets"), list) else []
    entry_date = str(workflow.get("entry_date") or runtime.get("summary", {}).get("entry_date") or "")
    last_alert_keys = state.get("last_alert_keys") if isinstance(state.get("last_alert_keys"), dict) else {}
    new_tickets = [item for item in tickets if force_send or _ticket_alert_key(item, entry_date) not in last_alert_keys]
    sent: list[dict[str, Any]] = []
    recipient = state.get("recipient_email") or None
    try:
        if new_tickets:
            subject = f"G3 State Alpha shadow buy {entry_date or '--'} {now_text[11:16]}"
            sent.append(_send_shadow_email(subject, _build_shadow_buy_email_body(now_text, workflow, new_tickets), recipient))
            for item in new_tickets:
                last_alert_keys[_ticket_alert_key(item, entry_date)] = now_text
            state["last_email_sent_at"] = now_text
        elif not workflow.get("ok"):
            should_send_blocker = force_send
            last_blocker_text = str(state.get("last_blocker_alert_at") or "").strip()
            if not should_send_blocker:
                try:
                    last_blocker_dt = datetime.fromisoformat(last_blocker_text) if last_blocker_text else None
                except Exception:
                    last_blocker_dt = None
                should_send_blocker = last_blocker_dt is None or (now - last_blocker_dt).total_seconds() >= 30 * 60
            if should_send_blocker:
                subject = f"G3 State Alpha blocker {entry_date or '--'} {now_text[11:16]}"
                sent.append(_send_shadow_email(subject, _build_blocker_email_body(now_text, workflow), recipient))
                state["last_blocker_alert_at"] = now_text
                state["last_email_sent_at"] = now_text
        elif _should_send_heartbeat(state, now, force_send=force_send):
            subject = f"G3 State Alpha heartbeat {entry_date or '--'} {now_text[11:16]}"
            sent.append(_send_shadow_email(subject, _build_blocker_email_body(now_text, workflow), recipient))
            state["last_heartbeat_sent_at"] = now_text
            state["last_email_sent_at"] = now_text
        if len(last_alert_keys) > 1000:
            last_alert_keys = dict(list(last_alert_keys.items())[-1000:])
        state["last_alert_keys"] = last_alert_keys
        state["last_email_error"] = None
        _save_monitor_state(state)
        result = {"ok": True, "email_sent": bool(sent), "sent": sent, "new_ticket_count": len(new_tickets)}
        _append_monitor_event({"type": "email", **result})
        return result
    except Exception as exc:
        state["last_email_error"] = str(exc)
        _save_monitor_state(state)
        result = {"ok": False, "email_sent": False, "error": str(exc), "new_ticket_count": len(new_tickets)}
        _append_monitor_event({"type": "email_error", **result})
        logger.warning("G3 State Alpha shadow email failed: %s", exc)
        return result


def _set_refresh_task(task_id: str, payload: dict[str, Any]) -> None:
    with REFRESH_TASK_LOCK:
        task = REFRESH_TASKS.setdefault(task_id, {})
        task.update(payload)


def _get_refresh_task(task_id: str) -> dict[str, Any] | None:
    with REFRESH_TASK_LOCK:
        task = REFRESH_TASKS.get(task_id)
        return dict(task) if isinstance(task, dict) else None


def _running_refresh_task() -> dict[str, Any] | None:
    with REFRESH_TASK_LOCK:
        for task in REFRESH_TASKS.values():
            if task.get("status") in {"queued", "running"}:
                return dict(task)
    return None


def _artifact_check(name: str, path: Path, min_bytes: int = 1) -> dict[str, Any]:
    status = _path_status(path)
    ok = bool(status.get("exists")) and int(status.get("size_bytes") or 0) >= min_bytes
    return {
        "name": name,
        "ok": ok,
        "status": "pass" if ok else "blocked",
        "path": str(path),
        "modified_at": status.get("modified_at"),
        "size_bytes": status.get("size_bytes"),
        "message": "产物可用" if ok else "产物缺失或为空",
    }


def _directory_writable_check(name: str, path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        ok = True
        message = "目录可写"
    except Exception as exc:
        ok = False
        message = str(exc)
    return {
        "name": name,
        "ok": ok,
        "status": "pass" if ok else "blocked",
        "path": str(path),
        "message": message,
    }


def _load_current_runtime(limit: int = 50) -> dict[str, Any]:
    summary_path = STATE_ALPHA_RUNTIME_DIR / "latest_summary.json"
    tickets_path = STATE_ALPHA_RUNTIME_DIR / "latest_shadow_tickets.csv"
    ledger_path = STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"
    diagnostics_path = STATE_ROUTER_RUNTIME_DIR / "latest_route_diagnostics.csv"
    summary = _read_json(summary_path) or _read_json(STATE_ROUTER_RUNTIME_DIR / "latest_summary.json")
    tickets = _enrich_trade_strategy_records(_read_csv_records(tickets_path, limit=limit))
    broker_snapshot = _broker_snapshot()
    broker_trades = broker_snapshot.get("broker_trades") if isinstance(broker_snapshot.get("broker_trades"), list) else []
    ledger = _attach_exit_advice(
        _enrich_trade_strategy_records(_read_shadow_ledger_records(ledger_path, limit=limit)),
        source="shadow_ledger",
        updated_at=summary.get("generated_at") if isinstance(summary, dict) else None,
        broker_trades=broker_trades,
    )
    diagnostics = _read_csv_records(diagnostics_path, limit=50)
    return {
        "summary": summary,
        "tickets": tickets,
        "ledger": ledger,
        "route_diagnostics": diagnostics,
        "pipeline": _build_pipeline(summary, tickets, diagnostics),
        "readiness_checks": _build_readiness(summary, tickets, ledger),
    }


def _build_workflow_status() -> dict[str, Any]:
    runtime = _load_current_runtime(limit=50)
    summary = runtime.get("summary") or {}
    tickets = runtime.get("tickets") or []
    monitor_state = _load_monitor_state()
    notification_check = _notification_config_check(monitor_state.get("recipient_email") or None)
    pipeline_checks = [
        _artifact_check("state_alpha_summary", STATE_ALPHA_RUNTIME_DIR / "latest_summary.json"),
        _artifact_check("latest_shadow_tickets", STATE_ALPHA_RUNTIME_DIR / "latest_shadow_tickets.csv"),
        _artifact_check("shadow_ledger", STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"),
        _artifact_check("route_diagnostics", STATE_ROUTER_RUNTIME_DIR / "latest_route_diagnostics.csv"),
        _directory_writable_check("state_alpha_runtime_dir", STATE_ALPHA_RUNTIME_DIR),
        notification_check,
    ]
    qualified_rows = int(summary.get("qualified_shadow_buy_rows") or 0)
    formal_rows = int(summary.get("formal_buy_signal_rows") or 0)
    auto_rows = int(summary.get("auto_order_allowed_rows") or 0)
    order_rows = int(summary.get("order_path_enabled_rows") or 0)
    qualified_ok = qualified_rows == len(tickets) if tickets else qualified_rows == 0
    m30_ok = all(_truthy(item.get("m30_confirmed")) for item in tickets) if tickets else True
    business_checks = [
        {
            "name": "qualified_shadow_buy",
            "ok": qualified_ok,
            "status": "pass" if qualified_ok else "blocked",
            "message": f"qualified_shadow_buy_rows={qualified_rows}, tickets={len(tickets)}",
        },
        {
            "name": "formal_gate_locked",
            "ok": formal_rows == 0 and auto_rows == 0 and order_rows == 0,
            "status": "pass" if formal_rows == 0 and auto_rows == 0 and order_rows == 0 else "blocked",
            "message": f"formal={formal_rows}, auto={auto_rows}, order_path={order_rows}",
        },
        {
            "name": "m30_confirmation_contract",
            "ok": m30_ok,
            "status": "pass" if m30_ok else "blocked",
            "message": "有票据时必须全部通过 30m 确认",
        },
    ]
    pipeline_checks.extend(business_checks)
    blockers = [item for item in pipeline_checks if not item.get("ok") and item.get("blocking", True)]
    monitor = configure_shadow_monitor_scheduler()
    observation_scheduler = configure_observation_scheduler()
    paper_execution_path_ok = True
    status = {
        "available": True,
        "ok": not blockers,
        "checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "entry_date": summary.get("entry_date"),
        "decision_date": summary.get("decision_date"),
        "diagnosis_code": summary.get("diagnosis_code"),
        "selected_route": summary.get("selected_route"),
        "monitor": monitor,
        "observation_scheduler": observation_scheduler,
        "pipeline": runtime.get("pipeline") or [],
        "readiness_checks": runtime.get("readiness_checks") or [],
        "pipeline_checks": pipeline_checks,
        "blockers": blockers,
        "last_result": monitor.get("last_result"),
        "recent_events": _recent_monitor_events(limit=20),
        "message": "G3 State Alpha 运行链路可用" if not blockers else "G3 State Alpha 运行链路存在阻断",
        "g2_parity": {
            "workflow_status": True,
            "manual_refresh_task": True,
            "shadow_monitor_state": True,
            "candidate_ledger": True,
            "verification": True,
            "email_alert": bool(notification_check.get("ok")),
            "scheduler_wired": bool(monitor.get("scheduler_wired")),
            "observation_scheduler_wired": bool(observation_scheduler.get("scheduler_wired")),
            "paper_execution_path": paper_execution_path_ok,
            "formal_order_path": False,
        },
    }
    _write_json(OPERATIONS_STATE_PATH, status)
    return status


def _run_refresh_task(
    task_id: str,
    entry_date: str | None = None,
    source: str = "manual",
    force_alert: bool = False,
) -> None:
    started_at = datetime.now()
    _set_refresh_task(
        task_id,
        {
            "task_id": task_id,
            "status": "running",
            "progress": 20,
            "source": source,
            "entry_date": entry_date,
            "started_at": started_at.isoformat(sep=" ", timespec="seconds"),
            "message": "G3 State Alpha shadow refresh running",
        },
    )
    monitor = _load_monitor_state()
    monitor["last_run_at"] = started_at.isoformat(sep=" ", timespec="seconds")
    monitor["last_refresh_task_id"] = task_id
    _save_monitor_state(monitor)
    result = _run_state_router_refresh(entry_date)
    workflow = _build_workflow_status()
    finished_at = datetime.now()
    ok = bool(result.get("ok")) and bool(workflow.get("ok"))
    final = {
        "task_id": task_id,
        "status": "success" if ok else "failed",
        "progress": 100,
        "source": source,
        "entry_date": entry_date or workflow.get("entry_date"),
        "started_at": started_at.isoformat(sep=" ", timespec="seconds"),
        "finished_at": finished_at.isoformat(sep=" ", timespec="seconds"),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
        "refresh": result,
        "workflow": workflow,
        "message": "G3 State Alpha refresh completed" if ok else "G3 State Alpha refresh finished with blockers",
    }
    alert_result = _run_shadow_notifications(workflow, _load_monitor_state(), force_send=force_alert)
    final["alert"] = alert_result
    _set_refresh_task(task_id, final)
    monitor = _load_monitor_state()
    monitor["last_result"] = final
    monitor["last_error"] = None if ok else final.get("message")
    if ok:
        monitor["last_success_at"] = finished_at.isoformat(sep=" ", timespec="seconds")
    _save_monitor_state(monitor)
    _append_monitor_event(
        {
            "type": "refresh",
            "task_id": task_id,
            "status": final.get("status"),
            "source": source,
            "entry_date": final.get("entry_date"),
            "duration_seconds": final.get("duration_seconds"),
            "email_sent": bool(alert_result.get("email_sent")),
            "message": final.get("message"),
        }
    )


def _verification_key(payload: dict[str, Any]) -> str:
    for key in ("ticket_key", "candidate_key", "trade_key"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    code = str(payload.get("code") or payload.get("code_raw") or "").strip()
    entry_date = str(payload.get("entry_date") or "").strip()
    route = str(payload.get("route") or "").strip()
    return "|".join([STRATEGY_ID, entry_date, route, code])


def _load_verifications() -> dict[str, Any]:
    data = _read_json(VERIFICATION_PATH)
    return data if isinstance(data, dict) else {}


def _save_verification(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "").strip()
    allowed = {"", "observed", "confirmed", "rejected", "missed", "manual_review"}
    if status not in allowed:
        return {"ok": False, "error": f"unsupported status: {status}", "allowed_statuses": sorted(allowed)}
    key = _verification_key(payload)
    if not key:
        return {"ok": False, "error": "missing verification key"}
    verifications = _load_verifications()
    if not status:
        verifications.pop(key, None)
        _write_json(VERIFICATION_PATH, verifications)
        return {"ok": True, "removed": True, "key": key}
    item = {
        "key": key,
        "status": status,
        "note": str(payload.get("note") or "").strip(),
        "code": str(payload.get("code") or payload.get("code_raw") or "").strip(),
        "name": str(payload.get("name") or payload.get("stock_name") or "").strip(),
        "entry_date": str(payload.get("entry_date") or "").strip(),
        "route": str(payload.get("route") or "").strip(),
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    verifications[key] = item
    _write_json(VERIFICATION_PATH, verifications)
    return {"ok": True, "key": key, "verification": item, "count": len(verifications)}


def _load_paper_executions() -> list[dict[str, Any]]:
    data = _read_json(PAPER_EXECUTIONS_PATH)
    rows = data.get("executions") if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def _save_paper_executions(rows: list[dict[str, Any]]) -> None:
    _write_json(PAPER_EXECUTIONS_PATH, {"executions": rows[-1000:]})


def _find_current_ticket(payload: dict[str, Any]) -> dict[str, Any] | None:
    runtime = _load_current_runtime(limit=200)
    tickets = runtime.get("tickets") if isinstance(runtime, dict) else []
    ticket_key = str(payload.get("ticket_key") or payload.get("candidate_key") or payload.get("trade_key") or "").strip()
    code = str(payload.get("code") or payload.get("code_raw") or "").strip().upper()
    for item in tickets or []:
        if not isinstance(item, dict):
            continue
        keys = {
            str(item.get("ticket_key") or "").strip(),
            str(item.get("candidate_key") or "").strip(),
            str(item.get("trade_key") or "").strip(),
        }
        item_code = str(item.get("code") or item.get("code_raw") or "").strip().upper()
        if ticket_key and ticket_key in keys:
            return item
        if code and item_code == code:
            return item
    return None


def _default_order_base_capital(base_capital: Any = None) -> float:
    explicit = _as_float(base_capital, None)
    if explicit is not None and explicit > 0:
        return explicit
    try:
        state = _load_broker_state()
        capital = _normalize_broker_capital(state.get("capital") or {}, state.get("holdings") or [])
        total_capital = _as_float(capital.get("total_capital"), None)
        if total_capital is not None and total_capital > 0:
            return total_capital
    except Exception:
        pass
    return 100000.0


def _default_paper_quantity(entry_price: Any, position_pct: Any, base_capital: Any = None) -> int:
    price = _as_float(entry_price, 0.0) or 0.0
    pct = _as_float(position_pct, 0.5) or 0.5
    capital = _default_order_base_capital(base_capital)
    if price <= 0:
        return 0
    raw = int((capital * pct) // price)
    return max(0, (raw // 100) * 100)


def _submit_paper_order(payload: dict[str, Any]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    ticket = _find_current_ticket(payload)
    if not ticket:
        return {"ok": False, "message": "No matching G3 State Alpha shadow ticket found."}
    if not _truthy(ticket.get("qualified_shadow_buy")):
        return {"ok": False, "message": "Ticket is not a qualified shadow buy.", "ticket": ticket}
    if not _truthy(ticket.get("m30_confirmed")):
        return {"ok": False, "message": "Ticket is missing 30m confirmation.", "ticket": ticket}

    price = _as_float(payload.get("execution_price"), None)
    if price is None:
        price = _as_float(ticket.get("reference_close"), None)
    position_pct = _as_float(payload.get("position_pct"), None)
    if position_pct is None:
        position_pct = _as_float(ticket.get("position_pct"), None)
    quantity = int(payload.get("quantity") or 0)
    if quantity <= 0:
        quantity = _default_paper_quantity(price, position_pct, payload.get("base_capital"))
    if price is None or price <= 0 or quantity <= 0:
        return {"ok": False, "message": "Invalid paper execution price or quantity.", "ticket": ticket}

    now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
    ticket_key = str(ticket.get("ticket_key") or ticket.get("candidate_key") or ticket.get("trade_key") or "")
    record = {
        "execution_id": f"g3paper_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}",
        "created_at": now_text,
        "status": "paper_submitted",
        "source": "g3_state_alpha_current_page",
        "strategy_id": STRATEGY_ID,
        "contract": LATEST_G3_PROFILE,
        "shadow_only": True,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "ticket_key": ticket_key,
        "entry_date": ticket.get("entry_date"),
        "decision_date": ticket.get("decision_date"),
        "route": ticket.get("route"),
        "route_label": ticket.get("route_label"),
        "code": ticket.get("code") or ticket.get("code_raw"),
        "name": ticket.get("name") or ticket.get("stock_name"),
        "planned_entry_ts": ticket.get("planned_entry_ts"),
        "confirm_datetime": ticket.get("confirm_datetime"),
        "execution_price": price,
        "quantity": quantity,
        "notional": round(price * quantity, 2),
        "position_pct": position_pct,
        "contract_position_pct": _as_float(ticket.get("position_pct")),
        "structure_stop": _as_float(ticket.get("structure_stop")),
        "hard_stop": _as_float(ticket.get("hard_stop")),
        "take_profit_1": _as_float(ticket.get("take_profit_1")),
        "exit_contract": ticket.get("exit_contract"),
        "notes": str(payload.get("notes") or "").strip(),
    }
    rows = _load_paper_executions()
    rows.append(record)
    _save_paper_executions(rows)
    _append_monitor_event(
        {
            "type": "paper_execution",
            "status": "paper_submitted",
            "ticket_key": ticket_key,
            "code": record.get("code"),
            "message": "G3 State Alpha paper execution recorded.",
        }
    )
    return {"ok": True, "record": record, "execution_count": len(rows)}


def _ptrade_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return _truthy(value)


def _ptrade_payload_requests_live_submit(payload: dict[str, Any]) -> bool:
    dry_run = _ptrade_bool(payload.get("dry_run"), True)
    require_approval = _ptrade_bool(payload.get("require_approval"), True)
    approved = _ptrade_bool(payload.get("approved"), False)
    return (not dry_run) and (approved or not require_approval)


def _ptrade_live_submit_readiness_guard(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not _ptrade_payload_requests_live_submit(payload):
        return None
    status = PTRADE_BRIDGE.status()
    if status.get("ready_for_live_order"):
        return None
    return {
        "ok": False,
        "message": "PTrade bridge is not ready for live order; keep dry_run=true or verify terminal heartbeat/probe first.",
        "bridge_status": status,
    }


def _active_g3_ptrade_order_exists(code: Any, signal_date: Any, side: str) -> bool:
    code6 = _normalize_code6(code)
    expected_side = str(side or "").strip().upper()
    expected_date = str(signal_date or "").strip()
    active_statuses = {"pending", "processing", "dry_run", "waiting_approval", "submitted"}
    try:
        rows = PTRADE_BRIDGE.list_orders(limit=500).get("rows") or []
    except Exception:
        return False
    for item in rows:
        if not isinstance(item, dict):
            continue
        item_status = str(item.get("status") or item.get("_bridge_status") or "").strip().lower()
        if item_status not in active_statuses:
            continue
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        item_code = _normalize_code6(item.get("code") or raw.get("code"))
        item_side = str(item.get("side") or raw.get("side") or "").strip().upper()
        item_date = str(raw.get("signal_date") or raw.get("entry_date") or item.get("signal_date") or "").strip()
        if item_code == code6 and item_side == expected_side and (not expected_date or item_date == expected_date):
            return True
    return False


def _ptrade_audit_record(order: dict[str, Any], ticket: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
    ticket = ticket if isinstance(ticket, dict) else {}
    price = _as_float(order.get("price"), None)
    quantity = int(order.get("quantity") or 0)
    side = str(order.get("side") or "").upper()
    ticket_key = str(
        ticket.get("ticket_key")
        or ticket.get("candidate_key")
        or ticket.get("trade_key")
        or payload.get("ticket_key")
        or ""
    )
    now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
    return {
        "execution_id": f"g3ptrade_{side.lower()}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}",
        "created_at": now_text,
        "status": f"ptrade_{side.lower()}_queued",
        "source": "g3_state_alpha_ptrade_bridge",
        "strategy_id": STRATEGY_ID,
        "contract": LATEST_G3_PROFILE,
        "shadow_only": True,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "ticket_key": ticket_key,
        "entry_date": ticket.get("entry_date") or payload.get("entry_date") or payload.get("signal_date"),
        "decision_date": ticket.get("decision_date") or payload.get("decision_date"),
        "route": ticket.get("route") or payload.get("route"),
        "route_label": ticket.get("route_label") or payload.get("route_label"),
        "code": order.get("code"),
        "name": order.get("name") or ticket.get("name") or ticket.get("stock_name") or payload.get("name"),
        "side": side,
        "execution_price": price,
        "quantity": quantity,
        "notional": round((price or 0.0) * quantity, 2),
        "position_pct": _as_float(ticket.get("position_pct") or payload.get("position_pct")),
        "structure_stop": _as_float(ticket.get("structure_stop") or payload.get("structure_stop")),
        "hard_stop": _as_float(ticket.get("hard_stop") or payload.get("hard_stop")),
        "take_profit_1": _as_float(ticket.get("take_profit_1") or payload.get("take_profit_1")),
        "exit_contract": ticket.get("exit_contract") or payload.get("exit_contract"),
        "ptrade_order_id": order.get("order_id"),
        "ptrade_bridge_status": order.get("status"),
        "ptrade_symbol": order.get("ptrade_symbol"),
        "dry_run": bool(order.get("dry_run")),
        "require_approval": bool(order.get("require_approval")),
        "approved": bool(order.get("approved")),
        "notes": str(payload.get("notes") or "").strip(),
    }


def _append_ptrade_audit(order: dict[str, Any], ticket: dict[str, Any] | None, payload: dict[str, Any]) -> dict[str, Any]:
    record = _ptrade_audit_record(order, ticket, payload)
    rows = _load_paper_executions()
    rows.append(record)
    _save_paper_executions(rows)
    _append_monitor_event(
        {
            "type": "ptrade_bridge_order",
            "status": record.get("status"),
            "ticket_key": record.get("ticket_key"),
            "code": record.get("code"),
            "ptrade_order_id": record.get("ptrade_order_id"),
            "message": "G3 State Alpha PTrade bridge order queued.",
        }
    )
    return record


def _latest_g3_ptrade_orders(limit: int = 200) -> list[dict[str, Any]]:
    try:
        rows = PTRADE_BRIDGE.list_orders(limit=limit).get("rows") or []
    except Exception:
        return []
    result: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        strategy = str(item.get("strategy") or raw.get("strategy") or "")
        source = str(item.get("source") or raw.get("source") or "")
        if strategy == LATEST_G3_PROFILE or source == "g3_state_alpha_page":
            result.append(item)
    return result


def _latest_g3_ptrade_order(side: str) -> dict[str, Any] | None:
    expected_side = str(side or "").strip().upper()
    for item in _latest_g3_ptrade_orders(limit=300):
        item_side = str(item.get("side") or item.get("raw", {}).get("side") or "").strip().upper()
        if item_side == expected_side:
            return item
    return None


def _g3_ptrade_status() -> dict[str, Any]:
    bridge_status = PTRADE_BRIDGE.status()
    readiness = bridge_status.get("readiness") if isinstance(bridge_status.get("readiness"), dict) else {}
    latest_buy = _latest_g3_ptrade_order("BUY")
    latest_sell = _latest_g3_ptrade_order("SELL")
    e2e_acceptance = _read_json(PTRADE_E2E_ACCEPTANCE_PATH)
    internal_acceptance = _read_json(PTRADE_INTERNAL_ACCEPTANCE_PATH)
    e2e_checks = e2e_acceptance.get("checks") if isinstance(e2e_acceptance, dict) else []
    internal_checks = internal_acceptance.get("checks") if isinstance(internal_acceptance, dict) else []
    e2e_acceptance_ok = bool(
        isinstance(e2e_acceptance, dict)
        and e2e_acceptance.get("ok")
        and isinstance(e2e_checks, list)
        and all(bool(item.get("ok")) for item in e2e_checks if isinstance(item, dict) and item.get("required", True))
    )
    internal_acceptance_ok = bool(
        isinstance(internal_acceptance, dict)
        and internal_acceptance.get("ok")
        and isinstance(internal_checks, list)
        and all(bool(item.get("ok")) for item in internal_checks if isinstance(item, dict) and item.get("required", True))
    )

    def _proven(order: dict[str, Any] | None) -> bool:
        if not isinstance(order, dict):
            return False
        status = str(order.get("status") or "").strip().lower()
        bridge_status_text = str(order.get("_bridge_status") or "").strip().lower()
        return status in {"dry_run", "waiting_approval", "submitted"} or bridge_status_text in {"ack", "fill"}

    simulation_ready = bool(
        readiness.get("local_submit_ready", True)
        and readiness.get("ptrade_heartbeat_recent")
        and readiness.get("no_stale_processing")
    )
    internal_strategy_running = bool(bridge_status.get("ptrade_internal_strategy_running"))
    ptrade_python_runner_running = bool(bridge_status.get("ptrade_python_script_runner_running"))
    local_runner_running = bool(bridge_status.get("local_api_runner_running"))
    if internal_strategy_running:
        implementation_stage = "ptrade_internal_strategy"
        implementation_message = "PTrade internal strategy heartbeat is active."
    elif ptrade_python_runner_running:
        implementation_stage = "ptrade_python_script_runner"
        implementation_message = "PTrade Python script runner is consuming dry-run orders; PTrade hosted strategy task is not proven yet."
    elif local_runner_running:
        implementation_stage = "local_api_runner"
        implementation_message = "Local PTrade API runner is consuming dry-run orders; PTrade internal strategy heartbeat is not proven yet."
    else:
        implementation_stage = "no_consumer_heartbeat"
        implementation_message = "No recent PTrade bridge consumer heartbeat is available."
    return {
        "ok": True,
        "mode": "g3_state_alpha_ptrade_status",
        "simulation_ready": simulation_ready,
        "simulation_message": (
            "G3 PTrade dry-run submit and terminal consumption are available."
            if simulation_ready
            else "G3 PTrade dry-run submit is local-only until the PTrade heartbeat is recent and processing is clear."
        ),
        "implementation_stage": implementation_stage,
        "implementation_message": implementation_message,
        "ptrade_internal_strategy_running": internal_strategy_running,
        "ptrade_python_script_runner_running": ptrade_python_runner_running,
        "local_api_runner_running": local_runner_running,
        "g3_dry_run_buy_proven": _proven(latest_buy),
        "g3_dry_run_sell_proven": _proven(latest_sell),
        "g3_e2e_acceptance_ok": e2e_acceptance_ok,
        "g3_e2e_acceptance": e2e_acceptance if isinstance(e2e_acceptance, dict) else {},
        "g3_e2e_acceptance_artifact": _path_status(PTRADE_E2E_ACCEPTANCE_PATH),
        "g3_internal_strategy_acceptance_ok": internal_acceptance_ok,
        "g3_internal_strategy_acceptance": internal_acceptance if isinstance(internal_acceptance, dict) else {},
        "g3_internal_strategy_acceptance_artifact": _path_status(PTRADE_INTERNAL_ACCEPTANCE_PATH),
        "latest_g3_buy_order": latest_buy,
        "latest_g3_sell_order": latest_sell,
        "bridge_status": bridge_status,
        "readiness": readiness,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def _submit_ptrade_buy_order(payload: dict[str, Any]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    ticket = _find_current_ticket(payload)
    if not ticket:
        return {"ok": False, "message": "No matching G3 State Alpha shadow ticket found."}
    if not _truthy(ticket.get("qualified_shadow_buy")):
        return {"ok": False, "message": "Ticket is not a qualified shadow buy.", "ticket": ticket}
    if not _truthy(ticket.get("m30_confirmed")):
        return {"ok": False, "message": "Ticket is missing 30m confirmation.", "ticket": ticket}

    price = _as_float(payload.get("price") or payload.get("execution_price"), None)
    if price is None:
        price = _as_float(ticket.get("reference_close"), None)
    position_pct = _as_float(payload.get("position_pct"), None)
    if position_pct is None:
        position_pct = _as_float(ticket.get("position_pct"), None)
    quantity = int(float(payload.get("quantity") or payload.get("shares") or 0))
    if quantity <= 0:
        quantity = _default_paper_quantity(price, position_pct, payload.get("base_capital"))
    if price is None or price <= 0 or quantity <= 0:
        return {"ok": False, "message": "Invalid PTrade buy price or quantity.", "ticket": ticket}

    code = ticket.get("code") or ticket.get("code_raw") or payload.get("code")
    entry_date = ticket.get("entry_date") or payload.get("signal_date")
    if _active_g3_ptrade_order_exists(code, entry_date, "BUY"):
        return {"ok": False, "message": "Active G3 PTrade BUY order already exists for this ticket.", "ticket": ticket}

    ticket_key = str(ticket.get("ticket_key") or ticket.get("candidate_key") or ticket.get("trade_key") or "")
    order_payload = {
        "source": "g3_state_alpha_page",
        "strategy": LATEST_G3_PROFILE,
        "signal_date": entry_date,
        "entry_date": entry_date,
        "ticket_key": ticket_key,
        "route": ticket.get("route"),
        "code": code,
        "name": ticket.get("name") or ticket.get("stock_name"),
        "side": "BUY",
        "quantity": quantity,
        "price": price,
        "price_type": str(payload.get("price_type") or "limit"),
        "dry_run": _ptrade_bool(payload.get("dry_run"), True),
        "require_approval": _ptrade_bool(payload.get("require_approval"), True),
        "approved": _ptrade_bool(payload.get("approved"), False),
        "reason": str(payload.get("reason") or "G3 State Alpha real-account capacity buy"),
        "risk": {
            "route": ticket.get("route"),
            "confirm_datetime": ticket.get("confirm_datetime"),
            "position_pct": position_pct,
            "contract_position_pct": _as_float(ticket.get("position_pct")),
            "structure_stop": _as_float(ticket.get("structure_stop")),
            "hard_stop": _as_float(ticket.get("hard_stop")),
            "take_profit_1": _as_float(ticket.get("take_profit_1")),
            "exit_contract": ticket.get("exit_contract"),
        },
    }
    guard = _ptrade_live_submit_readiness_guard(order_payload)
    if guard:
        return guard
    try:
        order = PTRADE_BRIDGE.submit_order(order_payload)
    except Exception as exc:
        return {"ok": False, "message": f"PTrade bridge submit failed: {exc}", "ticket": ticket}
    record = _append_ptrade_audit(order, ticket, payload)
    return {"ok": True, "order": order, "record": record, "bridge_status": PTRADE_BRIDGE.status()}


def _submit_ptrade_sell_order(payload: dict[str, Any]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    ticket = _find_current_ticket(payload) or {}
    code = payload.get("code") or ticket.get("code") or ticket.get("code_raw")
    code6 = _normalize_code6(code)
    if not code6:
        return {"ok": False, "message": "A 6 digit stock code is required for PTrade sell order."}
    price = _as_float(
        payload.get("price")
        or payload.get("execution_price")
        or payload.get("current_price")
        or payload.get("reference_close")
        or payload.get("entry_price")
        or ticket.get("reference_close")
        or ticket.get("entry_price"),
        None,
    )
    quantity = int(float(payload.get("quantity") or payload.get("shares") or payload.get("available_shares") or 0))
    if quantity <= 0:
        quantity = _default_paper_quantity(price, ticket.get("position_pct") or payload.get("position_pct"), payload.get("base_capital"))
    if quantity <= 0:
        return {"ok": False, "message": "Positive sell quantity is required.", "ticket": ticket or None}
    if price is not None and price <= 0:
        price = None

    entry_date = ticket.get("entry_date") or payload.get("entry_date") or payload.get("signal_date")
    if _active_g3_ptrade_order_exists(code6, entry_date, "SELL"):
        return {"ok": False, "message": "Active G3 PTrade SELL order already exists for this position.", "ticket": ticket or None}

    order_payload = {
        "source": "g3_state_alpha_page",
        "strategy": LATEST_G3_PROFILE,
        "signal_date": entry_date,
        "entry_date": entry_date,
        "ticket_key": ticket.get("ticket_key") or payload.get("ticket_key"),
        "route": ticket.get("route") or payload.get("route"),
        "code": code6,
        "name": payload.get("name") or ticket.get("name") or ticket.get("stock_name"),
        "side": "SELL",
        "quantity": quantity,
        "price": price,
        "price_type": str(payload.get("price_type") or ("limit" if price else "market")),
        "dry_run": _ptrade_bool(payload.get("dry_run"), True),
        "require_approval": _ptrade_bool(payload.get("require_approval"), True),
        "approved": _ptrade_bool(payload.get("approved"), False),
        "reason": str(payload.get("reason") or payload.get("exit_reason") or "G3 State Alpha manual exit check"),
        "risk": {
            "route": ticket.get("route") or payload.get("route"),
            "exit_reason": payload.get("exit_reason") or payload.get("management_action"),
            "management_action": payload.get("management_action"),
            "structure_stop": _as_float(ticket.get("structure_stop") or payload.get("structure_stop")),
            "hard_stop": _as_float(ticket.get("hard_stop") or payload.get("hard_stop")),
            "take_profit_1": _as_float(ticket.get("take_profit_1") or payload.get("take_profit_1")),
            "exit_contract": ticket.get("exit_contract") or payload.get("exit_contract"),
        },
    }
    guard = _ptrade_live_submit_readiness_guard(order_payload)
    if guard:
        return guard
    try:
        order = PTRADE_BRIDGE.submit_order(order_payload)
    except Exception as exc:
        return {"ok": False, "message": f"PTrade bridge submit failed: {exc}", "ticket": ticket or None}
    record = _append_ptrade_audit(order, ticket or payload, payload)
    return {"ok": True, "order": order, "record": record, "bridge_status": PTRADE_BRIDGE.status()}


def _load_observation_snapshots() -> list[dict[str, Any]]:
    data = _read_json(OBSERVATION_SNAPSHOTS_PATH)
    rows = data.get("snapshots") if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def _save_observation_snapshots(rows: list[dict[str, Any]]) -> None:
    rows = sorted(rows, key=lambda item: str(item.get("observation_date") or item.get("created_at") or ""))
    _write_json(OBSERVATION_SNAPSHOTS_PATH, {"snapshots": rows[-500:]})


def _accepted_observation_dates(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({
        str(item.get("observation_date") or "")[:10]
        for item in rows
        if isinstance(item, dict) and item.get("accepted") and str(item.get("observation_date") or "").strip()
    })


def _observation_freshness_audit(
    workflow: dict[str, Any],
    observation_rows: list[dict[str, Any]],
    accepted_dates: list[str],
) -> dict[str, Any]:
    latest_accepted = accepted_dates[-1] if accepted_dates else None
    workflow_entry = str((workflow or {}).get("entry_date") or "")[:10] or None
    latest_row = next(
        (
            item for item in sorted(
                [row for row in observation_rows if isinstance(row, dict)],
                key=lambda row: str(row.get("created_at") or row.get("observation_date") or ""),
                reverse=True,
            )
            if item.get("accepted")
        ),
        None,
    )
    blockers: list[str] = []
    if not latest_accepted:
        blockers.append("no_accepted_observation")
    if workflow_entry and latest_accepted and latest_accepted != workflow_entry:
        blockers.append("latest_observation_not_current_workflow")
    if latest_row and latest_row.get("contract") not in {None, "", LATEST_G3_PROFILE}:
        blockers.append("latest_observation_contract_mismatch")

    return {
        "ok": not blockers,
        "available": True,
        "blockers": blockers,
        "checks": {
            "latest_accepted_observation_date": latest_accepted,
            "workflow_entry_date": workflow_entry,
            "latest_created_at": latest_row.get("created_at") if isinstance(latest_row, dict) else None,
            "latest_contract": latest_row.get("contract") if isinstance(latest_row, dict) else None,
            "accepted_observation_days": len(accepted_dates),
        },
        "comment": "latest accepted observation matches current G3 workflow" if not blockers else "accepted observations are stale or unavailable",
    }


def _observation_quality_audit(observation_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [
        row for row in observation_rows
        if isinstance(row, dict) and str(row.get("contract") or LATEST_G3_PROFILE) == LATEST_G3_PROFILE
    ]
    rows = sorted(rows, key=lambda row: str(row.get("observation_date") or row.get("created_at") or ""))
    accepted = [row for row in rows if bool(row.get("accepted"))]
    blocked = [row for row in rows if not bool(row.get("accepted"))]
    critical = [row for row in rows if str(row.get("severity") or "").lower() == "critical"]
    operator_actions = [row for row in rows if bool(row.get("operator_action_required"))]
    repaired = [row for row in accepted if bool(row.get("minute30_repair_triggered")) or str(row.get("failure_category") or "") == "auto_repaired_accepted"]
    latest = rows[-1] if rows else None
    consecutive_blocked = 0
    for row in reversed(rows):
        if bool(row.get("accepted")):
            break
        consecutive_blocked += 1

    total = len(rows)
    accepted_count = len(accepted)
    accepted_rate = accepted_count / total if total else 0.0
    repair_rate = len(repaired) / accepted_count if accepted_count else 0.0
    blockers: list[str] = []
    if not rows:
        blockers.append("no_observation_quality_sample")
    if latest and not bool(latest.get("accepted")):
        blockers.append("latest_observation_blocked")
    if critical:
        blockers.append("critical_observation_present")
    if operator_actions:
        blockers.append("operator_action_required_present")
    if total >= 5 and accepted_rate < 0.8:
        blockers.append("observation_acceptance_rate_below_80pct")
    if consecutive_blocked >= 2:
        blockers.append("consecutive_blocked_observations")
    if accepted_count >= 5 and repair_rate > 0.5:
        blockers.append("observation_auto_repair_rate_too_high")

    return {
        "ok": not blockers,
        "available": True,
        "blockers": blockers,
        "checks": {
            "total_observation_count": total,
            "accepted_count": accepted_count,
            "blocked_count": len(blocked),
            "accepted_rate": accepted_rate,
            "critical_count": len(critical),
            "operator_action_required_count": len(operator_actions),
            "auto_repaired_accepted_count": len(repaired),
            "auto_repair_rate": repair_rate,
            "consecutive_blocked_count": consecutive_blocked,
            "latest_observation_date": latest.get("observation_date") if isinstance(latest, dict) else None,
            "latest_status": latest.get("status") if isinstance(latest, dict) else None,
            "latest_failure_category": latest.get("failure_category") if isinstance(latest, dict) else None,
        },
        "comment": "G3 observation quality is clean" if not blockers else "G3 observation quality has unresolved blockers",
    }


def _advance_trading_days(start_date: str | None, trading_days: int) -> tuple[str | None, str]:
    if not start_date:
        return None, "missing_start_date"
    try:
        current = datetime.strptime(start_date[:10], "%Y-%m-%d")
    except Exception:
        return None, "invalid_start_date"
    if trading_days <= 0:
        return current.strftime("%Y-%m-%d"), "already_satisfied"
    try:
        from scheduler.trading_calendar import TradingCalendar

        for _ in range(trading_days):
            current = TradingCalendar.get_next_trading_day(current)
        return current.strftime("%Y-%m-%d"), "trade_calendar"
    except Exception as exc:
        logger.warning("G3 retirement window trade calendar fallback: %s", exc)
        advanced = 0
        current = current + timedelta(days=1)
        while advanced < trading_days:
            if current.weekday() < 5:
                advanced += 1
                if advanced >= trading_days:
                    break
            current += timedelta(days=1)
        return current.strftime("%Y-%m-%d"), "weekday_fallback"


def _retirement_window_audit(
    accepted_dates: list[str],
    required_days: int = 30,
) -> dict[str, Any]:
    accepted_count = len(accepted_dates)
    remaining = max(0, required_days - accepted_count)
    latest = accepted_dates[-1] if accepted_dates else None
    first = accepted_dates[0] if accepted_dates else None
    earliest_review_date, source = _advance_trading_days(latest, remaining)
    progress = accepted_count / required_days if required_days else 1.0
    blockers: list[str] = []
    if accepted_count <= 0:
        blockers.append("no_accepted_observation_yet")
    if earliest_review_date is None:
        blockers.append("earliest_review_date_unavailable")

    return {
        "ok": earliest_review_date is not None,
        "available": True,
        "blockers": blockers,
        "checks": {
            "required_observation_days": required_days,
            "accepted_observation_days": accepted_count,
            "remaining_observation_days": remaining,
            "progress": progress,
            "first_accepted_observation_date": first,
            "latest_accepted_observation_date": latest,
            "earliest_g2_retirement_review_date": earliest_review_date,
            "calendar_source": source,
        },
        "comment": "G3 retirement review window is scheduled" if earliest_review_date else "G3 retirement review window cannot be scheduled yet",
    }


def _latest_pretrade_smoke() -> dict[str, Any]:
    return _read_json(PRETRADE_SMOKE_PATH)


def _run_pretrade_smoke(entry_date: str | None = None, refresh: bool = False) -> dict[str, Any]:
    cmd = [sys.executable, str(PRETRADE_SMOKE_SCRIPT)]
    if entry_date:
        cmd.extend(["--entry-date", entry_date])
    if refresh:
        cmd.append("--refresh")
    started = datetime.now()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        return {
            "ran": True,
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "duration_seconds": round((datetime.now() - started).total_seconds(), 1),
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
            "command": " ".join(cmd),
        }
    except Exception as exc:
        logger.exception("G3 pretrade smoke failed.")
        return {
            "ran": True,
            "ok": False,
            "error": str(exc),
            "duration_seconds": round((datetime.now() - started).total_seconds(), 1),
            "command": " ".join(cmd),
        }


def _smoke_has_minute30_blocker(smoke: dict[str, Any]) -> bool:
    blockers = smoke.get("blockers") if isinstance(smoke, dict) else []
    if not isinstance(blockers, list):
        return False
    minute_gates = {"clickhouse_minute30_table", "qualified_ticket_30m_confirmed", "qualified_ticket_30m_data"}
    for item in blockers:
        if not isinstance(item, dict):
            continue
        gate = str(item.get("gate") or "").strip()
        message = str(item.get("message") or "").lower()
        if gate in minute_gates or "30m" in message or "minute30" in message:
            return True
    return False


def _ticket_minute30_repair_targets(tickets: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, list[str]]:
    targets: dict[str, set[str]] = {}
    fallback_date = str((summary or {}).get("decision_date") or (summary or {}).get("entry_date") or "")[:10]
    for item in tickets:
        if not isinstance(item, dict) or not _truthy(item.get("qualified_shadow_buy")):
            continue
        code = str(item.get("code") or item.get("code_raw") or "").strip().upper()
        if not code:
            continue
        confirm_date = str(item.get("confirm_datetime") or "")[:10] or fallback_date
        if not confirm_date:
            continue
        targets.setdefault(confirm_date, set()).add(code)
    return {date: sorted(codes) for date, codes in targets.items() if date and codes}


def _run_minute30_repair_for_targets(targets: dict[str, list[str]]) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for target_date, codes in sorted((targets or {}).items()):
        if not target_date or not codes:
            continue
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "sync_intraday_minutes_fast.py"),
            "--target-date",
            target_date,
            "--periods",
            "30m",
            "--types",
            "stock,index",
            "--codes",
            ",".join(codes),
            "--batch-size",
            "100",
            "--min-complete-codes",
            str(max(1, len(set(codes)))),
        ]
        started = datetime.now()
        try:
            result = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            attempts.append(
                {
                    "name": "minute30_repair",
                    "ok": result.returncode == 0,
                    "target_date": target_date,
                    "codes": codes,
                    "returncode": result.returncode,
                    "duration_seconds": round((datetime.now() - started).total_seconds(), 1),
                    "stdout_tail": result.stdout[-4000:],
                    "stderr_tail": result.stderr[-4000:],
                    "command": " ".join(cmd),
                }
            )
        except Exception as exc:
            logger.exception("G3 minute30 auto repair failed.")
            attempts.append(
                {
                    "name": "minute30_repair",
                    "ok": False,
                    "target_date": target_date,
                    "codes": codes,
                    "duration_seconds": round((datetime.now() - started).total_seconds(), 1),
                    "error": str(exc),
                    "command": " ".join(cmd),
                }
            )
    return attempts


def _classify_observation_outcome(
    accepted: bool,
    workflow_ok: bool,
    smoke_ok: bool,
    m30_ok: bool,
    paper_ok: bool,
    repair_attempts: list[dict[str, Any]],
    missing_paper: list[dict[str, Any]],
    smoke: dict[str, Any],
) -> dict[str, Any]:
    repair_triggered = bool(repair_attempts)
    repair_failed = repair_triggered and not all(bool(item.get("ok")) for item in repair_attempts)
    if accepted:
        return {
            "failure_category": "accepted",
            "severity": "info",
            "action_required": "none",
            "operator_action_required": False,
            "next_action": "Continue automatic observation accumulation.",
        } if not repair_triggered else {
            "failure_category": "auto_repaired_accepted",
            "severity": "info",
            "action_required": "none",
            "operator_action_required": False,
            "next_action": "Minute30 data was repaired and the observation passed; keep the repair evidence.",
        }
    if repair_failed:
        return {
            "failure_category": "data_repair_failed",
            "severity": "critical",
            "action_required": "repair_data_source",
            "operator_action_required": True,
            "next_action": "Check TdxQuant/Gateway availability and rerun the 30m repair before judging the strategy.",
        }
    if not m30_ok or _smoke_has_minute30_blocker(smoke):
        return {
            "failure_category": "data_gap_30m",
            "severity": "warning",
            "action_required": "auto_repair_or_manual_data_repair",
            "operator_action_required": True,
            "next_action": "Let the scheduler run 30m auto-repair; if it still fails, inspect minute K-line coverage.",
        }
    if not paper_ok or missing_paper:
        return {
            "failure_category": "paper_execution_required",
            "severity": "action",
            "action_required": "record_paper_execution",
            "operator_action_required": True,
            "next_action": "Record the qualified ticket in the G3 paper execution ledger; do not auto-create it.",
        }
    if not workflow_ok:
        return {
            "failure_category": "workflow_blocked",
            "severity": "critical",
            "action_required": "fix_runtime_workflow",
            "operator_action_required": True,
            "next_action": "Inspect G3 workflow blockers and runtime artifacts.",
        }
    if not smoke_ok:
        return {
            "failure_category": "pretrade_smoke_blocked",
            "severity": "critical",
            "action_required": "inspect_smoke_blockers",
            "operator_action_required": True,
            "next_action": "Open latest_pretrade_smoke.json and fix the blocking gate before accepting the observation.",
        }
    return {
        "failure_category": "unknown_blocked",
        "severity": "warning",
        "action_required": "manual_review",
        "operator_action_required": True,
        "next_action": "Manual review required; blocker category could not be inferred.",
    }


def _paper_execution_matches_ticket(ticket: dict[str, Any], execution: dict[str, Any]) -> bool:
    ticket_key = str(ticket.get("ticket_key") or ticket.get("candidate_key") or ticket.get("trade_key") or "").strip()
    execution_key = str(execution.get("ticket_key") or "").strip()
    if ticket_key and execution_key and ticket_key == execution_key:
        return True
    code = str(ticket.get("code") or ticket.get("code_raw") or "").strip().upper()
    execution_code = str(execution.get("code") or "").strip().upper()
    entry_date = str(ticket.get("entry_date") or "")[:10]
    execution_date = str(execution.get("entry_date") or "")[:10]
    return bool(code and entry_date and code == execution_code and entry_date == execution_date)


def _current_paper_execution_audit() -> dict[str, Any]:
    runtime = _load_current_runtime(limit=500)
    tickets = runtime.get("tickets") if isinstance(runtime, dict) else []
    tickets = [item for item in tickets if isinstance(item, dict)]
    qualified_tickets = [item for item in tickets if _truthy(item.get("qualified_shadow_buy"))]
    paper_rows = _load_paper_executions()
    missing: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []

    for ticket in qualified_tickets:
        match = next((row for row in paper_rows if _paper_execution_matches_ticket(ticket, row)), None)
        info = {
            "ticket_key": ticket.get("ticket_key") or ticket.get("candidate_key") or ticket.get("trade_key"),
            "entry_date": ticket.get("entry_date"),
            "code": ticket.get("code") or ticket.get("code_raw"),
            "name": ticket.get("name") or ticket.get("stock_name"),
            "route": ticket.get("route"),
        }
        if match:
            matched.append({**info, "execution_id": match.get("execution_id"), "created_at": match.get("created_at")})
        else:
            missing.append(info)

    wrong_contract = [
        row for row in paper_rows
        if str(row.get("strategy_id") or "") == STRATEGY_ID
        and str(row.get("contract") or "") not in {"", LATEST_G3_PROFILE}
    ]
    guardrail_open = [
        row for row in paper_rows
        if _truthy(row.get("formal_buy_signal")) or _truthy(row.get("auto_order_allowed")) or _truthy(row.get("order_path_enabled"))
    ]
    blockers: list[str] = []
    if missing:
        blockers.append("missing_paper_execution")
    if wrong_contract:
        blockers.append("paper_contract_mismatch")
    if guardrail_open:
        blockers.append("paper_guardrail_open")

    return {
        "ok": not blockers,
        "available": True,
        "blockers": blockers,
        "checks": {
            "qualified_ticket_count": len(qualified_tickets),
            "matched_paper_count": len(matched),
            "missing_paper_count": len(missing),
            "paper_execution_count": len(paper_rows),
            "wrong_contract_count": len(wrong_contract),
            "guardrail_open_count": len(guardrail_open),
        },
        "missing": missing[:20],
        "matched": matched[:20],
        "comment": "all current qualified G3 tickets are reproducible in the paper ledger" if not blockers else "current qualified G3 tickets are not fully reproducible in the paper ledger",
        "path_status": _path_status(PAPER_EXECUTIONS_PATH),
    }


def _observation_scheduler_audit() -> dict[str, Any]:
    state = configure_observation_scheduler()
    blockers: list[str] = []
    if not bool(state.get("enabled")):
        blockers.append("observation_scheduler_disabled")
    if not bool(state.get("scheduler_wired")):
        blockers.append("observation_scheduler_not_wired")
    if not bool(state.get("scheduler_enabled")):
        blockers.append("observation_job_not_scheduled")
    if not state.get("next_run_time"):
        blockers.append("missing_next_run_time")
    if not bool(state.get("run_smoke")):
        blockers.append("run_smoke_disabled")
    if not bool(state.get("refresh_smoke")):
        blockers.append("refresh_smoke_disabled")
    if not bool(state.get("auto_repair_minute30")):
        blockers.append("minute30_auto_repair_disabled")
    if state.get("last_error"):
        blockers.append("last_scheduler_error_present")

    return {
        "ok": not blockers,
        "available": True,
        "blockers": blockers,
        "checks": {
            "enabled": bool(state.get("enabled")),
            "scheduler_enabled": bool(state.get("scheduler_enabled")),
            "scheduler_wired": bool(state.get("scheduler_wired")),
            "next_run_time": state.get("next_run_time"),
            "last_run_at": state.get("last_run_at"),
            "last_success_at": state.get("last_success_at"),
            "last_error": state.get("last_error"),
            "run_smoke": bool(state.get("run_smoke")),
            "refresh_smoke": bool(state.get("refresh_smoke")),
            "auto_repair_minute30": bool(state.get("auto_repair_minute30")),
            "hour": state.get("hour"),
            "minute": state.get("minute"),
        },
        "comment": "G3 automatic observation scheduler is active" if not blockers else "G3 automatic observation scheduler is not ready",
        "path_status": _path_status(OBSERVATION_STATE_PATH),
    }


def _shadow_monitor_audit() -> dict[str, Any]:
    state = configure_shadow_monitor_scheduler()
    notification = _notification_config_check(state.get("recipient_email") or None)
    email_enabled = bool(state.get("email_enabled"))
    blockers: list[str] = []
    if not bool(state.get("enabled")):
        blockers.append("shadow_monitor_disabled")
    if not bool(state.get("scheduler_wired")):
        blockers.append("shadow_monitor_not_wired")
    if not bool(state.get("scheduler_enabled")):
        blockers.append("shadow_monitor_job_not_scheduled")
    if not state.get("next_run_time"):
        blockers.append("missing_next_run_time")
    if int(state.get("interval_seconds") or 0) < 120:
        blockers.append("monitor_interval_too_short")
    if not bool(state.get("run_current_refresh")):
        blockers.append("current_refresh_disabled")
    if not email_enabled:
        blockers.append("email_alert_disabled")
    if email_enabled and not bool(notification.get("ok")):
        blockers.append("email_config_unavailable")
    if state.get("last_error"):
        blockers.append("last_monitor_error_present")
    if state.get("last_email_error"):
        blockers.append("last_email_error_present")

    return {
        "ok": not blockers,
        "available": True,
        "blockers": blockers,
        "checks": {
            "enabled": bool(state.get("enabled")),
            "scheduler_enabled": bool(state.get("scheduler_enabled")),
            "scheduler_wired": bool(state.get("scheduler_wired")),
            "next_run_time": state.get("next_run_time"),
            "interval_seconds": int(state.get("interval_seconds") or 0),
            "trading_hours_only": bool(state.get("trading_hours_only")),
            "run_current_refresh": bool(state.get("run_current_refresh")),
            "email_enabled": email_enabled,
            "email_config_ok": bool(notification.get("ok")),
            "email_message": notification.get("message"),
            "heartbeat_enabled": bool(state.get("heartbeat_enabled")),
            "heartbeat_minutes": int(state.get("heartbeat_minutes") or 0),
            "last_run_at": state.get("last_run_at"),
            "last_success_at": state.get("last_success_at"),
            "last_error": state.get("last_error"),
            "last_email_sent_at": state.get("last_email_sent_at"),
            "last_email_error": state.get("last_email_error"),
            "last_refresh_task_id": state.get("last_refresh_task_id"),
        },
        "comment": "G3 shadow monitor, refresh and email alert path is active" if not blockers else "G3 shadow monitor is not ready",
        "path_status": _path_status(MONITOR_STATE_PATH),
    }


def _strategy_trade_permission_audit(runtime: dict[str, Any]) -> dict[str, Any]:
    summary = runtime.get("summary") if isinstance(runtime, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    tickets = runtime.get("tickets") if isinstance(runtime, dict) else []
    tickets = [item for item in tickets if isinstance(item, dict)]
    diagnostics = runtime.get("route_diagnostics") if isinstance(runtime, dict) else []
    diagnostics = [item for item in diagnostics if isinstance(item, dict)]
    account_risk = summary.get("account_risk") if isinstance(summary.get("account_risk"), dict) else {}
    selected_route = str(summary.get("selected_route") or "").strip()
    selected_diag = next((item for item in diagnostics if str(item.get("route") or "").strip() == selected_route), None)
    qualified_tickets = [item for item in tickets if _truthy(item.get("qualified_shadow_buy"))]
    blocked_by_account_risk = [
        item for item in tickets
        if str(item.get("shadow_status") or "").strip() == "blocked_account_risk_pause_new_buy"
        or str(item.get("account_risk_action") or "").strip() == "pause_new_buy"
    ]

    risk_ok = bool(account_risk.get("account_risk_ok", True))
    risk_action = str(account_risk.get("account_risk_action") or "normal")
    position_scale = _as_float(account_risk.get("position_scale"), 1.0) or 0.0
    route_health_ok = True
    if selected_diag is not None and selected_route:
        route_health_ok = _truthy(selected_diag.get("route_health_ok"))

    blockers: list[str] = []
    if not risk_ok:
        blockers.append("account_risk_not_ok")
    if risk_action == "pause_new_buy":
        blockers.append("account_risk_pause_new_buy")
    if position_scale <= 0:
        blockers.append("position_scale_zero")
    if blocked_by_account_risk:
        blockers.append("tickets_blocked_by_account_risk")
    route_health_available = bool(selected_diag is not None) if selected_route else True

    decision = "pause_new_buy" if blockers else ("reduce_risk" if risk_action == "reduce_risk" or position_scale < 1.0 else "allow_shadow_buy")
    return {
        "ok": not blockers,
        "available": True,
        "decision": decision,
        "blockers": blockers,
        "checks": {
            "selected_route": selected_route,
            "qualified_ticket_count": len(qualified_tickets),
            "blocked_by_account_risk_count": len(blocked_by_account_risk),
            "account_risk_ok": risk_ok,
            "account_risk_action": risk_action,
            "account_risk_reason": account_risk.get("account_risk_reason"),
            "position_scale": position_scale,
            "current_drawdown": _as_float(account_risk.get("current_drawdown"), 0.0),
            "consecutive_realized_loss": _as_float(account_risk.get("consecutive_realized_loss"), 0.0),
            "recent_hard_stop_count": int(_as_float(account_risk.get("recent_hard_stop_count"), 0.0) or 0),
            "route_health_gate_policy": "observe_only",
            "route_health_available": route_health_available,
            "route_health_ok": route_health_ok,
            "route_health_count": int(_as_float((selected_diag or {}).get("route_health_count"), 0.0) or 0),
            "route_health_avg_ret": _as_float((selected_diag or {}).get("route_health_avg_ret"), None),
            "route_health_win_rate": _as_float((selected_diag or {}).get("route_health_win_rate"), None),
            "route_health_big_loss_rate": _as_float((selected_diag or {}).get("route_health_big_loss_rate"), None),
            "route_health_worst_ret": _as_float((selected_diag or {}).get("route_health_worst_ret"), None),
        },
        "comment": "G3 account risk allows shadow buys; route health is observation only" if not blockers else "G3 should pause new buys until account risk recovers",
    }


def _build_observation_snapshot(
    run_smoke: bool = False,
    refresh_smoke: bool = False,
    auto_repair_minute30: bool = False,
    source: str = "manual",
    entry_date: str | None = None,
) -> dict[str, Any]:
    smoke_run = _run_pretrade_smoke(entry_date=entry_date, refresh=refresh_smoke) if run_smoke else {"ran": False}
    workflow = _build_workflow_status()
    runtime = _load_current_runtime(limit=500)
    tickets = runtime.get("tickets") if isinstance(runtime, dict) else []
    tickets = [item for item in tickets if isinstance(item, dict)]
    paper_rows = _load_paper_executions()
    smoke = _latest_pretrade_smoke()
    repair_attempts: list[dict[str, Any]] = []
    repair_targets = _ticket_minute30_repair_targets(tickets, runtime.get("summary") or {})
    needs_minute30_repair = auto_repair_minute30 and repair_targets and _smoke_has_minute30_blocker(smoke)
    if needs_minute30_repair:
        repair_attempts = _run_minute30_repair_for_targets(repair_targets)
        smoke_run = {
            "initial": smoke_run,
            "minute30_auto_repair": repair_attempts,
            "after_repair": _run_pretrade_smoke(entry_date=entry_date, refresh=True) if run_smoke else {"ran": False},
        }
        workflow = _build_workflow_status()
        runtime = _load_current_runtime(limit=500)
        tickets = runtime.get("tickets") if isinstance(runtime, dict) else []
        tickets = [item for item in tickets if isinstance(item, dict)]
        paper_rows = _load_paper_executions()
        smoke = _latest_pretrade_smoke()

    observation_date = (
        str(entry_date or "").strip()
        or str((runtime.get("summary") or {}).get("entry_date") or "").strip()
        or datetime.now().strftime("%Y-%m-%d")
    )[:10]
    ticket_count = len(tickets)
    qualified_tickets = [item for item in tickets if _truthy(item.get("qualified_shadow_buy"))]
    m30_unconfirmed = [
        {
            "code": item.get("code") or item.get("code_raw"),
            "name": item.get("name") or item.get("stock_name"),
            "reason": item.get("m30_status") or item.get("shadow_status") or "missing_30m_confirmation",
        }
        for item in qualified_tickets
        if not _truthy(item.get("m30_confirmed"))
    ]
    missing_paper = [
        {
            "code": item.get("code") or item.get("code_raw"),
            "name": item.get("name") or item.get("stock_name"),
            "entry_date": item.get("entry_date"),
            "ticket_key": item.get("ticket_key") or item.get("candidate_key") or item.get("trade_key"),
        }
        for item in qualified_tickets
        if not any(_paper_execution_matches_ticket(item, row) for row in paper_rows)
    ]

    smoke_blockers = smoke.get("blockers") if isinstance(smoke, dict) else []
    smoke_verdict = str((smoke or {}).get("verdict") or "").strip()
    smoke_ok = bool(smoke) and smoke_verdict in {"shadow_ready", "shadow_ready_with_warnings", "no_trade"} and not smoke_blockers
    workflow_ok = bool(workflow.get("ok"))
    m30_ok = not m30_unconfirmed
    paper_ok = not missing_paper
    accepted = workflow_ok and smoke_ok and m30_ok and paper_ok

    blockers: list[dict[str, Any]] = []
    if not workflow_ok:
        blockers.append({"gate": "workflow", "message": workflow.get("message") or "workflow not ok"})
    if not smoke_ok:
        blockers.append({"gate": "pretrade_smoke", "message": smoke_verdict or "missing smoke result", "detail": smoke_blockers})
    if not m30_ok:
        blockers.append({"gate": "m30_confirmation", "message": "qualified tickets have no 30m confirmation", "detail": m30_unconfirmed})
    if not paper_ok:
        blockers.append({"gate": "paper_execution", "message": "qualified tickets are not reproducible in paper ledger", "detail": missing_paper})
    classification = _classify_observation_outcome(
        accepted=accepted,
        workflow_ok=workflow_ok,
        smoke_ok=smoke_ok,
        m30_ok=m30_ok,
        paper_ok=paper_ok,
        repair_attempts=repair_attempts,
        missing_paper=missing_paper,
        smoke=smoke,
    )

    record = {
        "observation_id": f"g3obs_{observation_date.replace('-', '')}_{uuid4().hex[:8]}",
        "created_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "observation_date": observation_date,
        "source": source,
        "accepted": accepted,
        "status": "accepted" if accepted else "blocked",
        "contract": LATEST_G3_PROFILE,
        "strategy_id": STRATEGY_ID,
        "workflow_ok": workflow_ok,
        "workflow_message": workflow.get("message"),
        "smoke_ok": smoke_ok,
        "smoke_verdict": smoke_verdict or None,
        "smoke_trade_action": (smoke or {}).get("trade_action"),
        "smoke_qualified_ticket_count": (smoke or {}).get("qualified_ticket_count"),
        "ticket_count": ticket_count,
        "qualified_ticket_count": len(qualified_tickets),
        "m30_ok": m30_ok,
        "paper_ok": paper_ok,
        "missing_paper_count": len(missing_paper),
        "blockers": blockers,
        **classification,
        "auto_repair_minute30": bool(auto_repair_minute30),
        "minute30_repair_triggered": bool(repair_attempts),
        "minute30_repair_targets": repair_targets,
        "repair_attempts": repair_attempts,
        "smoke_run": smoke_run,
        "artifacts": {
            "observation_snapshots": _path_status(OBSERVATION_SNAPSHOTS_PATH),
            "pretrade_smoke": _path_status(PRETRADE_SMOKE_PATH),
            "pretrade_smoke_gates": _path_status(PRETRADE_SMOKE_GATES_PATH),
            "paper_executions": _path_status(PAPER_EXECUTIONS_PATH),
        },
    }
    rows = [item for item in _load_observation_snapshots() if str(item.get("observation_date") or "")[:10] != observation_date]
    rows.append(record)
    _save_observation_snapshots(rows)
    saved = _load_observation_snapshots()
    for item in saved:
        if item.get("observation_id") == record.get("observation_id"):
            item["artifacts"] = {
                "observation_snapshots": _path_status(OBSERVATION_SNAPSHOTS_PATH),
                "pretrade_smoke": _path_status(PRETRADE_SMOKE_PATH),
                "pretrade_smoke_gates": _path_status(PRETRADE_SMOKE_GATES_PATH),
                "paper_executions": _path_status(PAPER_EXECUTIONS_PATH),
            }
            record = item
            _save_observation_snapshots(saved)
            break
    return {
        "ok": accepted,
        "mode": "g3_state_alpha_observation_snapshot",
        "snapshot": record,
        "accepted_observation_days": len(_accepted_observation_dates(saved)),
        "accepted_observation_dates": _accepted_observation_dates(saved)[-30:],
        "blockers": blockers,
        **classification,
    }


def _ensure_monitor_scheduler() -> BackgroundScheduler:
    global _monitor_scheduler
    if _monitor_scheduler is None:
        _monitor_scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        _monitor_scheduler.start()
    elif not _monitor_scheduler.running:
        _monitor_scheduler.start()
    return _monitor_scheduler


def _in_trading_window(now: datetime) -> bool:
    try:
        from scheduler.trading_calendar import TradingCalendar

        if not TradingCalendar.is_trading_day(now):
            return False
    except Exception:
        pass
    hhmm = now.strftime("%H:%M")
    return "09:25" <= hhmm <= "15:10"


def _monitor_job_wrapper() -> None:
    state = _load_monitor_state()
    if not state.get("enabled"):
        return
    now = datetime.now()
    if state.get("trading_hours_only") and not _in_trading_window(now):
        state["last_run_at"] = now.isoformat(sep=" ", timespec="seconds")
        state["last_result"] = {
            "skipped": True,
            "reason": "非交易日或非交易时段",
            "checked_at": state["last_run_at"],
        }
        _save_monitor_state(state)
        return
    if _running_refresh_task():
        return
    task_id = f"g3_state_alpha_refresh_{now.strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_refresh_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "source": "scheduler",
            "created_at": now.isoformat(sep=" ", timespec="seconds"),
            "message": "G3 State Alpha scheduled refresh queued",
        },
    )
    thread = threading.Thread(
        target=_run_refresh_task,
        args=(task_id, None, "scheduler", False),
        name=f"g3-state-alpha-scheduled-refresh-{task_id}",
        daemon=True,
    )
    thread.start()


def _observation_job_wrapper() -> None:
    state = _load_observation_scheduler_state()
    if not state.get("enabled"):
        return
    now = datetime.now()
    if state.get("trading_days_only"):
        try:
            from scheduler.trading_calendar import TradingCalendar

            if not TradingCalendar.is_trading_day(now):
                state["last_run_at"] = now.isoformat(sep=" ", timespec="seconds")
                state["last_result"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "not_trading_day",
                    "checked_at": state["last_run_at"],
                }
                _save_observation_scheduler_state(state)
                return
        except Exception:
            logger.exception("Failed to check trading day for G3 observation scheduler.")
    started_at = datetime.now()
    state["last_run_at"] = started_at.isoformat(sep=" ", timespec="seconds")
    _save_observation_scheduler_state(state)
    try:
        result = _build_observation_snapshot(
            run_smoke=bool(state.get("run_smoke", True)),
            refresh_smoke=bool(state.get("refresh_smoke", True)),
            auto_repair_minute30=bool(state.get("auto_repair_minute30", True)),
            source="scheduler",
        )
        finished_at = datetime.now()
        state = _load_observation_scheduler_state()
        state["last_run_at"] = started_at.isoformat(sep=" ", timespec="seconds")
        state["last_result"] = result
        state["last_error"] = None if result.get("ok") else result.get("failure_category") or (result.get("blockers") or [{}])[0].get("message")
        if result.get("ok"):
            state["last_success_at"] = finished_at.isoformat(sep=" ", timespec="seconds")
        _save_observation_scheduler_state(state)
        _append_monitor_event(
            {
                "type": "observation_snapshot",
                "status": "accepted" if result.get("ok") else "blocked",
                "source": "scheduler",
                "accepted_observation_days": result.get("accepted_observation_days"),
                "failure_category": result.get("failure_category"),
                "severity": result.get("severity"),
                "action_required": result.get("action_required"),
                "operator_action_required": result.get("operator_action_required"),
                "message": "G3 observation snapshot accepted" if result.get("ok") else "G3 observation snapshot blocked",
                "error": state.get("last_error"),
            }
        )
    except Exception as exc:
        logger.exception("G3 observation snapshot scheduler failed.")
        state = _load_observation_scheduler_state()
        state["last_error"] = str(exc)
        state["last_result"] = {"ok": False, "error": str(exc)}
        _save_observation_scheduler_state(state)
        _append_monitor_event(
            {
                "type": "observation_snapshot",
                "status": "error",
                "source": "scheduler",
                "message": "G3 observation snapshot scheduler failed",
                "error": str(exc),
            }
        )


def _broker_sync_job_wrapper() -> None:
    state = _load_broker_sync_state()
    if not state.get("enabled"):
        return
    now = datetime.now()
    if state.get("trading_days_only"):
        try:
            from scheduler.trading_calendar import TradingCalendar

            if not TradingCalendar.is_trading_day(now):
                state["last_run_at"] = now.isoformat(sep=" ", timespec="seconds")
                state["last_result"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "not_trading_day",
                    "checked_at": state["last_run_at"],
                }
                _save_broker_sync_state(state)
                return
        except Exception:
            logger.exception("Failed to check trading day for G3 broker sync scheduler.")
    if state.get("trading_hours_only") and not _in_trading_window(now):
        state["last_run_at"] = now.isoformat(sep=" ", timespec="seconds")
        state["last_result"] = {
            "ok": True,
            "skipped": True,
            "reason": "not_trading_window",
            "checked_at": state["last_run_at"],
        }
        _save_broker_sync_state(state)
        return
    try:
        result = _run_broker_sync_once(source="scheduler")
        _append_monitor_event(
            {
                "type": "broker_sync",
                "status": "accepted" if result.get("ok") else "blocked",
                "source": "scheduler",
                "message": "G3 broker account sync completed" if result.get("ok") else "G3 broker account sync blocked",
                "error": (_load_broker_sync_state().get("last_error") or None),
            }
        )
    except Exception as exc:
        logger.exception("G3 broker account sync scheduler failed.")
        state = _load_broker_sync_state()
        state["last_run_at"] = now.isoformat(sep=" ", timespec="seconds")
        state["last_error"] = str(exc)
        state["last_result"] = {"ok": False, "error": str(exc)}
        _save_broker_sync_state(state)
        _append_monitor_event(
            {
                "type": "broker_sync",
                "status": "error",
                "source": "scheduler",
                "message": "G3 broker account sync scheduler failed",
                "error": str(exc),
            }
        )


def configure_shadow_monitor_scheduler() -> dict[str, Any]:
    scheduler = _ensure_monitor_scheduler()
    state = _load_monitor_state()
    try:
        if scheduler.get_job(_monitor_job_id):
            scheduler.remove_job(_monitor_job_id)
    except Exception:
        logger.exception("Failed to remove existing G3 monitor job")
    if state.get("enabled"):
        scheduler.add_job(
            _monitor_job_wrapper,
            "interval",
            seconds=max(120, int(state.get("interval_seconds") or 120)),
            id=_monitor_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    job = scheduler.get_job(_monitor_job_id)
    return {
        **state,
        "scheduler_enabled": bool(job),
        "scheduler_wired": True,
        "job_id": _monitor_job_id,
        "next_run_time": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


def configure_observation_scheduler() -> dict[str, Any]:
    scheduler = _ensure_monitor_scheduler()
    state = _load_observation_scheduler_state()
    try:
        if scheduler.get_job(_observation_job_id):
            scheduler.remove_job(_observation_job_id)
    except Exception:
        logger.exception("Failed to remove existing G3 observation job")
    if state.get("enabled"):
        scheduler.add_job(
            _observation_job_wrapper,
            "cron",
            day_of_week="mon-fri",
            hour=int(state.get("hour") or 15),
            minute=int(state.get("minute") or 40),
            id=_observation_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    job = scheduler.get_job(_observation_job_id)
    return {
        **state,
        "scheduler_enabled": bool(job),
        "scheduler_wired": True,
        "job_id": _observation_job_id,
        "next_run_time": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


def configure_broker_sync_scheduler() -> dict[str, Any]:
    scheduler = _ensure_monitor_scheduler()
    state = _load_broker_sync_state()
    try:
        if scheduler.get_job(_broker_sync_job_id):
            scheduler.remove_job(_broker_sync_job_id)
    except Exception:
        logger.exception("Failed to remove existing G3 broker sync job")
    if state.get("enabled"):
        scheduler.add_job(
            _broker_sync_job_wrapper,
            "cron",
            day_of_week="mon-fri",
            hour=int(state.get("hour") or 17),
            minute=int(state.get("minute") if state.get("minute") is not None else 30),
            id=_broker_sync_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    job = scheduler.get_job(_broker_sync_job_id)
    return {
        **state,
        "scheduler_enabled": bool(job),
        "scheduler_wired": True,
        "job_id": _broker_sync_job_id,
        "next_run_time": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


def init_gen3_state_alpha_monitor_scheduler_from_config() -> dict[str, Any]:
    return {
        "shadow_monitor": configure_shadow_monitor_scheduler(),
        "observation_scheduler": configure_observation_scheduler(),
        "broker_sync": configure_broker_sync_scheduler(),
    }


def _historical_window_mask(df: pd.DataFrame, window: str | None) -> pd.Series:
    if not window or window == "all" or "entry_date" not in df.columns:
        return pd.Series([True] * len(df), index=df.index)
    entry_dates = pd.to_datetime(df["entry_date"], errors="coerce")
    if window == "pre_2024_09":
        return entry_dates < pd.Timestamp("2024-09-01")
    if window == "post_2024_09":
        return entry_dates >= pd.Timestamp("2024-09-01")
    if window == "weak_2022":
        return (entry_dates >= pd.Timestamp("2022-01-01")) & (entry_dates < pd.Timestamp("2023-01-01"))
    if window == "valid_2024":
        return (entry_dates >= pd.Timestamp("2024-01-01")) & (entry_dates < pd.Timestamp("2025-01-01"))
    if window == "blind_2026ytd":
        return entry_dates >= pd.Timestamp("2026-01-01")
    return pd.Series([True] * len(df), index=df.index)


def _trade_metrics(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {
            "trade_count": 0,
            "closed_trade_count": 0,
            "open_shadow_count": 0,
            "win_rate": None,
            "avg_trade_return": None,
            "median_trade_return": None,
            "worst_trade": None,
            "best_trade": None,
            "sum_realized_pnl": 0.0,
            "winning_trades": 0,
            "losing_trades": 0,
        }
    ret = pd.to_numeric(df.get("net_ret"), errors="coerce")
    pnl = pd.to_numeric(df.get("realized_pnl"), errors="coerce")
    ret_valid = ret.dropna()
    status = df.get("trade_status", pd.Series("", index=df.index)).fillna("").astype(str)
    open_shadow = status.eq("open_shadow")
    return {
        "trade_count": int(len(df)),
        "closed_trade_count": int(len(ret_valid)),
        "open_shadow_count": int(open_shadow.sum()),
        "win_rate": float((ret_valid > 0).mean()) if len(ret_valid) else None,
        "avg_trade_return": float(ret_valid.mean()) if len(ret_valid) else None,
        "median_trade_return": float(ret_valid.median()) if len(ret_valid) else None,
        "worst_trade": float(ret_valid.min()) if len(ret_valid) else None,
        "best_trade": float(ret_valid.max()) if len(ret_valid) else None,
        "sum_realized_pnl": float(pnl.sum()) if pnl.notna().any() else 0.0,
        "winning_trades": int((ret_valid > 0).sum()) if len(ret_valid) else 0,
        "losing_trades": int((ret_valid <= 0).sum()) if len(ret_valid) else 0,
    }


def _group_metrics(df: pd.DataFrame, group_col: str) -> list[dict[str, Any]]:
    if df.empty or group_col not in df.columns:
        return []
    rows: list[dict[str, Any]] = []
    for key, group in df.groupby(group_col, dropna=False):
        item = _trade_metrics(group)
        item[group_col] = key
        label_col = (
            "trade_strategy_label"
            if group_col == "trade_strategy" and "trade_strategy_label" in group.columns
            else "route_strategy_family_label"
            if group_col == "route_strategy_family" and "route_strategy_family_label" in group.columns
            else "route_strategy_label"
            if group_col == "route_strategy" and "route_strategy_label" in group.columns
            else "route_label"
        )
        if group_col in {"route", "route_strategy", "route_strategy_family", "trade_strategy"} and label_col in group.columns:
            labels = group[label_col].dropna().astype(str)
            item["route_label"] = labels.iloc[0] if len(labels) else key
        if group_col in {"route_strategy", "route_strategy_family", "trade_strategy"}:
            item["route"] = key
            parent_route_col = "route_parent" if "route_parent" in group.columns else "route"
            if parent_route_col in group.columns:
                routes = group[parent_route_col].dropna().astype(str)
                unique_routes = list(dict.fromkeys([x for x in routes if x]))
                item["parent_route"] = unique_routes[0] if len(unique_routes) == 1 else "mixed"
            parent_label_col = "route_parent_label" if "route_parent_label" in group.columns else "route_label"
            if parent_label_col in group.columns:
                parent_labels = group[parent_label_col].dropna().astype(str)
                unique_parent_labels = list(dict.fromkeys([x for x in parent_labels if x]))
                item["parent_route_label"] = " / ".join(unique_parent_labels[:4])
            if "route_strategy_label" in group.columns:
                strategy_labels = list(dict.fromkeys([x for x in group["route_strategy_label"].dropna().astype(str) if x]))
                item["strategy_labels"] = strategy_labels
                item["strategy_summary"] = "、".join(strategy_labels[:6])
        rows.append(item)
    return sorted(rows, key=lambda item: item.get("trade_count") or 0, reverse=True)


OLD_G3_STRATEGY_LABELS = {
    "strong_main": "强势突破",
    "strong_trend_breakout": "旧G3强趋势突破",
    "range_gap": "旧G3弱势/震荡修复",
    "weak_rebound_repair": "旧G3弱势反弹修复",
    "range_box_bottom": "旧G3箱体底部观察",
    "down_panic": "旧G3恐慌修复",
    "downtrend_panic_capitulation": "旧G3恐慌出清修复",
    "old_g3_route_v3": "旧G3兼容未细分",
}

G2_STRATEGY_LABELS = {
    "volume5_keep80_runup_sector_bonus": "G2量能续强+板块加分",
    "volume5_keep80_runup": "G2量能续强",
    "g2_gap_supplement": "G2补位未细分",
}

ROUTE_PARENT_LABELS = {
    "institutional_mainwave": "机构主升浪",
    "panic_repair": "恐慌修复",
    "old_g3_route_v3": "旧G3跨周期",
    "g2_gap_supplement": "G2空档补位",
}

ROUTE_STRATEGY_FALLBACK_LABELS = {
    "institutional_score120_core": "机构主升浪Score120核心",
    "institutional_mainwave": "机构主升浪",
    "panic_repair_range": "震荡恐慌修复",
    "panic_repair_downtrend": "下跌恐慌修复",
    "panic_repair": "恐慌修复",
}

ROUTE_STRATEGY_FAMILY_LABELS = {
    "repair_range_weak": "震荡/弱势修复",
    "panic_capitulation_repair": "恐慌出清修复",
    "mainwave_breakout_offense": "主升/突破进攻",
    "volume_runup_supplement": "量能续强补位",
    "other": "其他策略",
}

ROUTE_STRATEGY_TO_FAMILY = {
    "old_g3_range_gap": "repair_range_weak",
    "panic_repair_range": "repair_range_weak",
    "old_g3_old_g3_route_v3": "repair_range_weak",
    "old_g3_down_panic": "panic_capitulation_repair",
    "panic_repair_downtrend": "panic_capitulation_repair",
    "institutional_score120_core": "mainwave_breakout_offense",
    "institutional_institutional_mainwave": "mainwave_breakout_offense",
    "old_g3_strong_main": "mainwave_breakout_offense",
    "g2_volume5_keep80_runup_sector_bonus": "volume_runup_supplement",
    "g2_volume5_keep80_runup": "volume_runup_supplement",
}

TRADE_STRATEGY_LABELS = {
    "range_weak_repair": "震荡弱势修复",
    "panic_capitulation_repair": "恐慌出清修复",
    "institutional_score120_mainwave": "机构主升Score120",
    "old_g3_strong_breakout": "强势突破",
    "volume_runup_supplement": "量能续强补位",
    "other": "其他策略",
}

ROUTE_STRATEGY_TO_TRADE_STRATEGY = {
    "old_g3_range_gap": "range_weak_repair",
    "panic_repair_range": "range_weak_repair",
    "old_g3_old_g3_route_v3": "range_weak_repair",
    "old_g3_down_panic": "panic_capitulation_repair",
    "panic_repair_downtrend": "panic_capitulation_repair",
    "institutional_score120_core": "institutional_score120_mainwave",
    "institutional_institutional_mainwave": "institutional_score120_mainwave",
    "old_g3_strong_main": "old_g3_strong_breakout",
    "g2_volume5_keep80_runup_sector_bonus": "volume_runup_supplement",
    "g2_volume5_keep80_runup": "volume_runup_supplement",
}


def _first_text_series(df: pd.DataFrame, names: list[str], default: str = "") -> pd.Series:
    out = pd.Series(default, index=df.index, dtype="object")
    for name in names:
        if name not in df.columns:
            continue
        values = df[name].fillna("").astype(str).str.strip()
        out = out.where(out.astype(str).str.len() > 0, values)
    return out.fillna("").astype(str)


def _with_route_strategy_fields(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    route = _first_text_series(out, ["route", "mode"], "")
    mode = _first_text_series(out, ["mode"], "")
    route_label = _first_text_series(out, ["route_label", "mode_label"], "")
    old_mask = mode.eq("old_g3_route_v3") | route.isin({"old_g3_route_v3", "range_gap", "down_panic"})
    g2_mask = mode.eq("g2_gap_supplement") | route.eq("g2_gap_supplement")
    institutional_mask = mode.eq("institutional_mainwave") | route.isin({"institutional_mainwave", "score120_core"})
    panic_mask = mode.eq("panic_repair") | route.eq("panic_repair")

    strategy_key = route.copy()
    route_parent = route.copy()

    old_detail = _first_text_series(out, ["g3_chain", "chain", "route_source"], "")
    old_detail = old_detail.where(old_detail.isin(OLD_G3_STRATEGY_LABELS), route)
    strategy_key = strategy_key.where(~old_mask, "old_g3_" + old_detail.replace("", "old_g3_route_v3"))
    route_parent = route_parent.where(~old_mask, "old_g3_route_v3")

    g2_logic = _first_text_series(out, ["g2_v2_buy_logic", "signal_family", "source_family"], "")
    g2_logic = (
        g2_logic
        .str.replace(" + sector_score_bonus", "_sector_bonus", regex=False)
        .str.replace("volume5_keep80_runup_sector_bonus", "volume5_keep80_runup_sector_bonus", regex=False)
    )
    g2_logic = g2_logic.where(g2_logic.isin(G2_STRATEGY_LABELS), "g2_gap_supplement")
    strategy_key = strategy_key.where(~g2_mask, "g2_" + g2_logic)
    route_parent = route_parent.where(~g2_mask, "g2_gap_supplement")

    institutional_source = _first_text_series(out, ["route_source", "route", "mode"], "")
    institutional_source = institutional_source.where(institutional_source.isin({"score120_core"}), "institutional_mainwave")
    strategy_key = strategy_key.where(~institutional_mask, "institutional_" + institutional_source)
    route_parent = route_parent.where(~institutional_mask, "institutional_mainwave")

    market_style = _first_text_series(out, ["market_style"], "")
    panic_detail = pd.Series("panic_repair", index=out.index, dtype="object")
    panic_detail = panic_detail.where(~market_style.eq("standard_downtrend"), "panic_repair_downtrend")
    panic_detail = panic_detail.where(market_style.eq("standard_downtrend"), "panic_repair_range")
    strategy_key = strategy_key.where(~panic_mask, panic_detail)
    route_parent = route_parent.where(~panic_mask, "panic_repair")

    default_labels = {
        "institutional_mainwave": "机构主升浪",
        "score120_core": "机构主升浪",
        "strong_main": "机构主升浪",
        "panic_repair": "恐慌修复",
        "g2_gap_supplement": "G2空档补位",
    }
    strategy_label = route.map(default_labels).fillna(route_label.where(route_label.str.len() > 0, route))
    strategy_label = strategy_label.where(~old_mask, old_detail.map(OLD_G3_STRATEGY_LABELS).fillna("旧G3兼容未细分"))
    strategy_label = strategy_label.where(~g2_mask, g2_logic.map(G2_STRATEGY_LABELS).fillna("G2补位未细分"))
    strategy_label = strategy_label.where(~institutional_mask, strategy_key.map(ROUTE_STRATEGY_FALLBACK_LABELS).fillna("机构主升浪"))
    strategy_label = strategy_label.where(~panic_mask, strategy_key.map(ROUTE_STRATEGY_FALLBACK_LABELS).fillna("恐慌修复"))

    out["route_strategy"] = strategy_key
    out["route_strategy_label"] = strategy_label
    strategy_family = strategy_key.map(ROUTE_STRATEGY_TO_FAMILY).fillna("other")
    out["route_strategy_family"] = strategy_family
    out["route_strategy_family_label"] = strategy_family.map(ROUTE_STRATEGY_FAMILY_LABELS).fillna("其他策略")
    trade_strategy = strategy_key.map(ROUTE_STRATEGY_TO_TRADE_STRATEGY).fillna("other")
    out["trade_strategy"] = trade_strategy
    out["trade_strategy_label"] = trade_strategy.map(TRADE_STRATEGY_LABELS).fillna("其他策略")
    out["route_parent"] = route_parent
    out["route_parent_label"] = route_parent.map(ROUTE_PARENT_LABELS).fillna(route_parent)
    out["route_label"] = out["route_parent_label"]
    return out


def _latest_profile() -> dict[str, Any]:
    summary = _read_json(PROMOTION_SUMMARY_PATH)
    summaries = summary.get("contract_summaries") if isinstance(summary, dict) else None
    if isinstance(summaries, list):
        for item in summaries:
            if isinstance(item, dict) and item.get("contract") == LATEST_G3_PROFILE:
                return item
    for item in _read_csv_records(PROMOTION_SUMMARY_CSV_PATH):
        if item.get("contract") == LATEST_G3_PROFILE or item.get("model") == LATEST_G3_PROFILE:
            return {
                **item,
                "contract": LATEST_G3_PROFILE,
                "contract_name": FINAL_G3_PROFILE_NAME,
                "total_return": item.get("total_return", item.get("return")),
                "trade_count": item.get("trade_count", item.get("trades")),
            }
    return {}


def _latest_window_metrics() -> list[dict[str, Any]]:
    window_rows = _read_csv_records(FINAL_G3_WINDOW_SUMMARY_PATH)
    if window_rows:
        labels = {
            "full": "全周期",
            "pre_2024_09": "2024年9月前",
            "post_2024_09": "2024年9月后",
            "post_2024_10": "2024年10月后",
            "weak_2022": "2022弱市",
            "2022_bear": "2022弱市",
            "valid_2024": "2024验证段",
            "2024": "2024年",
            "2025": "2025年",
            "blind_2026ytd": "2026年内盲测",
            "2026ytd": "2026年内盲测",
        }
        rows: list[dict[str, Any]] = []
        for item in window_rows:
            if item.get("model") != LATEST_G3_PROFILE:
                continue
            key = str(item.get("window") or "")
            rows.append(
                {
                    "window": key,
                    "window_label": labels.get(key, key),
                    "trade_count": int(float(item.get("trades") or item.get("trade_count") or 0)),
                    "return": item.get("return"),
                    "max_drawdown": item.get("max_drawdown"),
                    "win_rate": item.get("win_rate"),
                    "sum_pnl": item.get("sum_pnl"),
                    "contract": LATEST_G3_PROFILE,
                }
            )
        if rows:
            return rows

    profile = _latest_profile()
    windows = profile.get("windows") if isinstance(profile, dict) else None
    if not isinstance(windows, dict):
        return []
    labels = {
        "full": "全周期",
        "pre_2024_09": "2024年9月前",
        "post_2024_09": "2024年9月后",
        "weak_2022": "2022弱市",
        "valid_2024": "2024验证段",
        "blind_2026ytd": "2026年内盲测",
    }
    rows: list[dict[str, Any]] = []
    for key, value in windows.items():
        if not isinstance(value, dict):
            continue
        rows.append(
            {
                "window": key,
                "window_label": labels.get(key, key),
                "trade_count": int(value.get("trades") or value.get("trade_count") or 0),
                "return": value.get("return"),
                "max_drawdown": value.get("max_drawdown"),
                "win_rate": value.get("win_rate"),
                "sum_pnl": value.get("sum_pnl"),
                "contract": LATEST_G3_PROFILE,
            }
        )
    return rows


def _read_route_attribution_records() -> list[dict[str, Any]]:
    if not HISTORICAL_TRADES_PATH.exists():
        return []
    try:
        df = pd.read_csv(HISTORICAL_TRADES_PATH, low_memory=False)
    except Exception:
        return []
    df = _normalize_latest_g3_closed_trades(df)
    df = _with_route_strategy_fields(df)
    return _group_metrics(df, "trade_strategy")


def _normalize_latest_g3_closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()

    def first_existing_series(names: list[str]) -> pd.Series:
        values = pd.Series(pd.NA, index=out.index, dtype="object")
        for name in names:
            if name not in out.columns:
                continue
            current = out[name]
            values = values.where(values.notna() & values.astype(str).str.strip().ne(""), current)
        return values

    def display_datetime(names: list[str]) -> pd.Series:
        raw = first_existing_series(names)
        dt = pd.to_datetime(raw, errors="coerce")
        formatted = dt.dt.strftime("%Y-%m-%d %H:%M:%S")
        date_only = dt.dt.strftime("%Y-%m-%d")
        formatted = formatted.mask(formatted.str.endswith(" 00:00:00", na=False), date_only)
        return formatted.where(dt.notna(), raw.fillna("").astype(str))

    def numeric_series(name: str, default: float | None = None) -> pd.Series:
        if name in out.columns:
            values = pd.to_numeric(out[name], errors="coerce")
        else:
            values = pd.Series(default, index=out.index, dtype="float64")
        return values.fillna(default) if default is not None else values

    if "raw_score" not in out.columns and "score" in out.columns:
        out["raw_score"] = pd.to_numeric(out["score"], errors="coerce")
    if "score_source" not in out.columns:
        out["score_source"] = "historical_route_score"
    if "score_scale" not in out.columns:
        out["score_scale"] = "route_internal"
    if "route_source" not in out.columns and "route" in out.columns:
        out["route_source"] = out["route"]
    if "mode" in out.columns:
        out["route"] = out["mode"].fillna(out.get("route"))
    if "mode_label" in out.columns:
        out["route_label"] = out["mode_label"]
    else:
        labels = {
            "institutional_mainwave": "机构主升浪",
            "score120_core": "机构主升浪",
            "strong_main": "机构主升浪",
            "panic_repair": "恐慌修复",
            "down_panic": "恐慌修复",
            "old_g3_route_v3": "旧G3跨周期",
            "range_gap": "旧G3跨周期",
            "g2_gap_supplement": "G2空档补位",
        }
        out["route_label"] = out.get("route", pd.Series("", index=out.index)).map(labels).fillna(out.get("route", ""))
    out = _with_route_strategy_fields(out)
    if "original_contract" not in out.columns and "contract" in out.columns:
        out["original_contract"] = out["contract"]
    out["contract"] = LATEST_G3_PROFILE
    out["strategy_profile"] = FINAL_G3_PROFILE
    out["strategy_profile_name"] = FINAL_G3_PROFILE_NAME
    if "trade_status" not in out.columns:
        out["trade_status"] = "closed"
    else:
        out["trade_status"] = out["trade_status"].fillna("closed")
    out["entry_ts"] = display_datetime(["entry_ts", "entry_datetime", "buy_datetime", "planned_entry_ts", "entry_date"])
    out["exit_ts"] = display_datetime(
        [
            "exit_ts",
            "exit_datetime",
            "sell_datetime",
            "policy_exit_datetime",
            "exit_date",
            "policy_exit_date",
        ]
    )
    out["source_type"] = "historical_closed_trade"
    out["position_slots"] = 2
    if "slot_pct" not in out.columns:
        stake = pd.to_numeric(out.get("stake"), errors="coerce")
        entry_equity = pd.to_numeric(out.get("entry_equity"), errors="coerce")
        out["slot_pct"] = (stake / entry_equity).where(entry_equity > 0, 0.50).fillna(0.50)
    if "position_pct" not in out.columns:
        out["position_pct"] = out["slot_pct"]
    if "reference_close" not in out.columns and "entry_price" in out.columns:
        out["reference_close"] = out["entry_price"]
    entry_price = numeric_series("entry_price")
    if "hard_stop" not in out.columns:
        hard_stop_pct = numeric_series("hard_stop_pct", 0.12)
        out["hard_stop"] = entry_price * (1 - hard_stop_pct)
    if "take_profit_1" not in out.columns:
        take_profit_pct = numeric_series("take_profit_pct", 0.12)
        out["take_profit_1"] = entry_price * (1 + take_profit_pct)
    if "exit_contract" not in out.columns:
        out["exit_contract"] = "2槽/单槽50%/12%硬止损/12%先减半/剩余仓前低保护"
    if "confirm_rule" not in out.columns:
        out["confirm_rule"] = "30m_confirmed"
    if "live_ready" not in out.columns:
        out["live_ready"] = True
    if "formal_buy_signal" not in out.columns:
        out["formal_buy_signal"] = False
    if "auto_order_allowed" not in out.columns:
        out["auto_order_allowed"] = False
    if "order_path_enabled" not in out.columns:
        out["order_path_enabled"] = False
    if "shadow_action" not in out.columns:
        out["shadow_action"] = "historical_replay_closed"
    out = _apply_institutional_mainwave_score_scale(out)
    return out


def _apply_institutional_mainwave_score_scale(df: pd.DataFrame) -> pd.DataFrame:
    """Use current institutional-mainwave score semantics for historical rows too."""
    if df.empty or "score" not in df.columns:
        return df
    out = df.copy()
    mode = out.get("mode", pd.Series("", index=out.index)).astype(str)
    route = out.get("route", pd.Series("", index=out.index)).astype(str)
    inst_mask = mode.eq("institutional_mainwave") | route.eq("institutional_mainwave") | route.eq("score120_core")
    if not inst_mask.any():
        return out

    if "wave_style_score" not in out.columns:
        out["wave_style_score"] = pd.NA
    wave = pd.to_numeric(out["wave_style_score"], errors="coerce")
    missing_wave = inst_mask & wave.isna()

    if missing_wave.any() and SCORE120_CORE_TRADES_PATH.exists():
        try:
            source = pd.read_csv(
                SCORE120_CORE_TRADES_PATH,
                usecols=lambda col: col in {"entry_date", "code", "wave_style_score", "sector_diffusion_score", "selected_score"},
                low_memory=False,
            )
            if not source.empty and {"entry_date", "code", "wave_style_score"}.issubset(source.columns):
                source = source.copy()
                source["entry_date"] = pd.to_datetime(source["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
                source["code"] = source["code"].astype(str)
                source = source.drop_duplicates(["entry_date", "code"], keep="last")
                lookup = source.set_index(["entry_date", "code"])
                keys = pd.MultiIndex.from_arrays([
                    pd.to_datetime(out.get("entry_date"), errors="coerce").dt.strftime("%Y-%m-%d"),
                    out.get("code", pd.Series("", index=out.index)).astype(str),
                ])
                joined_wave = pd.Series(lookup.reindex(keys)["wave_style_score"].to_numpy(), index=out.index)
                out.loc[missing_wave, "wave_style_score"] = joined_wave.loc[missing_wave].to_numpy()
                if "sector_diffusion_score" in source.columns and "sector_diffusion_score" not in out.columns:
                    out["sector_diffusion_score"] = pd.NA
                if "sector_diffusion_score" in source.columns:
                    joined_diffusion = pd.Series(lookup.reindex(keys)["sector_diffusion_score"].to_numpy(), index=out.index)
                    diffusion_missing = inst_mask & pd.to_numeric(out.get("sector_diffusion_score"), errors="coerce").isna()
                    out.loc[diffusion_missing, "sector_diffusion_score"] = joined_diffusion.loc[diffusion_missing].to_numpy()
        except Exception:
            logger.exception("Failed to align historical institutional-mainwave score scale: %s", SCORE120_CORE_TRADES_PATH)

    wave = pd.to_numeric(out["wave_style_score"], errors="coerce")
    usable_wave = inst_mask & wave.notna()
    out.loc[usable_wave, "score"] = wave.loc[usable_wave]
    out.loc[usable_wave, "score_source"] = "wave_style_score"
    out.loc[usable_wave, "score_scale"] = "mainwave_0_140"
    out.loc[inst_mask & ~usable_wave, "score_source"] = "historical_selected_score"
    out.loc[inst_mask & ~usable_wave, "score_scale"] = "legacy_scheduler"
    return out


def _read_equity_curve_df(window: str | None) -> tuple[pd.DataFrame, Path]:
    path = EQUITY_CURVE_PATH
    if not path.exists():
        return pd.DataFrame(), path
    try:
        curve = pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(), path
    except Exception:
        logger.exception("Failed to read G3 equity curve: %s", path)
        return pd.DataFrame(), path
    if MTM_EQUITY_CURVE_PATH.exists():
        try:
            mtm = pd.read_csv(MTM_EQUITY_CURVE_PATH, low_memory=False)
            if "date" in mtm.columns:
                mtm_slim = mtm[
                    [
                        col
                        for col in [
                            "date",
                            "equity_mtm",
                            "drawdown_mtm",
                            "ret_from_start_mtm",
                            "mtm_value",
                            "missing_price_count",
                        ]
                        if col in mtm.columns
                    ]
                ].copy()
                curve = curve.merge(mtm_slim, on="date", how="left")
        except Exception:
            logger.exception("Failed to merge G3 MTM equity curve: %s", MTM_EQUITY_CURVE_PATH)
    if "date" not in curve.columns:
        return curve, path
    curve["date"] = pd.to_datetime(curve["date"], errors="coerce")
    if not curve.empty:
        mask_source = curve.rename(columns={"date": "entry_date"})
        curve = curve[_historical_window_mask(mask_source, window)].copy()
    return curve.sort_values("date"), path


def _equity_curve_records(curve: pd.DataFrame, limit: int = 2000) -> list[dict[str, Any]]:
    if curve.empty:
        return []
    slim = [
        "date",
        "equity",
        "ret_from_start",
        "drawdown",
        "open_positions",
        "reserved_principal",
        "cash",
        "mtm_value",
        "equity_mtm",
        "ret_from_start_mtm",
        "drawdown_mtm",
        "missing_price_count",
        "realized_pnl",
    ]
    available = [col for col in slim if col in curve.columns]
    out = curve[available].copy() if available else curve.copy()
    if "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if len(out) > limit:
        step = max(1, len(out) // limit)
        sampled = out.iloc[::step].copy()
        if sampled.index[-1] != out.index[-1]:
            sampled = pd.concat([sampled, out.tail(1)], ignore_index=True)
        out = sampled
    out = out.astype(object).where(pd.notna(out), None)
    return json.loads(out.to_json(orient="records", force_ascii=False))


def _exposure_metrics(trades: pd.DataFrame, curve: pd.DataFrame) -> dict[str, Any]:
    if curve.empty:
        return {
            "available": False,
            "reason": "missing_equity_curve",
        }
    open_positions = pd.to_numeric(curve.get("open_positions"), errors="coerce").fillna(0)
    reserved = pd.to_numeric(curve.get("reserved_principal"), errors="coerce").fillna(0)
    equity = pd.to_numeric(curve.get("equity"), errors="coerce")
    base_equity = float(equity.dropna().iloc[0]) if equity.notna().any() else 1_000_000.0
    equity_safe = equity.where(equity > 0, base_equity).fillna(base_equity)
    exposure_to_current_equity = (reserved / equity_safe).clip(lower=0)
    exposure_to_initial_capital = (reserved / base_equity).clip(lower=0) if base_equity else exposure_to_current_equity
    active = open_positions > 0

    slot = pd.to_numeric(trades.get("slot_pct"), errors="coerce") if not trades.empty and "slot_pct" in trades.columns else pd.Series(dtype=float)
    holding_days = pd.Series(dtype=float)
    if not trades.empty and {"entry_date", "policy_exit_date"}.issubset(trades.columns):
        entry = pd.to_datetime(trades["entry_date"], errors="coerce")
        exit_date = pd.to_datetime(trades["policy_exit_date"], errors="coerce")
        holding_days = (exit_date - entry).dt.days

    active_exposure = exposure_to_current_equity[active]
    active_initial_exposure = exposure_to_initial_capital[active]
    active_positions = open_positions[active]
    avg_active_exposure = float(active_exposure.mean()) if len(active_exposure) else 0.0
    comment = "仓位利用率正常。"
    if avg_active_exposure < 0.25:
        comment = "交易时段平均占用低于单槽 25%，说明不是单票槽位太重，而是开仓频率/并发槽位偏轻。"
    elif avg_active_exposure < 0.45:
        comment = "交易时段平均仓位偏轻，当前收益主要来自少数强交易，仍有提高并发槽位或动态仓位的空间。"
    elif avg_active_exposure > 0.75:
        comment = "交易时段仓位已经偏重，继续加仓前应先压单笔大亏。"

    return {
        "available": True,
        "trading_days": int(len(curve)),
        "active_days": int(active.sum()),
        "active_day_ratio": float(active.mean()) if len(active) else 0.0,
        "avg_exposure_all_days": float(exposure_to_current_equity.mean()) if len(exposure_to_current_equity) else 0.0,
        "avg_exposure_active_days": avg_active_exposure,
        "avg_exposure_active_days_initial_capital": float(active_initial_exposure.mean()) if len(active_initial_exposure) else 0.0,
        "max_exposure": float(exposure_to_current_equity.max()) if len(exposure_to_current_equity) else 0.0,
        "max_open_positions": int(open_positions.max()) if len(open_positions) else 0,
        "avg_open_positions_active_days": float(active_positions.mean()) if len(active_positions) else 0.0,
        "avg_slot_pct": float(slot.mean()) if len(slot.dropna()) else None,
        "median_slot_pct": float(slot.median()) if len(slot.dropna()) else None,
        "avg_holding_days": float(holding_days.dropna().mean()) if len(holding_days.dropna()) else None,
        "comment": comment,
    }


def _future_leak_audit(trades: pd.DataFrame) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    passes: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    if trades.empty:
        checks["date_order_available"] = False
    else:
        def date_col(name: str) -> pd.Series:
            if name not in trades.columns:
                return pd.Series(pd.NaT, index=trades.index, dtype="datetime64[ns]")
            return pd.to_datetime(trades[name], errors="coerce")

        entry = date_col("entry_date")
        exit_date = date_col("policy_exit_date")
        decision = date_col("decision_date")
        context = date_col("context_date")
        confirm = date_col("confirm_datetime")
        checks = {
            "rows": int(len(trades)),
            "missing_entry_date": int(entry.isna().sum()),
            "exit_before_entry": int(((exit_date < entry) & exit_date.notna() & entry.notna()).sum()),
            "decision_after_entry": int(((decision >= entry) & decision.notna() & entry.notna()).sum()),
            "context_after_or_equal_entry": int(((context >= entry) & context.notna() & entry.notna()).sum()),
            "confirm_after_entry": int(((confirm >= entry) & confirm.notna() & entry.notna()).sum()),
        }
        if any(checks[key] for key in ["exit_before_entry", "decision_after_entry", "context_after_or_equal_entry", "confirm_after_entry"]):
            issues.append(
                {
                    "severity": "high",
                    "item": "trade_date_order_violation",
                    "evidence": checks,
                    "impact": "历史成交存在确认、上下文或退出日期顺序异常，不能作为正式策略收益依据。",
                    "fix": "修正成交生成链路并重建闭合成交与收益曲线。",
                }
            )
        else:
            passes.append(
                {
                    "item": "trade_date_order_clean",
                    "evidence": "closed trades use prior decision/context dates and next-open entry timestamps.",
                }
            )

    try:
        source = STATE_ROUTER_RESEARCH_SCRIPT.read_text(encoding="utf-8", errors="replace")
    except Exception:
        source = ""
    compact_source = source.replace(" ", "").replace("\n", "")
    if 'sort_values(["mode_pick_rank","score","net_ret"]' in compact_source:
        issues.append(
            {
                "severity": "high",
                "item": "same_day_candidate_tiebreak_uses_future_return",
                "evidence": "scripts/gen3_market_state_router_v1.py state_router sorts candidates by mode_pick_rank, score, net_ret.",
                "impact": "同一交易日多个候选并列时用最终收益 net_ret 选票，会把历史收益曲线和胜率系统性抬高。",
                "fix": "删除 net_ret 排序，只允许使用入场前可见字段，例如 score、流动性、主线强度、确认质量、代码稳定排序。",
            }
        )
    else:
        passes.append(
            {
                "item": "candidate_ranking_static_check",
                "evidence": "no net_ret tie-break was found in the current research router source.",
            }
        )

    if "pd.merge_asof" in source and "direction=\"backward\"" in source and "allow_exact_matches=False" in source:
        passes.append(
            {
                "item": "market_context_uses_previous_row",
                "evidence": "merge_asof uses backward context and disallows exact entry-date matches.",
            }
        )

    high = any(item.get("severity") == "high" for item in issues)
    return {
        "verdict": "needs_fix_before_formal_use" if high else "no_future_leak_found_in_checked_paths",
        "has_high_risk_issue": high,
        "date_order": checks,
        "issues": issues,
        "passes": passes,
        "audited_files": [
            str(STATE_ROUTER_RESEARCH_SCRIPT),
            str(HISTORICAL_TRADES_PATH),
        ],
    }


def _recent_replay_audit(sample_size: int = 30) -> dict[str, Any]:
    if not HISTORICAL_TRADES_PATH.exists():
        return {
            "ok": False,
            "available": False,
            "reason": "missing_historical_trades",
            "path": str(HISTORICAL_TRADES_PATH),
        }
    try:
        df = pd.read_csv(HISTORICAL_TRADES_PATH, low_memory=False)
    except pd.errors.EmptyDataError:
        return {
            "ok": False,
            "available": False,
            "reason": "empty_historical_trades",
            "path": str(HISTORICAL_TRADES_PATH),
        }
    except Exception as exc:
        logger.exception("Failed to audit recent G3 replay evidence: %s", HISTORICAL_TRADES_PATH)
        return {
            "ok": False,
            "available": False,
            "reason": "read_failed",
            "error": str(exc),
            "path": str(HISTORICAL_TRADES_PATH),
        }

    df = _normalize_latest_g3_closed_trades(df)
    if df.empty or "entry_date" not in df.columns:
        return {
            "ok": False,
            "available": False,
            "reason": "missing_entry_date",
            "path": str(HISTORICAL_TRADES_PATH),
        }

    work = df.copy()
    work["_entry_dt"] = pd.to_datetime(work.get("entry_date"), errors="coerce")
    work = work[work["_entry_dt"].notna()].sort_values("_entry_dt")
    recent = work.tail(sample_size).copy()
    if recent.empty:
        return {
            "ok": False,
            "available": False,
            "reason": "no_recent_rows",
            "path": str(HISTORICAL_TRADES_PATH),
        }

    def num_col(name: str) -> pd.Series:
        if name not in recent.columns:
            return pd.Series(dtype=float)
        return pd.to_numeric(recent[name], errors="coerce")

    def dt_col(name: str) -> pd.Series:
        if name not in recent.columns:
            return pd.Series(pd.NaT, index=recent.index, dtype="datetime64[ns]")
        return pd.to_datetime(recent[name], errors="coerce")

    entry = recent["_entry_dt"]
    decision = dt_col("decision_date")
    context = dt_col("context_date")
    account_loss = num_col("account_loss_pct")
    hard_stop = num_col("hard_stop_pct")
    take_profit = num_col("take_profit_pct")
    stake = num_col("stake")
    entry_equity = num_col("entry_equity")
    slot_pct = (stake / entry_equity).replace([float("inf"), -float("inf")], pd.NA)

    contract_match_count = 0
    if "contract" in recent.columns:
        contract_match_count = int((recent["contract"].astype(str) == LATEST_G3_PROFILE).sum())
    route_count = int(recent["route"].astype(str).replace("", pd.NA).dropna().nunique()) if "route" in recent.columns else 0
    date_violations = {
        "decision_after_or_equal_entry": int(((decision >= entry) & decision.notna()).sum()),
        "context_after_or_equal_entry": int(((context >= entry) & context.notna()).sum()),
    }
    hard_stop_match = int((hard_stop.dropna().round(4) == 0.12).sum()) if len(hard_stop.dropna()) else 0
    take_profit_match = int((take_profit.dropna().round(4) == 0.12).sum()) if len(take_profit.dropna()) else 0
    max_account_loss = float(account_loss.min()) if len(account_loss.dropna()) else None
    avg_slot_pct = float(slot_pct.dropna().mean()) if len(slot_pct.dropna()) else None
    max_slot_pct = float(slot_pct.dropna().max()) if len(slot_pct.dropna()) else None

    required_rows = min(sample_size, len(work))
    checks = {
        "recent_trade_count": int(len(recent)),
        "recent_entry_day_count": int(recent["_entry_dt"].dt.strftime("%Y-%m-%d").nunique()),
        "required_trade_count": int(min(20, required_rows)),
        "latest_entry_date": recent["_entry_dt"].max().strftime("%Y-%m-%d"),
        "earliest_recent_entry_date": recent["_entry_dt"].min().strftime("%Y-%m-%d"),
        "contract_match_count": contract_match_count,
        "route_count": route_count,
        "hard_stop_pct_match_count": hard_stop_match,
        "take_profit_pct_match_count": take_profit_match,
        "avg_slot_pct": avg_slot_pct,
        "max_slot_pct": max_slot_pct,
        "max_account_loss_pct": max_account_loss,
        "date_violations": date_violations,
    }
    blockers: list[str] = []
    if checks["recent_trade_count"] < checks["required_trade_count"]:
        blockers.append("recent_trade_sample_too_small")
    if contract_match_count != len(recent):
        blockers.append("contract_mismatch")
    if any(date_violations.values()):
        blockers.append("date_order_violation")
    if hard_stop_match != len(hard_stop.dropna()) or hard_stop_match == 0:
        blockers.append("hard_stop_contract_mismatch")
    if take_profit_match != len(take_profit.dropna()) or take_profit_match == 0:
        blockers.append("take_profit_contract_mismatch")
    if max_account_loss is None or max_account_loss < -0.20:
        blockers.append("account_loss_over_20pct")
    if avg_slot_pct is None or avg_slot_pct < 0.35 or max_slot_pct is None or max_slot_pct > 0.55:
        blockers.append("slot_pct_not_2slot_50pct_like")

    return {
        "ok": not blockers,
        "available": True,
        "sample_size": sample_size,
        "blockers": blockers,
        "checks": checks,
        "comment": (
            "recent replay sample matches the current 2-slot execution contract"
            if not blockers
            else "recent replay sample still has execution-contract blockers"
        ),
        "path": str(HISTORICAL_TRADES_PATH),
    }


def _pretrade_smoke_audit(workflow: dict[str, Any] | None = None, max_age_hours: float = 24.0) -> dict[str, Any]:
    smoke = _latest_pretrade_smoke()
    status = _path_status(PRETRADE_SMOKE_PATH)
    if not smoke:
        return {
            "ok": False,
            "available": False,
            "reason": "missing_pretrade_smoke",
            "path_status": status,
        }

    generated_at = str(smoke.get("generated_at") or "").strip()
    generated_dt = pd.to_datetime(generated_at, errors="coerce")
    age_hours: float | None = None
    if pd.notna(generated_dt):
        try:
            age_hours = round((pd.Timestamp(datetime.now()) - generated_dt).total_seconds() / 3600.0, 3)
        except Exception:
            age_hours = None

    verdict = str(smoke.get("verdict") or "").strip()
    action = str(smoke.get("trade_action") or "").strip()
    blockers = smoke.get("blockers") if isinstance(smoke.get("blockers"), list) else []
    ticket_count = int(smoke.get("qualified_ticket_count") or 0)
    entry_date = str(smoke.get("entry_date") or "")[:10]
    decision_date = str(smoke.get("decision_date") or "")[:10]
    workflow_entry = str((workflow or {}).get("entry_date") or "")[:10]
    workflow_decision = str((workflow or {}).get("decision_date") or "")[:10]
    runtime = _load_current_runtime(limit=500)
    runtime_tickets = runtime.get("tickets") if isinstance(runtime, dict) else []
    runtime_qualified = [
        item for item in runtime_tickets
        if isinstance(item, dict) and _truthy(item.get("qualified_shadow_buy"))
    ]

    blockers_out: list[str] = []
    if verdict not in {"shadow_ready", "shadow_ready_with_warnings", "no_trade"}:
        blockers_out.append("bad_smoke_verdict")
    if blockers:
        blockers_out.append("smoke_has_blockers")
    if age_hours is None or age_hours > max_age_hours:
        blockers_out.append("stale_smoke_result")
    if workflow_entry and entry_date and entry_date != workflow_entry:
        blockers_out.append("entry_date_mismatch")
    if workflow_decision and decision_date and decision_date != workflow_decision:
        blockers_out.append("decision_date_mismatch")
    if ticket_count != len(runtime_qualified):
        blockers_out.append("qualified_ticket_count_mismatch")
    if ticket_count > 0 and action != "buy_review":
        blockers_out.append("ticket_without_buy_review_action")
    if ticket_count == 0 and verdict != "no_trade":
        blockers_out.append("no_ticket_but_not_no_trade")
    if bool(smoke.get("live_order_enabled")):
        blockers_out.append("live_order_enabled")
    if bool(smoke.get("formal_buy_signal")) or bool(smoke.get("auto_order_allowed")) or bool(smoke.get("order_path_enabled")):
        blockers_out.append("formal_or_auto_order_guardrail_open")

    return {
        "ok": not blockers_out,
        "available": True,
        "blockers": blockers_out,
        "checks": {
            "generated_at": generated_at,
            "age_hours": age_hours,
            "max_age_hours": max_age_hours,
            "verdict": verdict,
            "trade_action": action,
            "entry_date": entry_date,
            "workflow_entry_date": workflow_entry,
            "decision_date": decision_date,
            "workflow_decision_date": workflow_decision,
            "qualified_ticket_count": ticket_count,
            "runtime_qualified_ticket_count": len(runtime_qualified),
            "smoke_blocker_count": len(blockers),
            "live_order_enabled": bool(smoke.get("live_order_enabled")),
        },
        "comment": "latest pretrade smoke matches current G3 runtime" if not blockers_out else "latest pretrade smoke is not current enough for replacement evidence",
        "path_status": status,
    }


def _current_shadow_trade_rows() -> pd.DataFrame:
    tickets_path = STATE_ALPHA_RUNTIME_DIR / "latest_shadow_tickets.csv"
    tickets = _read_csv_records(tickets_path)
    if not tickets:
        return pd.DataFrame()
    df = pd.DataFrame(tickets)
    if df.empty:
        return df
    if "ticket_key" in df.columns:
        df = df.drop_duplicates(subset=["ticket_key"], keep="last")
    if "entry_date" in df.columns:
        df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "decision_date" in df.columns:
        df["decision_date"] = pd.to_datetime(df["decision_date"], errors="coerce").dt.strftime("%Y-%m-%d")

    out = pd.DataFrame(index=df.index)
    out["entry_date"] = df.get("entry_date", "")
    out["policy_exit_date"] = pd.NA
    out["decision_date"] = df.get("decision_date", "")
    out["confirm_datetime"] = df.get("confirm_datetime", "")
    out["entry_ts"] = df.get("planned_entry_ts", df.get("entry_ts", ""))
    out["exit_ts"] = ""
    out["code"] = df.get("code", "")
    out["name"] = df.get("name", df.get("stock_name", ""))
    out["route"] = df.get("route", "")
    out["route_label"] = df.get("route_label", "")
    out["mode_label"] = "当前影子买入"
    out["score"] = pd.to_numeric(df.get("score"), errors="coerce")
    out["raw_score"] = pd.to_numeric(df.get("score"), errors="coerce")
    out["score_source"] = "wave_style_score"
    out["score_scale"] = "mainwave_0_140"
    out["net_ret"] = pd.NA
    out["stress_net_ret"] = pd.NA
    out["policy_net_ret"] = pd.NA
    out["entry_price"] = pd.to_numeric(df.get("reference_close"), errors="coerce")
    out["reference_close"] = pd.to_numeric(df.get("reference_close"), errors="coerce")
    out["stake"] = pd.NA
    out["exit_value"] = pd.NA
    out["realized_pnl"] = pd.NA
    out["market_style"] = ""
    out["index_mom20"] = pd.NA
    out["index_mom60"] = pd.NA
    out["up_rate"] = pd.NA
    out["big_down_rate"] = pd.NA
    out["policy"] = "g3_state_alpha_open_shadow"
    out["confirm_rule"] = df.get("m30_status", "")
    out["live_ready"] = df.get("paper_trade_ready", df.get("qualified_shadow_buy", False))
    out["shadow_action"] = "open_shadow_buy"
    out["formal_buy_signal"] = df.get("formal_buy_signal", False)
    out["auto_order_allowed"] = df.get("auto_order_allowed", False)
    out["order_path_enabled"] = df.get("order_path_enabled", False)
    out["block_reason"] = df.get("block_reason", "")
    out["position_slots"] = df.get("portfolio_slot_count", pd.NA)
    out["slot_pct"] = pd.to_numeric(df.get("position_pct"), errors="coerce")
    out["position_pct"] = pd.to_numeric(df.get("position_pct"), errors="coerce")
    out["trade_key"] = df.get("ticket_key", "")
    out["candidate_key"] = df.get("ticket_key", "")
    out["trade_status"] = "open_shadow"
    out["shadow_status"] = df.get("shadow_status", "")
    out["last_state"] = df.get("last_state", "")
    out["planned_entry_ts"] = df.get("planned_entry_ts", "")
    out["structure_stop"] = pd.to_numeric(df.get("structure_stop"), errors="coerce")
    out["hard_stop"] = pd.to_numeric(df.get("hard_stop"), errors="coerce")
    out["take_profit_1"] = pd.to_numeric(df.get("take_profit_1"), errors="coerce")
    out["exit_contract"] = df.get("exit_contract", "")
    out["source_type"] = "runtime_shadow_ledger"
    return out.reset_index(drop=True)


def _read_historical_trades(
    limit: int,
    route: str | None,
    window: str | None,
    sort_by: str,
    sort_order: str,
) -> dict[str, Any]:
    curve_df, curve_path = _read_equity_curve_df(window)
    shadow_df = _current_shadow_trade_rows()
    natural_policy_shadow = _read_natural_policy_shadow()
    if not HISTORICAL_TRADES_PATH.exists():
        if not shadow_df.empty and route and route != "all" and "route" in shadow_df.columns:
            shadow_df = shadow_df[shadow_df["route"].astype(str) == route]
        if not shadow_df.empty:
            shadow_df = shadow_df[_historical_window_mask(shadow_df, window)]
            shadow_df = _with_route_strategy_fields(shadow_df)
        page_df = shadow_df.head(limit).astype(object).where(pd.notna(shadow_df.head(limit)), None)
        return {
            "rows": json.loads(page_df.to_json(orient="records", force_ascii=False)) if not page_df.empty else [],
            "metrics": _trade_metrics(shadow_df),
            "route_metrics": _group_metrics(shadow_df, "trade_strategy"),
            "market_style_metrics": _group_metrics(shadow_df, "market_style")[:20],
            "equity_curve": _equity_curve_records(curve_df),
            "exposure_metrics": _exposure_metrics(shadow_df, curve_df),
            "future_leak_audit": _future_leak_audit(shadow_df),
            "window_metrics": _latest_window_metrics(),
            "filters": {
                "route": route or "all",
                "window": window or "all",
                "sort_by": sort_by,
                "sort_order": sort_order,
                "limit": limit,
            },
            "artifacts": {
                "closed_trades": _path_status(HISTORICAL_TRADES_PATH),
                "runtime_shadow_ledger": _path_status(STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"),
                "equity_curve": _path_status(curve_path),
                "mtm_equity_curve": _path_status(MTM_EQUITY_CURVE_PATH),
                "summary": _path_status(PROMOTION_SUMMARY_PATH),
                "gates": _path_status(PROMOTION_GATES_PATH),
            },
            "natural_policy_shadow": natural_policy_shadow,
        }
    try:
        df = pd.read_csv(HISTORICAL_TRADES_PATH, low_memory=False)
    except pd.errors.EmptyDataError:
        df = pd.DataFrame()
    except Exception:
        logger.exception("Failed to read historical trades: %s", HISTORICAL_TRADES_PATH)
        df = pd.DataFrame()

    df = _normalize_latest_g3_closed_trades(df)
    if not df.empty:
        if "trade_status" in df.columns:
            df["trade_status"] = df["trade_status"].fillna("closed")
        else:
            df["trade_status"] = "closed"
        if "source_type" in df.columns:
            df["source_type"] = df["source_type"].fillna("historical_closed_trade")
        else:
            df["source_type"] = "historical_closed_trade"
    if not shadow_df.empty:
        shadow_for_concat = shadow_df.dropna(axis=1, how="all")
        df = pd.concat([df, shadow_for_concat], ignore_index=True, sort=False) if not df.empty else shadow_df.copy()
        if "trade_key" in df.columns:
            df = df.drop_duplicates(subset=["trade_key"], keep="last")
        df = _with_route_strategy_fields(df)

    if not df.empty and route and route != "all" and "route" in df.columns:
        df = df[df["route"].astype(str) == route]
    if not df.empty:
        df = df[_historical_window_mask(df, window)]

    if not df.empty:
        sort_col = sort_by if sort_by in df.columns else "entry_date"
        ascending = sort_order == "asc"
        df = df.sort_values(sort_col, ascending=ascending, kind="mergesort")

    df = _attach_natural_policy_shadow_fields(df)
    metrics = _trade_metrics(df)
    route_metrics = _group_metrics(df, "trade_strategy")
    market_style_metrics = _group_metrics(df, "market_style")[:20]
    equity_curve = _equity_curve_records(curve_df)
    exposure_metrics = _exposure_metrics(df, curve_df)
    future_leak_audit = _future_leak_audit(df)
    slim_columns = [
        "entry_date",
        "policy_exit_date",
        "decision_date",
        "confirm_datetime",
        "entry_ts",
        "exit_ts",
        "code",
        "name",
        "route",
        "route_strategy",
        "route_strategy_label",
        "trade_strategy",
        "trade_strategy_label",
        "route_strategy_family",
        "route_strategy_family_label",
        "route_parent",
        "route_parent_label",
        "route_label",
        "mode_label",
        "score",
        "raw_score",
        "score_source",
        "score_scale",
        "net_ret",
        "stress_net_ret",
        "policy_net_ret",
        "entry_price",
        "stake",
        "exit_value",
        "realized_pnl",
        "market_style",
        "index_mom20",
        "index_mom60",
        "up_rate",
        "big_down_rate",
        "policy",
        "confirm_rule",
        "live_ready",
        "shadow_action",
        "formal_buy_signal",
        "auto_order_allowed",
        "order_path_enabled",
        "block_reason",
        "position_slots",
        "slot_pct",
        "position_pct",
        "trade_status",
        "shadow_status",
        "natural_action",
        "natural_action_label",
        "natural_position_pct",
        "natural_rule_hits",
        "natural_context_tag",
        "natural_reason",
        "exit_shadow_action",
        "exit_shadow_label",
        "exit_shadow_reason",
        "shadow_pnl_scale",
        "shadow_realized_pnl",
        "last_state",
        "planned_entry_ts",
        "reference_close",
        "structure_stop",
        "hard_stop",
        "take_profit_1",
        "exit_contract",
        "source_type",
        "trade_key",
        "candidate_key",
    ]
    available = [col for col in slim_columns if col in df.columns]
    page_df = df.head(limit)[available] if available else df.head(limit)
    page_df = page_df.astype(object).where(pd.notna(page_df), None)
    return {
        "rows": json.loads(page_df.to_json(orient="records", force_ascii=False)),
        "metrics": metrics,
        "route_metrics": route_metrics,
        "market_style_metrics": market_style_metrics,
        "equity_curve": equity_curve,
        "exposure_metrics": exposure_metrics,
        "future_leak_audit": future_leak_audit,
        "window_metrics": _latest_window_metrics(),
        "filters": {
            "route": route or "all",
            "window": window or "all",
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": limit,
        },
        "artifacts": {
            "closed_trades": _path_status(HISTORICAL_TRADES_PATH),
            "runtime_shadow_ledger": _path_status(STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"),
            "equity_curve": _path_status(curve_path),
            "mtm_equity_curve": _path_status(MTM_EQUITY_CURVE_PATH),
            "summary": _path_status(PROMOTION_SUMMARY_PATH),
            "summary_csv": _path_status(PROMOTION_SUMMARY_CSV_PATH),
            "gates": _path_status(PROMOTION_GATES_PATH),
            "natural_policy_shadow_summary": _path_status(NATURAL_POLICY_SHADOW_DIR / "summary.json"),
            "natural_policy_shadow_contract": _path_status(NATURAL_POLICY_SHADOW_DIR / "natural_policy_contract.json"),
            "natural_candidate_shadow_labels": _path_status(NATURAL_POLICY_SHADOW_DIR / "candidate_shadow_labels.csv"),
            "natural_closed_trade_shadow_actions": _path_status(NATURAL_POLICY_SHADOW_DIR / "closed_trade_shadow_actions.csv"),
        },
        "natural_policy_shadow": natural_policy_shadow,
    }


def _attach_natural_policy_shadow_fields(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "trade_key" not in df.columns:
        return df
    out = df.copy()
    label_cols = [
        "trade_key",
        "natural_action",
        "natural_action_label",
        "natural_position_pct",
        "natural_rule_hits",
        "natural_context_tag",
        "natural_reason",
    ]
    closed_cols = [
        "trade_key",
        "exit_shadow_action",
        "exit_shadow_label",
        "exit_shadow_reason",
        "shadow_pnl_scale",
        "shadow_realized_pnl",
    ]
    labels = _read_csv_df(NATURAL_POLICY_SHADOW_DIR / "candidate_shadow_labels.csv")
    closed = _read_csv_df(NATURAL_POLICY_SHADOW_DIR / "closed_trade_shadow_actions.csv")
    if not labels.empty:
        keep = [col for col in label_cols if col in labels.columns]
        labels = labels[keep].drop_duplicates("trade_key", keep="first") if "trade_key" in keep else pd.DataFrame()
        if not labels.empty:
            drop_cols = [col for col in keep if col != "trade_key" and col in out.columns]
            if drop_cols:
                out = out.drop(columns=drop_cols)
            out = out.merge(labels, on="trade_key", how="left")
    if not closed.empty:
        keep = [col for col in closed_cols if col in closed.columns]
        closed = closed[keep].drop_duplicates("trade_key", keep="first") if "trade_key" in keep else pd.DataFrame()
        if not closed.empty:
            drop_cols = [col for col in keep if col != "trade_key" and col in out.columns]
            if drop_cols:
                out = out.drop(columns=drop_cols)
            out = out.merge(closed, on="trade_key", how="left")
    out = _fill_natural_policy_shadow_fallback(out)
    return out


def _natural_context_tag_for_row(row: pd.Series) -> str:
    strategy = str(row.get("trade_strategy") or "")
    style = str(row.get("market_style") or "")
    route = str(row.get("route") or "")
    down_risk = _truthy(row.get("down_risk"))
    if strategy == "institutional_score120_mainwave" or route in {"institutional_mainwave", "score120_core"}:
        return "market_aligned_mainwave" if style == "standard_uptrend" else "stock_leads_market_mainwave"
    if strategy == "old_g3_strong_breakout" or route == "strong_main":
        return "market_aligned_breakout" if style == "standard_uptrend" else "stock_leads_market_breakout"
    if strategy in {"range_weak_repair", "panic_capitulation_repair"} or route in {"panic_repair", "range_gap", "down_panic"}:
        if down_risk or style == "standard_downtrend":
            return "repair_against_downrisk"
        if style == "standard_range":
            return "range_repair"
        return "weak_rebound_repair"
    if strategy == "volume_runup_supplement" or route == "g2_gap_supplement":
        return "g2_gap_supplement_rotation"
    return "unclassified"


def _fill_natural_policy_shadow_fallback(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    for col in [
        "natural_action",
        "natural_action_label",
        "natural_position_pct",
        "natural_rule_hits",
        "natural_context_tag",
        "natural_reason",
        "exit_shadow_action",
        "exit_shadow_label",
        "exit_shadow_reason",
    ]:
        if col not in out.columns:
            out[col] = None
    missing_exit = out["exit_shadow_action"].isna() | out["exit_shadow_action"].astype(str).eq("")
    if missing_exit.any():
        out.loc[missing_exit, "exit_shadow_action"] = "none"
        out.loc[missing_exit, "exit_shadow_label"] = "无"
        out.loc[missing_exit, "exit_shadow_reason"] = ""
    missing = out["natural_action"].isna() | out["natural_action"].astype(str).eq("")
    for idx, row in out[missing].iterrows():
        strategy = str(row.get("trade_strategy") or "")
        route = str(row.get("route") or "")
        context_tag = _natural_context_tag_for_row(row)
        if strategy == "volume_runup_supplement" or route == "g2_gap_supplement":
            out.at[idx, "natural_action"] = "allow_reduced"
            out.at[idx, "natural_action_label"] = "允许但降仓"
            out.at[idx, "natural_position_pct"] = 0.25
            out.at[idx, "natural_rule_hits"] = "N3"
            out.at[idx, "natural_reason"] = "运行时兜底：G2 空档补位按补位角色半槽观察"
        else:
            out.at[idx, "natural_action"] = "allow"
            out.at[idx, "natural_action_label"] = "允许"
            out.at[idx, "natural_position_pct"] = row.get("position_pct") or row.get("slot_pct") or 0.5
            out.at[idx, "natural_rule_hits"] = "N4" if context_tag != "unclassified" else ""
            if context_tag.startswith("stock_leads_market"):
                out.at[idx, "natural_reason"] = "运行时兜底：个股领先市场状态，需要解释为个股主升而非市场共振"
            elif "repair" in context_tag:
                out.at[idx, "natural_reason"] = "运行时兜底：修复类交易需要解释市场压力与修复证据"
            else:
                out.at[idx, "natural_reason"] = "运行时兜底：符合当前自然交易观察合同"
        out.at[idx, "natural_context_tag"] = context_tag
    return out


def _read_natural_policy_shadow() -> dict[str, Any]:
    return {
        "summary": _read_json(NATURAL_POLICY_SHADOW_DIR / "summary.json"),
        "contract": _read_json(NATURAL_POLICY_SHADOW_DIR / "natural_policy_contract.json"),
        "entry_position_summary": _read_csv_records(NATURAL_POLICY_SHADOW_DIR / "entry_position_shadow_summary.csv"),
        "exit_summary": _read_csv_records(NATURAL_POLICY_SHADOW_DIR / "exit_shadow_summary.csv"),
        "artifacts": {
            "summary": _path_status(NATURAL_POLICY_SHADOW_DIR / "summary.json"),
            "contract": _path_status(NATURAL_POLICY_SHADOW_DIR / "natural_policy_contract.json"),
            "candidate_shadow_labels": _path_status(NATURAL_POLICY_SHADOW_DIR / "candidate_shadow_labels.csv"),
            "closed_trade_shadow_actions": _path_status(NATURAL_POLICY_SHADOW_DIR / "closed_trade_shadow_actions.csv"),
            "report": _path_status(NATURAL_POLICY_SHADOW_DIR / "REPORT_CN.md"),
        },
    }


def _as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _gate(name: str, ok: bool, status: str, detail: str, blocking: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "ok": bool(ok),
        "status": status,
        "blocking": bool(blocking),
        "detail": detail,
    }


def _historical_buy_logic_parity_audit() -> dict[str, Any]:
    required_live_fields = [
        "m30_confirmed",
        "router_eligible",
        "source_quality",
        "account_risk_action",
    ]
    optional_context_fields = [
        "entry_date",
        "decision_date",
        "context_date",
        "mode",
        "route",
        "exit_reason",
    ]
    source = _read_json(PROMOTION_SUMMARY_PATH).get("source") or {}
    selected_path = Path(str(source.get("selected_candidates") or ""))
    paths = {
        "closed_trades": HISTORICAL_TRADES_PATH,
        "selected_candidates": selected_path if str(selected_path) else None,
    }
    checks: dict[str, Any] = {
        "required_live_fields": required_live_fields,
        "historical_backtest_scope": "state_router_candidates_plus_2slot_portfolio_plus_30m_exit_controls",
        "full_current_buy_scope": "router_eligible + source_quality + account_risk + m30_confirmation + paper_trade_ready; route_health is observation only",
        "paths": {key: _path_status(value) for key, value in paths.items() if value is not None},
    }
    missing_by_artifact: dict[str, list[str]] = {}
    present_by_artifact: dict[str, list[str]] = {}
    row_counts: dict[str, int] = {}

    for key, path in paths.items():
        if path is None or not path.exists():
            missing_by_artifact[key] = list(required_live_fields)
            row_counts[key] = 0
            continue
        try:
            df = pd.read_csv(path, low_memory=False)
        except Exception as exc:
            missing_by_artifact[key] = list(required_live_fields)
            row_counts[key] = 0
            checks[f"{key}_read_error"] = str(exc)
            continue
        cols = set(df.columns)
        row_counts[key] = int(len(df))
        present_by_artifact[key] = sorted([field for field in required_live_fields + optional_context_fields if field in cols])
        missing_by_artifact[key] = [field for field in required_live_fields if field not in cols]

    closed_missing = missing_by_artifact.get("closed_trades") or []
    selected_missing = missing_by_artifact.get("selected_candidates") or []
    ok = not closed_missing and not selected_missing
    blockers: list[str] = []
    if closed_missing:
        blockers.append(f"closed_trades_missing_live_fields:{','.join(closed_missing)}")
    if selected_missing:
        blockers.append(f"selected_candidates_missing_live_fields:{','.join(selected_missing)}")
    checks.update(
        {
            "ok": bool(ok),
            "row_counts": row_counts,
            "present_by_artifact": present_by_artifact,
            "missing_by_artifact": missing_by_artifact,
            "blockers": blockers,
        }
    )
    return {
        "ok": bool(ok),
        "available": True,
        "blockers": blockers,
        "checks": checks,
        "comment": (
            "historical replay proves full current buy logic parity"
            if ok
            else "historical replay has not yet proven parity with the current full G3 buy gate"
        ),
    }


def _gate_layer_policy_audit() -> dict[str, Any]:
    status = _path_status(GATE_LAYER_AUDIT_SUMMARY_PATH)
    if not status.get("exists"):
        return {
            "ok": False,
            "available": False,
            "blockers": ["missing_gate_layer_policy_audit"],
            "checks": {"summary": status, "report": _path_status(GATE_LAYER_AUDIT_REPORT_PATH)},
            "comment": "route-health gate layer audit has not been generated",
        }
    payload = _read_json(GATE_LAYER_AUDIT_SUMMARY_PATH)
    rows = payload.get("summary") if isinstance(payload, dict) else []
    rows = [item for item in rows if isinstance(item, dict)]
    by_name = {str(item.get("scenario") or ""): item for item in rows}
    original = by_name.get("original_no_route_health_gate") or {}
    hard = by_name.get("hard_block_route_health_fail") or {}
    soft50 = by_name.get("soft_scale_50pct_when_route_health_fail") or {}
    soft25 = by_name.get("soft_scale_25pct_when_route_health_fail") or {}

    original_ret = _as_float(original.get("total_return"), 0.0) or 0.0

    def retention(item: dict[str, Any]) -> float | None:
        ret = _as_float(item.get("total_return"))
        if original_ret <= 0 or ret is None:
            return None
        return ret / original_ret

    def drawdown_improvement(item: dict[str, Any]) -> float | None:
        base_dd = _as_float(original.get("max_drawdown"))
        dd = _as_float(item.get("max_drawdown"))
        if base_dd is None or dd is None:
            return None
        return dd - base_dd

    hard_retention = retention(hard)
    soft50_retention = retention(soft50)
    soft25_retention = retention(soft25)
    soft50_dd_improve = drawdown_improvement(soft50)
    soft25_dd_improve = drawdown_improvement(soft25)
    hard_dd_improve = drawdown_improvement(hard)
    original_max_drawdown = _as_float(original.get("max_drawdown"))
    original_mtm_max_drawdown = _as_float(original.get("mtm_max_drawdown"))
    original_worst_single = _as_float(original.get("max_single_account_loss"))
    original_risk_ok = (
        (original_max_drawdown is None or original_max_drawdown >= -0.20)
        and (original_mtm_max_drawdown is None or original_mtm_max_drawdown >= -0.20)
        and (original_worst_single is None or original_worst_single >= -0.07)
    )

    recommendation = "observe_only"
    recommendation_reason = "route_health_filter_hurts_return_and_original_risk_is_within_contract"
    if not original_risk_ok and soft50_retention is not None and soft50_retention >= 0.75 and (soft50_dd_improve or 0.0) > 0:
        recommendation = "soft_scale_50pct_when_route_health_fail"
        recommendation_reason = "keeps_most_return_and_reduces_drawdown"
    elif not original_risk_ok and soft25_retention is not None and soft25_retention >= 0.65 and (soft25_dd_improve or 0.0) > 0:
        recommendation = "soft_scale_25pct_when_route_health_fail"
        recommendation_reason = "more_defensive_scale_keeps_acceptable_return"
    elif not original_risk_ok and hard_retention is not None and hard_retention >= 0.75 and (hard_dd_improve or 0.0) > 0:
        recommendation = "hard_block_route_health_fail"
        recommendation_reason = "hard_block_has_acceptable_return_retention"

    ok = recommendation.startswith("soft_scale") or recommendation == "observe_only"
    checks = {
        "summary": status,
        "report": _path_status(GATE_LAYER_AUDIT_REPORT_PATH),
        "scenario_count": len(rows),
        "original_total_return": original.get("total_return"),
        "hard_block_total_return": hard.get("total_return"),
        "soft50_total_return": soft50.get("total_return"),
        "soft25_total_return": soft25.get("total_return"),
        "hard_block_retention": hard_retention,
        "soft50_retention": soft50_retention,
        "soft25_retention": soft25_retention,
        "original_risk_ok": original_risk_ok,
        "original_max_drawdown": original_max_drawdown,
        "original_mtm_max_drawdown": original_mtm_max_drawdown,
        "original_worst_single": original_worst_single,
        "hard_block_drawdown_improvement": hard_dd_improve,
        "soft50_drawdown_improvement": soft50_dd_improve,
        "soft25_drawdown_improvement": soft25_dd_improve,
        "recommendation": recommendation,
        "recommendation_reason": recommendation_reason,
        "gate_counts": payload.get("route_health_gate_counts") if isinstance(payload, dict) else [],
    }
    return {
        "ok": bool(ok),
        "available": True,
        "blockers": [] if ok else ["no_acceptable_gate_layer_policy"],
        "checks": checks,
        "comment": "route-health is observation only under the default return-first G3 contract",
    }


def _build_daily_action_plan(
    *,
    can_replace: bool,
    blocking: list[dict[str, Any]],
    historical_parity: dict[str, Any] | None,
    pretrade_smoke: dict[str, Any] | None,
    current_paper: dict[str, Any] | None,
    observation_scheduler: dict[str, Any] | None,
    shadow_monitor: dict[str, Any] | None,
    observation_freshness: dict[str, Any] | None,
    observation_quality: dict[str, Any] | None,
    trade_permission: dict[str, Any] | None,
    retirement_window: dict[str, Any] | None,
    accepted_observation_days: int,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []

    def add(
        key: str,
        priority: str,
        status: str,
        title: str,
        detail: str,
        owner: str = "system",
    ) -> None:
        actions.append({
            "key": key,
            "priority": priority,
            "status": status,
            "title": title,
            "detail": detail,
            "owner": owner,
        })

    smoke_checks = (pretrade_smoke or {}).get("checks") or {}
    paper_checks = (current_paper or {}).get("checks") or {}
    scheduler_checks = (observation_scheduler or {}).get("checks") or {}
    monitor_checks = (shadow_monitor or {}).get("checks") or {}
    freshness_checks = (observation_freshness or {}).get("checks") or {}
    quality_checks = (observation_quality or {}).get("checks") or {}
    permission_checks = (trade_permission or {}).get("checks") or {}
    retirement_checks = (retirement_window or {}).get("checks") or {}
    historical_checks = (historical_parity or {}).get("checks") or {}

    if not bool((historical_parity or {}).get("ok")):
        add(
            "rebuild_full_logic_historical_backtest",
            "critical",
            "open",
            "重跑完整买入逻辑历史回测",
            (
                "当前历史收益只证明核心路由与风控退出，尚未证明完整实盘买入 gate；"
                f"缺失字段={historical_checks.get('missing_by_artifact')}, blockers={(historical_parity or {}).get('blockers')}"
            ),
        )

    if not bool((pretrade_smoke or {}).get("ok")):
        add(
            "refresh_pretrade_smoke",
            "critical",
            "open",
            "刷新盘前/盘中烟测",
            (
                f"当前烟测 verdict={smoke_checks.get('verdict')}, "
                f"age_hours={smoke_checks.get('age_hours')}, blockers={(pretrade_smoke or {}).get('blockers')}"
            ),
        )

    if int(paper_checks.get("missing_paper_count") or 0) > 0 or not bool((current_paper or {}).get("ok")):
        add(
            "record_paper_execution",
            "critical",
            "open",
            "补齐合格票纸面成交",
            (
                f"合格票 {paper_checks.get('qualified_ticket_count')} 张，"
                f"已复现 {paper_checks.get('matched_paper_count')} 张，"
                f"缺失 {paper_checks.get('missing_paper_count')} 张。"
            ),
        )

    if not bool((observation_scheduler or {}).get("ok")):
        add(
            "fix_observation_scheduler",
            "high",
            "open",
            "修复每日观察调度",
            (
                f"enabled={scheduler_checks.get('enabled')}, wired={scheduler_checks.get('scheduler_wired')}, "
                f"next_run={scheduler_checks.get('next_run_time')}, blockers={(observation_scheduler or {}).get('blockers')}"
            ),
        )

    if not bool((shadow_monitor or {}).get("ok")):
        add(
            "fix_shadow_monitor",
            "high",
            "open",
            "修复影子监控链路",
            (
                f"enabled={monitor_checks.get('enabled')}, wired={monitor_checks.get('scheduler_wired')}, "
                f"email_ok={monitor_checks.get('email_config_ok')}, blockers={(shadow_monitor or {}).get('blockers')}"
            ),
        )

    if not bool((observation_freshness or {}).get("ok")):
        add(
            "record_today_observation",
            "high",
            "open",
            "补记最新观察日",
            (
                f"latest_accepted={freshness_checks.get('latest_accepted_observation_date')}, "
                f"workflow_entry={freshness_checks.get('workflow_entry_date')}, "
                f"blockers={(observation_freshness or {}).get('blockers')}"
            ),
        )

    if not bool((observation_quality or {}).get("ok")):
        add(
            "review_observation_blockers",
            "high",
            "open",
            "复核观察质量阻塞",
            (
                f"accepted_rate={quality_checks.get('accepted_rate')}, "
                f"blocked={quality_checks.get('blocked_count')}, "
                f"consecutive_blocked={quality_checks.get('consecutive_blocked_count')}, "
                f"blockers={(observation_quality or {}).get('blockers')}"
            ),
            owner="operator",
        )

    if not bool((trade_permission or {}).get("ok")):
        add(
            "pause_new_buy_and_review_risk",
            "critical",
            "open",
            "暂停新增买入并复核风控",
            (
                f"decision={(trade_permission or {}).get('decision')}, "
                f"risk_action={permission_checks.get('account_risk_action')}, "
                f"route_health_ok={permission_checks.get('route_health_ok')}, "
                f"blockers={(trade_permission or {}).get('blockers')}"
            ),
            owner="operator",
        )

    remaining = max(0, 30 - int(accepted_observation_days or 0))
    if can_replace:
        add(
            "start_g2_retirement_window",
            "normal",
            "ready",
            "启动 G2 退出复核窗口",
            "G3 替代硬门槛已全部通过，可以进入最终人工复核与 G2 降权/退出安排。",
            owner="operator",
        )
    elif any(item.get("name") == "live_shadow_observation" for item in blocking):
        add(
            "continue_observation_accumulation",
            "normal",
            "in_progress",
            "继续累积影子观察",
            (
                f"当前已接受 {accepted_observation_days}/30 个观察日，剩余 {remaining} 天；"
                f"最早复核日 {retirement_checks.get('earliest_g2_retirement_review_date') or '--'}。"
            ),
        )

    add(
        "keep_g2_fallback",
        "normal",
        "active" if not can_replace else "review",
        "保留 G2 作为 fallback",
        "在 G3 替代门槛完全通过并完成人工退出复核前，G2 不删除、不退出历史舞台。",
    )
    return actions


def _build_replacement_assessment(historical: dict[str, Any] | None = None) -> dict[str, Any]:
    workflow = _build_workflow_status()
    historical = historical or _read_historical_trades(
        limit=500,
        route="all",
        window="all",
        sort_by="entry_date",
        sort_order="desc",
    )
    metrics = historical.get("metrics") if isinstance(historical, dict) else {}
    exposure = historical.get("exposure_metrics") if isinstance(historical, dict) else {}
    future_leak = historical.get("future_leak_audit") if isinstance(historical, dict) else {}
    recent_replay = _recent_replay_audit()
    historical_parity = _historical_buy_logic_parity_audit()
    gate_layer_policy = _gate_layer_policy_audit()
    pretrade_smoke = _pretrade_smoke_audit(workflow)
    current_paper = _current_paper_execution_audit()
    observation_scheduler = _observation_scheduler_audit()
    shadow_monitor = _shadow_monitor_audit()
    artifacts = historical.get("artifacts") if isinstance(historical, dict) else {}
    profile = _latest_profile()
    windows = _latest_window_metrics()
    parity = workflow.get("g2_parity") if isinstance(workflow, dict) else {}
    monitor = workflow.get("monitor") if isinstance(workflow, dict) else {}
    runtime = _load_current_runtime(limit=500)
    trade_permission = _strategy_trade_permission_audit(runtime)
    runtime_ledger = runtime.get("ledger") if isinstance(runtime, dict) else []
    live_shadow_dates = sorted({
        str(item.get("entry_date") or "")[:10]
        for item in runtime_ledger
        if isinstance(item, dict) and str(item.get("entry_date") or "").strip()
    })
    live_shadow_days = len(live_shadow_dates)
    observation_rows = _load_observation_snapshots()
    accepted_observation_dates = _accepted_observation_dates(observation_rows)
    accepted_observation_days = len(accepted_observation_dates)
    observation_freshness = _observation_freshness_audit(workflow, observation_rows, accepted_observation_dates)
    observation_quality = _observation_quality_audit(observation_rows)
    retirement_window = _retirement_window_audit(accepted_observation_dates)

    closed_count = int((metrics or {}).get("closed_trade_count") or 0)
    open_shadow_count = int((metrics or {}).get("open_shadow_count") or 0)
    total_return = _as_float((profile or {}).get("total_return"))
    max_drawdown = _as_float((profile or {}).get("max_drawdown"))
    win_rate = _as_float((profile or {}).get("win_rate"))
    avg_active_exposure = _as_float((exposure or {}).get("avg_exposure_active_days"), 0.0) or 0.0
    active_days = int((exposure or {}).get("active_days") or 0)
    route_metrics = historical.get("route_metrics") if isinstance(historical, dict) else []
    route_count = len(route_metrics or [])
    weak_windows = [
        item for item in windows
        if _as_float(item.get("return"), 0.0) is not None and (_as_float(item.get("return"), 0.0) or 0.0) <= 0
    ]

    gates = [
        _gate(
            "current_workflow",
            bool(workflow.get("ok")),
            "pass" if workflow.get("ok") else "blocked",
            workflow.get("message") or "workflow status unavailable",
        ),
        _gate(
            "runtime_artifacts",
            all((artifacts.get(key) or {}).get("exists") for key in ("closed_trades", "equity_curve")),
            "pass" if all((artifacts.get(key) or {}).get("exists") for key in ("closed_trades", "equity_curve")) else "blocked",
            "closed trades and equity curve artifacts must exist",
        ),
        _gate(
            "historical_sample",
            closed_count >= 150,
            "pass" if closed_count >= 150 else "warn",
            f"closed_trade_count={closed_count}, required>=150",
            blocking=False,
        ),
        _gate(
            "historical_return",
            total_return is not None and total_return > 0,
            "pass" if total_return is not None and total_return > 0 else "blocked",
            f"total_return={total_return}, win_rate={win_rate}, max_drawdown={max_drawdown}",
        ),
        _gate(
            "drawdown_contract",
            max_drawdown is not None and max_drawdown >= -0.25,
            "pass" if max_drawdown is not None and max_drawdown >= -0.25 else "blocked",
            f"max_drawdown={max_drawdown}, required>=-0.25",
        ),
        _gate(
            "future_leak_audit",
            not bool((future_leak or {}).get("has_high_risk_issue")),
            "pass" if not bool((future_leak or {}).get("has_high_risk_issue")) else "blocked",
            (future_leak or {}).get("verdict") or "future leak audit unavailable",
        ),
        _gate(
            "recent_blind_replay",
            bool((recent_replay or {}).get("ok")),
            "pass" if (recent_replay or {}).get("ok") else "blocked",
            (
                f"recent_trade_count={((recent_replay or {}).get('checks') or {}).get('recent_trade_count')}, "
                f"entry_days={((recent_replay or {}).get('checks') or {}).get('recent_entry_day_count')}, "
                f"max_account_loss_pct={((recent_replay or {}).get('checks') or {}).get('max_account_loss_pct')}, "
                f"blockers={(recent_replay or {}).get('blockers')}"
            ),
        ),
        _gate(
            "historical_buy_logic_parity",
            bool((historical_parity or {}).get("ok")),
            "pass" if (historical_parity or {}).get("ok") else "blocked",
            (
                f"scope={((historical_parity or {}).get('checks') or {}).get('historical_backtest_scope')}; "
                f"missing={((historical_parity or {}).get('checks') or {}).get('missing_by_artifact')}; "
                f"blockers={(historical_parity or {}).get('blockers')}"
            ),
        ),
        _gate(
            "gate_layer_policy_audit",
            bool((gate_layer_policy or {}).get("ok")),
            "pass" if (gate_layer_policy or {}).get("ok") else "warn",
            (
                f"recommendation={((gate_layer_policy or {}).get('checks') or {}).get('recommendation')}; "
                f"soft50_retention={((gate_layer_policy or {}).get('checks') or {}).get('soft50_retention')}; "
                f"hard_retention={((gate_layer_policy or {}).get('checks') or {}).get('hard_block_retention')}; "
                f"blockers={(gate_layer_policy or {}).get('blockers')}"
            ),
            blocking=False,
        ),
        _gate(
            "pretrade_smoke_current",
            bool((pretrade_smoke or {}).get("ok")),
            "pass" if (pretrade_smoke or {}).get("ok") else "blocked",
            (
                f"verdict={((pretrade_smoke or {}).get('checks') or {}).get('verdict')}, "
                f"age_hours={((pretrade_smoke or {}).get('checks') or {}).get('age_hours')}, "
                f"qualified_tickets={((pretrade_smoke or {}).get('checks') or {}).get('qualified_ticket_count')}, "
                f"runtime_qualified={((pretrade_smoke or {}).get('checks') or {}).get('runtime_qualified_ticket_count')}, "
                f"blockers={(pretrade_smoke or {}).get('blockers')}"
            ),
        ),
        _gate(
            "current_paper_execution",
            bool((current_paper or {}).get("ok")),
            "pass" if (current_paper or {}).get("ok") else "blocked",
            (
                f"qualified_tickets={((current_paper or {}).get('checks') or {}).get('qualified_ticket_count')}, "
                f"matched_paper={((current_paper or {}).get('checks') or {}).get('matched_paper_count')}, "
                f"missing_paper={((current_paper or {}).get('checks') or {}).get('missing_paper_count')}, "
                f"blockers={(current_paper or {}).get('blockers')}"
            ),
        ),
        _gate(
            "observation_scheduler_active",
            bool((observation_scheduler or {}).get("ok")),
            "pass" if (observation_scheduler or {}).get("ok") else "blocked",
            (
                f"enabled={((observation_scheduler or {}).get('checks') or {}).get('enabled')}, "
                f"wired={((observation_scheduler or {}).get('checks') or {}).get('scheduler_wired')}, "
                f"next_run_time={((observation_scheduler or {}).get('checks') or {}).get('next_run_time')}, "
                f"last_success_at={((observation_scheduler or {}).get('checks') or {}).get('last_success_at')}, "
                f"blockers={(observation_scheduler or {}).get('blockers')}"
            ),
        ),
        _gate(
            "shadow_monitor_active",
            bool((shadow_monitor or {}).get("ok")),
            "pass" if (shadow_monitor or {}).get("ok") else "blocked",
            (
                f"enabled={((shadow_monitor or {}).get('checks') or {}).get('enabled')}, "
                f"wired={((shadow_monitor or {}).get('checks') or {}).get('scheduler_wired')}, "
                f"next_run_time={((shadow_monitor or {}).get('checks') or {}).get('next_run_time')}, "
                f"email_ok={((shadow_monitor or {}).get('checks') or {}).get('email_config_ok')}, "
                f"last_success_at={((shadow_monitor or {}).get('checks') or {}).get('last_success_at')}, "
                f"blockers={(shadow_monitor or {}).get('blockers')}"
            ),
        ),
        _gate(
            "latest_observation_fresh",
            bool((observation_freshness or {}).get("ok")),
            "pass" if (observation_freshness or {}).get("ok") else "blocked",
            (
                f"latest_accepted={((observation_freshness or {}).get('checks') or {}).get('latest_accepted_observation_date')}, "
                f"workflow_entry={((observation_freshness or {}).get('checks') or {}).get('workflow_entry_date')}, "
                f"contract={((observation_freshness or {}).get('checks') or {}).get('latest_contract')}, "
                f"blockers={(observation_freshness or {}).get('blockers')}"
            ),
        ),
        _gate(
            "observation_quality",
            bool((observation_quality or {}).get("ok")),
            "pass" if (observation_quality or {}).get("ok") else "blocked",
            (
                f"accepted_rate={((observation_quality or {}).get('checks') or {}).get('accepted_rate')}, "
                f"blocked_count={((observation_quality or {}).get('checks') or {}).get('blocked_count')}, "
                f"operator_action_required={((observation_quality or {}).get('checks') or {}).get('operator_action_required_count')}, "
                f"consecutive_blocked={((observation_quality or {}).get('checks') or {}).get('consecutive_blocked_count')}, "
                f"blockers={(observation_quality or {}).get('blockers')}"
            ),
        ),
        _gate(
            "g2_retirement_window_scheduled",
            bool((retirement_window or {}).get("ok")),
            "pass" if (retirement_window or {}).get("ok") else "blocked",
            (
                f"earliest_review={((retirement_window or {}).get('checks') or {}).get('earliest_g2_retirement_review_date')}, "
                f"remaining={((retirement_window or {}).get('checks') or {}).get('remaining_observation_days')}, "
                f"progress={((retirement_window or {}).get('checks') or {}).get('progress')}, "
                f"source={((retirement_window or {}).get('checks') or {}).get('calendar_source')}, "
                f"blockers={(retirement_window or {}).get('blockers')}"
            ),
        ),
        _gate(
            "strategy_trade_permission",
            bool((trade_permission or {}).get("ok")),
            "pass" if (trade_permission or {}).get("ok") else "blocked",
            (
                f"decision={(trade_permission or {}).get('decision')}, "
                f"risk_action={((trade_permission or {}).get('checks') or {}).get('account_risk_action')}, "
                f"position_scale={((trade_permission or {}).get('checks') or {}).get('position_scale')}, "
                f"route_health_ok={((trade_permission or {}).get('checks') or {}).get('route_health_ok')}, "
                f"blockers={(trade_permission or {}).get('blockers')}"
            ),
        ),
        _gate(
            "live_shadow_observation",
            accepted_observation_days >= 30,
            "pass" if accepted_observation_days >= 30 else "blocked",
            (
                f"accepted_observation_days={accepted_observation_days}, "
                f"live_shadow_ticket_days={live_shadow_days}, "
                f"latest_accepted_dates={accepted_observation_dates[-5:]}, "
                "required>=30 accepted observations after current contract switch"
            ),
        ),
        _gate(
            "route_coverage",
            route_count >= 2,
            "pass" if route_count >= 2 else "warn",
            f"route_count={route_count}; G3 should not rely on a single market style",
            blocking=False,
        ),
        _gate(
            "window_stability",
            not weak_windows,
            "pass" if not weak_windows else "warn",
            f"non_positive_windows={[item.get('window') for item in weak_windows]}",
            blocking=False,
        ),
        _gate(
            "g2_parity_runtime",
            all(bool(parity.get(key)) for key in (
                "workflow_status",
                "manual_refresh_task",
                "shadow_monitor_state",
                "candidate_ledger",
                "verification",
                "email_alert",
                "scheduler_wired",
                "observation_scheduler_wired",
                "paper_execution_path",
            )),
            "pass" if all(bool(parity.get(key)) for key in (
                "workflow_status",
                "manual_refresh_task",
                "shadow_monitor_state",
                "candidate_ledger",
                "verification",
                "email_alert",
                "scheduler_wired",
                "observation_scheduler_wired",
                "paper_execution_path",
            )) else "blocked",
            f"g2_parity={parity}",
        ),
        _gate(
            "execution_path",
            bool(parity.get("paper_execution_path")),
            "pass" if parity.get("paper_execution_path") else "blocked",
            "G3 has a paper execution ledger; formal/auto order remains locked.",
        ),
        _gate(
            "formal_order_guardrail",
            not bool(parity.get("formal_order_path")),
            "pass",
            "Formal and automatic order routing must remain disabled until a separate promotion audit.",
            blocking=False,
        ),
    ]

    blocking = [item for item in gates if item.get("blocking") and not item.get("ok")]
    warnings = [item for item in gates if not item.get("blocking") and not item.get("ok")]
    can_replace = not blocking
    verdict = "ready_to_replace_g2" if can_replace else "not_ready_to_replace_g2"
    daily_action_plan = _build_daily_action_plan(
        can_replace=can_replace,
        blocking=blocking,
        historical_parity=historical_parity,
        pretrade_smoke=pretrade_smoke,
        current_paper=current_paper,
        observation_scheduler=observation_scheduler,
        shadow_monitor=shadow_monitor,
        observation_freshness=observation_freshness,
        observation_quality=observation_quality,
        trade_permission=trade_permission,
        retirement_window=retirement_window,
        accepted_observation_days=accepted_observation_days,
    )
    assessment = {
        "ok": True,
        "checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "strategy_id": STRATEGY_ID,
        "contract": LATEST_G3_PROFILE,
        "verdict": verdict,
        "g2_can_exit_now": can_replace,
        "recommended_action": "keep_g2_as_fallback" if blocking else "start_g2_retirement_window",
        "summary": {
            "blocking_count": len(blocking),
            "warning_count": len(warnings),
            "next_primary_action": (daily_action_plan[0] or {}).get("title") if daily_action_plan else None,
            "g2_fallback_required": not can_replace,
            "daily_action_count": len(daily_action_plan),
            "closed_trade_count": closed_count,
            "open_shadow_count": open_shadow_count,
            "active_days": active_days,
            "accepted_observation_days": accepted_observation_days,
            "accepted_observation_days_remaining": max(0, 30 - accepted_observation_days),
            "live_shadow_ticket_days": live_shadow_days,
            "live_shadow_days": live_shadow_days,
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "win_rate": win_rate,
            "avg_exposure_active_days": avg_active_exposure,
            "recent_replay_ok": bool((recent_replay or {}).get("ok")),
            "recent_replay_trade_count": int(((recent_replay or {}).get("checks") or {}).get("recent_trade_count") or 0),
            "historical_buy_logic_parity_ok": bool((historical_parity or {}).get("ok")),
            "historical_buy_logic_missing": ((historical_parity or {}).get("checks") or {}).get("missing_by_artifact"),
            "gate_layer_policy_ok": bool((gate_layer_policy or {}).get("ok")),
            "gate_layer_recommendation": ((gate_layer_policy or {}).get("checks") or {}).get("recommendation"),
            "gate_layer_soft50_retention": ((gate_layer_policy or {}).get("checks") or {}).get("soft50_retention"),
            "gate_layer_hard_retention": ((gate_layer_policy or {}).get("checks") or {}).get("hard_block_retention"),
            "pretrade_smoke_ok": bool((pretrade_smoke or {}).get("ok")),
            "pretrade_smoke_age_hours": ((pretrade_smoke or {}).get("checks") or {}).get("age_hours"),
            "current_paper_ok": bool((current_paper or {}).get("ok")),
            "current_paper_missing_count": int(((current_paper or {}).get("checks") or {}).get("missing_paper_count") or 0),
            "observation_scheduler_ok": bool((observation_scheduler or {}).get("ok")),
            "observation_next_run_time": ((observation_scheduler or {}).get("checks") or {}).get("next_run_time"),
            "observation_fresh_ok": bool((observation_freshness or {}).get("ok")),
            "latest_accepted_observation_date": ((observation_freshness or {}).get("checks") or {}).get("latest_accepted_observation_date"),
            "observation_quality_ok": bool((observation_quality or {}).get("ok")),
            "observation_acceptance_rate": ((observation_quality or {}).get("checks") or {}).get("accepted_rate"),
            "observation_consecutive_blocked_count": ((observation_quality or {}).get("checks") or {}).get("consecutive_blocked_count"),
            "g2_retirement_earliest_review_date": ((retirement_window or {}).get("checks") or {}).get("earliest_g2_retirement_review_date"),
            "g2_retirement_progress": ((retirement_window or {}).get("checks") or {}).get("progress"),
            "g2_retirement_calendar_source": ((retirement_window or {}).get("checks") or {}).get("calendar_source"),
            "trade_permission_ok": bool((trade_permission or {}).get("ok")),
            "trade_permission_decision": (trade_permission or {}).get("decision"),
            "account_risk_action": ((trade_permission or {}).get("checks") or {}).get("account_risk_action"),
            "route_health_ok": ((trade_permission or {}).get("checks") or {}).get("route_health_ok"),
            "shadow_monitor_ok": bool((shadow_monitor or {}).get("ok")),
            "shadow_monitor_next_run_time": ((shadow_monitor or {}).get("checks") or {}).get("next_run_time"),
            "shadow_monitor_email_ok": bool(((shadow_monitor or {}).get("checks") or {}).get("email_config_ok")),
            "selected_route": workflow.get("selected_route"),
            "monitor_enabled": bool(monitor.get("enabled")),
            "scheduler_wired": bool(monitor.get("scheduler_wired")),
        },
        "gates": gates,
        "blocking_gates": blocking,
        "warnings": warnings,
        "daily_action_plan": daily_action_plan,
        "next_convergence_steps": [
            "Keep the G3 paper execution ledger reproducible for every qualified ticket.",
            "Accumulate at least 30 accepted observation days after the 2-slot contract switch.",
            "Keep future-leak audit green after every rebuild.",
            "Only then start a G2 retirement window instead of deleting G2 immediately.",
        ],
        "evidence": {
            "workflow": {
                "ok": workflow.get("ok"),
                "entry_date": workflow.get("entry_date"),
                "decision_date": workflow.get("decision_date"),
                "diagnosis_code": workflow.get("diagnosis_code"),
                "g2_parity": parity,
                "accepted_observation_dates": accepted_observation_dates[-30:],
                "live_shadow_dates": live_shadow_dates[-30:],
            },
            "artifacts": artifacts,
            "future_leak_audit": future_leak,
            "recent_replay_audit": recent_replay,
            "historical_buy_logic_parity_audit": historical_parity,
            "gate_layer_policy_audit": gate_layer_policy,
            "pretrade_smoke_audit": pretrade_smoke,
            "current_paper_execution_audit": current_paper,
            "observation_scheduler_audit": observation_scheduler,
            "observation_freshness_audit": observation_freshness,
            "observation_quality_audit": observation_quality,
            "g2_retirement_window_audit": retirement_window,
            "strategy_trade_permission_audit": trade_permission,
            "shadow_monitor_audit": shadow_monitor,
            "exposure_metrics": exposure,
            "window_metrics": windows,
        },
    }
    _write_json(REPLACEMENT_ASSESSMENT_PATH, assessment)
    return assessment


def _run_state_router_refresh(entry_date: str | None = None) -> dict[str, Any]:
    cmd = [sys.executable, str(STATE_ROUTER_SCRIPT)]
    if entry_date:
        cmd.extend(["--entry-date", entry_date])
    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        return {
            "ran": True,
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout_tail": result.stdout[-4000:],
            "stderr_tail": result.stderr[-4000:],
        }
    except Exception as exc:
        logger.exception("G3 State Alpha refresh failed")
        return {
            "ran": True,
            "ok": False,
            "returncode": None,
            "error": str(exc),
        }


def _default_contract() -> dict[str, Any]:
    return {
        "strategy_id": STRATEGY_ID,
        "name": STRATEGY_NAME,
        "name_cn": STRATEGY_NAME_CN,
        "stage": "shadow_current",
        "shadow_trading": {
            "enabled": True,
            "ledger": "runtime/gen3_state_alpha/shadow_ledger.csv",
            "latest_tickets": "runtime/gen3_state_alpha/latest_shadow_tickets.csv",
        },
        "hard_guardrails": {
            "shadow_only": True,
            "observe_only": True,
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "real_order_integration": "disabled",
            "account_risk_gate": "enabled_for_shadow_pretrade",
        },
        "portfolio_contract": {
            "daily_open_limit": 2,
            "position_framework": "2_slots_compound_default",
            "slots": 2,
            "slot_pct": 0.50,
            "trade_strategy_framework": "5_consolidated_trade_strategies",
            "trade_strategy_count": 5,
            "institutional_mainwave_position_pct": 0.50,
            "panic_repair_position_pct": 0.50,
            "old_g3_route_position_pct": 0.50,
            "max_single_name_position_pct": 0.50,
            "final_profile": FINAL_G3_PROFILE,
            "final_profile_name": FINAL_G3_PROFILE_NAME,
            "legacy_base_profile": FINAL_G3_LEGACY_BASE_PROFILE,
            "g2_gap_supplement_position_pct": 0.50,
            "g2_gap_supplement_role": "fill unused G3 slots only; does not replace panic_repair or institutional_mainwave primary routes",
            "same_sector_policy": "allow same sector only when both selected candidates are institutional_mainwave; otherwise skip duplicated sector exposure",
        },
        "trade_strategy_policy": [
            {
                "trade_strategy": "institutional_score120_mainwave",
                "label_cn": "机构主升Score120",
                "source_routes": ["institutional_mainwave"],
                "source_strategies": ["机构主升浪Score120核心"],
                "trading_assumption": "机构主线扩散和 Score120 强确认后的主升延续。",
                "default_position_pct": 0.50,
                "risk_note": "允许主线/板块共振，但高热度和主线退潮时优先降仓或禁开。",
            },
            {
                "trade_strategy": "old_g3_strong_breakout",
                "label_cn": "强势突破",
                "source_routes": ["old_g3_route_v3"],
                "source_strategies": ["强势突破"],
                "trading_assumption": "跨周期结构链路里的强势突破，不等同于 Score120 机构主升。",
                "default_position_pct": 0.50,
                "risk_note": "保留旧G3结构条件和市场适配复盘，不和机构主升混排评分。",
            },
            {
                "trade_strategy": "volume_runup_supplement",
                "label_cn": "量能续强补位",
                "source_routes": ["g2_gap_supplement"],
                "source_strategies": ["G2量能续强+板块加分", "G2量能续强"],
                "trading_assumption": "量能延续候选只补空槽，不替代 G3 主路由。",
                "default_position_pct": 0.50,
                "risk_note": "保留补位身份；无板块加分样本需谨慎观察。",
            },
            {
                "trade_strategy": "range_weak_repair",
                "label_cn": "震荡弱势修复",
                "source_routes": ["old_g3_route_v3", "panic_repair"],
                "source_strategies": ["旧G3弱势/震荡修复", "震荡恐慌修复"],
                "trading_assumption": "非主升、非下跌主杀环境中的弱势/震荡修复；旧G3结构修复与震荡恐慌深洗修复合并为同一正式策略。",
                "entry_contract": "market_style 以 standard_range / weak_rebound 为主；日线要求中短期回撤后出现收盘修复、下影或反包，量能不过冷不过热；震荡恐慌来源额外保留压力释放过滤并优先要求 30m 修复确认。",
                "exit_contract": "-6%结构止损、-10%硬止损、+8%先减半，剩余仓以前低跌破或30m转弱退出。",
                "default_position_pct": 0.20,
                "max_position_pct": 0.25,
                "risk_note": "保留 source_strategy_label 做复盘分层：旧G3来源偏结构修复，震荡恐慌来源偏深洗释放；分层影响仓位和观察等级，不再拆成两个正式交易策略。",
            },
            {
                "trade_strategy": "panic_capitulation_repair",
                "label_cn": "恐慌出清修复",
                "source_routes": ["old_g3_route_v3", "panic_repair"],
                "source_strategies": ["旧G3恐慌修复", "下跌恐慌修复"],
                "trading_assumption": "恐慌扩散或下跌压力释放后的修复介入；旧G3 down_panic 与 panic_repair standard_downtrend 只保留来源差异，交易策略合并。",
                "entry_contract": "日线先满足恐慌出清/压力释放，入场前优先要求 30m 修复确认；旧G3来源若无盘中确认数据，先作为观察票据而非正式买入。",
                "exit_contract": "-5%结构止损、-8%硬止损、+8%先减半，剩余仓以前低跌破或30m转弱退出。",
                "default_position_pct": 0.20,
                "pressure_position_pct": 0.125,
                "risk_note": "和震荡弱势修复同属修复系，但恐慌出清修复保留独立风控；下跌压力态只降首仓，不再拆成另一套交易策略。",
            },
        ],
        "exit_contract": {
            "source": "g2_stop_structure_cooldown_migrated",
            "hard_stop": {
                "type": "m30_hard_stop",
                "loss_pct": 0.12,
                "action": "sell_all",
            },
            "take_profit": {
                "type": "m30_take_profit_partial",
                "profit_pct": 0.12,
                "sell_ratio": 0.50,
            },
            "structure_exit": {
                "type": "previous_day_low_break_after_take_profit",
                "confirm_bar": "30m",
                "action": "sell_remaining",
            },
            "cooldown": {
                "trigger": "hard_stop_30m_count>=2",
                "lookback_trading_days": 20,
                "cooldown_trading_days": 3,
                "scope": "institutional_mainwave_new_buys",
            },
            "risk_limits": {
                "max_single_trade_account_loss_pct": 0.065,
                "max_mtm_drawdown_pct": 0.18,
                "max_consecutive_realized_loss_pct": 0.20,
                "mtm_drawdown_reduce_risk_pct": 0.15,
                "mtm_drawdown_pause_new_buy_pct": 0.18,
                "hard_stop_cooldown_trigger": "last_20_closed_shadow_trades_hard_stop_count>=2",
            },
        },
        "contract_modes": [
            "research_backtest",
            "shadow_current",
            "formal_disabled",
            "risk_audit",
            "evidence_archive",
        ],
        "route_policy": [
            {
                "mode": "panic_repair",
                "label_cn": "恐慌修复",
                "status": "shadow_current",
                "router_eligible": "intraday_confirmed",
                "route_health_observation": "最近 240 天已退出 panic_repair 样本健康度只记录观察，不阻断买入",
            },
            {
                "mode": "institutional_mainwave",
                "label_cn": "机构主升",
                "status": "shadow_current",
                "router_eligible": "score>=120 && sector_diffusion>=65 && 30m_confirmed; index_mom60<=5% uses 50% slot, 5%-10% keeps candidate but reduces slot to 25%, >10% blocks new open",
                "route_health_observation": "最近 240 天已退出 institutional_mainwave 样本健康度只记录观察，不阻断买入",
            },
            {
                "mode": "old_g3_route_v3",
                "label_cn": "旧G3跨周期路由",
                "status": "shadow_current",
                "router_eligible": "g3_chain != range_box_bottom",
                "route_health_observation": "最近 240 天已退出 old_g3_route_v3 样本健康度只记录观察，不阻断买入",
            },
            {
                "mode": "risk_blocked_observation",
                "label_cn": "风险阻断观察",
                "status": "blocked_observe_only",
            },
        ],
    }


def _contract() -> dict[str, Any]:
    contract = (
        _read_json(STATE_ALPHA_RUNTIME_DIR / "latest_strategy_contract.json")
        or _read_json(BLUEPRINT_DIR / "g3_state_alpha_contract.json")
        or _default_contract()
    )
    default = _default_contract()
    default_policies = default.get("trade_strategy_policy", [])
    policies = contract.get("trade_strategy_policy") or []
    if policies:
        policy_by_key = {str(item.get("trade_strategy") or ""): dict(item) for item in default_policies}
        for item in policies:
            key = str(item.get("trade_strategy") or "")
            if key:
                merged = dict(policy_by_key.get(key, {}))
                merged.update(item)
                policy_by_key[key] = merged
        contract["trade_strategy_policy"] = [policy_by_key[str(item.get("trade_strategy") or "")] for item in default_policies if str(item.get("trade_strategy") or "") in policy_by_key]
    else:
        contract["trade_strategy_policy"] = default_policies
    portfolio = contract.setdefault("portfolio_contract", {})
    default_portfolio = default.get("portfolio_contract") or {}
    portfolio.setdefault("trade_strategy_framework", default_portfolio.get("trade_strategy_framework"))
    portfolio.setdefault("trade_strategy_count", default_portfolio.get("trade_strategy_count"))
    return contract


def _wrap_guardrails(payload: dict[str, Any]) -> dict[str, Any]:
    payload["strategy_id"] = STRATEGY_ID
    payload["name"] = STRATEGY_NAME
    payload["name_cn"] = STRATEGY_NAME_CN
    payload["final_profile"] = FINAL_G3_PROFILE
    payload["final_profile_name"] = FINAL_G3_PROFILE_NAME
    payload["legacy_base_profile"] = FINAL_G3_LEGACY_BASE_PROFILE
    payload["stage"] = payload.get("stage") or "shadow_current"
    payload["shadow_only"] = True
    payload["observe_only"] = True
    payload["formal_buy_signal"] = False
    payload["auto_order_allowed"] = False
    payload["order_path_enabled"] = False
    return payload


def _normalize_code6(value: Any) -> str:
    raw = str(value or "").strip().upper()
    hit = re.search(r"\d{6}", raw)
    return hit.group(0) if hit else ""


def _split_table_row(line: str) -> list[str]:
    raw = str(line or "").rstrip("\r\n")
    if not raw.strip():
        return []
    if "\t" in raw:
        return [part.strip() for part in raw.split("\t")]
    return [part.strip() for part in re.split(r"\s{2,}", raw.strip()) if part.strip()]


def _parse_broker_number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _normalize_trade_datetime(date_text: Any, time_text: Any = "") -> str | None:
    d = str(date_text or "").strip()
    t = str(time_text or "").strip()
    if not d:
        return None
    digits = re.sub(r"\D", "", d)
    if len(digits) >= 8:
        d = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    try:
        if t:
            t_digits = re.sub(r"\D", "", t)
            if len(t_digits) >= 6:
                t = f"{t_digits[:2]}:{t_digits[2:4]}:{t_digits[4:6]}"
            elif len(t_digits) >= 4:
                t = f"{t_digits[:2]}:{t_digits[2:4]}:00"
            return pd.Timestamp(f"{d} {t}").strftime("%Y-%m-%d %H:%M:%S")
        return pd.Timestamp(d).strftime("%Y-%m-%d 00:00:00")
    except Exception:
        return None


def _parse_trade_side(action: Any) -> str:
    text = str(action or "").strip().lower()
    if "买" in text or "buy" in text:
        return "BUY"
    if "卖" in text or "sell" in text:
        return "SELL"
    return ""


def _trade_signature(row: dict[str, Any]) -> str:
    return "|".join(
        [
            str(row.get("trade_time") or ""),
            str(row.get("side") or ""),
            str(row.get("code") or ""),
            str(row.get("shares") or ""),
            str(row.get("price") or ""),
        ]
    )


def _parse_broker_trade_text(raw_text: str) -> dict[str, Any]:
    lines = [line.rstrip("\r") for line in str(raw_text or "").splitlines() if str(line).strip()]
    rows = [_split_table_row(line) for line in lines]
    header_idx = -1
    headers: list[str] = []
    for idx, cells in enumerate(rows):
        compact = "|".join(cells)
        if ("成交" in compact and "日期" in compact and "代码" in compact and "操作" in compact):
            header_idx = idx
            headers = cells
            break
    if header_idx < 0:
        return {"ok": False, "message": "未识别到同花顺历史成交表头", "rows": [], "raw_line_count": len(lines)}

    def idx_of(*names: str) -> int:
        for i, header in enumerate(headers):
            text = str(header or "").replace(" ", "")
            if any(name in text for name in names):
                return i
        return -1

    date_idx = idx_of("成交日期", "日期")
    time_idx = idx_of("成交时间", "时间")
    code_idx = idx_of("证券代码", "股票代码", "代码")
    name_idx = idx_of("证券名称", "股票名称", "名称")
    action_idx = idx_of("操作", "买卖")
    shares_idx = idx_of("成交数量", "数量")
    price_idx = idx_of("成交均价", "成交价格", "价格", "均价")
    amount_idx = idx_of("成交金额", "金额")
    remark_idx = idx_of("备注")

    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cells in rows[header_idx + 1 :]:
        if not cells:
            continue
        code = _normalize_code6(cells[code_idx] if 0 <= code_idx < len(cells) else "")
        trade_time = _normalize_trade_datetime(
            cells[date_idx] if 0 <= date_idx < len(cells) else "",
            cells[time_idx] if 0 <= time_idx < len(cells) else "",
        )
        side = _parse_trade_side(cells[action_idx] if 0 <= action_idx < len(cells) else "")
        shares = int(abs(_parse_broker_number(cells[shares_idx] if 0 <= shares_idx < len(cells) else "") or 0))
        price = _parse_broker_number(cells[price_idx] if 0 <= price_idx < len(cells) else "")
        amount = _parse_broker_number(cells[amount_idx] if 0 <= amount_idx < len(cells) else "")
        if not code or not trade_time or side not in {"BUY", "SELL"} or shares <= 0 or not price or price <= 0:
            continue
        row = {
            "trade_time": trade_time,
            "trade_date": trade_time[:10],
            "side": side,
            "side_label": "买入" if side == "BUY" else "卖出",
            "code": code,
            "name": str(cells[name_idx]).strip() if 0 <= name_idx < len(cells) else "",
            "shares": shares,
            "price": round(float(price), 3),
            "amount": round(float(amount), 3) if amount is not None else round(float(price) * shares, 3),
            "remark": str(cells[remark_idx]).strip() if 0 <= remark_idx < len(cells) else "",
            "source": "ths_history_trade",
        }
        sig = _trade_signature(row)
        if sig in seen:
            continue
        seen.add(sig)
        parsed.append(row)
    parsed.sort(key=lambda item: str(item.get("trade_time") or ""), reverse=True)
    return {"ok": True, "rows": parsed, "parsed_count": len(parsed), "raw_line_count": len(lines)}


def _load_broker_state() -> dict[str, Any]:
    state = _read_json(BROKER_STATE_PATH)
    if not state:
        state = {
            "holdings": [],
            "capital": {},
            "broker_trades": [],
            "updated_at": None,
            "trade_updated_at": None,
        }
    return state


def _save_broker_state(state: dict[str, Any]) -> None:
    _write_json(BROKER_STATE_PATH, state)


def _broker_market_value_sum(holdings: list[dict[str, Any]]) -> float | None:
    total = 0.0
    found = False
    for row in holdings:
        if not isinstance(row, dict):
            continue
        market_value = _parse_broker_number(row.get("market_value"))
        shares = int(_parse_broker_number(row.get("shares")) or 0)
        current_price = _parse_broker_number(row.get("current_price"))
        if market_value is None and current_price is not None and shares > 0:
            market_value = current_price * shares
        if market_value is None:
            continue
        total += market_value
        found = True
    return total if found else None


def _normalize_broker_capital(
    capital: dict[str, Any] | None,
    holdings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw = dict(capital or {})
    total_capital = _parse_broker_number(raw.get("total_capital"))
    available_cash = _parse_broker_number(raw.get("available_cash"))
    market_value = _parse_broker_number(raw.get("market_value"))
    cost_value = _parse_broker_number(raw.get("cost_value"))
    holding_market_value = _broker_market_value_sum(holdings or [])
    holdings_stale = False
    if market_value is not None and holding_market_value is not None:
        tolerance = max(abs(market_value) * 0.02, 1.0)
        holdings_stale = abs(market_value - holding_market_value) > tolerance
    if holding_market_value is not None and (market_value is None or market_value <= 0):
        market_value = holding_market_value

    available_cash_derived = False
    if total_capital is None and available_cash is not None and market_value is not None:
        total_capital = available_cash + market_value
    if available_cash is None and total_capital is not None and market_value is not None:
        available_cash = max(total_capital - market_value, 0.0)
        available_cash_derived = True

    normalized = {
        **raw,
        "total_capital": total_capital,
        "available_cash": available_cash,
        "market_value": market_value,
        "cost_value": cost_value,
        "holding_market_value": market_value if holdings_stale and market_value is not None else (holding_market_value if holding_market_value is not None else market_value),
        "holdings_stale": holdings_stale or bool(raw.get("holdings_stale")),
        "available_cash_derived": available_cash_derived or bool(raw.get("available_cash_derived")),
    }
    if available_cash is not None and market_value is not None:
        normalized["available_with_holdings"] = available_cash + market_value
    elif total_capital is not None:
        normalized["available_with_holdings"] = total_capital
    return normalized


def _normalize_broker_holding(row: dict[str, Any], capital: dict[str, Any] | None = None) -> dict[str, Any]:
    code = _normalize_code6(row.get("code"))
    shares = int(_parse_broker_number(row.get("shares")) or 0)
    cost_price = _parse_broker_number(row.get("cost_price"))
    current_price = _parse_broker_number(row.get("current_price"))
    market_value = _parse_broker_number(row.get("market_value"))
    if market_value is None and current_price is not None and shares > 0:
        market_value = current_price * shares
    total_capital = _parse_broker_number((capital or {}).get("total_capital"))
    position_pct = (market_value / total_capital) if total_capital and market_value is not None else None
    return {
        "code": code,
        "name": row.get("name") or code,
        "shares": shares,
        "cost_price": cost_price,
        "current_price": current_price,
        "market_value": market_value,
        "pnl_ratio": _parse_broker_number(row.get("pnl_ratio")),
        "position_pct": position_pct,
        "entry_price": cost_price,
        "reference_close": current_price or cost_price,
        "hard_stop": round(cost_price * 0.88, 3) if cost_price else None,
        "take_profit_1": round(cost_price * 1.12, 3) if cost_price else None,
        "route": "broker_real_position",
        "route_label": "同花顺真实持仓",
        "trade_status": "broker_open",
        "management_action": "按G3合同检查退出",
        "exit_contract": "真实持仓同步自同花顺；默认按G3 12%硬止损、12%先减半、剩余仓前低保护进行管理检查。",
        "source": row.get("source") or "ths_capital_holdings",
    }


def _staleness_minutes(updated_at: Any) -> float | None:
    text = str(updated_at or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        try:
            ts = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return max((datetime.now() - ts).total_seconds() / 60.0, 0.0)


def _recent_sell_shares(code: Any, trades: list[dict[str, Any]] | None, lookback_days: int = 5) -> int:
    code6 = _normalize_code6(code)
    if not code6:
        return 0
    cutoff = datetime.now() - timedelta(days=lookback_days)
    total = 0
    for item in trades or []:
        if not isinstance(item, dict):
            continue
        if _normalize_code6(item.get("code")) != code6:
            continue
        side = str(item.get("side") or item.get("side_label") or "").upper()
        if "SELL" not in side and "卖" not in side:
            continue
        trade_time = str(item.get("trade_time") or item.get("trade_date") or "").strip()
        try:
            ts = datetime.fromisoformat(trade_time[:19])
        except Exception:
            ts = None
        if ts is not None and ts < cutoff:
            continue
        shares = abs(int(_parse_broker_number(item.get("shares")) or 0))
        total += shares
    return total


def _exit_advice_for_position(
    row: dict[str, Any],
    *,
    source: str,
    updated_at: Any = None,
    broker_trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current_price = _as_float(
        row.get("current_price")
        or row.get("latest_price")
        or row.get("last_price")
        or row.get("close")
        or row.get("reference_close"),
        None,
    )
    entry_price = _as_float(row.get("entry_price") or row.get("cost_price") or row.get("execution_price") or row.get("reference_close"), None)
    hard_stop = _as_float(row.get("hard_stop"), None)
    structure_stop = _as_float(row.get("structure_stop") or row.get("previous_low_stop"), None)
    take_profit = _as_float(row.get("take_profit_1"), None)
    shares = int(_as_float(row.get("shares") or row.get("quantity") or row.get("available_shares"), 0.0) or 0)
    recent_sell = _recent_sell_shares(row.get("code") or row.get("code_raw"), broker_trades)
    half_done = _truthy(row.get("half_take_profit_done") or row.get("take_profit_1_done")) or (shares > 0 and recent_sell >= max(shares * 0.4, 100))
    stale_minutes = _staleness_minutes(updated_at or row.get("updated_at") or row.get("created_at"))
    data_fresh = stale_minutes is None or stale_minutes <= 30

    pnl_ratio = None
    if current_price is not None and entry_price is not None and entry_price > 0:
        pnl_ratio = current_price / entry_price - 1.0

    action = "hold_observe"
    action_label = "持有观察"
    reason = "exit_contract_not_triggered"
    reason_label = "未触发退出合同"
    sell_ratio = 0.0
    priority = "normal"
    trigger_price = None

    if current_price is None or current_price <= 0:
        action = "data_missing"
        action_label = "数据不足"
        reason = "missing_current_price"
        reason_label = "缺少最新价，不能判断实时卖出"
        priority = "warning"
    elif hard_stop is not None and hard_stop > 0 and current_price <= hard_stop:
        action = "sell_all"
        action_label = "清仓检查"
        reason = "hard_stop_triggered"
        reason_label = "最新价触发硬止损"
        sell_ratio = 1.0
        priority = "critical"
        trigger_price = hard_stop
    elif structure_stop is not None and structure_stop > 0 and current_price <= structure_stop:
        action = "sell_all"
        action_label = "清仓检查"
        reason = "structure_stop_triggered"
        reason_label = "最新价触发结构保护"
        sell_ratio = 1.0
        priority = "critical"
        trigger_price = structure_stop
    elif take_profit is not None and take_profit > 0 and current_price >= take_profit and not half_done:
        action = "sell_half"
        action_label = "止盈减半"
        reason = "take_profit_1_triggered"
        reason_label = "最新价触发第一止盈位"
        sell_ratio = 0.5
        priority = "take_profit"
        trigger_price = take_profit
    elif take_profit is not None and take_profit > 0 and current_price >= take_profit and half_done:
        action = "protect_remaining"
        action_label = "保护剩余仓"
        reason = "take_profit_done_hold_remaining"
        reason_label = "已满足止盈区间，剩余仓按前低/结构保护"
        priority = "protect"
        trigger_price = structure_stop

    if not data_fresh and action == "hold_observe":
        action = "refresh_before_decision"
        action_label = "先刷新再判断"
        reason = "price_data_stale"
        reason_label = "最新价时间偏旧，卖出前需刷新"
        priority = "warning"

    suggested_quantity = 0
    if shares > 0 and sell_ratio > 0:
        suggested_quantity = int((shares * sell_ratio) // 100) * 100
        if suggested_quantity <= 0 and shares > 0:
            suggested_quantity = shares

    return {
        "exit_action": action,
        "exit_action_label": action_label,
        "exit_reason": reason,
        "exit_reason_label": reason_label,
        "exit_priority": priority,
        "suggested_sell_ratio": sell_ratio,
        "suggested_sell_quantity": suggested_quantity,
        "current_price_for_exit": current_price,
        "entry_price_for_exit": entry_price,
        "pnl_ratio_for_exit": pnl_ratio,
        "hard_stop_for_exit": hard_stop,
        "structure_stop_for_exit": structure_stop,
        "take_profit_1_for_exit": take_profit,
        "trigger_price": trigger_price,
        "recent_sell_shares": recent_sell,
        "half_take_profit_done": bool(half_done),
        "price_stale_minutes": stale_minutes,
        "price_data_fresh": bool(data_fresh),
        "exit_advice_source": source,
        "exit_checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def _attach_exit_advice(
    rows: list[dict[str, Any]],
    *,
    source: str,
    updated_at: Any = None,
    broker_trades: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item.update(_exit_advice_for_position(item, source=source, updated_at=updated_at, broker_trades=broker_trades))
        item["management_action"] = item.get("exit_action_label") or item.get("management_action")
        out.append(item)
    return out


def _exit_advice_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    action_counts: dict[str, int] = {}
    urgent = 0
    for row in rows or []:
        action = str(row.get("exit_action") or "unknown")
        action_counts[action] = action_counts.get(action, 0) + 1
        if action in {"sell_all", "sell_half"}:
            urgent += 1
    return {
        "rows": len(rows or []),
        "urgent_exit_rows": urgent,
        "action_counts": action_counts,
        "checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }


def _broker_snapshot() -> dict[str, Any]:
    state = _load_broker_state()
    broker_trades = state.get("broker_trades") or []
    holdings = _attach_exit_advice(
        state.get("holdings") or [],
        source="broker_real_position",
        updated_at=state.get("updated_at"),
        broker_trades=broker_trades,
    )
    capital = _normalize_broker_capital(state.get("capital") or {}, holdings)
    return {
        "ok": True,
        "mode": "g3_state_alpha_broker_snapshot",
        "holdings": holdings,
        "capital": capital,
        "broker_trades": broker_trades,
        "exit_advice_summary": _exit_advice_summary(holdings),
        "updated_at": state.get("updated_at"),
        "trade_updated_at": state.get("trade_updated_at"),
        "artifacts": {"broker_state": _path_status(BROKER_STATE_PATH)},
    }


@router.get("/contract")
async def get_gen3_state_alpha_contract() -> dict[str, Any]:
    contract = _contract()
    return _wrap_guardrails(
        {
            "ok": True,
            "contract": contract,
            "artifacts": {
                "runtime_contract": _path_status(STATE_ALPHA_RUNTIME_DIR / "latest_strategy_contract.json"),
                "blueprint_contract": _path_status(BLUEPRINT_DIR / "g3_state_alpha_contract.json"),
                "blueprint_report": _path_status(BLUEPRINT_DIR / "REPORT_CN.md"),
            },
        }
    )


@router.get("/current")
async def get_gen3_state_alpha_current(
    limit: int = Query(default=30, ge=1, le=200, description="Maximum candidate rows returned."),
    refresh: bool = Query(default=False, description="Run the current G3 State Alpha shadow refresh before reading artifacts."),
    entry_date: str | None = Query(default=None, description="Optional entry date for refresh, e.g. 2026-06-18."),
) -> dict[str, Any]:
    refresh_result = _run_state_router_refresh(entry_date.strip() if isinstance(entry_date, str) and entry_date.strip() else None) if refresh else {
        "ran": False,
        "ok": None,
        "returncode": None,
    }
    summary_path = STATE_ROUTER_RUNTIME_DIR / "latest_summary.json"
    selected_path = STATE_ROUTER_RUNTIME_DIR / "latest_candidates.csv"
    all_path = STATE_ROUTER_RUNTIME_DIR / "latest_all_source_candidates.csv"
    diagnostics_path = STATE_ROUTER_RUNTIME_DIR / "latest_route_diagnostics.csv"
    router_contract_path = STATE_ROUTER_RUNTIME_DIR / "latest_strategy_contract.json"
    alpha_summary_path = STATE_ALPHA_RUNTIME_DIR / "latest_summary.json"
    tickets_path = STATE_ALPHA_RUNTIME_DIR / "latest_shadow_tickets.csv"
    afterhours_summary_path = STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_summary.json"
    afterhours_tickets_path = STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_shadow_tickets.csv"
    afterhours_contract_path = STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_strategy_contract.json"
    ledger_path = STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"

    summary = _read_json(alpha_summary_path) or _read_json(summary_path)
    afterhours_summary = _read_json(afterhours_summary_path)
    if not summary:
        summary = {
            "diagnosis_code": "NO_G3_STATE_ALPHA_CURRENT_RUNTIME",
            "diagnosis": "未找到 G3 State Alpha 当前影子产物；需要先运行 state-router shadow 更新。",
            "selected_rows": 0,
            "source_rows": 0,
            "auto_order_allowed_rows": 0,
            "formal_buy_signal_rows": 0,
            "order_path_enabled_rows": 0,
        }
    if isinstance(summary, dict):
        summary = dict(summary)
        summary["g2_gap_supplement_status"] = _extract_g2_gap_supplement_status(summary)
    tickets = _enrich_trade_strategy_records(_read_csv_records(tickets_path, limit=limit))
    afterhours_tickets = _enrich_trade_strategy_records(_read_csv_records(afterhours_tickets_path, limit=limit))
    broker_snapshot = _broker_snapshot()
    broker_trades = broker_snapshot.get("broker_trades") if isinstance(broker_snapshot.get("broker_trades"), list) else []
    ledger = _attach_exit_advice(
        _enrich_trade_strategy_records(_read_shadow_ledger_records(ledger_path, limit=limit)),
        source="shadow_ledger",
        updated_at=summary.get("generated_at") if isinstance(summary, dict) else None,
        broker_trades=broker_trades,
    )
    selected_candidates = _enrich_trade_strategy_records(_read_csv_records(selected_path, limit=limit))
    all_source_candidates = _enrich_trade_strategy_records(_read_csv_records(all_path, limit=limit))
    route_diagnostics = _read_csv_records(diagnostics_path, limit=50)
    blocked_candidates = [
        item for item in all_source_candidates
        if not _truthy(item.get("router_eligible")) or item.get("block_reason")
    ][:limit]
    qualified_tickets = [item for item in tickets if _truthy(item.get("qualified_shadow_buy"))]
    next_trade_buy_tickets = _filter_next_trade_buy_tickets(afterhours_tickets, summary, tickets)
    date_display_analysis = _build_date_display_analysis(
        summary,
        tickets,
        afterhours_summary,
        afterhours_tickets,
        next_trade_buy_tickets,
    )
    real_exit_rows = broker_snapshot.get("holdings") if isinstance(broker_snapshot.get("holdings"), list) else []
    exit_rows = [*real_exit_rows, *ledger]

    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_qualified_shadow_trade",
            "summary": summary,
            "shadow_tickets": tickets,
            "qualified_tickets": qualified_tickets,
            "afterhours_summary": afterhours_summary,
            "afterhours_shadow_tickets": afterhours_tickets,
            "next_trade_buy_summary": afterhours_summary or {},
            "next_trade_buy_tickets": next_trade_buy_tickets,
            "date_display_analysis": date_display_analysis,
            "selected_ticket": _first_record(qualified_tickets) or _first_record(tickets),
            "shadow_ledger": ledger,
            "exit_management": {
                "real_holdings": real_exit_rows,
                "shadow_holdings": ledger,
                "summary": _exit_advice_summary(exit_rows),
                "contract": "realtime_exit_advice_v1",
                "formal_order_status": "dry_run_or_manual_only",
            },
            "selected_candidates": selected_candidates,
            "all_source_candidates": all_source_candidates,
            "blocked_candidates": blocked_candidates,
            "route_diagnostics": route_diagnostics,
            "pipeline": _build_pipeline(summary, tickets, route_diagnostics),
            "readiness_checks": _build_readiness(summary, tickets, ledger),
            "backtest_snapshot": _read_backtest_snapshot(),
            "strategy_contract": _contract(),
            "legacy_router_contract": _read_json(router_contract_path),
            "refresh": refresh_result,
            "artifacts": {
                "state_alpha_runtime_dir": _path_status(STATE_ALPHA_RUNTIME_DIR),
                "state_router_runtime_dir": _path_status(STATE_ROUTER_RUNTIME_DIR),
                "state_router_report_dir": _path_status(STATE_ROUTER_REPORT_DIR),
                "state_alpha_summary": _path_status(alpha_summary_path),
                "latest_shadow_tickets": _path_status(tickets_path),
                "afterhours_state_alpha_summary": _path_status(afterhours_summary_path),
                "afterhours_shadow_tickets": _path_status(afterhours_tickets_path),
                "afterhours_strategy_contract": _path_status(afterhours_contract_path),
                "shadow_ledger": _path_status(ledger_path),
                "summary": _path_status(summary_path),
                "selected_candidates": _path_status(selected_path),
                "all_source_candidates": _path_status(all_path),
                "route_diagnostics": _path_status(diagnostics_path),
                "legacy_router_contract": _path_status(router_contract_path),
            },
        }
    )


@router.get("/mainwave-opportunities")
async def get_gen3_state_alpha_mainwave_opportunities(
    limit: int = Query(default=80, ge=1, le=300, description="Maximum institutional-mainwave candidate rows returned."),
) -> dict[str, Any]:
    source_label, source_path, summary, source_meta = _select_mainwave_summary_source()
    ticket_rows = _read_csv_records(STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_shadow_tickets.csv", limit=limit)
    if not ticket_rows:
        ticket_rows = _read_csv_records(STATE_ALPHA_RUNTIME_DIR / "latest_shadow_tickets.csv", limit=limit)
    candidates = _build_mainwave_candidates(source_meta, ticket_rows, limit=limit)
    sectors = _build_mainwave_sector_opportunities(candidates)
    recommended = [item for item in candidates if item.get("is_recommended")]
    m30_ok_count = len([item for item in candidates if item.get("m30_confirmed") or str(item.get("m30_status") or "") == "ok"])
    entry_date = _date_text(summary.get("entry_date") or source_meta.get("entry_date") or (recommended[0].get("entry_date") if recommended else None))
    if recommended:
        entry_date = _date_text(recommended[0].get("entry_date") or entry_date)
    decision_date = _date_text(summary.get("decision_date") or source_meta.get("decision_date") or (recommended[0].get("decision_date") if recommended else None))
    index_mom60 = _to_float_or_none(source_meta.get("index_mom60") or (source_meta.get("index") or {}).get("index_mom60"))
    scan = source_meta.get("scan") if isinstance(source_meta.get("scan"), dict) else {}
    strongest_sector = sectors[0] if sectors else {}

    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_institutional_mainwave_opportunities",
            "strategy": {
                "route": "institutional_mainwave",
                "route_label": "机构主升浪",
                "trade_strategy": "institutional_score120_mainwave",
                "trade_strategy_label": "机构主升Score120",
                "source": "institutional_mainwave_current_builder_v1",
                "source_script": "scripts/gen3_institutional_mainwave_current_v1.py",
                "purpose": "识别机构集体主升、板块扩散和重要行业机会，并为下一交易日候选提供来源。",
                "ranking_basis": "wave_style_score 主升分 + sector_diffusion_score 板块扩散 + 30m确认 + 市场热度降仓规则。",
            },
            "summary": {
                "entry_date": entry_date,
                "decision_date": decision_date,
                "source_label": source_label,
                "source_path": str(source_path) if source_path else "",
                "candidate_count": len(candidates),
                "sector_count": len(sectors),
                "recommended_count": len(recommended),
                "m30_ok_count": m30_ok_count,
                "strong_sector_count": len([item for item in sectors if item.get("state") == "strong_mainwave"]),
                "important_sector_count": len([item for item in sectors if item.get("state") in {"strong_mainwave", "important_industry"}]),
                "strongest_sector": strongest_sector.get("sector_name") or "",
                "strongest_sector_state": strongest_sector.get("state_label") or "",
                "index_mom60": index_mom60,
                "index_heat_label": "高热度降仓" if index_mom60 is not None and index_mom60 > 0.05 else "正常热度",
                "min_score": _to_float_or_none(source_meta.get("min_score")) or 120.0,
                "min_sector_diffusion": _to_float_or_none(source_meta.get("min_sector_diffusion")) or 65.0,
                "max_index_mom60": _to_float_or_none(source_meta.get("max_index_mom60")) or 0.1,
                "scan_target_date": scan.get("target_date") or "",
                "scan_source_mode": scan.get("source_mode") or "",
                "template_pool_rows": _to_int_or_zero(source_meta.get("template_pool_rows")),
                "pre_confirm_rows": _to_int_or_zero(source_meta.get("pre_confirm_rows") or source_meta.get("rows")),
            },
            "sector_opportunities": sectors,
            "candidates": candidates,
            "recommended_tickets": recommended,
            "diagnostics": {
                "source_status": source_meta.get("status") or ("missing" if not source_meta else "unknown"),
                "diagnosis_code": summary.get("diagnosis_code") or "",
                "selected_route": summary.get("selected_route") or "",
                "route_reason": summary.get("route_reason") or "",
                "read_order": [
                    "gen3_state_alpha/afterhours_latest_summary.json",
                    "gen3_state_alpha/latest_summary.json",
                    "gen3_state_router_shadow/latest_summary.json",
                    "gen3_institutional_mainwave_current/latest_summary.json",
                ],
                "empty_reason": "" if candidates else "未找到机构主升预确认或下一交易日票据；请先刷新 G3 状态路由或机构主升当前候选。",
            },
            "artifacts": {
                "state_alpha_afterhours_summary": _path_status(STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_summary.json"),
                "state_alpha_afterhours_tickets": _path_status(STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_shadow_tickets.csv"),
                "state_alpha_summary": _path_status(STATE_ALPHA_RUNTIME_DIR / "latest_summary.json"),
                "state_alpha_tickets": _path_status(STATE_ALPHA_RUNTIME_DIR / "latest_shadow_tickets.csv"),
                "mainwave_runtime_summary": _path_status(MAINWAVE_RUNTIME_DIR / "latest_summary.json"),
                "mainwave_runtime_candidates": _path_status(MAINWAVE_RUNTIME_DIR / "latest_candidates.csv"),
                "mainwave_runtime_blocked_candidates": _path_status(MAINWAVE_RUNTIME_DIR / "latest_blocked_candidates.csv"),
                "mainwave_report_dir": _path_status(MAINWAVE_REPORT_DIR),
            },
        }
    )


@router.get("/historical-trades")
async def get_gen3_state_alpha_historical_trades(
    limit: int = Query(default=200, ge=1, le=2000, description="Maximum closed trade rows returned."),
    route: str | None = Query(default="all", description="Route filter: all, institutional_mainwave, panic_repair, old_g3_route_v3."),
    window: str | None = Query(default="all", description="Window filter: all, pre_2024_09, post_2024_09, weak_2022, valid_2024, blind_2026ytd."),
    sort_by: str = Query(default="entry_date", description="Column used for sorting."),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$", description="Sort order."),
) -> dict[str, Any]:
    historical = _read_historical_trades(
        limit=limit,
        route=route.strip() if isinstance(route, str) and route.strip() else "all",
        window=window.strip() if isinstance(window, str) and window.strip() else "all",
        sort_by=sort_by.strip() if isinstance(sort_by, str) and sort_by.strip() else "entry_date",
        sort_order=sort_order,
    )
    replacement_assessment = _build_replacement_assessment(historical)
    strategy_reduction_recovery = _read_strategy_reduction_recovery()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_historical_closed_trades",
            "historical_trades": historical["rows"],
            "metrics": historical["metrics"],
            "route_metrics": historical["route_metrics"],
            "market_style_metrics": historical["market_style_metrics"],
            "equity_curve": historical["equity_curve"],
            "exposure_metrics": historical["exposure_metrics"],
            "future_leak_audit": historical["future_leak_audit"],
            "window_metrics": historical["window_metrics"],
            "filters": historical["filters"],
            "strategy_contract": _contract(),
            "artifacts": historical["artifacts"],
            "replacement_assessment": replacement_assessment,
            "strategy_reduction_recovery": strategy_reduction_recovery,
        }
    )


@router.get("/broker/holdings")
async def get_gen3_state_alpha_broker_holdings() -> dict[str, Any]:
    return _wrap_guardrails(_broker_snapshot())


def _read_ths_capital_holdings_via_gateway() -> dict[str, Any]:
    gateway_url = str(os.environ.get("AISTOCK_TDX_GATEWAY_URL") or "").strip().rstrip("/")
    if not gateway_url:
        raise RuntimeError("AISTOCK_TDX_GATEWAY_URL is not configured")
    response = requests.post(f"{gateway_url}/account/ths/capital-holdings", timeout=90)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"unexpected TDX gateway THS response type: {type(body).__name__}")
    body.setdefault("source", "tdx_gateway_ths_bridge")
    return body


def _read_ths_trades_via_gateway() -> dict[str, Any]:
    gateway_url = str(os.environ.get("AISTOCK_TDX_GATEWAY_URL") or "").strip().rstrip("/")
    if not gateway_url:
        raise RuntimeError("AISTOCK_TDX_GATEWAY_URL is not configured")
    response = requests.post(f"{gateway_url}/account/ths/trades", timeout=45)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"unexpected TDX gateway THS trades response type: {type(body).__name__}")
    body.setdefault("source", "tdx_gateway_ths_bridge")
    return body


def _sync_broker_holdings_from_ths() -> dict[str, Any]:
    try:
        result = _read_ths_capital_holdings_via_gateway()
    except Exception as gateway_exc:
        result = {
            "ok": False,
            "source": "tdx_gateway_ths_bridge",
            "message": f"Failed to read THS capital/holdings via TDX Gateway: {gateway_exc}",
        }
    if not result.get("ok"):
        return {"ok": False, "mode": "g3_state_alpha_broker_holdings_sync", **result}
    raw_holdings = [
        item
        for item in (result.get("holdings") or [])
        if isinstance(item, dict) and _normalize_code6(item.get("code"))
    ]
    if result.get("capital_only") and not raw_holdings:
        state = _load_broker_state()
        holdings = list(state.get("holdings") or [])
        capital = _normalize_broker_capital(result.get("capital") or {}, holdings)
        state.update(
            {
                "capital": capital,
                "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
                "holding_source": result.get("source") or "ths_capital_holdings",
                "fallback_cache": bool(result.get("fallback_cache")),
                "sync_message": result.get("message"),
                "capital_only": True,
            }
        )
        _save_broker_state(state)
        return {
            "ok": True,
            "mode": "g3_state_alpha_broker_holdings_sync",
            "capital_only": True,
            "holdings": holdings,
            "capital": capital,
            "updated_at": state.get("updated_at"),
            "fallback_cache": bool(result.get("fallback_cache")),
            "message": result.get("message"),
            "artifacts": {"broker_state": _path_status(BROKER_STATE_PATH)},
        }
    capital = _normalize_broker_capital(result.get("capital") or {}, raw_holdings)
    holdings = [
        _normalize_broker_holding(item, capital)
        for item in raw_holdings
    ]
    capital = _normalize_broker_capital(capital, holdings)
    state = _load_broker_state()
    state.update(
        {
            "holdings": holdings,
            "capital": capital,
            "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "holding_source": result.get("source") or "ths_capital_holdings",
            "fallback_cache": bool(result.get("fallback_cache")),
            "sync_message": result.get("message"),
        }
    )
    _save_broker_state(state)
    return {
        "ok": True,
        "mode": "g3_state_alpha_broker_holdings_sync",
        "holdings": holdings,
        "capital": capital,
        "updated_at": state.get("updated_at"),
        "fallback_cache": bool(result.get("fallback_cache")),
        "message": result.get("message"),
        "artifacts": {"broker_state": _path_status(BROKER_STATE_PATH)},
    }


def _sync_broker_trades_from_ths(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    source = str(data.get("source") or "delivery_file").strip().lower()
    raw_text = str(data.get("raw_text") or "")
    if raw_text.strip():
        raw_result = {"ok": True, "raw_text": raw_text, "source": "payload_raw_text"}
    else:
        try:
            raw_result = _read_ths_trades_via_gateway()
        except Exception as gateway_exc:
            raw_result = {
                "ok": False,
                "source": "tdx_gateway_ths_bridge",
                "message": f"Failed to read THS trades via TDX Gateway: {gateway_exc}",
            }
    if not raw_result.get("ok"):
        return {"ok": False, "mode": "g3_state_alpha_broker_trades_sync", **raw_result}
    if isinstance(raw_result.get("rows"), list):
        parsed = {"ok": True, "rows": raw_result.get("rows") or [], "parsed_count": len(raw_result.get("rows") or [])}
    else:
        parsed = _parse_broker_trade_text(str(raw_result.get("raw_text") or ""))
    if not parsed.get("ok"):
        return {"ok": False, "mode": "g3_state_alpha_broker_trades_sync", **parsed, "raw_source": raw_result}
    state = _load_broker_state()
    old_rows = list(state.get("broker_trades") or [])
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in [*(parsed.get("rows") or []), *old_rows]:
        if not isinstance(row, dict):
            continue
        sig = _trade_signature(row)
        if sig in seen:
            continue
        seen.add(sig)
        merged.append(row)
    merged.sort(key=lambda item: str(item.get("trade_time") or ""), reverse=True)
    state["broker_trades"] = merged
    state["trade_updated_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    state["trade_source"] = raw_result.get("source") or source
    _save_broker_state(state)
    return {
        "ok": True,
        "mode": "g3_state_alpha_broker_trades_sync",
        "imported_count": len(parsed.get("rows") or []),
        "total_count": len(merged),
        "broker_trades": merged[:200],
        "trade_updated_at": state.get("trade_updated_at"),
        "raw_source": {
            "source": raw_result.get("source") or source,
            "file_path": raw_result.get("file_path"),
            "file_mtime": raw_result.get("file_mtime"),
            "view_type": raw_result.get("view_type"),
            "preview_lines": raw_result.get("preview_lines"),
        },
        "artifacts": {"broker_state": _path_status(BROKER_STATE_PATH)},
    }


def _run_broker_sync_once(source: str = "manual") -> dict[str, Any]:
    state = _load_broker_sync_state()
    started_at = datetime.now()
    state["last_run_at"] = started_at.isoformat(sep=" ", timespec="seconds")
    holdings_result = _sync_broker_holdings_from_ths() if state.get("sync_holdings", True) else None
    trades_result = _sync_broker_trades_from_ths() if state.get("sync_trades", True) else None
    holdings_ok = bool((holdings_result or {}).get("ok"))
    trades_ok = bool((trades_result or {}).get("ok"))
    result = {
        "ok": holdings_ok or trades_ok,
        "mode": "g3_state_alpha_broker_sync",
        "source": source,
        "started_at": state["last_run_at"],
        "finished_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "holdings": holdings_result,
        "trades": trades_result,
    }
    errors: list[str] = []
    for item in [holdings_result, trades_result]:
        if isinstance(item, dict) and not item.get("ok"):
            msg = str(item.get("message") or item.get("error") or "").strip()
            if msg:
                errors.append(msg)
    state["last_result"] = result
    state["last_error"] = "; ".join(errors) if errors and not result.get("ok") else None
    if holdings_ok:
        state["last_holdings_success_at"] = result["finished_at"]
    if trades_ok:
        state["last_trades_success_at"] = result["finished_at"]
    if result.get("ok"):
        state["last_success_at"] = result["finished_at"]
    _save_broker_sync_state(state)
    return result


@router.post("/broker/holdings/sync-ths")
async def sync_gen3_state_alpha_broker_holdings_from_ths() -> dict[str, Any]:
    return _wrap_guardrails(_sync_broker_holdings_from_ths())
    try:
        result = _read_ths_capital_holdings_via_gateway()
    except Exception as gateway_exc:
        result = {
            "ok": False,
            "source": "tdx_gateway_ths_bridge",
            "message": f"通过 TDX Gateway 读取同花顺资金持仓失败: {gateway_exc}",
        }
    if not result.get("ok"):
        return _wrap_guardrails({"ok": False, "mode": "g3_state_alpha_broker_holdings_sync", **result})
    raw_holdings = [
        item
        for item in (result.get("holdings") or [])
        if isinstance(item, dict) and _normalize_code6(item.get("code"))
    ]
    capital = _normalize_broker_capital(result.get("capital") or {}, raw_holdings)
    holdings = [
        _normalize_broker_holding(item, capital)
        for item in raw_holdings
    ]
    capital = _normalize_broker_capital(capital, holdings)
    state = _load_broker_state()
    state.update(
        {
            "holdings": holdings,
            "capital": capital,
            "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "holding_source": result.get("source") or "ths_capital_holdings",
            "fallback_cache": bool(result.get("fallback_cache")),
            "sync_message": result.get("message"),
        }
    )
    _save_broker_state(state)
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_broker_holdings_sync",
            "holdings": holdings,
            "capital": capital,
            "updated_at": state.get("updated_at"),
            "fallback_cache": bool(result.get("fallback_cache")),
            "message": result.get("message"),
            "artifacts": {"broker_state": _path_status(BROKER_STATE_PATH)},
        }
    )


@router.post("/broker/refresh")
async def refresh_gen3_state_alpha_broker_snapshot(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    state = _load_broker_state()
    sync_ths = bool(payload.get("sync_ths")) if isinstance(payload, dict) else False
    if sync_ths or not state.get("holdings"):
        synced = await sync_gen3_state_alpha_broker_holdings_from_ths()
        if synced.get("ok"):
            state = _load_broker_state()
    return _wrap_guardrails(_broker_snapshot())


@router.get("/broker/trades")
async def get_gen3_state_alpha_broker_trades(
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict[str, Any]:
    state = _load_broker_state()
    rows = list(state.get("broker_trades") or [])
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_broker_trades",
            "broker_trades": rows[:limit],
            "count": len(rows),
            "trade_updated_at": state.get("trade_updated_at"),
            "artifacts": {"broker_state": _path_status(BROKER_STATE_PATH)},
        }
    )


@router.post("/broker/trades/sync-ths")
async def sync_gen3_state_alpha_broker_trades_from_ths(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return _wrap_guardrails(_sync_broker_trades_from_ths(payload))
    source = str((payload or {}).get("source") or "delivery_file").strip().lower() if isinstance(payload, dict) else "delivery_file"
    raw_text = str((payload or {}).get("raw_text") or "") if isinstance(payload, dict) else ""
    if raw_text.strip():
        raw_result = {"ok": True, "raw_text": raw_text, "source": "payload_raw_text"}
    else:
        try:
            raw_result = _read_ths_trades_via_gateway()
        except Exception as gateway_exc:
            raw_result = {
                "ok": False,
                "source": "tdx_gateway_ths_bridge",
                "message": f"通过 TDX Gateway 读取同花顺历史成交失败: {gateway_exc}",
            }
    if not raw_result.get("ok"):
        return _wrap_guardrails({"ok": False, "mode": "g3_state_alpha_broker_trades_sync", **raw_result})
    if isinstance(raw_result.get("rows"), list):
        parsed = {"ok": True, "rows": raw_result.get("rows") or [], "parsed_count": len(raw_result.get("rows") or [])}
    else:
        parsed = _parse_broker_trade_text(str(raw_result.get("raw_text") or ""))
    if not parsed.get("ok"):
        return _wrap_guardrails({"ok": False, "mode": "g3_state_alpha_broker_trades_sync", **parsed, "raw_source": raw_result})
    state = _load_broker_state()
    old_rows = list(state.get("broker_trades") or [])
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in [*(parsed.get("rows") or []), *old_rows]:
        if not isinstance(row, dict):
            continue
        sig = _trade_signature(row)
        if sig in seen:
            continue
        seen.add(sig)
        merged.append(row)
    merged.sort(key=lambda item: str(item.get("trade_time") or ""), reverse=True)
    state["broker_trades"] = merged
    state["trade_updated_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    state["trade_source"] = raw_result.get("source") or source
    _save_broker_state(state)
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_broker_trades_sync",
            "imported_count": len(parsed.get("rows") or []),
            "total_count": len(merged),
            "broker_trades": merged[:200],
            "trade_updated_at": state.get("trade_updated_at"),
            "raw_source": {
                "source": raw_result.get("source") or source,
                "file_path": raw_result.get("file_path"),
                "file_mtime": raw_result.get("file_mtime"),
                "view_type": raw_result.get("view_type"),
                "preview_lines": raw_result.get("preview_lines"),
            },
            "artifacts": {"broker_state": _path_status(BROKER_STATE_PATH)},
        }
    )


@router.get("/broker/sync/status")
async def get_gen3_state_alpha_broker_sync_status() -> dict[str, Any]:
    state = configure_broker_sync_scheduler()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_broker_sync",
            "sync": state,
            "broker": _broker_snapshot(),
            "scheduler": {
                "enabled": bool(state.get("scheduler_enabled")),
                "wired": bool(state.get("scheduler_wired")),
                "next_run_time": state.get("next_run_time"),
                "message": "G3 broker account file sync scheduler wired.",
            },
            "artifacts": {
                "broker_state": _path_status(BROKER_STATE_PATH),
                "broker_sync_state": _path_status(BROKER_SYNC_STATE_PATH),
            },
        }
    )


@router.post("/broker/sync/config")
async def set_gen3_state_alpha_broker_sync_config(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    state = _load_broker_sync_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "interval_seconds" in payload:
            value = payload.get("interval_seconds")
            state["interval_seconds"] = max(60, int(value)) if value not in (None, "") else None
        if "trading_hours_only" in payload:
            state["trading_hours_only"] = bool(payload.get("trading_hours_only"))
        if "trading_days_only" in payload:
            state["trading_days_only"] = bool(payload.get("trading_days_only"))
        if "hour" in payload:
            state["hour"] = min(23, max(0, int(payload.get("hour") or 17)))
        if "minute" in payload:
            state["minute"] = min(59, max(0, int(payload.get("minute") if payload.get("minute") is not None else 30)))
        if "sync_holdings" in payload:
            state["sync_holdings"] = bool(payload.get("sync_holdings"))
        if "sync_trades" in payload:
            state["sync_trades"] = bool(payload.get("sync_trades"))
    _save_broker_sync_state(state)
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_broker_sync_config",
            "sync": configure_broker_sync_scheduler(),
        }
    )


@router.post("/broker/sync/run-once")
async def run_gen3_state_alpha_broker_sync_once(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _run_broker_sync_once(source=str((payload or {}).get("source") or "manual") if isinstance(payload, dict) else "manual")
    return _wrap_guardrails(
        {
            **result,
            "sync": configure_broker_sync_scheduler(),
            "broker": _broker_snapshot(),
        }
    )


@router.get("/replacement-assessment")
async def get_gen3_state_alpha_replacement_assessment() -> dict[str, Any]:
    return _wrap_guardrails(_build_replacement_assessment())


@router.get("/workflow/status")
async def get_gen3_state_alpha_workflow_status() -> dict[str, Any]:
    return _wrap_guardrails(_build_workflow_status())


@router.post("/workflow/refresh/run-once")
async def run_gen3_state_alpha_refresh_once(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    running = _running_refresh_task()
    if running:
        return _wrap_guardrails(running)
    entry_date = None
    force_alert = False
    if isinstance(payload, dict):
        txt = str(payload.get("entry_date") or "").strip()
        entry_date = txt or None
        force_alert = bool(payload.get("force_send") or payload.get("force_alert"))
    task_id = f"g3_state_alpha_refresh_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"
    _set_refresh_task(
        task_id,
        {
            "task_id": task_id,
            "status": "queued",
            "progress": 0,
            "entry_date": entry_date,
            "created_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "message": "G3 State Alpha refresh queued",
        },
    )
    thread = threading.Thread(
        target=_run_refresh_task,
        args=(task_id, entry_date, "manual", force_alert),
        name=f"g3-state-alpha-refresh-{task_id}",
        daemon=True,
    )
    thread.start()
    return _wrap_guardrails(_get_refresh_task(task_id) or {"task_id": task_id, "status": "queued"})


@router.get("/workflow/refresh-task/{task_id}")
async def get_gen3_state_alpha_refresh_task(task_id: str) -> dict[str, Any]:
    task = _get_refresh_task(task_id)
    if not task:
        task = {"task_id": task_id, "status": "missing", "progress": 0, "message": "task not found"}
    return _wrap_guardrails(task)


@router.get("/shadow-monitor/status")
async def get_gen3_state_alpha_shadow_monitor_status() -> dict[str, Any]:
    state = configure_shadow_monitor_scheduler()
    workflow = _build_workflow_status()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_shadow_monitor",
            "monitor": state,
            "workflow": workflow,
            "scheduler": {
                "enabled": bool(state.get("scheduler_enabled")),
                "wired": bool(state.get("scheduler_wired")),
                "next_run_time": state.get("next_run_time"),
                "message": "G3 monitor scheduler wired.",
            },
        }
    )


@router.post("/shadow-monitor/config")
async def set_gen3_state_alpha_shadow_monitor_config(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    state = _load_monitor_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "interval_seconds" in payload:
            state["interval_seconds"] = max(120, int(payload.get("interval_seconds") or 120))
        if "trading_hours_only" in payload:
            state["trading_hours_only"] = bool(payload.get("trading_hours_only"))
        if "email_enabled" in payload:
            state["email_enabled"] = bool(payload.get("email_enabled"))
        if "heartbeat_enabled" in payload:
            state["heartbeat_enabled"] = bool(payload.get("heartbeat_enabled"))
        if "heartbeat_minutes" in payload:
            state["heartbeat_minutes"] = max(30, int(payload.get("heartbeat_minutes") or 30))
        if "recipient_email" in payload:
            state["recipient_email"] = str(payload.get("recipient_email") or "").strip()
        if "run_current_refresh" in payload:
            state["run_current_refresh"] = bool(payload.get("run_current_refresh"))
    _save_monitor_state(state)
    state = configure_shadow_monitor_scheduler()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_shadow_monitor_config",
            "monitor": state,
        }
    )


@router.post("/shadow-monitor/run-once")
async def run_gen3_state_alpha_shadow_monitor_once(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = dict(payload) if isinstance(payload, dict) else {}
    data["force_send"] = True
    return await run_gen3_state_alpha_refresh_once(data)


@router.post("/shadow-verification")
async def save_gen3_state_alpha_shadow_verification(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _save_verification(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            "ok": bool(result.get("ok")),
            "mode": "g3_state_alpha_shadow_verification",
            **result,
        }
    )


@router.get("/paper-executions")
async def get_gen3_state_alpha_paper_executions(
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum paper execution rows returned."),
) -> dict[str, Any]:
    rows = _load_paper_executions()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_paper_executions",
            "paper_executions": rows[-limit:][::-1],
            "count": len(rows),
            "artifacts": {
                "paper_executions": _path_status(PAPER_EXECUTIONS_PATH),
            },
        }
    )


@router.post("/paper-order")
async def submit_gen3_state_alpha_paper_order(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _submit_paper_order(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            "ok": bool(result.get("ok")),
            "mode": "g3_state_alpha_paper_order",
            **result,
        }
    )


@router.post("/ptrade/buy-order")
async def submit_gen3_state_alpha_ptrade_buy_order(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _submit_ptrade_buy_order(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            "ok": bool(result.get("ok")),
            "mode": "g3_state_alpha_ptrade_buy_order",
            **result,
        }
    )


@router.get("/ptrade/status")
async def get_gen3_state_alpha_ptrade_status() -> dict[str, Any]:
    return _wrap_guardrails(_g3_ptrade_status())


@router.post("/ptrade/e2e-acceptance")
async def run_gen3_state_alpha_ptrade_e2e_acceptance(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    try:
        from scripts.gen3_ptrade_e2e_acceptance import run_acceptance as run_ptrade_e2e_acceptance

        result = run_ptrade_e2e_acceptance(
            bridge_dir=PTRADE_BRIDGE.paths.root,
            code=str(data.get("code") or "600001"),
            timeout_seconds=float(data.get("timeout_seconds") or 30.0),
            poll_seconds=float(data.get("poll_seconds") or 1.0),
            max_heartbeat_age_seconds=float(data.get("max_heartbeat_age_seconds") or 15.0),
        )
        _write_json(PTRADE_E2E_ACCEPTANCE_PATH, result)
    except Exception as exc:
        logger.exception(f"G3 PTrade e2e acceptance failed: {exc}")
        result = {
            "ok": False,
            "message": str(exc),
            "completed_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        }
    return _wrap_guardrails(
        {
            "ok": bool(result.get("ok")),
            "mode": "g3_state_alpha_ptrade_e2e_acceptance",
            "acceptance": result,
            "ptrade_status": _g3_ptrade_status(),
        }
    )


@router.post("/ptrade/internal-strategy-acceptance")
async def run_gen3_state_alpha_ptrade_internal_strategy_acceptance(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    try:
        from scripts.gen3_ptrade_internal_strategy_acceptance import run_internal_strategy_acceptance

        result = run_internal_strategy_acceptance(
            bridge_dir=PTRADE_BRIDGE.paths.root,
            code=str(data.get("code") or "600001"),
            wait_seconds=float(data.get("wait_seconds") or 0.0),
            timeout_seconds=float(data.get("timeout_seconds") or 30.0),
            poll_seconds=float(data.get("poll_seconds") or 1.0),
            max_heartbeat_age_seconds=float(data.get("max_heartbeat_age_seconds") or 15.0),
        )
        _write_json(PTRADE_INTERNAL_ACCEPTANCE_PATH, result)
    except Exception as exc:
        logger.exception(f"G3 PTrade internal strategy acceptance failed: {exc}")
        result = {
            "ok": False,
            "message": str(exc),
            "checks": [],
            "completed_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        }
        _write_json(PTRADE_INTERNAL_ACCEPTANCE_PATH, result)
    return _wrap_guardrails(
        {
            "ok": bool(result.get("ok")),
            "mode": "g3_state_alpha_ptrade_internal_strategy_acceptance",
            "acceptance": result,
            "ptrade_status": _g3_ptrade_status(),
        }
    )


@router.post("/ptrade/sell-order")
async def submit_gen3_state_alpha_ptrade_sell_order(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _submit_ptrade_sell_order(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            "ok": bool(result.get("ok")),
            "mode": "g3_state_alpha_ptrade_sell_order",
            **result,
        }
    )


@router.get("/observation-snapshots")
async def get_gen3_state_alpha_observation_snapshots(
    limit: int = Query(default=100, ge=1, le=500, description="Maximum observation snapshots returned."),
) -> dict[str, Any]:
    rows = _load_observation_snapshots()
    accepted_dates = _accepted_observation_dates(rows)
    scheduler_state = configure_observation_scheduler()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_observation_snapshots",
            "snapshots": rows[-limit:][::-1],
            "count": len(rows),
            "accepted_observation_days": len(accepted_dates),
            "accepted_observation_dates": accepted_dates[-30:],
            "scheduler": scheduler_state,
            "artifacts": {
                "observation_snapshots": _path_status(OBSERVATION_SNAPSHOTS_PATH),
                "pretrade_smoke": _path_status(PRETRADE_SMOKE_PATH),
                "pretrade_smoke_gates": _path_status(PRETRADE_SMOKE_GATES_PATH),
                "paper_executions": _path_status(PAPER_EXECUTIONS_PATH),
            },
        }
    )


@router.get("/observation-scheduler/status")
async def get_gen3_state_alpha_observation_scheduler_status() -> dict[str, Any]:
    rows = _load_observation_snapshots()
    accepted_dates = _accepted_observation_dates(rows)
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_observation_scheduler",
            "scheduler": configure_observation_scheduler(),
            "accepted_observation_days": len(accepted_dates),
            "accepted_observation_dates": accepted_dates[-30:],
            "latest_snapshot": rows[-1] if rows else None,
            "artifacts": {
                "observation_state": _path_status(OBSERVATION_STATE_PATH),
                "observation_snapshots": _path_status(OBSERVATION_SNAPSHOTS_PATH),
            },
        }
    )


@router.post("/observation-scheduler/config")
async def set_gen3_state_alpha_observation_scheduler_config(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    state = _load_observation_scheduler_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "hour" in payload:
            state["hour"] = min(23, max(0, int(payload.get("hour") or 15)))
        if "minute" in payload:
            state["minute"] = min(59, max(0, int(payload.get("minute") or 40)))
        if "trading_days_only" in payload:
            state["trading_days_only"] = bool(payload.get("trading_days_only"))
        if "run_smoke" in payload:
            state["run_smoke"] = bool(payload.get("run_smoke"))
        if "refresh_smoke" in payload:
            state["refresh_smoke"] = bool(payload.get("refresh_smoke"))
        if "auto_repair_minute30" in payload:
            state["auto_repair_minute30"] = bool(payload.get("auto_repair_minute30"))
    _save_observation_scheduler_state(state)
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_observation_scheduler_config",
            "scheduler": configure_observation_scheduler(),
        }
    )


@router.post("/observation-snapshot/run-once")
async def run_gen3_state_alpha_observation_snapshot(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    result = _build_observation_snapshot(
        run_smoke=bool(data.get("run_smoke", True)),
        refresh_smoke=bool(data.get("refresh_smoke", False)),
        auto_repair_minute30=bool(data.get("auto_repair_minute30", True)),
        source=str(data.get("source") or "manual"),
        entry_date=str(data.get("entry_date") or "").strip() or None,
    )
    return _wrap_guardrails(result)


@router.post("/observation-scheduler/run-once")
async def run_gen3_state_alpha_observation_scheduler_once(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    state = _load_observation_scheduler_state()
    run_smoke = bool(data.get("run_smoke", state.get("run_smoke", True)))
    refresh_smoke = bool(data.get("refresh_smoke", state.get("refresh_smoke", True)))
    auto_repair_minute30 = bool(data.get("auto_repair_minute30", state.get("auto_repair_minute30", True)))
    state["last_run_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    _save_observation_scheduler_state(state)
    result = _build_observation_snapshot(
        run_smoke=run_smoke,
        refresh_smoke=refresh_smoke,
        auto_repair_minute30=auto_repair_minute30,
        source="manual_scheduler",
        entry_date=str(data.get("entry_date") or "").strip() or None,
    )
    state = _load_observation_scheduler_state()
    state["last_result"] = result
    state["last_error"] = None if result.get("ok") else result.get("failure_category") or (result.get("blockers") or [{}])[0].get("message")
    if result.get("ok"):
        state["last_success_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
    _save_observation_scheduler_state(state)
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_state_alpha_observation_scheduler_run_once",
            "scheduler": configure_observation_scheduler(),
        }
    )


@router.get("/evidence-inventory")
async def get_gen3_state_alpha_evidence_inventory(
    limit: int = Query(default=200, ge=1, le=1000, description="Maximum inventory rows returned."),
) -> dict[str, Any]:
    inventory_path = BLUEPRINT_DIR / "g3_cleanup_inventory.csv"
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_evidence_archive_index",
            "inventory": _read_csv_records(inventory_path, limit=limit),
            "artifacts": {
                "inventory": _path_status(inventory_path),
                "blueprint_report": _path_status(BLUEPRINT_DIR / "REPORT_CN.md"),
                "summary": _path_status(BLUEPRINT_DIR / "summary.json"),
            },
        }
    )

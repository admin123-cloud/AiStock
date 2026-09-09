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
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import APIRouter, Body, Query

from scheduler.trading_calendar import TradingCalendar
from services.daily_trend_exit_monitor import evaluate_daily_rising_trend_exit, evaluate_intraday_rising_trend_line
from services.g3_holding_t_daily_review import REVIEW_DIR as HOLDING_T_REVIEW_DIR
from services.g3_holding_t_daily_review import record_manual_execution, run_daily_review
from services.g3_holding_t_portfolio_state import confirm_position_action, load_state as load_holding_t_portfolio_state, save_state as save_holding_t_portfolio_state
from services.runtime_health import read_snapshot, strategy_data_checks, operations_notification_owner
from utils.config import config as app_config
from utils.logger import get_logger
from utils.paths import report_path, runtime_path
from utils.strategy_contracts import formal_g3_score88_contract, formal_g3_score88_contract_metadata


router = APIRouter(prefix="/gen3-state-alpha", tags=["G3 State Alpha"])
logger = get_logger("gen3_state_alpha")

FORMAL_G3_CONTRACT = formal_g3_score88_contract()
STRATEGY_ID = FORMAL_G3_CONTRACT["strategy_id"]
STRATEGY_NAME = "G3 Institutional Mainwave 30m Volume Breakout and Cash"
STRATEGY_NAME_CN = "G3机构主升 + 空仓"
FINAL_G3_PROFILE = STRATEGY_ID
FINAL_G3_FORMAL_POLICY = "mainwave_hard_le_5_50"
FINAL_G3_PROFILE_NAME = "G3最终版：二槽主升 + G2空档补位"
FINAL_G3_PROFILE_NAME = "G3机构主升 + 空仓"
FINAL_G3_LEGACY_BASE_PROFILE = "g3_final_top2_mainwave_sector_exempt_v1"
FULL_G3_DISABLED_REPLAY_ROUTES = {"panic_repair", "old_g3_route_v3", "g2_gap_supplement"}
G2_GAP_SUPPLEMENT_LIVE_ENABLED = False
G2_GAP_SUPPLEMENT_RETIRE_REASON = "historical_replay_negative_and_no_portfolio_improvement_retired_from_live_trading_2026_06_21"

BLUEPRINT_DIR = report_path("gen3_strategy_rebuild_blueprint_v1")
STATE_ALPHA_RUNTIME_DIR = runtime_path("gen3_state_alpha")
STATE_ROUTER_RUNTIME_DIR = runtime_path("gen3_state_router_shadow")
STATE_ROUTER_REPORT_DIR = report_path("gen3_state_router_shadow_daily_v1")
STATE_ROUTER_DAILY_ARCHIVE_DIR = STATE_ROUTER_REPORT_DIR / "daily_archive"
MAINWAVE_RUNTIME_DIR = runtime_path("gen3_institutional_mainwave_current")
MAINWAVE_REPORT_DIR = report_path("gen3_institutional_mainwave_current_v1")
CURRENT_WAVE_SCAN_DIR = report_path("current_wave_style_candidate_scan_v1")
PROMOTION_REPORT_DIR = report_path("gen3_promotion_self_test_v1")
FULL_G3_BACKTEST_ROOT = report_path("g3_final_mom60_position_policy_v1")
FINAL_G3_BACKTEST_DIR = FULL_G3_BACKTEST_ROOT / FINAL_G3_FORMAL_POLICY
FINAL_G3_REPLAY_DIR = report_path("g3_formal_unified_five_strategy_contract_v1")
CURRENT_G3_HISTORICAL_REVIEW_DIR = report_path("g3_mainwave_breakout_historical_review_v1")
LATEST_G3_PROFILE = FINAL_G3_PROFILE
HISTORICAL_TRADES_BASE_PATH = CURRENT_G3_HISTORICAL_REVIEW_DIR / "historical_trades.csv"
HISTORICAL_TRADES_WITH_OPEN_PATH = report_path(
    "g3_historical_trades_with_open_positions_v1",
    "historical_trades_with_open_positions.csv",
)
HISTORICAL_TRADES_PATH = HISTORICAL_TRADES_BASE_PATH
SCORE120_CORE_TRADES_PATH = report_path("gen3_score120_formal_institutional_source_v1", "closed_trades.csv")
SCORE120_ENTRY_TIMING_DIR = report_path("score120_entry_timing_variants_v1")
SCORE120_ENTRY_SOURCE_SIGNALS_PATH = SCORE120_ENTRY_TIMING_DIR / "source_signals.csv"
SCORE88_REPLAY_DIR = report_path(
    "g3_recalled_mainwave_contract_v1",
    "20200101_20260630_breakout_score88_sector2_stop10%_top10",
)
SCORE88_REPLAY_SUMMARY_PATH = SCORE88_REPLAY_DIR / "summary.json"
QMT_NEW_HIGH_BREADTH_PATH = report_path("qmt_new_high_breadth_v1", "new_high_breadth_daily.parquet")
EQUITY_CURVE_PATH = CURRENT_G3_HISTORICAL_REVIEW_DIR / "two_slot_realised_proxy_curve.csv"
MTM_EQUITY_CURVE_PATH = CURRENT_G3_HISTORICAL_REVIEW_DIR / "mtm_not_available.csv"
PROMOTION_SUMMARY_PATH = CURRENT_G3_HISTORICAL_REVIEW_DIR / "summary.json"
PROMOTION_SUMMARY_CSV_PATH = FULL_G3_BACKTEST_ROOT / "summary.csv"
FINAL_G3_WINDOW_SUMMARY_PATH = CURRENT_G3_HISTORICAL_REVIEW_DIR / "window_summary.csv"
PROMOTION_GATES_PATH = PROMOTION_REPORT_DIR / "gates.csv"
STRATEGY_REDUCTION_RECOVERY_DIR = report_path("g3_strategy_reduction_recovery_v1")
NATIVE_BRIDGE_BACKTEST_DIR = report_path("g3_five_strategies_native_bridge_v1")
FORMAL_UNIFIED_CONTRACT_DIR = FINAL_G3_REPLAY_DIR
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
EXIT_MONITOR_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "shadow_exit_monitor_state.json"
DAILY_TREND_EXIT_MONITOR_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "daily_trend_exit_monitor_state.json"
OBSERVATION_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "observation_scheduler_state.json"
VERIFICATION_PATH = STATE_ALPHA_RUNTIME_DIR / "shadow_verifications.json"
MONITOR_EVENTS_PATH = STATE_ALPHA_RUNTIME_DIR / "shadow_monitor_events.json"
REPLACEMENT_ASSESSMENT_PATH = STATE_ALPHA_RUNTIME_DIR / "replacement_assessment.json"
PAPER_EXECUTIONS_PATH = STATE_ALPHA_RUNTIME_DIR / "paper_executions.json"
OBSERVATION_SNAPSHOTS_PATH = STATE_ALPHA_RUNTIME_DIR / "observation_snapshots.json"
PRETRADE_TICKET_REVIEWS_PATH = STATE_ALPHA_RUNTIME_DIR / "pretrade_ticket_reviews.json"
PAPER_WATCH_REVIEWS_PATH = STATE_ALPHA_RUNTIME_DIR / "paper_watch_reviews.json"
CANDIDATE_OMISSION_REVIEWS_PATH = STATE_ALPHA_RUNTIME_DIR / "candidate_omission_reviews.json"
NO_TRADE_DAY_REVIEWS_PATH = STATE_ALPHA_RUNTIME_DIR / "no_trade_day_reviews.json"
FORMAL_ACTION_REVIEWS_PATH = STATE_ALPHA_RUNTIME_DIR / "formal_action_reviews.json"
DAILY_REVIEW_CHECKLIST_REVIEWS_PATH = STATE_ALPHA_RUNTIME_DIR / "daily_review_checklist_reviews.json"
LAUNCH_DAY_PLAYBOOK_REVIEWS_PATH = STATE_ALPHA_RUNTIME_DIR / "launch_day_playbook_reviews.json"
LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH = STATE_ALPHA_RUNTIME_DIR / "live_launch_review_snapshots.json"
PREMARKET_ACTION_ATTEMPTS_PATH = STATE_ALPHA_RUNTIME_DIR / "premarket_action_attempts.json"
STRATEGY_TUNING_TASK_REVIEWS_PATH = STATE_ALPHA_RUNTIME_DIR / "strategy_tuning_task_reviews.json"
BROKER_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "broker_state.json"
BROKER_SYNC_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "broker_sync_state.json"
HOLDING_TICK_WATCHLIST_PATH = STATE_ALPHA_RUNTIME_DIR / "holding_tick_watchlist.json"
HOLDING_TICK_ALERT_STATE_PATH = STATE_ALPHA_RUNTIME_DIR / "holding_tick_alert_state.json"
PRETRADE_SMOKE_DIR = report_path("gen3_pretrade_smoke_test_v1")
PRETRADE_SMOKE_PATH = PRETRADE_SMOKE_DIR / "latest_pretrade_smoke.json"
PRETRADE_SMOKE_GATES_PATH = PRETRADE_SMOKE_DIR / "latest_pretrade_gates.csv"
REALTIME_READINESS_REVIEW_DIR = report_path("g3_realtime_readiness_review_v1")
LIVE_LAUNCH_PACKET_DIR = report_path("g3_live_launch_packet_v1")
LIVE_LAUNCH_PACKET_PATH = LIVE_LAUNCH_PACKET_DIR / "launch_packet.json"
LIVE_LAUNCH_REVIEW_ARCHIVE_DIR = report_path("g3_live_launch_review_snapshots_v1")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_ROUTER_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_state_router_shadow_daily_v1.py"
STATE_ROUTER_RESEARCH_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_market_state_router_v1.py"
PRETRADE_SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "gen3_pretrade_smoke_test_v1.py"
REALTIME_READINESS_REVIEW_SCRIPT = PROJECT_ROOT / "scripts" / "audit_g3_realtime_readiness_review_v1.py"
LIVE_LAUNCH_PACKET_SCRIPT = PROJECT_ROOT / "scripts" / "build_g3_live_launch_packet_v1.py"
REFRESH_TASKS: dict[str, dict[str, Any]] = {}
REFRESH_TASK_LOCK = threading.Lock()
SHADOW_ENTRY_LOCK = threading.Lock()
MONITOR_EVENTS_LOCK = threading.Lock()
_monitor_scheduler: BackgroundScheduler | None = None
_monitor_job_id = "g3_state_alpha_shadow_monitor"
_exit_monitor_job_id = "g3_state_alpha_shadow_exit_monitor"
_daily_trend_exit_monitor_job_id = "g3_state_alpha_daily_trend_exit_monitor"
_observation_job_id = "g3_state_alpha_observation_snapshot"
_observation_retry_job_id = "g3_state_alpha_observation_first_bar_retry"
_broker_sync_job_id = "g3_state_alpha_broker_sync"
_auto_order_job_id = "g3_state_alpha_auto_order_30m"


def _startup_schedulers_enabled() -> bool:
    value = str(os.environ.get("AISTOCK_STARTUP_SCHEDULERS_ENABLED", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _scheduler_disabled_result(state: dict[str, Any], job_id: str) -> dict[str, Any]:
    return {
        **state,
        "scheduler_enabled": False,
        "scheduler_wired": False,
        "job_id": job_id,
        "next_run_time": None,
        "scheduler_disabled_reason": "AISTOCK_STARTUP_SCHEDULERS_ENABLED=0",
    }


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


def _formal_observation_date(requested_date: str | None = None) -> str:
    """Use the actual Shanghai observation day unless an explicit backfill date is supplied."""
    return _date_text(requested_date) or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _clean_review_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return default if text.lower() in {"nan", "nat", "none"} else text


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
    current_entry = _date_text(current_tickets[0].get("entry_date")) if current_tickets else ""
    out: list[dict[str, Any]] = []
    for item in tickets:
        if not isinstance(item, dict):
            continue
        if _truthy(item.get("retired_from_live_trading")) or _is_g2_gap_supplement_record(item):
            continue
        if not _truthy(item.get("qualified_shadow_buy")):
            continue
        entry_date = _date_text(item.get("entry_date") or item.get("planned_entry_ts"))
        if not entry_date or not _is_trading_day_text(entry_date):
            continue
        if current_entry and entry_date <= current_entry:
            continue
        out.append(item)
    return out


def _is_g2_gap_supplement_record(item: dict[str, Any]) -> bool:
    route = str(item.get("route") or item.get("mode") or "").strip()
    strategy = str(item.get("trade_strategy") or "").strip()
    return route == "g2_gap_supplement" or strategy == "volume_runup_supplement"


def _retire_g2_gap_supplement_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if G2_GAP_SUPPLEMENT_LIVE_ENABLED:
        return records
    out: list[dict[str, Any]] = []
    for row in records or []:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        if _is_g2_gap_supplement_record(item):
            existing = str(item.get("block_reason") or "").strip()
            item["router_eligible"] = False
            item["qualified_shadow_buy"] = False
            item["formal_buy_signal"] = False
            item["auto_order_allowed"] = False
            item["order_path_enabled"] = False
            item["paper_trade_allowed"] = False
            item["retired_from_live_trading"] = True
            item["retire_reason"] = G2_GAP_SUPPLEMENT_RETIRE_REASON
            item["shadow_status"] = "retired_g2_gap_supplement_observe"
            item["block_reason"] = G2_GAP_SUPPLEMENT_RETIRE_REASON if not existing else f"{existing}|{G2_GAP_SUPPLEMENT_RETIRE_REASON}"
        out.append(item)
    return out


def _retire_g2_gap_supplement_diagnostics(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if G2_GAP_SUPPLEMENT_LIVE_ENABLED:
        return records
    out: list[dict[str, Any]] = []
    for row in records or []:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        if str(item.get("route") or "").strip() == "g2_gap_supplement":
            rows = item.get("rows") or item.get("source_rows") or 0
            item["eligible_rows"] = 0
            item["blocked_rows"] = rows
            item["route_health_observation"] = G2_GAP_SUPPLEMENT_RETIRE_REASON
            item["top_block_reason"] = G2_GAP_SUPPLEMENT_RETIRE_REASON
            item["live_enabled"] = False
            item["retired_from_live_trading"] = True
            item["retire_reason"] = G2_GAP_SUPPLEMENT_RETIRE_REASON
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
    current_entry = _date_text(current_tickets[0].get("entry_date")) if current_tickets else ""
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
        message = f"已识别 {display_entry} 的买入建议；日期用于标记候选批次，不强制等同下一交易日。"
    elif non_trading_count:
        status = "filtered_non_trading"
        message = f"已过滤 {non_trading_count} 条非交易日候选；当前按 {display_entry or expected_entry or '--'} 口径等待有效买入票。"
    elif afterhours_entry and expected_entry and afterhours_entry != expected_entry:
        status = "date_note"
        message = f"盘后候选日 {afterhours_entry} 与下一交易日 {expected_entry} 不一致；页面按当前买入建议展示，执行前以真实账户和交易时点复核。"
    else:
        status = "no_candidate"
        message = f"{display_entry or expected_entry or '当前'} 暂无合格买入建议。"

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
        "display_label": "买入建议",
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read json artifact: %s", path)
        return {}


def _qmt_new_high_breadth_environment(summary: dict[str, Any] | None) -> dict[str, Any]:
    """Return a read-only prior-session breadth annotation for the current batch.

    This deliberately never mutates candidate eligibility, ranking, ticket count,
    position size, or order permission.  The signal-date/entry-date split makes
    the data-availability rule explicit: intraday entry may use only the fully
    completed QMT breadth value from the preceding trading day.
    """
    summary = summary if isinstance(summary, dict) else {}
    entry_date = _date_text(summary.get("entry_date"))
    observation_date = _date_text(summary.get("decision_date"))
    base = {
        "mode": "observation_only",
        "source": "QMT canonical A-share daily kline breadth",
        "lag_rule": "use_previous_completed_trade_day_for_intraday_entry",
        "entry_date": entry_date or None,
        "observation_date": observation_date or None,
        "candidate_eligibility_changed": False,
        "ranking_changed": False,
        "position_size_changed": False,
        "order_permission_changed": False,
        "artifact": _path_status(QMT_NEW_HIGH_BREADTH_PATH),
    }
    if not observation_date:
        return {
            **base,
            "available": False,
            "status": "missing_observation_date",
            "message": "当前批次缺少信号日，未应用新高扩散度观察。",
        }
    if not QMT_NEW_HIGH_BREADTH_PATH.exists():
        return {
            **base,
            "available": False,
            "status": "missing_breadth_artifact",
            "message": "QMT 全A新高扩散度产物缺失，未应用环境观察。",
        }
    try:
        frame = pd.read_parquet(QMT_NEW_HIGH_BREADTH_PATH)
        frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        row = frame.loc[frame["trade_date"] == observation_date]
        if row.empty:
            latest = frame["trade_date"].dropna().max() if "trade_date" in frame else None
            return {
                **base,
                "available": False,
                "status": "observation_date_not_finalized",
                "latest_finalized_trade_date": latest,
                "message": "信号日前一交易日的QMT扩散度尚未产出；不使用更早日期替代。",
            }
        record = row.iloc[-1]
        fields = (
            "eligible_stocks", "nh20_breadth", "nh100_breadth", "nh100_ma5",
            "nh100_change_1d", "nhall_breadth", "nhall_ma5", "nhall_change_1d",
            "breakout_environment",
        )
        values = {
            field: (bool(record[field]) if field == "breakout_environment" else _to_float_or_none(record[field]))
            for field in fields if field in record.index
        }
        environment_pass = bool(values.get("breakout_environment"))
        return {
            **base,
            "available": True,
            "status": "environment_positive" if environment_pass else "environment_negative",
            "environment_pass": environment_pass,
            "values": values,
            "message": (
                "新高扩散度环境偏正向，仅作为候选解释与后续分组审计，不筛除候选。"
                if environment_pass
                else "新高扩散度环境偏弱，仅作为候选解释与后续分组审计，不筛除候选。"
            ),
        }
    except Exception as exc:
        logger.warning("Failed to read QMT new-high breadth artifact: %s", exc)
        return {
            **base,
            "available": False,
            "status": "breadth_artifact_read_error",
            "message": "QMT 新高扩散度产物读取失败，未应用环境观察。",
        }
def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temp_path, path)


def _load_premarket_action_attempts() -> list[dict[str, Any]]:
    data = _read_json(PREMARKET_ACTION_ATTEMPTS_PATH)
    rows = data.get("attempts") if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def _append_premarket_action_attempt(attempt: dict[str, Any]) -> None:
    rows = _load_premarket_action_attempts()
    rows.append(attempt)
    _write_json(PREMARKET_ACTION_ATTEMPTS_PATH, {"attempts": rows[-200:]})


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


def _historical_trades_path() -> Path:
    # The current two-mode contract owns the history page.  The integrated
    # five-strategy/open-position file remains diagnostic evidence only.
    if HISTORICAL_TRADES_BASE_PATH.exists():
        return HISTORICAL_TRADES_BASE_PATH
    if HISTORICAL_TRADES_WITH_OPEN_PATH.exists():
        return HISTORICAL_TRADES_WITH_OPEN_PATH
    return HISTORICAL_TRADES_BASE_PATH


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
    candidates = sorted(_mainwave_summary_candidates(), key=lambda x: (str(x[2].get("entry_date") or ""), str(x[2].get("generated_at") or "")), reverse=True)
    for label, path, summary in candidates:
        source = _find_institutional_mainwave_source(summary)
        if source and (source.get("pre_confirm_preview") or source.get("rows") or "rows" in source):
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


def _first_mainwave_text(row: dict[str, Any], names: list[str]) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value and value.lower() not in {"nan", "none", "null"}:
            return value
    return ""


def _is_qmt_sw_sector_text(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith(("qmt:SW", "SW1", "SW2", "SW3", "industry:"))


def _is_tdx_mainline_sector_text(value: str) -> bool:
    text = str(value or "").strip()
    if not text or _is_qmt_sw_sector_text(text):
        return False
    block_exact = {
        "最近情绪指数",
        "连续亏损",
        "ST板块",
        "退市整理",
        "融资融券",
        "转融券标的",
        "沪股通",
        "深股通",
        "MSCI成份",
        "MSCI中盘",
        "QFII新进",
        "保险重仓",
        "私募重仓",
        "含可转债",
        "专精特新",
        "主营变更",
        "控制权变更",
        "近期新高",
        "历史新高",
        "近期强势",
        "最近异动",
        "活跃股",
        "昨日上榜",
        "昨日振荡",
        "昨高换手",
        "昨曾跌停",
        "最近多板",
        "非周期股",
        "周期股",
        "含H股",
        "专项贷款",
        "股权转让",
        "百元股",
        "低安全分",
        "上证指数",
        "深证成指",
        "创业板指",
        "科创50",
        "沪深300",
        "中证500",
        "中证1000",
    }
    if text in block_exact:
        return False
    extra_noise_exact = {
        "高贝塔值",
        "高市净率",
        "低市净率",
        "高市盈率",
        "低市盈率",
        "绩优股",
        "亏损股",
        "微利股",
        "高分红股",
        "回购计划",
        "自由现金流",
        "并购重组股",
        "久不分红",
        "预盈预增",
        "股权激励",
        "机构调研",
    }
    if text in extra_noise_exact:
        return False
    block_tokens = [
        "昨日",
        "涨停",
        "跌停",
        "减持",
        "低价股",
        "高价股",
        "微盘股",
        "小盘股",
        "大盘股",
        "MSCI",
        "QFII",
        "重仓",
        "可转债",
        "主营变更",
        "控制权",
        "专精特新",
        "新高",
        "强势",
        "异动",
        "活跃股",
        "换手",
        "上榜",
        "振荡",
        "情绪",
        "多板",
        "周期股",
        "含H股",
        "专项贷款",
        "股权转让",
        "百元股",
        "安全分",
        "成份",
        "成分",
        "指数",
    ]
    if any(token in text for token in block_tokens):
        return False
    extra_noise_tokens = [
        "贝塔",
        "市净率",
        "市盈率",
        "绩优",
        "亏损股",
        "微利股",
        "分红",
        "回购",
        "现金流",
        "并购重组",
        "不分红",
        "预盈",
        "预增",
        "股权激励",
        "机构调研",
    ]
    if any(token in text for token in extra_noise_tokens):
        return False
    province_prefixes = [
        "北京",
        "上海",
        "天津",
        "重庆",
        "河北",
        "河南",
        "山东",
        "山西",
        "江苏",
        "浙江",
        "安徽",
        "福建",
        "江西",
        "湖北",
        "湖南",
        "广东",
        "海南",
        "四川",
        "贵州",
        "云南",
        "陕西",
        "甘肃",
        "青海",
        "内蒙",
        "广西",
        "西藏",
        "宁夏",
        "新疆",
        "深圳",
    ]
    if text.endswith("板块") and any(text.startswith(prefix) for prefix in province_prefixes):
        return False
    return True


def _mainwave_sector_identity(row: dict[str, Any]) -> dict[str, str]:
    sw_name = _first_mainwave_text(row, ["sw_l2_industry_name", "l2_sector_name", "industry"])
    sw_code = _first_mainwave_text(row, ["sw_l2_industry_code", "l2_sector_code"])
    tdx_name = _first_mainwave_text(row, ["tdx_mainline_sector_name", "tdx_sector_name", "tdx_industry_name"])
    tdx_code = _first_mainwave_text(row, ["tdx_mainline_sector_code", "tdx_sector_code", "tdx_industry_code"])
    sector_source = _first_mainwave_text(row, ["sector_source", "mainline_sector_source"])
    existing_sector = _first_mainwave_text(row, ["sector_name"])
    existing_code = _first_mainwave_text(row, ["sector_code"])
    if not tdx_name and sector_source == "tdx" and _is_tdx_mainline_sector_text(existing_sector):
        tdx_name = existing_sector
        tdx_code = existing_code or tdx_code
    if not tdx_name:
        tags = _first_mainwave_text(row, ["tdx_mainline_tags", "tdx_block_names", "tdx_concept_names"])
        for token in re.split(r"[,|/，、]+", tags):
            token = token.strip()
            if _is_tdx_mainline_sector_text(token):
                tdx_name = token
                break
    if _is_qmt_sw_sector_text(tdx_name):
        tdx_name = ""
        tdx_code = ""
    return {
        "mainline_sector_name": tdx_name,
        "mainline_sector_code": tdx_code,
        "mainline_sector_source": "tdx" if tdx_name else "sw_fallback",
        "sw_industry_name": sw_name,
        "sw_industry_code": sw_code,
        "display_sector_name": tdx_name or "未映射TDX主线",
        "display_sector_code": tdx_code if tdx_name else "",
    }


def _tdx_mainline_map_for_codes(codes: list[str]) -> dict[str, dict[str, str]]:
    normalized = sorted({_row_code(code) for code in codes if _row_code(code)})
    if not normalized:
        return {}
    try:
        from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists

        if not clickhouse_table_exists("source_sector_stocks"):
            return {}
        quoted = _quote_sql_list(normalized)
        if not quoted:
            return {}
        raw = clickhouse_query_df(
            f"""
            SELECT canonical_code, sector_code, sector_name, sector_type
            FROM source_sector_stocks
            WHERE source = 'tdx'
              AND alias_status = 'active'
              AND canonical_code IN ({quoted})
              AND snapshot_date = (
                  SELECT max(snapshot_date)
                  FROM source_sector_stocks
                  WHERE source = 'tdx'
                    AND alias_status = 'active'
                    AND canonical_code != ''
              )
            """
        )
        if raw.empty:
            return {}
        raw["sector_name"] = raw["sector_name"].fillna("").astype(str).str.strip()
        raw = raw[raw["sector_name"].map(_is_tdx_mainline_sector_text)].copy()
        if raw.empty:
            return {}
        raw["canonical_code"] = raw["canonical_code"].fillna("").astype(str).map(_row_code)
        raw["sector_code"] = raw["sector_code"].fillna("").astype(str)
        raw = raw.sort_values(["canonical_code", "sector_type", "sector_name", "sector_code"])
        picked = raw.drop_duplicates("canonical_code", keep="first")
        return {
            str(row.get("canonical_code") or ""): {
                "mainline_sector_code": str(row.get("sector_code") or ""),
                "mainline_sector_name": str(row.get("sector_name") or ""),
                "mainline_sector_source": "tdx",
            }
            for _, row in picked.iterrows()
            if str(row.get("canonical_code") or "") and str(row.get("sector_name") or "")
        }
    except Exception as exc:
        logger.warning("Failed to load TDX mainline sector map: %s", exc)
        return {}


def _apply_tdx_mainline_sector(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return records
    missing_codes = [
        str(item.get("code") or "")
        for item in records
        if str(item.get("mainline_sector_source") or "") != "tdx"
    ]
    sector_map = _tdx_mainline_map_for_codes(missing_codes)
    for item in records:
        code = _row_code(item.get("code"))
        mapped = sector_map.get(code)
        if mapped and str(mapped.get("mainline_sector_name") or ""):
            item["mainline_sector_name"] = mapped["mainline_sector_name"]
            item["mainline_sector_code"] = mapped.get("mainline_sector_code") or ""
            item["mainline_sector_source"] = "tdx"
            item["sector_name"] = mapped["mainline_sector_name"]
            item["sector_code"] = mapped.get("mainline_sector_code") or ""
    return records


def _mainwave_candidate_from_row(row: dict[str, Any], source: str, recommended_codes: set[str]) -> dict[str, Any]:
    code = _row_code(row.get("code") or row.get("code_raw"), row.get("code6"))
    sector_identity = _mainwave_sector_identity(row)
    sector = sector_identity["display_sector_name"]
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
        "sector_code": sector_identity["display_sector_code"],
        "mainline_sector_name": sector_identity["mainline_sector_name"],
        "mainline_sector_code": sector_identity["mainline_sector_code"],
        "mainline_sector_source": sector_identity["mainline_sector_source"],
        "sw_industry_name": sector_identity["sw_industry_name"],
        "sw_industry_code": sector_identity["sw_industry_code"],
        "route": row.get("route") or "institutional_mainwave",
        "route_label": row.get("route_label") or "机构主升浪",
        "trade_strategy": row.get("trade_strategy") or "institutional_mainwave_score88",
        "trade_strategy_label": row.get("trade_strategy_label") or "机构主升Score88",
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
        "mainwave_dynamic_cooldown_active": _truthy(row.get("mainwave_dynamic_cooldown_active")),
        "mainwave_dynamic_cooldown_reason": row.get("mainwave_dynamic_cooldown_reason") or "",
        "mainwave_cooldown_elapsed_trading_days": _to_int_or_zero(row.get("mainwave_cooldown_elapsed_trading_days")),
        "mainwave_recovery_signal_ok": _truthy(row.get("mainwave_recovery_signal_ok")),
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
    rows = _apply_tdx_mainline_sector(rows)
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


def _mainwave_watch_candidate_from_row(row: dict[str, Any], source: str, diffusion_by_sector: dict[str, float]) -> dict[str, Any]:
    code = _row_code(row.get("code") or row.get("code_raw"), row.get("code6"))
    sector_identity = _mainwave_sector_identity(row)
    sector = sector_identity["display_sector_name"]
    score = _to_float_or_none(row.get("wave_style_score") or row.get("score"))
    diffusion = _to_float_or_none(row.get("sector_diffusion_score"))
    if diffusion is None:
        diffusion = diffusion_by_sector.get(str(sector))
    template_pass = _truthy(row.get("template_pass"))
    block_parts: list[str] = []
    if score is not None and score < 120:
        block_parts.append("score<120")
    if not template_pass:
        block_parts.append("template_pass=false")
    if diffusion is not None and diffusion < 65:
        block_parts.append("sector_diffusion<65")
    return {
        "code": code,
        "name": row.get("name") or row.get("stock_name") or "",
        "sector_name": sector,
        "sector_code": sector_identity["display_sector_code"],
        "mainline_sector_name": sector_identity["mainline_sector_name"],
        "mainline_sector_code": sector_identity["mainline_sector_code"],
        "mainline_sector_source": sector_identity["mainline_sector_source"],
        "sw_industry_name": sector_identity["sw_industry_name"],
        "sw_industry_code": sector_identity["sw_industry_code"],
        "route": "institutional_mainwave",
        "route_label": "机构主升浪",
        "trade_strategy": "institutional_mainwave_score88",
        "trade_strategy_label": "机构主升Score88",
        "template_label": row.get("template_label") or "",
        "template_pass": template_pass,
        "wave_style_score": score,
        "score": score,
        "score_gap_to_formal": None if score is None else round(max(0.0, 120.0 - score), 6),
        "sector_diffusion_score": diffusion,
        "trade_date": _date_text(row.get("trade_date")),
        "decision_date": _date_text(row.get("trade_date")),
        "reference_close": _to_float_or_none(row.get("close")),
        "ret5": _to_float_or_none(row.get("ret5")),
        "ret20": _to_float_or_none(row.get("ret20")),
        "mom60": _to_float_or_none(row.get("mom60")),
        "amount5_20": _to_float_or_none(row.get("amount5_20")),
        "max_dd20": _to_float_or_none(row.get("max_dd20")),
        "near_high60": _truthy(row.get("near_high60")),
        "m30_status": "not_formal_candidate",
        "m30_confirmed": False,
        "is_recommended": False,
        "watch_only": True,
        "watch_tier": "sector_watch",
        "block_reason": "|".join(block_parts) or "watch_only_not_formal_ticket",
        "source": source,
    }


def _build_mainwave_watch_candidates(limit: int) -> list[dict[str, Any]]:
    sector_df = _read_csv_df(MAINWAVE_REPORT_DIR / "current_sector_diffusion.csv")
    diffusion_by_sector: dict[str, float] = {}
    sector_key = "tdx_mainline_sector_name" if "tdx_mainline_sector_name" in sector_df.columns else "l2_sector_name"
    if not sector_df.empty and {sector_key, "sector_diffusion_score"}.issubset(sector_df.columns):
        for _, row in sector_df.iterrows():
            sector = str(row.get(sector_key) or "")
            value = _to_float_or_none(row.get("sector_diffusion_score"))
            if sector and value is not None:
                diffusion_by_sector[sector] = value

    frames: list[tuple[str, pd.DataFrame]] = []
    for source, path in [
        ("mainwave_pool_top500", MAINWAVE_REPORT_DIR / "current_wave_pool_top500.csv"),
        ("current_wave_top_candidates", CURRENT_WAVE_SCAN_DIR / "top_candidates.csv"),
    ]:
        df = _read_csv_df(path)
        if not df.empty:
            frames.append((source, df))

    by_code: dict[str, dict[str, Any]] = {}
    for source, df in frames:
        records = json.loads(df.astype(object).where(pd.notna(df), None).to_json(orient="records", force_ascii=False))
        for row in records:
            if not isinstance(row, dict):
                continue
            item = _mainwave_watch_candidate_from_row(row, source, diffusion_by_sector)
            code = str(item.get("code") or "")
            if not code:
                continue
            existing = by_code.get(code)
            if not existing or (_to_float_or_none(item.get("wave_style_score")) or 0) > (_to_float_or_none(existing.get("wave_style_score")) or 0):
                by_code[code] = item

    rows = _apply_tdx_mainline_sector(list(by_code.values()))
    for item in rows:
        if _to_float_or_none(item.get("sector_diffusion_score")) is None:
            item["sector_diffusion_score"] = diffusion_by_sector.get(str(item.get("sector_name") or ""))
    rows = [item for item in rows if not item.get("template_pass") or (_to_float_or_none(item.get("wave_style_score")) or 0) < 120]
    rows.sort(
        key=lambda item: (
            _to_float_or_none(item.get("sector_diffusion_score")) or 0,
            _to_float_or_none(item.get("wave_style_score")) or 0,
            _to_float_or_none(item.get("ret20")) or 0,
        ),
        reverse=True,
    )
    return rows[:limit]


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
        sector_code = next(
            (
                str(item.get("sector_code") or item.get("l2_sector_code") or "").strip()
                for item in rows
                if str(item.get("sector_code") or item.get("l2_sector_code") or "").strip()
            ),
            "",
        )
        out.append(
            {
                "sector_name": sector,
                "sector_code": sector_code,
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


def _build_mainwave_sector_opportunities_v2(candidates: list[dict[str, Any]], watch_candidates: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        groups.setdefault(str(item.get("sector_name") or ""), []).append(item)
    watch_groups: dict[str, list[dict[str, Any]]] = {}
    for item in watch_candidates or []:
        watch_groups.setdefault(str(item.get("sector_name") or ""), []).append(item)
    for sector in watch_groups:
        groups.setdefault(sector, [])

    out: list[dict[str, Any]] = []
    for sector, rows in groups.items():
        watch_rows = watch_groups.get(sector, [])
        scoring_rows = rows or watch_rows
        scores = [_to_float_or_none(item.get("wave_style_score")) for item in scoring_rows]
        scores = [value for value in scores if value is not None]
        diffusions = [_to_float_or_none(item.get("sector_diffusion_score")) for item in scoring_rows]
        diffusions = [value for value in diffusions if value is not None]
        max_score = max(scores) if scores else 0.0
        avg_score = sum(scores) / len(scores) if scores else 0.0
        avg_diffusion = sum(diffusions) / len(diffusions) if diffusions else 0.0
        recommended = [item for item in rows if item.get("is_recommended")]
        m30_ok = [item for item in rows if item.get("m30_confirmed") or str(item.get("m30_status") or "") == "ok"]
        state, state_label = _mainwave_sector_state(avg_diffusion, max_score, len(rows), len(recommended))
        if not rows and watch_rows:
            state, state_label = "sector_watch", "板块观察"
        top_rows = sorted(scoring_rows, key=lambda item: _to_float_or_none(item.get("wave_style_score")) or 0, reverse=True)[:5]
        templates = sorted({str(item.get("template_label") or "") for item in scoring_rows if item.get("template_label")})
        sector_code = next(
            (
                str(item.get("sector_code") or item.get("l2_sector_code") or "").strip()
                for item in scoring_rows
                if str(item.get("sector_code") or item.get("l2_sector_code") or "").strip()
            ),
            "",
        )
        out.append(
            {
                "sector_name": sector,
                "sector_code": sector_code,
                "state": state,
                "state_label": state_label,
                "candidate_count": len(rows),
                "watch_candidate_count": len(watch_rows),
                "recommended_count": len(recommended),
                "m30_ok_count": len(m30_ok),
                "max_wave_style_score": max_score,
                "avg_wave_style_score": avg_score,
                "avg_sector_diffusion_score": avg_diffusion,
                "top_candidates": top_rows,
                "top_candidate_names": " / ".join([str(item.get("name") or item.get("code") or "") for item in top_rows[:3]]),
                "template_labels": templates,
                "rank_score": avg_diffusion * 0.55 + max_score * 0.35 + min(len(rows), 8) * 2.0 + min(len(watch_rows), 8) * 1.2 + len(recommended) * 5.0,
            }
        )
    out.sort(key=lambda item: _to_float_or_none(item.get("rank_score")) or 0, reverse=True)
    return out


def _quote_sql_text(value: Any) -> str:
    return "'" + str(value or "").replace("\\", "\\\\").replace("'", "\\'") + "'"


def _quote_sql_list(values: list[str]) -> str:
    return ",".join(_quote_sql_text(value) for value in values if str(value or "").strip())


def _mainwave_sector_index_code(name: str) -> str | None:
    text = str(name or "").strip()
    if text in {"创业板", "创业板指"}:
        return "399006.SZ"
    if text in {"深证成指", "深市指数", "深证指数"}:
        return "399001.SZ"
    if text in {"上证指数", "沪市指数", "上证综指"}:
        return "999999.SH"
    if text in {"沪深300", "沪深指数"}:
        return "000300.SH"
    if text in {"中证500"}:
        return "000905.SH"
    if text in {"中证1000"}:
        return "000852.SH"
    if text in {"科创50", "科创板"}:
        return "000688.SH"
    return None


def _enrich_mainwave_sector_codes(sectors: list[dict[str, Any]]) -> None:
    names = sorted({str(item.get("sector_name") or "").strip() for item in sectors if str(item.get("sector_name") or "").strip()})
    if not names:
        return
    try:
        from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists

        if not clickhouse_table_exists("sectors"):
            return
        quoted_names = _quote_sql_list(names)
        if not quoted_names:
            return
        df = clickhouse_query_df(
            f"""
            SELECT name, code
            FROM sectors
            WHERE type = 'industry'
              AND level = 2
              AND name IN ({quoted_names})
            """
        )
        if df.empty:
            return
        code_by_name = {
            str(row.get("name") or "").strip(): str(row.get("code") or "").strip()
            for _, row in df.iterrows()
            if str(row.get("name") or "").strip() and str(row.get("code") or "").strip()
        }
        for item in sectors:
            if str(item.get("sector_code") or "").strip():
                continue
            code = code_by_name.get(str(item.get("sector_name") or "").strip())
            if code:
                item["sector_code"] = code
    except Exception as exc:
        logger.warning("Failed to enrich mainwave sector codes: %s", exc)


def _build_mainwave_sector_kline_map(sectors: list[dict[str, Any]], days: int = 260) -> dict[str, list[dict[str, Any]]]:
    names = sorted({str(item.get("sector_name") or "").strip() for item in sectors if str(item.get("sector_name") or "").strip()})
    sector_code_to_name = {
        str(item.get("sector_code") or item.get("mainline_sector_code") or "").strip(): str(item.get("sector_name") or "").strip()
        for item in sectors
        if str(item.get("sector_code") or item.get("mainline_sector_code") or "").strip()
        and str(item.get("sector_name") or "").strip()
    }
    if not names and not sector_code_to_name:
        return {}
    try:
        from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists

        out: dict[str, list[dict[str, Any]]] = {}
        index_code_to_name = {code: name for name in names if (code := _mainwave_sector_index_code(name))}
        if index_code_to_name and clickhouse_table_exists("kline_daily"):
            quoted_index_codes = _quote_sql_list(sorted(index_code_to_name))
            index_raw = clickhouse_query_df(
                f"""
                SELECT code, trade_date, open, high, low, close, volume, amount, change_pct
                FROM kline_daily
                WHERE code IN ({quoted_index_codes})
                  AND trade_date >= today() - INTERVAL 430 DAY
                ORDER BY code, trade_date
                """
            )
            if not index_raw.empty:
                for code, frame in index_raw.groupby(index_raw["code"].astype(str), dropna=False):
                    sector_name = index_code_to_name.get(str(code), str(code))
                    rows = frame.sort_values("trade_date").tail(max(int(days), 260))
                    kline_rows: list[dict[str, Any]] = []
                    for _, row in rows.iterrows():
                        kline_rows.append(
                            {
                                "date": _date_text(row.get("trade_date")),
                                "open": _to_float_or_none(row.get("open")),
                                "high": _to_float_or_none(row.get("high")),
                                "low": _to_float_or_none(row.get("low")),
                                "close": _to_float_or_none(row.get("close")),
                                "change_pct": _to_float_or_none(row.get("change_pct")),
                                "volume": _to_float_or_none(row.get("volume")),
                                "amount": _to_float_or_none(row.get("amount")),
                                "synthetic_ohlc": False,
                                "source": f"kline_daily:{code}",
                            }
                        )
                    if kline_rows:
                        out[sector_name] = kline_rows

        code_to_name: dict[str, str] = dict(sector_code_to_name)
        if clickhouse_table_exists("sectors"):
            quoted_names = _quote_sql_list(names)
            expanded_names = sorted(
                {
                    item
                    for name in names
                    for item in (
                        name,
                    )
                    if item
                }
            )
            quoted_expanded_names = _quote_sql_list(expanded_names)
            sector_df = clickhouse_query_df(
                f"""
                SELECT code, name
                FROM sectors
                WHERE name IN ({quoted_expanded_names})
                   OR code IN ({quoted_expanded_names})
                """
            )
            if not sector_df.empty:
                for _, row in sector_df.iterrows():
                    code = str(row.get("code") or "").strip()
                    name = str(row.get("name") or "").strip()
                    if code and name:
                        code_to_name[code] = name

        for name in names:
            code_to_name.setdefault(name, name)

        codes = sorted(code_to_name)
        quoted_codes = _quote_sql_list(codes)
        if not quoted_codes:
            return {}
        if clickhouse_table_exists("sector_kline_daily"):
            schema = clickhouse_query_df("DESCRIBE TABLE sector_kline_daily")
            available_columns = set(schema["name"].astype(str).tolist()) if not schema.empty and "name" in schema.columns else set()
            has_ohlc = {"open", "high", "low", "close"}.issubset(available_columns)
            price_columns = "open, high, low, close," if has_ohlc else ""
            raw = clickhouse_query_df(
                f"""
                SELECT code, trade_date, {price_columns} change_pct, total_volume, total_amount
                FROM sector_kline_daily
                WHERE code IN ({quoted_codes})
                  AND trade_date >= today() - INTERVAL 430 DAY
                ORDER BY code, trade_date
                """
            )
            if not raw.empty:
                for code, frame in raw.groupby(raw["code"].astype(str), dropna=False):
                    sector_name = code_to_name.get(str(code), str(code))
                    rows = frame.sort_values("trade_date").tail(max(int(days), 260))
                    kline_rows: list[dict[str, Any]] = []
                    synthetic_close = 1000.0
                    for _, row in rows.iterrows():
                        change_pct = _to_float_or_none(row.get("change_pct"))
                        if has_ohlc:
                            open_price = _to_float_or_none(row.get("open"))
                            high_price = _to_float_or_none(row.get("high"))
                            low_price = _to_float_or_none(row.get("low"))
                            close_price = _to_float_or_none(row.get("close"))
                        else:
                            open_price = synthetic_close
                            close_price = synthetic_close * (1.0 + ((change_pct or 0.0) / 100.0))
                            high_price = max(open_price, close_price)
                            low_price = min(open_price, close_price)
                            synthetic_close = close_price
                        kline_rows.append(
                            {
                                "date": _date_text(row.get("trade_date")),
                                "open": open_price,
                                "high": high_price,
                                "low": low_price,
                                "close": close_price,
                                "change_pct": change_pct,
                                "volume": _to_float_or_none(row.get("total_volume")),
                                "amount": _to_float_or_none(row.get("total_amount")),
                                "synthetic_ohlc": not has_ohlc,
                                "source": "sector_kline_daily",
                            }
                        )
                    if kline_rows and sector_name not in out:
                        out[sector_name] = kline_rows

        missing_tdx_codes = {
            code: name
            for code, name in code_to_name.items()
            if code.endswith((".SH", ".SZ"))
            and code[:3].isdigit()
            and code.startswith("880")
            and name
            and name not in out
        }
        if missing_tdx_codes and clickhouse_table_exists("source_sector_stocks") and clickhouse_table_exists("kline_daily"):
            for code, sector_name in missing_tdx_codes.items():
                quoted_code = _quote_sql_text(code)
                synthetic_raw = clickhouse_query_df(
                    f"""
                    SELECT
                        m.sector_code AS sector_code,
                        kd.trade_date AS trade_date,
                        avg(kd.close / nullIf(bp.base_close, 0)) * 1000.0 AS close_index,
                        avg(kd.change_pct) AS avg_change_pct,
                        sum(kd.volume) AS total_volume,
                        sum(kd.amount) AS total_amount,
                        count() AS member_count
                    FROM kline_daily AS kd
                    INNER JOIN (
                        SELECT DISTINCT sector_code, canonical_code
                        FROM source_sector_stocks
                        WHERE source = 'tdx'
                          AND snapshot_date = (SELECT max(snapshot_date) FROM source_sector_stocks WHERE source = 'tdx')
                          AND alias_status != 'inactive'
                          AND sector_code = {quoted_code}
                          AND canonical_code != ''
                    ) AS m ON m.canonical_code = kd.code
                    INNER JOIN (
                        SELECT kd1.code AS code, argMin(kd1.close, kd1.trade_date) AS base_close
                        FROM kline_daily AS kd1
                        INNER JOIN (
                            SELECT DISTINCT canonical_code
                            FROM source_sector_stocks
                            WHERE source = 'tdx'
                              AND snapshot_date = (SELECT max(snapshot_date) FROM source_sector_stocks WHERE source = 'tdx')
                              AND alias_status != 'inactive'
                              AND sector_code = {quoted_code}
                              AND canonical_code != ''
                        ) AS cm ON cm.canonical_code = kd1.code
                        WHERE kd1.trade_date >= today() - INTERVAL 430 DAY
                        GROUP BY kd1.code
                    ) AS bp ON bp.code = kd.code
                    WHERE kd.trade_date >= today() - INTERVAL 430 DAY
                    GROUP BY m.sector_code, kd.trade_date
                    ORDER BY m.sector_code, kd.trade_date
                    """
                )
                if synthetic_raw.empty:
                    continue
                rows = synthetic_raw.sort_values("trade_date").tail(max(int(days), 260))
                kline_rows = []
                previous_close: float | None = None
                for _, row in rows.iterrows():
                    close_price = _to_float_or_none(row.get("close_index"))
                    if close_price is None:
                        continue
                    open_price = previous_close if previous_close is not None else close_price
                    change_pct = _to_float_or_none(row.get("avg_change_pct"))
                    high_price = max(open_price, close_price)
                    low_price = min(open_price, close_price)
                    previous_close = close_price
                    kline_rows.append(
                        {
                            "date": _date_text(row.get("trade_date")),
                            "open": open_price,
                            "high": high_price,
                            "low": low_price,
                            "close": close_price,
                            "change_pct": change_pct,
                            "volume": _to_float_or_none(row.get("total_volume")),
                            "amount": _to_float_or_none(row.get("total_amount")),
                            "member_count": _to_float_or_none(row.get("member_count")),
                            "synthetic_ohlc": True,
                            "source": "tdx_member_equal_weight_daily",
                        }
                    )
                if kline_rows:
                    out[sector_name] = kline_rows
        return out
    except Exception as exc:
        logger.warning("Failed to load mainwave sector kline data: %s", exc)
        return {}


def _build_mainwave_sector_component_map(sectors: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    sector_codes = sorted(
        {
            str(item.get("sector_code") or "").strip()
            for item in sectors
            if str(item.get("sector_code") or "").strip()
        }
    )
    if not sector_codes:
        return {}
    try:
        from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists

        if not clickhouse_table_exists("sector_stocks"):
            return {}
        quoted_codes = _quote_sql_list(sector_codes)
        if not quoted_codes:
            return {}
        has_stocks = clickhouse_table_exists("stocks")
        has_kline = clickhouse_table_exists("kline_daily")
        name_expr = "anyOrNull(st.name)" if has_stocks else "CAST(NULL, 'Nullable(String)')"
        latest_join = ""
        latest_select = (
            "CAST(NULL, 'Nullable(Date)') AS trade_date, "
            "CAST(NULL, 'Nullable(Float64)') AS close, "
            "CAST(NULL, 'Nullable(Float64)') AS change_pct, "
            "CAST(NULL, 'Nullable(Float64)') AS amount"
        )
        if has_kline:
            latest_join = """
                LEFT JOIN
                (
                    SELECT code, trade_date, close, change_pct, amount
                    FROM kline_daily
                    WHERE (code, trade_date) IN
                    (
                        SELECT code, max(trade_date)
                        FROM kline_daily
                        GROUP BY code
                    )
                ) AS kd ON kd.code = ss.stock_code
            """
            latest_select = "anyOrNull(kd.trade_date) AS trade_date, anyOrNull(kd.close) AS close, anyOrNull(kd.change_pct) AS change_pct, anyOrNull(kd.amount) AS amount"
        stock_join = "LEFT JOIN stocks AS st ON st.code = ss.stock_code" if has_stocks else ""
        raw = clickhouse_query_df(
            f"""
            SELECT
                ss.sector_code AS sector_code,
                ss.stock_code AS code,
                {name_expr} AS name,
                anyOrNull(ss.weight) AS weight,
                {latest_select}
            FROM sector_stocks AS ss
            {stock_join}
            {latest_join}
            WHERE ss.sector_code IN ({quoted_codes})
            GROUP BY ss.sector_code, ss.stock_code
            ORDER BY ss.sector_code, weight DESC, amount DESC, ss.stock_code
            """
        )
        if raw.empty:
            return {}
        out: dict[str, list[dict[str, Any]]] = {}
        for sector_code, frame in raw.groupby(raw["sector_code"].astype(str), dropna=False):
            rows: list[dict[str, Any]] = []
            for _, row in frame.iterrows():
                code = str(row.get("code") or "").strip()
                if not code:
                    continue
                rows.append(
                    {
                        "code": code,
                        "name": str(row.get("name") or "").strip() or code,
                        "weight": _to_float_or_none(row.get("weight")),
                        "latest_date": _date_text(row.get("trade_date")),
                        "latest_close": _to_float_or_none(row.get("close")),
                        "change_pct": _to_float_or_none(row.get("change_pct")),
                        "amount": _to_float_or_none(row.get("amount")),
                    }
                )
            out[str(sector_code)] = rows
        return out
    except Exception as exc:
        logger.warning("Failed to load mainwave sector component stocks: %s", exc)
        return {}


def _mainwave_kline_window_return(rows: list[dict[str, Any]], days: int) -> float | None:
    if not rows:
        return None
    span = rows[-min(max(int(days), 2), len(rows)) :]
    if len(span) < 2:
        return None
    first_close = _to_float_or_none(span[0].get("close"))
    last_close = _to_float_or_none(span[-1].get("close"))
    if first_close is None or first_close <= 0 or last_close is None:
        return None
    return (last_close / first_close) - 1.0


def _apply_mainwave_sector_trend_adjustment(sectors: list[dict[str, Any]]) -> None:
    for sector in sectors:
        raw_diffusion = _to_float_or_none(sector.get("avg_sector_diffusion_score"))
        if raw_diffusion is None:
            continue
        rows = sector.get("kline_daily")
        if not isinstance(rows, list) or not rows:
            continue
        ret20 = _mainwave_kline_window_return(rows, 20)
        ret60 = _mainwave_kline_window_return(rows, 60)
        ret120 = _mainwave_kline_window_return(rows, 120)
        latest_change = _to_float_or_none(rows[-1].get("change_pct"))
        sector["raw_sector_diffusion_score"] = raw_diffusion
        sector["sector_ret20"] = ret20
        sector["sector_ret60"] = ret60
        sector["sector_ret120"] = ret120
        penalty = 0.0
        reasons: list[str] = []
        if ret20 is not None and ret20 < 0:
            penalty += min(8.0, abs(ret20) * 100.0 * 0.8)
            reasons.append("20日趋势为负")
        if ret60 is not None and ret60 < 0:
            penalty += min(12.0, abs(ret60) * 100.0 * 0.7)
            reasons.append("60日趋势为负")
        if ret120 is not None and ret120 < 0:
            penalty += min(10.0, abs(ret120) * 100.0 * 0.45)
            reasons.append("120日趋势为负")
        if latest_change is not None and latest_change < -2.0:
            penalty += 4.0
            reasons.append("最新交易日明显回落")
        adjusted = max(0.0, raw_diffusion - penalty)
        if ret20 is not None and ret60 is not None and ret120 is not None and ret20 < 0 and ret60 < 0 and ret120 < 0:
            adjusted = min(adjusted, 64.9)
            reasons.append("短中长期板块趋势均退潮")
            if not sector.get("candidate_count"):
                sector["state"] = "sector_watch"
                sector["state_label"] = "退潮观察"
        sector["avg_sector_diffusion_score"] = adjusted
        sector["sector_trend_penalty"] = penalty
        sector["sector_trend_adjustment_reason"] = "；".join(reasons) if reasons else ""
        sector["rank_score"] = (
            adjusted * 0.55
            + (_to_float_or_none(sector.get("max_wave_style_score")) or 0.0) * 0.35
            + min(int(sector.get("candidate_count") or 0), 8) * 2.0
            + min(int(sector.get("watch_candidate_count") or 0), 8) * 1.2
            + int(sector.get("recommended_count") or 0) * 5.0
        )
    sectors.sort(key=lambda item: _to_float_or_none(item.get("rank_score")) or 0, reverse=True)


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


def _filter_open_shadow_ledger_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    open_states = {
        "open",
        "opened",
        "holding",
        "open_shadow",
        "shadow_holding",
        "position_open",
        "active",
    }
    planned_only_states = {"planned", "qualified_shadow_buy", "candidate", "ticket"}
    closed_states = {"closed", "sold", "exit", "exited", "cancelled", "canceled", "rejected"}
    for row in records or []:
        if not isinstance(row, dict):
            continue
        exit_date = _date_text(
            row.get("exit_date")
            or row.get("policy_exit_date")
            or row.get("closed_at")
            or row.get("sell_date")
            or row.get("sell_datetime")
        )
        if exit_date:
            continue
        state_text = " ".join(
            str(row.get(key) or "").strip().lower()
            for key in ["trade_status", "position_status", "holding_status", "last_state", "shadow_status"]
        )
        states = {part for part in re.split(r"[^a-z0-9_]+", state_text) if part}
        if states & closed_states:
            continue
        if states & open_states:
            out.append(row)
            continue
        if states and states <= planned_only_states:
            continue
    return out


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
    live_enabled = _truthy(source.get("live_enabled")) and G2_GAP_SUPPLEMENT_LIVE_ENABLED
    retire_reason = source.get("retire_reason") or summary.get("g2_gap_supplement_retire_reason") or G2_GAP_SUPPLEMENT_RETIRE_REASON
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
    if not live_enabled:
        status = "retired_from_live_trading_observe"
        message = f"G2 空档补位已退出实盘交易，仅保留观察；当前源候选 {rows} 条。"
    elif fresh_for_entry_date:
        message = f"G2 空档补位源已覆盖 {requested_entry_date}，当前源候选 {rows} 条。"
    elif stale_reason:
        message = f"G2 空档补位源未覆盖 {requested_entry_date}，最新源日期 {latest_source_date or '--'}。"
    else:
        message = f"G2 空档补位源已读取，但当前无候选，状态 {status}。"

    return {
        "ok": bool(fresh_for_entry_date and live_enabled),
        "live_enabled": bool(live_enabled),
        "retired_from_live_trading": not live_enabled,
        "retire_reason": retire_reason,
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
        "heartbeat_enabled": False,
        "heartbeat_minutes": 30,
        "recipient_email": "",
        "run_current_refresh": True,
        "paper_entry_enabled": True,
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
    # Notifications are event-driven: normal operation does not send email.
    base["heartbeat_enabled"] = False
    base["heartbeat_minutes"] = max(30, int(base.get("heartbeat_minutes") or 30))
    base["recipient_email"] = str(base.get("recipient_email") or "").strip()
    base["run_current_refresh"] = bool(base.get("run_current_refresh", True))
    base["paper_entry_enabled"] = bool(base.get("paper_entry_enabled", True))
    if not isinstance(base.get("last_alert_keys"), dict):
        base["last_alert_keys"] = {}
    return base


def _compact_monitor_value(value: Any, depth: int = 0) -> Any:
    if depth >= 3:
        if isinstance(value, dict):
            return {"omitted": True, "type": "dict", "keys": list(value.keys())[:20]}
        if isinstance(value, list):
            return {"omitted": True, "type": "list", "count": len(value)}
        if isinstance(value, str) and len(value) > 1000:
            return value[:1000] + "...[truncated]"
        return value
    if isinstance(value, dict):
        return {
            str(key): _compact_monitor_value(item, depth + 1)
            for key, item in list(value.items())[:50]
        }
    if isinstance(value, list):
        return [_compact_monitor_value(item, depth + 1) for item in value[:20]]
    if isinstance(value, str) and len(value) > 1000:
        return value[:1000] + "...[truncated]"
    return value


def _compact_monitor_last_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return _compact_monitor_value(result)
    keep_keys = [
        "ok",
        "status",
        "skipped",
        "reason",
        "message",
        "checked_at",
        "started_at",
        "finished_at",
        "entry_date",
        "decision_date",
        "diagnosis_code",
        "selected_route",
        "task_id",
        "source",
        "duration_seconds",
        "error",
        "blockers",
        "open_holding_count",
        "exit_candidate_count",
        "triggered_exit_count",
        "skipped_count",
        "saved_exits",
        "skipped",
        "pipeline_checks",
        "last_result",
    ]
    compacted = {
        key: _compact_monitor_value(result.get(key))
        for key in keep_keys
        if key in result
    }
    compacted["compacted"] = True
    return compacted


def _save_monitor_state(state: dict[str, Any]) -> None:
    payload = dict(state)
    if "last_result" in payload:
        payload["last_result"] = _compact_monitor_last_result(payload.get("last_result"))
    _write_json(MONITOR_STATE_PATH, payload)


def _default_exit_monitor_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "interval_seconds": 120,
        "trading_hours_only": True,
        "paper_exit_enabled": True,
        "update_ledger_enabled": True,
        "last_run_at": None,
        "last_success_at": None,
        "last_error": None,
        "last_result": None,
    }


def _load_exit_monitor_state() -> dict[str, Any]:
    state = _read_json(EXIT_MONITOR_STATE_PATH)
    base = _default_exit_monitor_state()
    if isinstance(state, dict):
        base.update(state)
    base["enabled"] = bool(base.get("enabled"))
    base["interval_seconds"] = max(120, int(base.get("interval_seconds") or 120))
    base["trading_hours_only"] = bool(base.get("trading_hours_only", True))
    base["paper_exit_enabled"] = bool(base.get("paper_exit_enabled", True))
    base["update_ledger_enabled"] = bool(base.get("update_ledger_enabled", True))
    return base


def _save_exit_monitor_state(state: dict[str, Any]) -> None:
    payload = dict(state)
    if "last_result" in payload:
        payload["last_result"] = _compact_monitor_last_result(payload.get("last_result"))
    _write_json(EXIT_MONITOR_STATE_PATH, payload)


def _default_daily_trend_exit_monitor_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "interval_seconds": 300,
        "trading_hours_only": True,
        "lookback_days": 180,
        "pivot_window": 3,
        "min_pivot_separation": 5,
        "break_buffer_pct": 0.0,
        "email_enabled": True,
        "alerted_signal_keys": [],
        "last_run_at": None,
        "last_success_at": None,
        "last_error": None,
        "last_result": None,
    }


def _load_daily_trend_exit_monitor_state() -> dict[str, Any]:
    state = _read_json(DAILY_TREND_EXIT_MONITOR_STATE_PATH)
    base = _default_daily_trend_exit_monitor_state()
    if isinstance(state, dict):
        base.update(state)
    base["enabled"] = bool(base.get("enabled"))
    base["interval_seconds"] = min(1800, max(300, int(base.get("interval_seconds") or 300)))
    base["trading_hours_only"] = bool(base.get("trading_hours_only", True))
    base["hour"] = min(23, max(0, int(base.get("hour") or 15)))
    base["minute"] = min(59, max(0, int(base.get("minute") or 10)))
    base["lookback_days"] = min(500, max(60, int(base.get("lookback_days") or 180)))
    base["pivot_window"] = min(10, max(1, int(base.get("pivot_window") or 3)))
    base["min_pivot_separation"] = min(30, max(1, int(base.get("min_pivot_separation") or 5)))
    base["break_buffer_pct"] = min(0.05, max(0.0, float(base.get("break_buffer_pct") or 0.0)))
    base["email_enabled"] = bool(base.get("email_enabled", True))
    keys = base.get("alerted_signal_keys")
    base["alerted_signal_keys"] = [str(item) for item in keys[-500:]] if isinstance(keys, list) else []
    return base


def _save_daily_trend_exit_monitor_state(state: dict[str, Any]) -> None:
    payload = dict(state)
    if "last_result" in payload:
        payload["last_result"] = _compact_monitor_last_result(payload.get("last_result"))
    _write_json(DAILY_TREND_EXIT_MONITOR_STATE_PATH, payload)


def _coerce_scheduler_minutes(value: Any, default: list[int]) -> list[int]:
    if value in (None, ""):
        items = default
    elif isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    minutes: list[int] = []
    for item in items:
        try:
            minute = int(item)
        except Exception:
            continue
        if 0 <= minute <= 59 and minute not in minutes:
            minutes.append(minute)
    return sorted(minutes)


def _default_observation_scheduler_state() -> dict[str, Any]:
    return {
        "enabled": True,
        "hour": 15,
        "minute": 40,
        "retry_hour": 10,
        "retry_minutes": [1, 6, 11, 16, 31, 36],
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
    base["retry_hour"] = min(23, max(0, int(base.get("retry_hour") or 10)))
    base["retry_minutes"] = _coerce_scheduler_minutes(base.get("retry_minutes"), [1, 6, 11, 16, 31, 36])
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


def _load_monitor_event_rows_with_repair() -> list[dict[str, Any]]:
    if not MONITOR_EVENTS_PATH.exists():
        return []
    try:
        data = json.loads(MONITOR_EVENTS_PATH.read_text(encoding="utf-8"))
        rows = data.get("events") if isinstance(data, dict) else []
        return rows if isinstance(rows, list) else []
    except Exception:
        # A previous non-atomic writer may have concatenated several complete
        # JSON documents. Recover their event arrays, preserve the source for
        # audit, then let the next append rewrite one valid document.
        raw = MONITOR_EVENTS_PATH.read_text(encoding="utf-8", errors="replace")
        decoder = json.JSONDecoder()
        cursor = 0
        recovered: list[dict[str, Any]] = []
        while cursor < len(raw):
            while cursor < len(raw) and raw[cursor].isspace():
                cursor += 1
            if cursor >= len(raw):
                break
            try:
                item, cursor = decoder.raw_decode(raw, cursor)
            except json.JSONDecodeError:
                break
            rows = item.get("events") if isinstance(item, dict) else []
            if isinstance(rows, list):
                recovered.extend(row for row in rows if isinstance(row, dict))
        backup = MONITOR_EVENTS_PATH.with_name(
            f"{MONITOR_EVENTS_PATH.stem}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}{MONITOR_EVENTS_PATH.suffix}"
        )
        if not backup.exists():
            backup.write_text(raw, encoding="utf-8")
        logger.warning("Recovered %s monitor events from malformed artifact: %s", len(recovered), MONITOR_EVENTS_PATH)
        return recovered[-300:]


def _append_monitor_event(event: dict[str, Any]) -> None:
    with MONITOR_EVENTS_LOCK:
        rows = _load_monitor_event_rows_with_repair()
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


def _build_shadow_buy_email_body(
    now_text: str,
    workflow: dict[str, Any],
    tickets: list[dict[str, Any]],
    runtime: dict[str, Any] | None = None,
) -> str:
    runtime = runtime if isinstance(runtime, dict) else {}
    summary_lines = [
        f"生成时间：{now_text}",
        "报告范围：AiStock 全局运行快照",
        f"G3策略：{STRATEGY_NAME_CN}（{STRATEGY_NAME}）",
        f"买入候选日期：{workflow.get('entry_date') or '--'}",
        f"当前选中路线：{workflow.get('selected_route') or '--'}",
        f"诊断结果：{_email_diagnosis_label(workflow.get('diagnosis_code'))}",
        "",
        *_build_aistock_data_source_email_lines(workflow, runtime),
        "",
        *_build_aistock_feedback_email_lines(workflow, runtime),
        "",
        "影子买入候选：",
    ]
    for idx, item in enumerate(tickets, start=1):
        summary_lines.append(
            (
                f"{idx}. {item.get('code') or item.get('code_raw') or '--'} "
                f"{item.get('name') or item.get('stock_name') or ''} | "
                f"路线：{item.get('route_label') or item.get('route') or '--'} | "
                f"计划买入：{item.get('planned_entry_ts') or '--'} | "
                f"确认时间：{item.get('confirm_datetime') or '--'} | "
                f"建议仓位：{_fmt_num(float(item.get('position_pct') or 0) * 100, 1, '%')} | "
                f"参考价：{_fmt_num(item.get('reference_close'), 2)} | "
                f"结构止损/硬止损：{_fmt_num(item.get('structure_stop'), 2)} / {_fmt_num(item.get('hard_stop'), 2)} | "
                f"30分钟确认：{item.get('m30_status') or item.get('m30_confirmed') or '--'}"
            )
        )
        exit_contract = str(item.get("exit_contract") or "").strip()
        if exit_contract:
            summary_lines.append(f"   卖出合同：{exit_contract[:180]}")
    summary_lines.extend(["", "安全边界：当前仅作影子观察；没有正式买入信号，也不会自动下单。"])
    return "\n".join(summary_lines).strip() + "\n"


def _send_shadow_exit_notification(records: list[dict[str, Any]]) -> dict[str, Any]:
    monitor = _load_monitor_state()
    if not monitor.get("email_enabled", True):
        return {"sent": False, "reason": "email_disabled"}
    lines = ["影子盘卖出事件（未发送真实委托）："]
    for item in records:
        lines.append(
            f"- {item.get('code') or '--'} {item.get('name') or ''} | "
            f"原因：{item.get('exit_reason') or '--'} | "
            f"价格：{_fmt_num(item.get('execution_price'), 2)} | "
            f"数量：{item.get('quantity') or 0} | "
            f"收益：{_fmt_num((_to_float(item.get('realized_ret')) or 0) * 100, 2, '%')}"
        )
    try:
        return _send_shadow_email(
            f"AiStock {datetime.now().strftime('%Y-%m-%d %H:%M')} 影子盘卖出",
            "\n".join(lines) + "\n",
            monitor.get("recipient_email") or None,
        )
    except Exception as exc:
        _append_monitor_event({"type": "email_error", "status": "error", "message": "Shadow exit email failed", "error": str(exc)})
        return {"sent": False, "error": str(exc)}


def _send_exception_notification(subject: str, detail: str) -> dict[str, Any]:
    monitor = _load_monitor_state()
    if not monitor.get("email_enabled", True):
        return {"sent": False, "reason": "email_disabled"}
    try:
        return _send_shadow_email(
            f"AiStock 异常：{subject}",
            f"时间：{datetime.now().isoformat(sep=' ', timespec='seconds')}\n异常：{detail}\n安全边界：未发送真实委托。\n",
            monitor.get("recipient_email") or None,
        )
    except Exception as exc:
        _append_monitor_event({"type": "email_error", "status": "error", "message": "Exception email failed", "error": str(exc)})
        return {"sent": False, "error": str(exc)}


def _notify_holding_tick_qmt_unavailable(
    codes: list[str],
    source: dict[str, Any],
    exc: Exception,
    *,
    cooldown_minutes: int = 30,
) -> dict[str, Any]:
    monitor = _load_monitor_state()
    now = datetime.now()
    now_text = now.isoformat(sep=" ", timespec="seconds")
    if not monitor.get("email_enabled", True):
        return {"sent": False, "reason": "email_disabled"}
    if monitor.get("trading_hours_only", True) and not _in_trading_window(now):
        return {"sent": False, "reason": "not_trading_window", "checked_at": now_text}

    state = _read_json(HOLDING_TICK_ALERT_STATE_PATH)
    last_text = str(state.get("last_unavailable_alert_at") or state.get("last_unavailable_alert_attempt_at") or "").strip()
    last_dt = None
    if last_text:
        try:
            last_dt = datetime.fromisoformat(last_text)
        except Exception:
            last_dt = None
    if last_dt is not None and (now - last_dt).total_seconds() < max(1, cooldown_minutes) * 60:
        return {"sent": False, "reason": "cooldown", "last_sent_at": last_text}

    normalized_codes = [str(code or "").strip().upper() for code in codes if str(code or "").strip()]
    error_text = f"{type(exc).__name__}: {exc}"
    provider = str(source.get("provider") or "QMT xtdata").strip()
    bridge_url = str(source.get("bridge_url") or os.getenv("AISTOCK_QMT_TICK_BRIDGE_URL") or "").strip()
    body = "\n".join(
        [
            "QMT Tick 行情读取失败，持仓盘口与做T确认已进入只读阻断状态。",
            f"时间：{now_text}",
            f"来源：{provider}",
            f"桥接地址：{bridge_url or '--'}",
            f"标的数量：{len(normalized_codes)}",
            f"标的样例：{', '.join(normalized_codes[:20]) or '--'}",
            f"错误：{error_text}",
            "",
            "安全边界：本通知不会发送任何真实委托；请检查 QMT、xtdata 或 host bridge 服务是否在线。",
        ]
    ) + "\n"
    try:
        send_result = _send_shadow_email(
            f"[AiStock 异常] QMT Tick 行情不可用 {now_text[11:16]}",
            body,
            monitor.get("recipient_email") or None,
        )
        payload = {
            "status": "unavailable",
            "last_unavailable_alert_at": now_text,
            "last_unavailable_error": error_text,
            "last_codes": normalized_codes[:50],
            "last_send_result": send_result,
        }
        _write_json(HOLDING_TICK_ALERT_STATE_PATH, payload)
        result = {"sent": True, "checked_at": now_text, **send_result}
        _append_monitor_event({"type": "qmt_tick_unavailable", "status": "alert_sent", "error": error_text, "codes": normalized_codes[:20]})
        return result
    except Exception as mail_exc:
        payload = {
            "status": "unavailable",
            "last_unavailable_alert_attempt_at": now_text,
            "last_unavailable_error": error_text,
            "last_send_error": str(mail_exc),
            "last_codes": normalized_codes[:50],
        }
        _write_json(HOLDING_TICK_ALERT_STATE_PATH, payload)
        result = {"sent": False, "error": str(mail_exc), "checked_at": now_text}
        _append_monitor_event({"type": "qmt_tick_unavailable", "status": "email_error", "error": error_text, "email_error": str(mail_exc)})
        return result


def _mark_holding_tick_qmt_available(codes: list[str], source: dict[str, Any]) -> None:
    state = _read_json(HOLDING_TICK_ALERT_STATE_PATH)
    if state.get("status") != "unavailable":
        return
    now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
    normalized_codes = [str(code or "").strip().upper() for code in codes if str(code or "").strip()]
    recovery_email: dict[str, Any] | None = None
    recovery_email_error: str | None = None
    monitor = _load_monitor_state()
    if state.get("last_unavailable_alert_at") and monitor.get("email_enabled", True):
        body = "\n".join(
            [
                "QMT Tick 行情读取已恢复，持仓盘口与做T确认恢复只读扫描。",
                f"恢复时间：{now_text}",
                f"前次异常时间：{state.get('last_unavailable_alert_at') or '--'}",
                f"来源：{source.get('provider') or 'QMT xtdata'}",
                f"标的样例：{', '.join(normalized_codes[:20]) or '--'}",
                "",
                "安全边界：本通知不会发送任何真实委托；只是关闭上一条 QMT Tick 异常提醒。",
            ]
        ) + "\n"
        try:
            recovery_email = _send_shadow_email(
                f"[AiStock 恢复] QMT Tick 行情已恢复 {now_text[11:16]}",
                body,
                monitor.get("recipient_email") or None,
            )
        except Exception as exc:
            recovery_email_error = str(exc)
    state["status"] = "available"
    state["last_recovered_at"] = now_text
    state["last_recovered_codes"] = normalized_codes[:50]
    if recovery_email is not None:
        state["last_recovery_email"] = recovery_email
    if recovery_email_error:
        state["last_recovery_email_error"] = recovery_email_error
    _write_json(HOLDING_TICK_ALERT_STATE_PATH, state)
    _append_monitor_event({
        "type": "qmt_tick_recovered",
        "status": "available",
        "provider": source.get("provider"),
        "codes": normalized_codes[:20],
        "email_sent": bool(recovery_email),
        "email_error": recovery_email_error,
    })


def _build_heartbeat_email_body(
    now_text: str,
    workflow: dict[str, Any],
    runtime: dict[str, Any] | None = None,
) -> str:
    """Keep routine heartbeats short; detailed evidence belongs to alerts and the UI."""
    runtime = runtime if isinstance(runtime, dict) else {}
    summary = runtime.get("summary") if isinstance(runtime.get("summary"), dict) else {}
    tickets = runtime.get("tickets") if isinstance(runtime.get("tickets"), list) else []
    ledger = runtime.get("ledger") if isinstance(runtime.get("ledger"), list) else []
    monitor = workflow.get("monitor") if isinstance(workflow.get("monitor"), dict) else {}
    last_entry = monitor.get("last_entry_result") if isinstance(monitor.get("last_entry_result"), dict) else {}
    last_refresh = monitor.get("last_result") if isinstance(monitor.get("last_result"), dict) else {}
    repair = last_refresh.get("ledger_reconciliation") if isinstance(last_refresh.get("ledger_reconciliation"), dict) else {}
    if not repair:
        repair = last_entry.get("ledger_reconciliation") if isinstance(last_entry.get("ledger_reconciliation"), dict) else {}
    status = "正常" if workflow.get("ok") else "异常"
    lines = [
        f"生成时间：{now_text}",
        f"运行：{status} | 路线：{workflow.get('selected_route') or summary.get('selected_route') or '--'} | 诊断：{_email_diagnosis_label(workflow.get('diagnosis_code') or summary.get('diagnosis_code'))}",
        f"候选：影子票据 {len(tickets)} 张，合格影子买点 {_email_count(summary.get('qualified_shadow_buy_rows'))} 条；影子持仓 {len(ledger)} 条。",
        "安全边界：仅纸面观察；正式买点=0，自动下单=0，真实订单=0。",
    ]
    if repair.get("repaired_count"):
        lines.append(f"自动修复：已从纸面成交记录补回 {repair['repaired_count']} 条影子台账。")
    elif repair.get("unreconciled_count"):
        lines.append(f"需关注：仍有 {repair['unreconciled_count']} 条纸面成交未能写入影子台账。")
    if workflow.get("blockers"):
        lines.append(f"阻断：{len(workflow['blockers'])} 项，详见系统页面。")
    return "\n".join(lines) + "\n"


def _email_count(value: Any) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


def _email_bool_label(value: Any) -> str:
    return "是" if bool(value) else "否"


def _email_diagnosis_label(value: Any) -> str:
    text = str(value or "").strip()
    labels = {
        "NO_STATE_ROUTER_CANDIDATE": "无状态路由候选",
        "HAS_STATE_ROUTER_SHADOW_CANDIDATE": "有状态路由影子候选",
        "NO_FORMAL_BUY_SIGNAL": "无正式买点",
        "FORMAL_BUY_SIGNAL_LOCKED": "正式买点已锁定",
    }
    return f"{labels[text]}（{text}）" if text in labels else (text or "--")


def _email_status_label(value: Any) -> str:
    text = str(value or "").strip()
    labels = {
        "validated": "已验证",
        "pending": "待处理",
        "issue_found": "发现问题",
        "continue_watch": "继续观察",
        "manual_approved": "人工放行",
        "paper_watch": "纸面观察",
        "wait_refresh": "等待刷新",
        "skip": "跳过",
        "reject": "否决",
        "paper_exit_submitted": "纸面卖出已提交",
        "paper_buy_submitted": "纸面买入已提交",
        "submitted": "已提交",
        "error": "异常",
        "unknown": "未知",
    }
    return f"{labels[text]}（{text}）" if text in labels else (text or "--")


def _email_source_label(value: Any) -> str:
    text = str(value or "").strip()
    labels = {
        "qmtmini_readonly_snapshot": "QMT Mini只读快照",
        "qmtmini_trade_snapshot": "QMT Mini成交快照",
        "ths_export_file": "同花顺导出文件",
        "ths_ui_export_file": "同花顺界面导出文件",
        "ths_clipboard": "同花顺剪贴板",
        "tdx_gateway_ths_bridge": "TDX网关转接同花顺",
    }
    return f"{labels[text]}（{text}）" if text in labels else (text or "--")


def _count_dict_values(rows: dict[str, Any], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in rows.values():
        if not isinstance(item, dict):
            continue
        value = _email_status_label(item.get(field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _format_count_map(counts: dict[str, int], limit: int = 6) -> str:
    if not counts:
        return "--"
    items = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    return ", ".join(f"{key}={value}" for key, value in items)


def _build_aistock_data_source_email_lines(workflow: dict[str, Any], runtime: dict[str, Any]) -> list[str]:
    summary = runtime.get("summary") if isinstance(runtime.get("summary"), dict) else {}
    target_date = (
        _date_text(workflow.get("decision_date"))
        or _date_text(summary.get("decision_date"))
        or _date_text(workflow.get("entry_date"))
        or _date_text(summary.get("entry_date"))
    )
    lines = ["AiStock 数据源录入情况："]
    try:
        source_cfg = app_config.get("data_sources", default={}, config_file="settings.yaml") or {}
        preferred = source_cfg.get("preferred_source") or source_cfg.get("primary") or "qmt_xtquant"
        lines.append(f"- 主数据源：{preferred}")
    except Exception:
        lines.append("- 主数据源：qmt_xtquant")
    try:
        from utils.market_warehouse import clickhouse_available, clickhouse_client, clickhouse_table_exists

        if not clickhouse_available():
            lines.append("- ClickHouse：不可用")
            return lines
        client = clickhouse_client()
        if not target_date:
            latest_candidates: list[str] = []
            for table_name, column in [
                ("kline_daily", "trade_date"),
                ("kline_minute_5", "toDate(datetime)"),
                ("kline_minute_15", "toDate(datetime)"),
                ("kline_minute_30", "toDate(datetime)"),
            ]:
                if not clickhouse_table_exists(table_name):
                    continue
                rows = client.query(f"SELECT max({column}) FROM {table_name}").result_rows
                if rows and rows[0][0] is not None:
                    latest_candidates.append(str(rows[0][0])[:10])
            target_date = max(latest_candidates) if latest_candidates else ""
        lines.append(f"- 统计日期：{target_date or '--'}")
        if clickhouse_table_exists("stocks"):
            rows = client.query("SELECT count(), countIf(type = 'stock'), countIf(type != 'stock') FROM stocks").result_rows
            if rows:
                lines.append(f"- 股票池/指数池：总数 {int(rows[0][0] or 0)}，股票 {int(rows[0][1] or 0)}，指数或其他 {int(rows[0][2] or 0)}")
        if target_date and clickhouse_table_exists("kline_daily"):
            rows = client.query(
                f"""
                SELECT uniqExact(code), count(), max(trade_date)
                FROM kline_daily
                WHERE trade_date = toDate('{target_date}')
                """
            ).result_rows
            if rows:
                row_count = int(rows[0][1] or 0)
                latest = str(rows[0][2])[:10] if row_count > 0 and rows[0][2] is not None else "--"
                lines.append(f"- 日线1d：覆盖 {int(rows[0][0] or 0)} 个代码，记录 {row_count} 行，最新日期 {latest}")
        for period, table_name in [("5m", "kline_minute_5"), ("15m", "kline_minute_15"), ("30m", "kline_minute_30"), ("60m", "kline_minute_60")]:
            if not clickhouse_table_exists(table_name):
                lines.append(f"- 分钟线{period}：表不存在（{table_name}）")
                continue
            if not target_date:
                continue
            rows = client.query(
                f"""
                SELECT uniqExact(code), count(), max(datetime)
                FROM {table_name}
                WHERE toDate(datetime) = toDate('{target_date}')
                """
            ).result_rows
            if rows:
                row_count = int(rows[0][1] or 0)
                latest = str(rows[0][2]) if row_count > 0 and rows[0][2] is not None else "--"
                lines.append(f"- 分钟线{period}：覆盖 {int(rows[0][0] or 0)} 个代码，记录 {row_count} 行，最新时间 {latest}")
        if target_date and clickhouse_table_exists("sector_kline_daily"):
            rows = client.query(
                f"""
                SELECT count(), max(trade_date)
                FROM sector_kline_daily
                WHERE trade_date = toDate('{target_date}')
                """
            ).result_rows
            if rows:
                row_count = int(rows[0][0] or 0)
                latest = str(rows[0][1])[:10] if row_count > 0 and rows[0][1] is not None else "--"
                lines.append(f"- 板块日线：记录 {row_count} 行，最新日期 {latest}")
        if target_date and clickhouse_table_exists("qmt_intraday_latest_5m"):
            rows = client.query(
                f"""
                SELECT uniqExact(code), max(datetime)
                FROM qmt_intraday_latest_5m
                WHERE trade_date = toDate('{target_date}')
                """
            ).result_rows
            if rows:
                code_count = int(rows[0][0] or 0)
                latest = str(rows[0][1]) if code_count > 0 and rows[0][1] is not None else "--"
                lines.append(f"- QMT盘中最新5分钟：覆盖 {code_count} 个代码，最新时间 {latest}")
    except Exception as exc:
        lines.append(f"- 覆盖检查异常：{type(exc).__name__}: {exc}")
    return lines


def _build_aistock_feedback_email_lines(workflow: dict[str, Any], runtime: dict[str, Any]) -> list[str]:
    summary = runtime.get("summary") if isinstance(runtime.get("summary"), dict) else {}
    tickets = runtime.get("tickets") if isinstance(runtime.get("tickets"), list) else []
    ledger = runtime.get("ledger") if isinstance(runtime.get("ledger"), list) else []
    broker_state = _read_json(BROKER_STATE_PATH)
    broker_capital = broker_state.get("capital") if isinstance(broker_state.get("capital"), dict) else {}
    broker_holdings = broker_state.get("holdings") if isinstance(broker_state.get("holdings"), list) else []
    pretrade_review_data = _read_json(PRETRADE_TICKET_REVIEWS_PATH)
    formal_review_data = _read_json(FORMAL_ACTION_REVIEWS_PATH)
    paper_execution_data = _read_json(PAPER_EXECUTIONS_PATH)
    pretrade_reviews = pretrade_review_data.get("reviews") if isinstance(pretrade_review_data, dict) else {}
    formal_reviews = formal_review_data.get("reviews") if isinstance(formal_review_data, dict) else {}
    pretrade_reviews = pretrade_reviews if isinstance(pretrade_reviews, dict) else {}
    formal_reviews = formal_reviews if isinstance(formal_reviews, dict) else {}
    paper_executions = paper_execution_data.get("executions") if isinstance(paper_execution_data, dict) else []
    paper_executions = paper_executions if isinstance(paper_executions, list) else []
    pretrade_smoke = _read_json(PRETRADE_SMOKE_PATH)
    smoke_summary = pretrade_smoke.get("summary") if isinstance(pretrade_smoke.get("summary"), dict) else {}
    lines = [
        "AiStock 买卖点反馈：",
        (
            "- 买入候选："
            f"票据 {len(tickets)} 条，"
            f"合格影子买点 {_email_count(summary.get('qualified_shadow_buy_rows'))} 条，"
            f"正式买点 {_email_count(summary.get('formal_buy_signal_rows'))} 条，"
            f"自动下单许可 {_email_count(summary.get('auto_order_allowed_rows'))} 条"
        ),
        (
            "- 路由状态："
            f"诊断 {_email_diagnosis_label(workflow.get('diagnosis_code') or summary.get('diagnosis_code'))}，"
            f"选中路线 {workflow.get('selected_route') or summary.get('selected_route') or '--'}，"
            f"影子持仓 {len(ledger)} 条"
        ),
        (
            "- 逐票盘前复盘："
            f"共 {len(pretrade_reviews)} 条，"
            f"动作分布：{_format_count_map(_count_dict_values(pretrade_reviews, 'review_action'))}"
        ),
        (
            "- 正式动作复盘："
            f"共 {len(formal_reviews)} 条，"
            f"状态分布：{_format_count_map(_count_dict_values(formal_reviews, 'review_status'))}"
        ),
        (
            "- 纸面执行反馈："
            f"记录 {len(paper_executions)} 条，"
            f"最新状态 {_email_status_label(paper_executions[-1].get('status') if paper_executions and isinstance(paper_executions[-1], dict) else '--')}"
        ),
        (
            "- 券商/QMT资金持仓快照："
            f"更新时间 {broker_state.get('updated_at') or '--'}，"
            f"来源 {_email_source_label(broker_state.get('holding_source'))}，"
            f"持仓 {len(broker_holdings)} 条，"
            f"可用现金 {_fmt_num(broker_capital.get('available_cash'), 2)}，"
            f"总资产 {_fmt_num(broker_capital.get('total_capital'), 2)}"
        ),
        (
            "- 盘前冒烟检查："
            f"生成时间 {pretrade_smoke.get('generated_at') or smoke_summary.get('generated_at') or '--'}，"
            f"是否就绪 {_email_bool_label(pretrade_smoke.get('ready') if 'ready' in pretrade_smoke else smoke_summary.get('ready')) if ('ready' in pretrade_smoke or 'ready' in smoke_summary) else '--'}，"
            f"正式就绪 {smoke_summary.get('pretrade_review_formal_ready_count', '--')} 条，"
            f"待处理动作 {smoke_summary.get('pretrade_action_pending_count', '--')} 条"
        ),
    ]
    return lines


def _build_strategy_result_email_body(
    now_text: str,
    workflow: dict[str, Any],
    runtime: dict[str, Any] | None = None,
) -> list[str]:
    runtime = runtime if isinstance(runtime, dict) else {}
    summary = runtime.get("summary") if isinstance(runtime.get("summary"), dict) else {}
    tickets = runtime.get("tickets") if isinstance(runtime.get("tickets"), list) else []
    ledger = runtime.get("ledger") if isinstance(runtime.get("ledger"), list) else []
    diagnostics = runtime.get("route_diagnostics") if isinstance(runtime.get("route_diagnostics"), list) else []
    last_result = workflow.get("last_result") if isinstance(workflow.get("last_result"), dict) else {}
    monitor = workflow.get("monitor") if isinstance(workflow.get("monitor"), dict) else {}
    exit_monitor = workflow.get("exit_monitor") if isinstance(workflow.get("exit_monitor"), dict) else {}
    generated_at = summary.get("generated_at") or last_result.get("finished_at") or workflow.get("checked_at") or now_text
    lines = [
        "G3 策略结果明细：",
        f"- 生成时间：{generated_at}",
        f"- 买入候选日期：{workflow.get('entry_date') or summary.get('entry_date') or '--'}",
        f"- 决策/采集日期：{workflow.get('decision_date') or summary.get('decision_date') or '--'}",
        f"- 诊断结果：{_email_diagnosis_label(workflow.get('diagnosis_code') or summary.get('diagnosis_code'))}",
        f"- 选中路线：{workflow.get('selected_route') or summary.get('selected_route') or '--'}",
        (
            "- 候选统计："
            f"源数据 {_email_count(summary.get('source_rows'))} 行，"
            f"可进入路由 {_email_count(summary.get('eligible_source_rows'))} 行，"
            f"被阻断 {_email_count(summary.get('blocked_source_rows'))} 行，"
            f"选中 {_email_count(summary.get('selected_rows'))} 行，"
            f"票据 {len(tickets)} 条，"
            f"合格影子买点 {_email_count(summary.get('qualified_shadow_buy_rows'))} 条"
        ),
        (
            "- 下单闸门："
            f"正式买点 {_email_count(summary.get('formal_buy_signal_rows'))} 条，"
            f"自动许可 {_email_count(summary.get('auto_order_allowed_rows'))} 条，"
            f"下单通道 {_email_count(summary.get('order_path_enabled_rows'))} 条，"
            f"实盘下单开关 {_email_bool_label(summary.get('live_order_enabled'))}"
        ),
        (
            "- 监控任务："
            f"影子买入监控 {_email_bool_label(monitor.get('scheduler_enabled'))}，"
            f"卖出监控 {_email_bool_label(exit_monitor.get('scheduler_enabled'))}，"
            f"最近刷新状态 {_email_status_label(last_result.get('status') or '--')}，"
            f"耗时 {last_result.get('duration_seconds') or '--'} 秒"
        ),
        f"- 当前影子持仓：{len(ledger)} 条",
    ]
    if diagnostics:
        lines.extend(["", "路由诊断："])
        for item in diagnostics[:5]:
            if not isinstance(item, dict):
                continue
            lines.append(
                (
                    f"- {item.get('route') or '--'}: "
                    f"总行数 {_email_count(item.get('rows'))}，"
                    f"可用 {_email_count(item.get('eligible_rows'))}，"
                    f"阻断 {_email_count(item.get('blocked_rows'))}，"
                    f"主要阻断原因 {item.get('top_block_reason') or '--'}"
                )
            )
    return lines


def _build_blocker_email_body(now_text: str, workflow: dict[str, Any], runtime: dict[str, Any] | None = None) -> str:
    blockers = workflow.get("blockers") if isinstance(workflow.get("blockers"), list) else []
    runtime = runtime if isinstance(runtime, dict) else {}
    summary = runtime.get("summary") if isinstance(runtime.get("summary"), dict) else {}
    lines = [
        f"生成时间：{now_text}",
        "报告范围：AiStock 全局运行快照",
        f"G3策略：{STRATEGY_NAME_CN}（{STRATEGY_NAME}）",
        f"买入候选日期：{workflow.get('entry_date') or summary.get('entry_date') or '--'}",
        f"当前选中路线：{workflow.get('selected_route') or summary.get('selected_route') or '--'}",
        f"诊断结果：{_email_diagnosis_label(workflow.get('diagnosis_code') or summary.get('diagnosis_code'))}",
        f"工作流是否正常：{_email_bool_label(workflow.get('ok'))}",
        "",
        *_build_aistock_data_source_email_lines(workflow, runtime),
        "",
        *_build_aistock_feedback_email_lines(workflow, runtime),
        "",
        *_build_strategy_result_email_body(now_text, workflow, runtime),
        "",
        "阻断项：",
    ]
    if blockers:
        for idx, item in enumerate(blockers[:10], start=1):
            lines.append(f"{idx}. {item.get('name') or '--'} | {item.get('message') or '--'}")
    else:
        lines.append("- 当前没有阻断项；上方已包含策略结果快照。")
    lines.extend(["", "安全边界：当前仅作影子观察；没有正式买入信号，也不会自动下单。"])
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
    # Data delivery incidents have one notification owner. The independent host
    # publisher observes the repair window and sends even when no tickets exist.
    if (any(not item.get("ok") for item in workflow.get("data_checks", []))
            and operations_notification_owner(runtime_path("operations", "latest.json"))):
        result = {"ok": True, "email_sent": False, "reason": "data_incident_owned_by_operations_publisher"}
        _append_monitor_event({"type": "email_skipped", **result})
        return result
    tickets = runtime.get("tickets") if isinstance(runtime.get("tickets"), list) else []
    entry_date = str(workflow.get("entry_date") or runtime.get("summary", {}).get("entry_date") or "")
    last_alert_keys = state.get("last_alert_keys") if isinstance(state.get("last_alert_keys"), dict) else {}
    new_tickets = [item for item in tickets if force_send or _ticket_alert_key(item, entry_date) not in last_alert_keys]
    sent: list[dict[str, Any]] = []
    recipient = state.get("recipient_email") or None
    try:
        if new_tickets and workflow.get("ok"):
            subject = f"AiStock全局运行摘要 {entry_date or '--'} {now_text[11:16]} 买入候选"
            sent.append(_send_shadow_email(subject, _build_shadow_buy_email_body(now_text, workflow, new_tickets, runtime), recipient))
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
                subject = f"AiStock全局运行摘要 {entry_date or '--'} {now_text[11:16]} 阻断"
                sent.append(_send_shadow_email(subject, _build_blocker_email_body(now_text, workflow, runtime), recipient))
                state["last_blocker_alert_at"] = now_text
                state["last_email_sent_at"] = now_text
        # Routine heartbeat mail is deliberately suppressed.  Only new buy
        # candidates, blockers, sell events, and repair failures notify users.
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
    afterhours_summary_path = STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_summary.json"
    afterhours_tickets_path = STATE_ALPHA_RUNTIME_DIR / "afterhours_latest_shadow_tickets.csv"
    ledger_path = STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"
    diagnostics_path = STATE_ROUTER_RUNTIME_DIR / "latest_route_diagnostics.csv"
    summary = _read_json(summary_path) or _read_json(STATE_ROUTER_RUNTIME_DIR / "latest_summary.json")
    afterhours_summary = _read_json(afterhours_summary_path)
    use_afterhours_pair = bool(
        isinstance(afterhours_summary, dict)
        and afterhours_summary.get("pending_next_session_confirmation")
    )
    if use_afterhours_pair:
        summary = afterhours_summary
        tickets_path = afterhours_tickets_path
    tickets = _enrich_trade_strategy_records(_read_csv_records(tickets_path, limit=limit))
    broker_snapshot = _broker_snapshot()
    broker_trades = broker_snapshot.get("broker_trades") if isinstance(broker_snapshot.get("broker_trades"), list) else []
    ledger_audit = _attach_exit_advice(
        _enrich_trade_strategy_records(_read_shadow_ledger_records(ledger_path, limit=limit)),
        source="shadow_ledger",
        updated_at=summary.get("generated_at") if isinstance(summary, dict) else None,
        broker_trades=broker_trades,
    )
    ledger = _filter_open_shadow_ledger_records(ledger_audit)
    diagnostics = _read_csv_records(diagnostics_path, limit=50)
    return {
        "summary": summary,
        "tickets": tickets,
        "runtime_pair": "afterhours_pending_confirmation" if use_afterhours_pair else "current",
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
    pending_next_session = bool(summary.get("pending_next_session_confirmation"))
    qualified_ok = True if pending_next_session else (qualified_rows == len(tickets) if tickets else qualified_rows == 0)
    m30_ok = True if pending_next_session else (all(_truthy(item.get("m30_confirmed")) for item in tickets) if tickets else True)
    business_checks = [
        {
            "name": "qualified_shadow_buy",
            "ok": qualified_ok,
            "status": "pass" if qualified_ok else "blocked",
            "message": "next-session candidates are pending first completed 30m confirmation" if pending_next_session else f"qualified_shadow_buy_rows={qualified_rows}, tickets={len(tickets)}",
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
    data_checks = strategy_data_checks(summary, read_snapshot(runtime_path("health", "latest.json")))
    pipeline_checks.extend(data_checks)
    blockers = [item for item in pipeline_checks if not item.get("ok") and item.get("blocking", True)]
    monitor = configure_shadow_monitor_scheduler()
    exit_monitor = configure_shadow_exit_monitor_scheduler()
    observation_scheduler = configure_observation_scheduler()
    paper_execution_path_ok = True
    status = {
        "available": True,
        "ok": not blockers,
        "data_checks": data_checks,
        "checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "entry_date": summary.get("entry_date"),
        "decision_date": summary.get("decision_date"),
        "diagnosis_code": summary.get("diagnosis_code"),
        "selected_route": summary.get("selected_route"),
        "monitor": monitor,
        "exit_monitor": exit_monitor,
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
            "exit_scheduler_wired": bool(exit_monitor.get("scheduler_wired")),
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
    reconciliation = _reconcile_shadow_ledger_from_paper_executions() if result.get("ok") else {
        "skipped": True,
        "reason": "refresh_failed",
    }
    workflow = _build_workflow_status()
    if not result.get("ok"):
        workflow["ok"] = False
        blockers = workflow.get("blockers") if isinstance(workflow.get("blockers"), list) else []
        blockers.append({"name": "state_router_refresh", "message": result.get("error") or result.get("stderr_tail") or "State router refresh failed."})
        workflow["blockers"] = blockers
    if int(reconciliation.get("unreconciled_count") or 0) > 0:
        workflow["ok"] = False
        blockers = workflow.get("blockers") if isinstance(workflow.get("blockers"), list) else []
        blockers.append({
            "name": "shadow_ledger_reconciliation",
            "message": f"{reconciliation['unreconciled_count']} paper executions could not be restored to the shadow ledger.",
        })
        workflow["blockers"] = blockers
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
        "ledger_reconciliation": reconciliation,
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


def _load_pretrade_ticket_reviews() -> dict[str, Any]:
    data = _read_json(PRETRADE_TICKET_REVIEWS_PATH)
    rows = data.get("reviews") if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _save_pretrade_ticket_review(payload: dict[str, Any]) -> dict[str, Any]:
    key = _verification_key(payload)
    if not key:
        return {"ok": False, "error": "missing ticket review key"}
    action = str(payload.get("review_action") or payload.get("action") or "").strip()
    allowed_actions = {"", "paper_watch", "manual_approved", "skip", "wait_refresh", "reject"}
    if action not in allowed_actions:
        return {"ok": False, "error": f"unsupported review_action: {action}", "allowed_actions": sorted(allowed_actions)}
    rows = _load_pretrade_ticket_reviews()
    if not action:
        rows.pop(key, None)
        _write_json(PRETRADE_TICKET_REVIEWS_PATH, {"reviews": rows})
        return {"ok": True, "removed": True, "key": key, "count": len(rows)}
    confirmation_items = payload.get("confirmation_items")
    if isinstance(confirmation_items, str):
        confirmation_items = [x.strip() for x in confirmation_items.replace("；", "\n").replace(";", "\n").splitlines() if x.strip()]
    elif not isinstance(confirmation_items, list):
        confirmation_items = []
    confirmation_items = [str(x or "").strip() for x in confirmation_items if str(x or "").strip()]
    checked_items = payload.get("checked_items")
    if isinstance(checked_items, str):
        checked_items = [x.strip() for x in checked_items.replace("；", "\n").replace(";", "\n").splitlines() if x.strip()]
    elif not isinstance(checked_items, list):
        checked_items = []
    checked_items = [str(x or "").strip() for x in checked_items if str(x or "").strip()]
    missing_items = [item for item in confirmation_items if item not in set(checked_items)]
    review_evidence = payload.get("review_evidence")
    review_evidence = review_evidence if isinstance(review_evidence, dict) else {}
    item = {
        "key": key,
        "ticket_key": str(payload.get("ticket_key") or payload.get("candidate_key") or payload.get("trade_key") or key).strip(),
        "review_action": action,
        "review_label": {
            "paper_watch": "纸面观察",
            "manual_approved": "人工放行",
            "skip": "跳过",
            "wait_refresh": "等待刷新",
            "reject": "否决",
        }.get(action, action),
        "review_confidence": str(payload.get("review_confidence") or payload.get("confidence") or "medium").strip(),
        "risk_acknowledged": _truthy(payload.get("risk_acknowledged")),
        "note": str(payload.get("note") or "").strip(),
        "code": str(payload.get("code") or payload.get("code_raw") or "").strip(),
        "name": str(payload.get("name") or payload.get("stock_name") or "").strip(),
        "entry_date": str(payload.get("entry_date") or "").strip(),
        "route": str(payload.get("route") or "").strip(),
        "natural_action": str(payload.get("natural_action") or "").strip(),
        "natural_reason": str(payload.get("natural_reason") or "").strip(),
        "confirmation_items": confirmation_items,
        "checked_items": checked_items,
        "missing_confirmation_items": missing_items,
        "confirmation_complete": bool(confirmation_items) and not missing_items,
        "execution_posture": str(payload.get("execution_posture") or "").strip(),
        "decision_level": str(payload.get("decision_level") or "").strip(),
        "consistency_grade": str(payload.get("consistency_grade") or "").strip(),
        "consistency_score": _as_float(payload.get("consistency_score"), None),
        "operational_readiness_score": _as_float(payload.get("operational_readiness_score"), None),
        "downgrade_rule": str(payload.get("downgrade_rule") or "").strip(),
        "required_confirmation": str(payload.get("required_confirmation") or "").strip(),
        "review_decision_reason": str(payload.get("review_decision_reason") or "").strip(),
        "next_review_trigger": str(payload.get("next_review_trigger") or "").strip(),
        "review_evidence": review_evidence,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    rows[key] = item
    _write_json(PRETRADE_TICKET_REVIEWS_PATH, {"reviews": rows})
    return {"ok": True, "key": key, "review": item, "count": len(rows)}


def _pretrade_paper_watch_payload_from_ticket(ticket: dict[str, Any], *, batch_id: str, reason: str = "") -> dict[str, Any]:
    code = str(ticket.get("code") or ticket.get("code_raw") or "").strip()
    name = str(ticket.get("name") or ticket.get("stock_name") or "").strip()
    review_reason = reason or "实战启动批次：先进入纸面观察，从买点、选股、策略切换和卖点合同逐票复盘"
    return {
        "ticket_key": ticket.get("ticket_key") or ticket.get("candidate_key") or ticket.get("trade_key"),
        "code": code,
        "name": name,
        "entry_date": ticket.get("entry_date"),
        "route": ticket.get("route"),
        "natural_action": ticket.get("natural_action"),
        "natural_reason": ticket.get("natural_reason") or ticket.get("notes"),
        "review_action": "paper_watch",
        "review_confidence": "medium",
        "risk_acknowledged": False,
        "execution_posture": ticket.get("pretrade_review_execution_posture") or ticket.get("natural_action"),
        "decision_level": ticket.get("pretrade_review_decision_level"),
        "consistency_grade": ticket.get("pretrade_review_consistency_grade"),
        "consistency_score": ticket.get("pretrade_review_consistency_score"),
        "operational_readiness_score": ticket.get("pretrade_review_operational_readiness_score"),
        "review_decision_reason": f"{code} {name} {review_reason}".strip(),
        "next_review_trigger": "收盘后记录纸面观察后评估，归因到买点、选股、策略切换或卖点合同",
        "review_evidence": {
            "batch_id": batch_id,
            "strategy": ticket.get("strategy") or ticket.get("trade_strategy_label"),
            "route": ticket.get("route"),
            "natural_action": ticket.get("natural_action"),
            "natural_reason": ticket.get("natural_reason") or ticket.get("notes"),
            "warnings": ticket.get("warnings"),
        },
        "note": f"{review_reason}；batch_id={batch_id}",
    }


def _attach_pretrade_reviews_to_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    reviews = _load_pretrade_ticket_reviews()
    out: list[dict[str, Any]] = []
    for row in records:
        item = dict(row)
        key = _verification_key(item)
        review = reviews.get(key) if key else None
        if isinstance(review, dict):
            item["pretrade_review_action"] = review.get("review_action")
            item["pretrade_review_label"] = review.get("review_label")
            item["pretrade_review_confidence"] = review.get("review_confidence")
            item["pretrade_review_note"] = review.get("note")
            item["pretrade_review_updated_at"] = review.get("updated_at")
            item["pretrade_review_risk_acknowledged"] = review.get("risk_acknowledged")
            item["pretrade_review_confirmation_items"] = review.get("confirmation_items") if isinstance(review.get("confirmation_items"), list) else []
            item["pretrade_review_checked_items"] = review.get("checked_items") if isinstance(review.get("checked_items"), list) else []
            item["pretrade_review_missing_confirmation_items"] = review.get("missing_confirmation_items") if isinstance(review.get("missing_confirmation_items"), list) else []
            item["pretrade_review_confirmation_complete"] = _truthy(review.get("confirmation_complete"))
            item["pretrade_review_execution_posture"] = review.get("execution_posture")
            item["pretrade_review_decision_level"] = review.get("decision_level")
            item["pretrade_review_consistency_grade"] = review.get("consistency_grade")
            item["pretrade_review_consistency_score"] = review.get("consistency_score")
            item["pretrade_review_operational_readiness_score"] = review.get("operational_readiness_score")
            item["pretrade_review_downgrade_rule"] = review.get("downgrade_rule")
            item["pretrade_review_required_confirmation"] = review.get("required_confirmation")
            item["pretrade_review_decision_reason"] = review.get("review_decision_reason")
            item["pretrade_review_next_review_trigger"] = review.get("next_review_trigger")
            item["pretrade_review_evidence"] = review.get("review_evidence") if isinstance(review.get("review_evidence"), dict) else {}
        else:
            item["pretrade_review_action"] = ""
            item["pretrade_review_label"] = "未复盘"
            item["pretrade_review_confidence"] = ""
            item["pretrade_review_note"] = ""
            item["pretrade_review_updated_at"] = ""
            item["pretrade_review_risk_acknowledged"] = False
            item["pretrade_review_confirmation_items"] = []
            item["pretrade_review_checked_items"] = []
            item["pretrade_review_missing_confirmation_items"] = []
            item["pretrade_review_confirmation_complete"] = False
            item["pretrade_review_execution_posture"] = ""
            item["pretrade_review_decision_level"] = ""
            item["pretrade_review_consistency_grade"] = ""
            item["pretrade_review_consistency_score"] = None
            item["pretrade_review_operational_readiness_score"] = None
            item["pretrade_review_downgrade_rule"] = ""
            item["pretrade_review_required_confirmation"] = ""
            item["pretrade_review_decision_reason"] = ""
            item["pretrade_review_next_review_trigger"] = ""
            item["pretrade_review_evidence"] = {}
        out.append(item)
    return out


def _load_paper_watch_reviews() -> dict[str, Any]:
    data = _read_json(PAPER_WATCH_REVIEWS_PATH)
    rows = data.get("reviews") if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _paper_watch_result_label(result: str) -> str:
    return {
        "as_expected": "符合预期",
        "buy_point_too_early": "买点偏早",
        "buy_point_chasing": "买点追高",
        "selection_issue": "选股隐患",
        "model_switch_issue": "策略切换隐患",
        "risk_exit_issue": "风控/卖点隐患",
        "missed_opportunity": "可能误杀",
        "invalid_signal": "信号失效",
        "continue_watch": "继续观察",
    }.get(result, result)


def _paper_watch_issue_area(result: str) -> str:
    if result in {"buy_point_too_early", "buy_point_chasing"}:
        return "buy_point"
    if result == "selection_issue":
        return "selection"
    if result == "model_switch_issue":
        return "model_switch"
    if result == "risk_exit_issue":
        return "sell_exit"
    if result in {"invalid_signal", "missed_opportunity"}:
        return "signal_validation"
    return "observation"


def _save_paper_watch_review(payload: dict[str, Any]) -> dict[str, Any]:
    key = _verification_key(payload)
    if not key:
        return {"ok": False, "error": "missing paper watch review key"}
    result = str(payload.get("watch_result") or payload.get("result") or "").strip()
    allowed_results = {
        "",
        "as_expected",
        "buy_point_too_early",
        "buy_point_chasing",
        "selection_issue",
        "model_switch_issue",
        "risk_exit_issue",
        "missed_opportunity",
        "invalid_signal",
        "continue_watch",
    }
    if result not in allowed_results:
        return {"ok": False, "error": f"unsupported watch_result: {result}", "allowed_results": sorted(allowed_results)}
    rows = _load_paper_watch_reviews()
    if not result:
        rows.pop(key, None)
        _write_json(PAPER_WATCH_REVIEWS_PATH, {"reviews": rows})
        return {"ok": True, "removed": True, "key": key, "count": len(rows)}
    item = {
        "key": key,
        "ticket_key": str(payload.get("ticket_key") or payload.get("candidate_key") or payload.get("trade_key") or key).strip(),
        "code": str(payload.get("code") or payload.get("code_raw") or "").strip(),
        "name": str(payload.get("name") or payload.get("stock_name") or "").strip(),
        "entry_date": str(payload.get("entry_date") or "").strip(),
        "route": str(payload.get("route") or "").strip(),
        "source": str(payload.get("source") or "g3_state_alpha_risk_page").strip(),
        "review_context": str(payload.get("review_context") or "paper_watch").strip(),
        "watch_result": result,
        "watch_result_label": _paper_watch_result_label(result),
        "issue_area": str(payload.get("issue_area") or _paper_watch_issue_area(result)).strip(),
        "observed_date": str(payload.get("observed_date") or "").strip(),
        "observed_price": _as_float(payload.get("observed_price"), None),
        "max_gain_pct": _as_float(payload.get("max_gain_pct"), None),
        "max_drawdown_pct": _as_float(payload.get("max_drawdown_pct"), None),
        "naturalness_score": _as_float(payload.get("naturalness_score"), None),
        "hidden_risk": str(payload.get("hidden_risk") or "").strip(),
        "optimization_suggestion": str(payload.get("optimization_suggestion") or "").strip(),
        "selection_state": str(payload.get("selection_state") or "").strip(),
        "strategy_switch_assessment": str(payload.get("strategy_switch_assessment") or "").strip(),
        "candidate_block_reason": str(payload.get("candidate_block_reason") or "").strip(),
        "action_recommendation": str(payload.get("action_recommendation") or "").strip(),
        "review_note": str(payload.get("review_note") or payload.get("note") or "").strip(),
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    rows[key] = item
    _write_json(PAPER_WATCH_REVIEWS_PATH, {"reviews": rows})
    return {"ok": True, "key": key, "review": item, "count": len(rows)}


def _load_candidate_omission_reviews() -> dict[str, Any]:
    data = _read_json(CANDIDATE_OMISSION_REVIEWS_PATH)
    rows = data.get("reviews") if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _candidate_omission_result_label(result: str) -> str:
    return {
        "as_expected": "挡得合理",
        "missed_opportunity": "可能误杀",
        "invalid_signal": "信号失效",
        "selection_issue": "选股隐患",
        "model_switch_issue": "策略切换隐患",
        "continue_watch": "继续观察",
    }.get(result, result)


def _candidate_omission_review_status(result: str) -> str:
    if result in {"as_expected", "invalid_signal"}:
        return "validated"
    if result in {"missed_opportunity", "selection_issue", "model_switch_issue"}:
        return "issue_found"
    if result == "continue_watch":
        return "continue_watch"
    return "pending_observation"


def _save_candidate_omission_review(payload: dict[str, Any]) -> dict[str, Any]:
    has_review_identity = any(
        _clean_review_text(payload.get(key))
        for key in ("ticket_key", "candidate_key", "trade_key", "code", "code_raw", "entry_date", "route")
    )
    if not has_review_identity:
        return {"ok": False, "error": "missing candidate omission review key"}
    key = _verification_key(payload)
    if not key:
        return {"ok": False, "error": "missing candidate omission review key"}
    result = _clean_review_text(payload.get("review_result") or payload.get("watch_result") or payload.get("result"))
    allowed_results = {"", "as_expected", "missed_opportunity", "invalid_signal", "selection_issue", "model_switch_issue", "continue_watch"}
    if result not in allowed_results:
        return {"ok": False, "error": f"unsupported review_result: {result}", "allowed_results": sorted(allowed_results)}
    rows = _load_candidate_omission_reviews()
    if not result:
        rows.pop(key, None)
        _write_json(CANDIDATE_OMISSION_REVIEWS_PATH, {"reviews": rows})
        return {"ok": True, "removed": True, "key": key, "count": len(rows)}
    item = {
        "key": key,
        "ticket_key": _clean_review_text(payload.get("ticket_key") or payload.get("candidate_key") or payload.get("trade_key") or key),
        "code": _clean_review_text(payload.get("code") or payload.get("code_raw")),
        "name": _clean_review_text(payload.get("name") or payload.get("stock_name")),
        "entry_date": _clean_review_text(payload.get("entry_date")),
        "route": _clean_review_text(payload.get("route")),
        "source": _clean_review_text(payload.get("source"), "candidate_omission_checklist"),
        "review_context": "candidate_omission",
        "watch_result": result,
        "watch_result_label": _candidate_omission_result_label(result),
        "review_result": result,
        "review_result_label": _candidate_omission_result_label(result),
        "review_status": _candidate_omission_review_status(result),
        "issue_area": _clean_review_text(payload.get("issue_area") or _paper_watch_issue_area(result)),
        "hidden_risk": _clean_review_text(payload.get("hidden_risk")),
        "optimization_suggestion": _clean_review_text(payload.get("optimization_suggestion")),
        "selection_state": _clean_review_text(payload.get("selection_state")),
        "strategy_switch_assessment": _clean_review_text(payload.get("strategy_switch_assessment")),
        "candidate_block_reason": _clean_review_text(payload.get("candidate_block_reason")),
        "action_recommendation": _clean_review_text(payload.get("action_recommendation")),
        "review_note": _clean_review_text(payload.get("review_note") or payload.get("note")),
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    rows[key] = item
    _write_json(CANDIDATE_OMISSION_REVIEWS_PATH, {"reviews": rows})
    return {"ok": True, "key": key, "review": item, "count": len(rows)}


def _load_no_trade_day_reviews() -> dict[str, Any]:
    data = _read_json(NO_TRADE_DAY_REVIEWS_PATH)
    rows = data.get("reviews") if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _no_trade_day_review_key(payload: dict[str, Any]) -> str:
    for key in ("review_key", "key"):
        value = _clean_review_text(payload.get(key))
        if value:
            return value
    parts = [
        _clean_review_text(payload.get("entry_date")),
        _clean_review_text(payload.get("review_type")),
        _clean_review_text(payload.get("source")),
    ]
    if not any(parts):
        return ""
    return "|".join(parts)


def _no_trade_day_result_label(result: str) -> str:
    return {
        "natural_no_trade": "空仓合理",
        "missed_opportunity": "可能误杀",
        "data_gap": "数据/确认缺口",
        "process_gap": "流程缺口",
        "continue_watch": "继续观察",
    }.get(result, result)


def _no_trade_day_review_status(result: str) -> str:
    if result == "natural_no_trade":
        return "validated"
    if result in {"missed_opportunity", "data_gap", "process_gap"}:
        return "issue_found"
    if result == "continue_watch":
        return "continue_watch"
    return "pending_review"


def _save_no_trade_day_review(payload: dict[str, Any]) -> dict[str, Any]:
    key = _no_trade_day_review_key(payload)
    if not key:
        return {"ok": False, "error": "missing no trade day review key"}
    result = _clean_review_text(payload.get("review_result") or payload.get("result"))
    allowed_results = {"", "natural_no_trade", "missed_opportunity", "data_gap", "process_gap", "continue_watch"}
    if result not in allowed_results:
        return {"ok": False, "error": f"unsupported review_result: {result}", "allowed_results": sorted(allowed_results)}
    rows = _load_no_trade_day_reviews()
    if not result:
        rows.pop(key, None)
        _write_json(NO_TRADE_DAY_REVIEWS_PATH, {"reviews": rows})
        return {"ok": True, "removed": True, "key": key, "count": len(rows)}
    item = {
        "key": key,
        "entry_date": _clean_review_text(payload.get("entry_date")),
        "review_type": _clean_review_text(payload.get("review_type")),
        "posture": _clean_review_text(payload.get("posture")),
        "source": _clean_review_text(payload.get("source"), "no_trade_day_review"),
        "review_result": result,
        "review_result_label": _no_trade_day_result_label(result),
        "review_status": _no_trade_day_review_status(result),
        "issue_area": _clean_review_text(payload.get("issue_area"), "selection_or_switch" if result == "missed_opportunity" else "observation"),
        "evidence": _clean_review_text(payload.get("evidence")),
        "hidden_risk": _clean_review_text(payload.get("hidden_risk")),
        "natural_decision": _clean_review_text(payload.get("natural_decision")),
        "optimization_suggestion": _clean_review_text(payload.get("optimization_suggestion")),
        "review_note": _clean_review_text(payload.get("review_note") or payload.get("note")),
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    rows[key] = item
    _write_json(NO_TRADE_DAY_REVIEWS_PATH, {"reviews": rows})
    return {"ok": True, "key": key, "review": item, "count": len(rows)}


def _load_formal_action_reviews() -> dict[str, Any]:
    data = _read_json(FORMAL_ACTION_REVIEWS_PATH)
    rows = data.get("reviews") if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _formal_action_review_key(payload: dict[str, Any]) -> str:
    for key in ("review_key", "key"):
        value = _clean_review_text(payload.get(key))
        if value:
            return value
    parts = [
        _clean_review_text(payload.get("source")),
        _clean_review_text(payload.get("object") or payload.get("action_object")),
        _clean_review_text(payload.get("action") or payload.get("required_action")),
    ]
    if not any(parts):
        return ""
    return "|".join(parts)


def _formal_action_result_label(result: str) -> str:
    return {
        "manual_done": "人工已处理",
        "system_triggered": "系统动作已触发",
        "blocked": "处理卡住",
        "continue_watch": "继续观察",
    }.get(result, result)


def _formal_action_review_status(result: str) -> str:
    if result in {"manual_done", "system_triggered"}:
        return "validated"
    if result == "blocked":
        return "issue_found"
    if result == "continue_watch":
        return "continue_watch"
    return "pending"


def _save_formal_action_review(payload: dict[str, Any]) -> dict[str, Any]:
    key = _formal_action_review_key(payload)
    if not key:
        return {"ok": False, "error": "missing formal action review key"}
    result = _clean_review_text(payload.get("review_result") or payload.get("result"))
    allowed_results = {"", "manual_done", "system_triggered", "blocked", "continue_watch"}
    if result not in allowed_results:
        return {"ok": False, "error": f"unsupported review_result: {result}", "allowed_results": sorted(allowed_results)}
    rows = _load_formal_action_reviews()
    if not result:
        rows.pop(key, None)
        _write_json(FORMAL_ACTION_REVIEWS_PATH, {"reviews": rows})
        return {"ok": True, "removed": True, "key": key, "count": len(rows)}
    item = {
        "key": key,
        "review_key": key,
        "object": _clean_review_text(payload.get("object") or payload.get("action_object")),
        "action": _clean_review_text(payload.get("action") or payload.get("required_action")),
        "source": _clean_review_text(payload.get("source"), "formal_action"),
        "review_result": result,
        "review_result_label": _formal_action_result_label(result),
        "review_status": _formal_action_review_status(result),
        "issue_area": _clean_review_text(payload.get("issue_area"), "execution_process" if result == "blocked" else "operation_evidence"),
        "evidence": _clean_review_text(payload.get("evidence")),
        "natural_decision": _clean_review_text(payload.get("natural_decision")),
        "optimization_suggestion": _clean_review_text(payload.get("optimization_suggestion")),
        "review_note": _clean_review_text(payload.get("review_note") or payload.get("note")),
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    rows[key] = item
    _write_json(FORMAL_ACTION_REVIEWS_PATH, {"reviews": rows})
    return {"ok": True, "key": key, "review": item, "count": len(rows)}


def _load_daily_review_checklist_reviews() -> dict[str, Any]:
    data = _read_json(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH)
    rows = data.get("reviews") if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _daily_review_checklist_review_key(payload: dict[str, Any]) -> str:
    axis = _clean_review_text(payload.get("review_axis"))
    for key in ("review_key", "key", "ticket_key"):
        value = _clean_review_text(payload.get(key))
        if value:
            return f"{axis}|{value}" if axis and not value.startswith(f"{axis}|") else value
    parts = [
        axis,
        _clean_review_text(payload.get("review_scope")),
        _clean_review_text(payload.get("object") or payload.get("review_object")),
        _clean_review_text(payload.get("code")),
        _clean_review_text(payload.get("entry_date")),
    ]
    if not any(parts):
        return ""
    return "|".join(parts)


def _daily_review_checklist_result_label(result: str) -> str:
    return {
        "validated": "符合预期",
        "issue_found": "发现隐患",
        "continue_watch": "继续观察",
        "blocked": "卡住/阻断",
        "data_gap": "数据缺口",
        "process_gap": "流程缺口",
    }.get(result, result)


def _daily_review_checklist_review_status(result: str) -> str:
    if result == "validated":
        return "validated"
    if result in {"issue_found", "blocked", "data_gap", "process_gap"}:
        return "issue_found"
    if result == "continue_watch":
        return "continue_watch"
    return "pending_review"


def _save_daily_review_checklist_review(payload: dict[str, Any]) -> dict[str, Any]:
    key = _daily_review_checklist_review_key(payload)
    if not key:
        return {"ok": False, "error": "missing daily review checklist key"}
    result = _clean_review_text(payload.get("review_result") or payload.get("result"))
    allowed_results = {"", "validated", "issue_found", "continue_watch", "blocked", "data_gap", "process_gap"}
    if result not in allowed_results:
        return {"ok": False, "error": f"unsupported review_result: {result}", "allowed_results": sorted(allowed_results)}
    rows = _load_daily_review_checklist_reviews()
    if not result:
        rows.pop(key, None)
        _write_json(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH, {"reviews": rows})
        return {"ok": True, "removed": True, "key": key, "count": len(rows)}
    item = {
        "key": key,
        "review_key": key,
        "ticket_key": _clean_review_text(payload.get("ticket_key")),
        "review_axis": _clean_review_text(payload.get("review_axis")),
        "review_scope": _clean_review_text(payload.get("review_scope")),
        "object": _clean_review_text(payload.get("object") or payload.get("review_object")),
        "code": _clean_review_text(payload.get("code")),
        "name": _clean_review_text(payload.get("name")),
        "entry_date": _clean_review_text(payload.get("entry_date")),
        "review_result": result,
        "review_result_label": _daily_review_checklist_result_label(result),
        "review_status": _daily_review_checklist_review_status(result),
        "issue_area": _clean_review_text(payload.get("issue_area") or payload.get("review_axis")),
        "evidence": _clean_review_text(payload.get("evidence")),
        "review_note": _clean_review_text(payload.get("review_note") or payload.get("note")),
        "natural_decision": _clean_review_text(payload.get("natural_decision")),
        "optimization_suggestion": _clean_review_text(payload.get("optimization_suggestion")),
        "source": _clean_review_text(payload.get("source"), "live_daily_review_execution_checklist"),
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    rows[key] = item
    _write_json(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH, {"reviews": rows})
    return {"ok": True, "key": key, "review": item, "count": len(rows)}


def _load_launch_day_playbook_reviews() -> dict[str, Any]:
    data = _read_json(LAUNCH_DAY_PLAYBOOK_REVIEWS_PATH)
    rows = data.get("reviews") if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _launch_day_playbook_review_key(payload: dict[str, Any]) -> str:
    for key in ("review_key", "launch_playbook_key", "key"):
        value = _clean_review_text(payload.get(key))
        if value:
            return value
    parts = [
        _clean_review_text(payload.get("window")),
        _clean_review_text(payload.get("checkpoint_time")),
        _clean_review_text(payload.get("review_axis")),
        _clean_review_text(payload.get("object") or payload.get("review_object")),
        _clean_review_text(payload.get("action_type")),
    ]
    if not any(parts):
        return ""
    return "|".join(parts).strip("|")


def _launch_day_playbook_result_label(result: str) -> str:
    return {
        "validated": "符合预期",
        "issue_found": "发现隐患",
        "continue_watch": "继续观察",
        "data_gap": "数据缺口",
        "manual_done": "已处理",
    }.get(result, result)


def _launch_day_playbook_review_status(result: str) -> str:
    if result in {"validated", "manual_done"}:
        return "validated"
    if result in {"issue_found", "data_gap"}:
        return "issue_found"
    if result == "continue_watch":
        return "continue_watch"
    return "pending_review"


def _save_launch_day_playbook_review(payload: dict[str, Any]) -> dict[str, Any]:
    key = _launch_day_playbook_review_key(payload)
    if not key:
        return {"ok": False, "error": "missing launch day playbook review key"}
    result = _clean_review_text(payload.get("review_result") or payload.get("result"))
    allowed_results = {"", "validated", "issue_found", "continue_watch", "data_gap", "manual_done"}
    if result not in allowed_results:
        return {"ok": False, "error": f"unsupported review_result: {result}", "allowed_results": sorted(allowed_results)}
    rows = _load_launch_day_playbook_reviews()
    if not result:
        rows.pop(key, None)
        _write_json(LAUNCH_DAY_PLAYBOOK_REVIEWS_PATH, {"reviews": rows})
        return {"ok": True, "removed": True, "key": key, "count": len(rows)}
    item = {
        "key": key,
        "review_key": key,
        "priority": _clean_review_text(payload.get("priority")),
        "window": _clean_review_text(payload.get("window")),
        "checkpoint_time": _clean_review_text(payload.get("checkpoint_time")),
        "action_type": _clean_review_text(payload.get("action_type")),
        "review_axis": _clean_review_text(payload.get("review_axis")),
        "object": _clean_review_text(payload.get("object") or payload.get("review_object")),
        "required_action": _clean_review_text(payload.get("required_action")),
        "evidence_to_collect": _clean_review_text(payload.get("evidence_to_collect")),
        "pass_condition": _clean_review_text(payload.get("pass_condition")),
        "fail_condition": _clean_review_text(payload.get("fail_condition")),
        "review_result": result,
        "review_result_label": _launch_day_playbook_result_label(result),
        "review_status": _launch_day_playbook_review_status(result),
        "issue_area": _clean_review_text(payload.get("issue_area") or payload.get("review_axis")),
        "evidence": _clean_review_text(payload.get("evidence") or payload.get("evidence_to_collect")),
        "review_note": _clean_review_text(payload.get("review_note") or payload.get("note")),
        "natural_decision": _clean_review_text(payload.get("natural_decision")),
        "optimization_suggestion": _clean_review_text(payload.get("optimization_suggestion")),
        "source": _clean_review_text(payload.get("source"), "launch_day_playbook"),
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    rows[key] = item
    _write_json(LAUNCH_DAY_PLAYBOOK_REVIEWS_PATH, {"reviews": rows})
    return {"ok": True, "key": key, "review": item, "count": len(rows)}


def _load_strategy_tuning_task_reviews() -> dict[str, Any]:
    data = _read_json(STRATEGY_TUNING_TASK_REVIEWS_PATH)
    rows = data.get("reviews") if isinstance(data, dict) else {}
    return rows if isinstance(rows, dict) else {}


def _strategy_tuning_task_review_key(payload: dict[str, Any]) -> str:
    for key in ("task_key", "review_key", "key"):
        value = _clean_review_text(payload.get(key))
        if value:
            return value
    parts = [
        _clean_review_text(payload.get("axis")),
        _clean_review_text(payload.get("origin")),
        _clean_review_text(payload.get("object")),
        _clean_review_text(payload.get("problem_signal"))[:80],
    ]
    if not any(parts):
        return ""
    return "|".join(parts)


def _strategy_tuning_task_result_label(result: str) -> str:
    return {
        "validated": "validated",
        "issue_found": "issue_found",
        "continue_watch": "continue_watch",
        "data_gap": "data_gap",
        "evidence_pending": "evidence_pending",
        "defer_contract_review": "defer_contract_review",
    }.get(result, result)


def _strategy_tuning_task_review_status(result: str) -> str:
    if result == "validated":
        return "reviewed"
    if result in {"issue_found", "data_gap", "evidence_pending", "defer_contract_review"}:
        return "needs_followup"
    if result == "continue_watch":
        return "watch"
    return "pending_review"


def _save_strategy_tuning_task_review(payload: dict[str, Any]) -> dict[str, Any]:
    key = _strategy_tuning_task_review_key(payload)
    if not key:
        return {"ok": False, "error": "missing strategy tuning task review key"}
    result = _clean_review_text(payload.get("review_result") or payload.get("result"))
    allowed_results = {"", "validated", "issue_found", "continue_watch", "data_gap", "evidence_pending", "defer_contract_review"}
    if result not in allowed_results:
        return {"ok": False, "error": f"unsupported review_result: {result}", "allowed_results": sorted(allowed_results)}
    rows = _load_strategy_tuning_task_reviews()
    if not result:
        rows.pop(key, None)
        _write_json(STRATEGY_TUNING_TASK_REVIEWS_PATH, {"reviews": rows})
        return {"ok": True, "removed": True, "key": key, "count": len(rows)}
    axis = _clean_review_text(payload.get("axis"), "natural_trade_consistency")
    item = {
        "key": key,
        "task_key": key,
        "axis": axis,
        "axis_label": _clean_review_text(payload.get("axis_label") or _g3_strategy_tuning_axis_meta(axis).get("axis_label")),
        "origin": _clean_review_text(payload.get("origin")),
        "object": _clean_review_text(payload.get("object")),
        "problem_signal": _clean_review_text(payload.get("problem_signal")),
        "source_status": _clean_review_text(payload.get("source_status")),
        "review_result": result,
        "review_result_label": _strategy_tuning_task_result_label(result),
        "review_status": _strategy_tuning_task_review_status(result),
        "evidence": _clean_review_text(payload.get("evidence") or payload.get("completion_evidence")),
        "review_note": _clean_review_text(payload.get("review_note") or payload.get("note")),
        "hidden_risk": _clean_review_text(payload.get("hidden_risk")),
        "decision": _clean_review_text(payload.get("decision") or payload.get("natural_decision")),
        "optimization_suggestion": _clean_review_text(payload.get("optimization_suggestion") or payload.get("suggested_learning")),
        "next_action": _clean_review_text(payload.get("next_action")),
        "completion_evidence": _clean_review_text(payload.get("completion_evidence")),
        "optimization_boundary": _clean_review_text(payload.get("optimization_boundary")),
        "can_execute_trade": False,
        "can_change_strategy_contract": False,
        "profit_only_optimization_allowed": False,
        "updated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    rows[key] = item
    _write_json(STRATEGY_TUNING_TASK_REVIEWS_PATH, {"reviews": rows})
    return {"ok": True, "key": key, "review": item, "count": len(rows)}


def _load_paper_executions() -> list[dict[str, Any]]:
    data = _read_json(PAPER_EXECUTIONS_PATH)
    rows = data.get("executions") if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def _formal_score88_paper_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep old paper records auditable without letting them prove Score88 fills."""
    return [
        row for row in rows
        if (
            isinstance(row, dict)
            and str(row.get("strategy_id") or "") == STRATEGY_ID
            and str(row.get("contract") or "") == LATEST_G3_PROFILE
        )
    ]


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
        "batch_id": str(payload.get("batch_id") or "").strip(),
        "created_at": now_text,
        "status": "paper_submitted",
        "source": str(payload.get("source") or "g3_state_alpha_current_page").strip(),
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
    formal_rows = _formal_score88_paper_rows(rows)
    if any(_paper_execution_matches_ticket(ticket, item) and _paper_execution_side(item) == "BUY" for item in formal_rows):
        return {"ok": False, "message": "Paper buy already exists for this shadow ticket.", "ticket": ticket}
    rows.append(record)
    _save_paper_executions(rows)
    ledger_updated = _update_shadow_ledger_for_entry(ticket, record)
    _append_monitor_event(
        {
            "type": "paper_execution",
            "status": "paper_submitted",
            "ticket_key": ticket_key,
            "code": record.get("code"),
            "message": "G3 State Alpha paper execution recorded.",
        }
    )
    return {"ok": True, "record": record, "execution_count": len(rows), "ledger_updated": ledger_updated}


def _reconcile_shadow_ledger_from_paper_executions() -> dict[str, Any]:
    """Restore open paper fills after a candidate refresh rewrites the ledger.

    This remains strictly inside the shadow/paper boundary: it only rehydrates
    the CSV ledger from already-recorded paper executions and never calls an
    order API.
    """
    paper_rows = _formal_score88_paper_rows(_load_paper_executions())
    runtime = _load_current_runtime(limit=200)
    tickets = [item for item in (runtime.get("tickets") or []) if isinstance(item, dict)]
    ticket_by_key = {
        str(item.get("ticket_key") or item.get("candidate_key") or item.get("trade_key") or "").strip(): item
        for item in tickets
    }
    ledger_records = _read_shadow_ledger_records(STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv", limit=None)
    repaired: list[dict[str, Any]] = []
    unreconciled: list[dict[str, Any]] = []
    for record in paper_rows:
        if not isinstance(record, dict) or _paper_execution_side(record) != "BUY":
            continue
        ticket_key = str(record.get("ticket_key") or "").strip()
        code = str(record.get("code") or record.get("code_raw") or "").strip().upper()
        if _paper_open_quantity(ticket_key, code, paper_rows) <= 0:
            continue
        matching = [
            row for row in ledger_records
            if str(row.get("ticket_key") or row.get("candidate_key") or row.get("trade_key") or "").strip() == ticket_key
            or (
                not ticket_key
                and str(row.get("code") or row.get("code_raw") or "").strip().upper() == code
            )
        ]
        # Contract migrations can change the ticket prefix while retaining the
        # same symbol, route and entry date.  Match that stable identity before
        # treating a historical paper buy as an orphan.
        if not matching:
            matching = [
                row for row in ledger_records
                if _ledger_match_mask(pd.DataFrame([row]), record).iloc[0]
            ]
        ticket = ticket_by_key.get(ticket_key)
        # A later no-candidate refresh has no current ticket for an earlier
        # paper fill.  Its ledger record remains the authoritative ticket
        # envelope for reconciliation, so do not silently skip that fill.
        if not ticket and matching:
            ticket = matching[-1]
        if not ticket:
            unreconciled.append({"ticket_key": ticket_key, "code": code, "execution_id": record.get("execution_id"), "reason": "ticket_missing"})
            continue
        current = matching[-1] if matching else None
        if str((current or {}).get("trade_status") or "").strip() == "closed" or str((current or {}).get("position_status") or "").strip() == "closed":
            continue
        current_is_open = bool(current and current in _filter_open_shadow_ledger_records([current]))
        already_linked = str((current or {}).get("paper_execution_id") or "").strip() == str(record.get("execution_id") or "").strip()
        if current_is_open and already_linked:
            continue
        if _update_shadow_ledger_for_entry(ticket, record):
            repaired.append({"ticket_key": ticket_key, "code": code, "execution_id": record.get("execution_id")})
            ledger_records = _read_shadow_ledger_records(STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv", limit=None)
        else:
            unreconciled.append({"ticket_key": ticket_key, "code": code, "execution_id": record.get("execution_id")})
    result = {
        "checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "open_paper_execution_count": sum(
            1 for row in paper_rows
            if isinstance(row, dict)
            and _paper_execution_side(row) == "BUY"
            and _paper_open_quantity(str(row.get("ticket_key") or ""), str(row.get("code") or row.get("code_raw") or ""), paper_rows) > 0
        ),
        "repaired_count": len(repaired),
        "repaired": repaired,
        "unreconciled_count": len(unreconciled),
        "unreconciled": unreconciled,
    }
    if repaired or unreconciled:
        _append_monitor_event({"type": "shadow_ledger_reconciliation", **result})
    return result


def _update_shadow_ledger_for_entry(ticket: dict[str, Any], record: dict[str, Any]) -> bool:
    """Promote a planned ticket to an open shadow holding after a paper buy."""
    ledger_path = STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"
    if not ledger_path.exists():
        return False
    try:
        df = pd.read_csv(ledger_path, low_memory=False)
    except (pd.errors.EmptyDataError, OSError):
        return False
    if df.empty:
        return False
    df = _ensure_ledger_columns(
        df,
        [
            "trade_status", "position_status", "last_state", "shadow_status",
            "entry_price", "entry_datetime", "entry_quantity", "entry_notional",
            "entry_position_pct", "paper_execution_id", "repair_tag", "repair_updated_at",
        ],
    )
    # Fresh CSV columns that are entirely blank are inferred as float by
    # pandas; make the mixed execution fields explicit objects before writing.
    execution_columns = [
        "trade_status", "position_status", "last_state", "shadow_status",
        "entry_price", "entry_datetime", "entry_quantity", "entry_notional",
        "entry_position_pct", "paper_execution_id", "repair_tag", "repair_updated_at",
    ]
    df[execution_columns] = df[execution_columns].astype(object)
    mask = _ledger_match_mask(df, ticket)
    if not bool(mask.any()):
        return False
    idx = list(df.index[mask])[-1]
    now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
    df.at[idx, "trade_status"] = "open_shadow"
    df.at[idx, "position_status"] = "open_shadow"
    df.at[idx, "last_state"] = "open_shadow"
    df.at[idx, "shadow_status"] = "open_shadow"
    df.at[idx, "entry_price"] = record.get("execution_price")
    df.at[idx, "entry_datetime"] = record.get("created_at") or now_text
    df.at[idx, "entry_quantity"] = record.get("quantity")
    df.at[idx, "entry_notional"] = record.get("notional")
    df.at[idx, "entry_position_pct"] = record.get("position_pct")
    df.at[idx, "paper_execution_id"] = record.get("execution_id")
    df.at[idx, "repair_tag"] = "shadow_entry_monitor"
    df.at[idx, "repair_updated_at"] = now_text
    temp_path = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
    df.to_csv(temp_path, index=False, encoding="utf-8-sig")
    os.replace(temp_path, ledger_path)
    return True


def _buyable_tick(tick: dict[str, Any], now: datetime) -> tuple[float | None, str]:
    if not isinstance(tick, dict):
        return None, "tick_missing"
    timetag = str(tick.get("timetag") or "").strip()
    if not timetag.startswith(now.strftime("%Y%m%d")):
        return None, "tick_not_from_today"
    last_price = _as_float(tick.get("lastPrice"), 0.0) or 0.0
    if last_price <= 0 or (_as_float(tick.get("volume"), 0.0) or 0.0) <= 0:
        return None, "tick_has_no_trade"
    asks = tick.get("askPrice") if isinstance(tick.get("askPrice"), list) else []
    ask_volumes = tick.get("askVol") if isinstance(tick.get("askVol"), list) else []
    ask_price = _as_float(asks[0], 0.0) if asks else 0.0
    ask_volume = _as_float(ask_volumes[0], 0.0) if ask_volumes else 0.0
    if not ask_price or ask_price <= 0 or not ask_volume or ask_volume <= 0:
        return None, "tick_not_buyable_no_best_ask"
    return ask_price, "best_ask_tick"


def _run_shadow_entry_monitor_once(source: str = "manual") -> dict[str, Any]:
    """Fill today's qualified planned tickets on their first executable QMT Tick.

    This is strictly a paper-ledger operation. It never calls the QMT order API.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if not _in_trading_window(now):
        return {"ok": True, "status": "skipped", "reason": "not_trading_window", "checked_at": now.isoformat(sep=" ", timespec="seconds")}
    if not SHADOW_ENTRY_LOCK.acquire(blocking=False):
        return {"ok": True, "status": "skipped", "reason": "entry_monitor_already_running", "checked_at": now.isoformat(sep=" ", timespec="seconds")}
    try:
        reconciliation = _reconcile_shadow_ledger_from_paper_executions()
        runtime = _load_current_runtime(limit=200)
        tickets = [item for item in (runtime.get("tickets") or []) if isinstance(item, dict)]
        ledger_records = _read_shadow_ledger_records(STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv", limit=None)
        paper_rows = _load_paper_executions()
        open_rows = _filter_open_shadow_ledger_records(ledger_records)
        open_codes = {str(row.get("code") or row.get("code_raw") or "").strip().upper() for row in open_rows}
        max_slots = max([int(_as_float(item.get("portfolio_slot_count"), 2) or 2) for item in tickets] or [2])
        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for ticket in tickets:
            code = str(ticket.get("code") or ticket.get("code_raw") or "").strip().upper()
            if _date_text(ticket.get("entry_date")) != today:
                continue
            if not _truthy(ticket.get("qualified_shadow_buy")) or not _truthy(ticket.get("m30_confirmed")):
                continue
            if code in open_codes:
                skipped.append({"code": code, "reason": "already_open_shadow"})
                continue
            if any(_paper_execution_matches_ticket(ticket, row) and _paper_execution_side(row) == "BUY" for row in paper_rows if isinstance(row, dict)):
                skipped.append({"code": code, "reason": "paper_execution_exists"})
                continue
            matching = [row for row in ledger_records if _ledger_match_mask(pd.DataFrame([row]), ticket).iloc[0]]
            if not matching or str(matching[-1].get("last_state") or "").strip().lower() != "planned":
                skipped.append({"code": code, "reason": "ledger_not_planned"})
                continue
            candidates.append(ticket)
        available_slots = max(0, max_slots - len(open_rows))
        candidates = candidates[:available_slots]
        tick_source: dict[str, Any] = {}
        ticks = _read_qmt_ticks([str(item.get("code") or item.get("code_raw") or "") for item in candidates], tick_source) if candidates else {}
        saved: list[dict[str, Any]] = []
        for ticket in candidates:
            code = str(ticket.get("code") or ticket.get("code_raw") or "").strip().upper()
            execution_price, price_source = _buyable_tick(ticks.get(code, {}) if isinstance(ticks, dict) else {}, now)
            if execution_price is None:
                skipped.append({"code": code, "reason": price_source})
                continue
            result = _submit_paper_order({
                "ticket_key": ticket.get("ticket_key"), "code": code,
                "execution_price": execution_price, "position_pct": ticket.get("position_pct"),
                "source": "g3_shadow_entry_tick_monitor",
                "notes": f"First buyable QMT Tick fill at best ask; tick={now.strftime('%Y-%m-%d %H:%M:%S')}; source={price_source}.",
            })
            if result.get("ok"):
                saved.append(result.get("record") or {})
                open_codes.add(code)
            else:
                skipped.append({"code": code, "reason": result.get("message") or "paper_order_failed"})
        result = {
            "ok": True, "mode": "g3_shadow_entry_tick_monitor", "source": source,
            "checked_at": now.isoformat(sep=" ", timespec="seconds"), "candidate_count": len(candidates),
            "filled_count": len(saved), "saved_entries": saved, "skipped": skipped,
            "tick_source": tick_source, "formal_order_status": "paper_only_no_real_order",
            "ledger_reconciliation": reconciliation,
        }
        for item in saved:
            _append_monitor_event({"type": "shadow_entry", "status": "open_shadow", "source": source, "code": item.get("code"), "message": "Qualified G3 ticket filled from first buyable QMT Tick; paper only."})
        return result
    finally:
        SHADOW_ENTRY_LOCK.release()


def _paper_execution_side(row: dict[str, Any]) -> str:
    side = str(row.get("side") or "").strip().upper()
    if side:
        return side
    text = " ".join(str(row.get(key) or "").lower() for key in ["status", "source", "exit_reason"])
    return "SELL" if "sell" in text or "exit" in text else "BUY"


def _paper_open_quantity(ticket_key: str, code: str, rows: list[dict[str, Any]]) -> int:
    buy_qty = 0
    sell_qty = 0
    code = str(code or "").strip().upper()
    ticket_key = str(ticket_key or "").strip()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        row_ticket = str(row.get("ticket_key") or "").strip()
        row_code = str(row.get("code") or row.get("code_raw") or "").strip().upper()
        if ticket_key and row_ticket and row_ticket != ticket_key:
            continue
        if not ticket_key and code and row_code != code:
            continue
        qty = int(_as_float(row.get("quantity"), 0.0) or 0)
        if qty <= 0:
            continue
        if _paper_execution_side(row) == "SELL":
            sell_qty += qty
        else:
            buy_qty += qty
    return max(0, buy_qty - sell_qty)


def _paper_exit_exists(ticket_key: str, code: str, exit_reason: str, exit_date: str, rows: list[dict[str, Any]]) -> bool:
    ticket_key = str(ticket_key or "").strip()
    code = str(code or "").strip().upper()
    exit_reason = str(exit_reason or "").strip()
    exit_date = str(exit_date or "").strip()
    for row in rows or []:
        if not isinstance(row, dict) or _paper_execution_side(row) != "SELL":
            continue
        row_ticket = str(row.get("ticket_key") or "").strip()
        row_code = str(row.get("code") or row.get("code_raw") or "").strip().upper()
        row_reason = str(row.get("exit_reason") or row.get("reason") or "").strip()
        row_date = _date_text(row.get("exit_date") or row.get("exit_datetime") or row.get("created_at"))
        if ticket_key and row_ticket and row_ticket != ticket_key:
            continue
        if code and row_code != code:
            continue
        if exit_reason and row_reason != exit_reason:
            continue
        if exit_date and row_date != exit_date:
            continue
        return True
    return False


def _latest_exit_market_prices(codes: list[str]) -> dict[str, dict[str, Any]]:
    clean_codes = sorted({str(code or "").strip().upper() for code in codes if str(code or "").strip()})
    clean_codes = [code for code in clean_codes if re.fullmatch(r"[0-9A-Z.]+", code)]
    if not clean_codes:
        return {}
    quoted_codes = ", ".join(f"'{code}'" for code in clean_codes)
    out: dict[str, dict[str, Any]] = {}
    try:
        from utils.market_warehouse import clickhouse_query_df

        minute = clickhouse_query_df(
            f"""
            SELECT k.code, k.datetime, k.close
            FROM kline_minute_30 AS k
            INNER JOIN (
                SELECT code, max(datetime) AS max_datetime
                FROM kline_minute_30
                WHERE code IN ({quoted_codes})
                GROUP BY code
            ) AS latest
                ON k.code = latest.code AND k.datetime = latest.max_datetime
            """
        )
        for item in minute.to_dict(orient="records"):
            code = str(item.get("code") or "").strip().upper()
            close = _as_float(item.get("close"), None)
            if not code or close is None or close <= 0:
                continue
            out[code] = {
                "current_price": close,
                "latest_price": close,
                "last_price": close,
                "close": close,
                "latest_price_datetime": str(item.get("datetime") or ""),
                "latest_price_date": _date_text(item.get("datetime")),
                "latest_price_source": "clickhouse:kline_minute_30",
            }
    except Exception:
        logger.exception("Failed to load latest 30m prices for G3 shadow exit monitor.")

    missing = [code for code in clean_codes if code not in out]
    if not missing:
        return out
    quoted_missing = ", ".join(f"'{code}'" for code in missing)
    try:
        from utils.market_warehouse import clickhouse_query_df

        daily = clickhouse_query_df(
            f"""
            SELECT k.code, k.trade_date, k.close
            FROM kline_daily AS k
            INNER JOIN (
                SELECT code, max(trade_date) AS max_date
                FROM kline_daily
                WHERE code IN ({quoted_missing})
                GROUP BY code
            ) AS latest
                ON k.code = latest.code AND k.trade_date = latest.max_date
            """
        )
        for item in daily.to_dict(orient="records"):
            code = str(item.get("code") or "").strip().upper()
            close = _as_float(item.get("close"), None)
            if not code or close is None or close <= 0:
                continue
            out[code] = {
                "current_price": close,
                "latest_price": close,
                "last_price": close,
                "close": close,
                "latest_price_datetime": str(item.get("trade_date") or ""),
                "latest_price_date": _date_text(item.get("trade_date")),
                "latest_price_source": "clickhouse:kline_daily",
            }
    except Exception:
        logger.exception("Failed to load latest daily prices for G3 shadow exit monitor.")
    return out


def _apply_latest_exit_prices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    codes = [str(row.get("code") or row.get("code_raw") or "").strip().upper() for row in rows if isinstance(row, dict)]
    prices = _latest_exit_market_prices(codes)
    if not prices:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        code = str(row.get("code") or row.get("code_raw") or "").strip().upper()
        latest = prices.get(code)
        if not latest:
            out.append(row)
            continue
        item = dict(row)
        item.update(latest)
        out.append(item)
    return out


def _shadow_exit_reason_for_action(action: str, reason: str) -> str:
    if action == "sell_half":
        return "take_profit_partial_30m"
    if reason == "hard_stop_triggered":
        return "hard_stop_30m"
    if reason == "structure_stop_triggered":
        return "prev_low_break_30m"
    return reason or action or "shadow_exit"


def _ledger_match_mask(df: pd.DataFrame, row: dict[str, Any]) -> pd.Series:
    mask = pd.Series([False] * len(df), index=df.index)
    ticket_key = str(row.get("ticket_key") or "").strip()
    if ticket_key and "ticket_key" in df.columns:
        mask = df["ticket_key"].astype(str).str.strip().eq(ticket_key)
        if bool(mask.any()):
            return mask
    code = str(row.get("code") or row.get("code_raw") or "").strip().upper()
    entry_date = _date_text(row.get("entry_date"))
    route = str(row.get("route") or "").strip()
    if code and "code" in df.columns:
        mask = df["code"].astype(str).str.strip().str.upper().eq(code)
        if entry_date and "entry_date" in df.columns:
            mask &= pd.to_datetime(df["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d").eq(entry_date)
        if route and "route" in df.columns:
            mask &= df["route"].astype(str).str.strip().eq(route)
    return mask


def _ensure_ledger_columns(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for col in columns:
        if col not in df.columns:
            df[col] = None
    return df


def _update_shadow_ledger_for_exit(df: pd.DataFrame, row: dict[str, Any], record: dict[str, Any], action: str, remaining_pct: float) -> pd.DataFrame:
    df = _ensure_ledger_columns(
        df,
        [
            "trade_status",
            "position_status",
            "last_state",
            "exit_date",
            "exit_datetime",
            "exit_price",
            "exit_reason",
            "realized_ret",
            "remaining_position_pct",
            "half_take_profit_done",
            "entry_position_pct",
            "last_exit_date",
            "last_exit_datetime",
            "last_exit_price",
            "last_exit_reason",
            "repair_tag",
            "repair_updated_at",
        ],
    )
    mask = _ledger_match_mask(df, row)
    if not bool(mask.any()):
        return df
    idx = list(df.index[mask])[-1]
    now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
    entry_pct = _as_float(df.at[idx, "entry_position_pct"], None)
    if entry_pct is None or entry_pct <= 0:
        entry_pct = _as_float(row.get("entry_position_pct") or row.get("max_position_pct") or row.get("position_pct"), None)
    if entry_pct is not None:
        df.at[idx, "entry_position_pct"] = entry_pct
    df.at[idx, "realized_ret"] = record.get("realized_ret")
    df.at[idx, "remaining_position_pct"] = remaining_pct
    df.at[idx, "repair_tag"] = "shadow_exit_monitor"
    df.at[idx, "repair_updated_at"] = now_text
    if action == "sell_half":
        df.at[idx, "trade_status"] = "partial_taken"
        df.at[idx, "position_status"] = "open_shadow"
        df.at[idx, "last_state"] = "partial_taken"
        df.at[idx, "half_take_profit_done"] = True
        df.at[idx, "last_exit_date"] = record.get("exit_date")
        df.at[idx, "last_exit_datetime"] = record.get("exit_datetime")
        df.at[idx, "last_exit_price"] = record.get("execution_price")
        df.at[idx, "last_exit_reason"] = record.get("exit_reason")
    else:
        df.at[idx, "trade_status"] = "closed"
        df.at[idx, "position_status"] = "closed"
        df.at[idx, "last_state"] = "closed"
        df.at[idx, "exit_date"] = record.get("exit_date")
        df.at[idx, "exit_datetime"] = record.get("exit_datetime")
        df.at[idx, "exit_price"] = record.get("execution_price")
        df.at[idx, "exit_reason"] = record.get("exit_reason")
    return df


def _run_shadow_exit_monitor_once(
    *,
    source: str = "manual",
    paper_exit_enabled: bool = True,
    update_ledger_enabled: bool = True,
) -> dict[str, Any]:
    started_at = datetime.now()
    ledger_path = STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"
    if not ledger_path.exists():
        return {
            "ok": True,
            "status": "skipped",
            "reason": "shadow_ledger_missing",
            "checked_at": started_at.isoformat(sep=" ", timespec="seconds"),
            "triggered_exit_count": 0,
        }
    try:
        ledger_df = pd.read_csv(ledger_path, low_memory=False)
    except pd.errors.EmptyDataError:
        ledger_df = pd.DataFrame()
    if ledger_df.empty:
        return {
            "ok": True,
            "status": "skipped",
            "reason": "shadow_ledger_empty",
            "checked_at": started_at.isoformat(sep=" ", timespec="seconds"),
            "triggered_exit_count": 0,
        }

    ledger_records = _read_shadow_ledger_records(ledger_path, limit=None)
    open_rows = _filter_open_shadow_ledger_records(ledger_records)
    priced_rows = _apply_latest_exit_prices(open_rows)
    advised_rows = _attach_exit_advice(priced_rows, source="shadow_ledger", updated_at=started_at.isoformat(sep=" ", timespec="seconds"))
    exit_rows = [
        row for row in advised_rows
        if isinstance(row, dict) and str(row.get("exit_action") or "") in {"sell_all", "sell_half"}
    ]
    paper_rows = _formal_score88_paper_rows(_load_paper_executions())
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    updated_df = ledger_df.copy()
    for row in exit_rows:
        code = str(row.get("code") or row.get("code_raw") or "").strip().upper()
        ticket_key = str(row.get("ticket_key") or "").strip()
        action = str(row.get("exit_action") or "").strip()
        exit_reason = _shadow_exit_reason_for_action(action, str(row.get("exit_reason") or ""))
        price = _as_float(row.get("trigger_price") or row.get("current_price_for_exit") or row.get("current_price"), None)
        if price is None or price <= 0:
            skipped.append({"code": code, "ticket_key": ticket_key, "reason": "invalid_exit_price"})
            continue
        exit_dt = str(row.get("latest_price_datetime") or started_at.isoformat(sep=" ", timespec="seconds"))
        exit_date = _date_text(exit_dt or started_at)
        if _paper_exit_exists(ticket_key, code, exit_reason, exit_date, paper_rows):
            skipped.append({"code": code, "ticket_key": ticket_key, "reason": "paper_exit_already_exists", "exit_reason": exit_reason, "exit_date": exit_date})
            continue
        if not paper_exit_enabled:
            skipped.append({"code": code, "ticket_key": ticket_key, "reason": "paper_exit_disabled", "exit_reason": exit_reason, "exit_date": exit_date})
            continue
        open_qty = _paper_open_quantity(ticket_key, code, paper_rows)
        if open_qty <= 0:
            open_qty = _default_paper_quantity(price, row.get("position_pct") or row.get("remaining_position_pct"), None)
        sell_ratio = _as_float(row.get("suggested_sell_ratio"), 1.0 if action == "sell_all" else 0.5) or 0.0
        quantity = open_qty if action == "sell_all" else int((open_qty * sell_ratio) // 100) * 100
        if quantity <= 0 and action == "sell_half" and open_qty > 0:
            quantity = min(open_qty, 100)
        if quantity <= 0:
            skipped.append({"code": code, "ticket_key": ticket_key, "reason": "invalid_exit_quantity", "open_quantity": open_qty})
            continue
        position_pct = _as_float(row.get("position_pct") or row.get("remaining_position_pct"), 0.0) or 0.0
        sold_pct = position_pct if action == "sell_all" else max(0.0, position_pct * sell_ratio)
        remaining_pct = 0.0 if action == "sell_all" else max(0.0, position_pct - sold_pct)
        entry_price = _as_float(row.get("entry_price_for_exit") or row.get("entry_price") or row.get("reference_close"), None)
        realized_ret = None
        if entry_price is not None and entry_price > 0:
            realized_ret = round(price / entry_price - 1.0, 8)
        record = {
            "execution_id": f"g3paper_exit_{started_at.strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}",
            "created_at": started_at.isoformat(sep=" ", timespec="seconds"),
            "status": "paper_exit_submitted",
            "source": source,
            "strategy_id": row.get("strategy_id") or STRATEGY_ID,
            "contract": row.get("strategy_profile_name") or row.get("strategy_profile") or LATEST_G3_PROFILE,
            "shadow_only": True,
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "ticket_key": ticket_key,
            "entry_date": row.get("entry_date"),
            "decision_date": row.get("decision_date"),
            "exit_date": exit_date,
            "exit_datetime": exit_dt,
            "route": row.get("route"),
            "route_label": row.get("route_label"),
            "code": code,
            "name": row.get("name") or row.get("stock_name"),
            "side": "SELL",
            "execution_price": price,
            "quantity": quantity,
            "notional": round(price * quantity, 2),
            "position_pct": sold_pct,
            "remaining_position_pct_after": remaining_pct,
            "exit_reason": exit_reason,
            "exit_action": action,
            "realized_ret": realized_ret,
            "price_source": row.get("latest_price_source"),
            "notes": "G3 shadow exit monitor recorded paper sell only; no real order sent.",
        }
        saved.append(record)
        paper_rows.append(record)
        if update_ledger_enabled:
            updated_df = _update_shadow_ledger_for_exit(updated_df, row, record, action, remaining_pct)

    if saved:
        _save_paper_executions(paper_rows)
    if saved and update_ledger_enabled:
        backup_path = ledger_path.with_name(f"{ledger_path.name}.bak_shadow_exit_monitor_{started_at.strftime('%Y%m%d')}")
        if not backup_path.exists():
            ledger_path.replace(backup_path)
            updated_df.to_csv(ledger_path, index=False, encoding="utf-8-sig")
        else:
            updated_df.to_csv(ledger_path, index=False, encoding="utf-8-sig")
    for record in saved:
        _append_monitor_event(
            {
                "type": "shadow_exit",
                "status": "paper_exit_submitted",
                "source": source,
                "ticket_key": record.get("ticket_key"),
                "code": record.get("code"),
                "exit_reason": record.get("exit_reason"),
                "message": "G3 shadow exit monitor recorded a paper sell.",
            }
        )
    exit_email = _send_shadow_exit_notification(saved) if saved else {"sent": False, "reason": "no_exit"}

    finished_at = datetime.now()
    return {
        "ok": True,
        "status": "success",
        "source": source,
        "started_at": started_at.isoformat(sep=" ", timespec="seconds"),
        "finished_at": finished_at.isoformat(sep=" ", timespec="seconds"),
        "open_holding_count": len(open_rows),
        "exit_candidate_count": len(exit_rows),
        "triggered_exit_count": len(saved),
        "skipped_count": len(skipped),
        "saved_exits": saved,
        "exit_email": exit_email,
        "skipped": skipped,
        "artifacts": {
            "shadow_ledger": _path_status(ledger_path),
            "paper_executions": _path_status(PAPER_EXECUTIONS_PATH),
            "exit_monitor_state": _path_status(EXIT_MONITOR_STATE_PATH),
        },
    }


def _day1_pack_by_code() -> dict[str, dict[str, Any]]:
    rows = _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "day1_paper_review_pack.csv")
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        code = str(row.get("code") or "").strip().upper()
        if code:
            out[code] = row
    return out


def _day1_launch_posture_blocks_execution(pack_row: dict[str, Any] | None) -> bool:
    if not pack_row:
        return False
    posture = str(pack_row.get("launch_posture") or "").strip()
    if posture in {"contract_block_observation", "skip", "wait_refresh"}:
        return True
    reason = str(pack_row.get("contract_block_reason") or pack_row.get("paper_watch_action") or "")
    return "index_mom60<=5" in reason or "只做观察" in reason


def _day1_paper_execution_preview(ticket: dict[str, Any], pack_row: dict[str, Any] | None, base_capital: Any = None) -> dict[str, Any]:
    price = _as_float(ticket.get("reference_close"), None)
    position_pct = _as_float(ticket.get("position_pct"), None)
    quantity = _default_paper_quantity(price, position_pct, base_capital)
    return {
        "ticket_key": ticket.get("ticket_key") or ticket.get("candidate_key") or ticket.get("trade_key"),
        "code": ticket.get("code") or ticket.get("code_raw"),
        "name": ticket.get("name") or ticket.get("stock_name"),
        "entry_date": ticket.get("entry_date"),
        "route": ticket.get("route"),
        "launch_posture": (pack_row or {}).get("launch_posture"),
        "execution_price": price,
        "quantity": quantity,
        "notional": round((price or 0.0) * quantity, 2),
        "position_pct": position_pct,
        "paper_watch_action": (pack_row or {}).get("paper_watch_action"),
        "hidden_risk_focus": (pack_row or {}).get("hidden_risk_focus"),
        "after_close_required_note": (pack_row or {}).get("after_close_required_note"),
    }


def _day1_paper_order_payload(ticket: dict[str, Any], pack_row: dict[str, Any] | None, base_capital: Any = None) -> dict[str, Any]:
    preview = _day1_paper_execution_preview(ticket, pack_row, base_capital=base_capital)
    notes = "Day1纸面执行：按复盘包记录，不触发真实下单。"
    hidden_focus = str((pack_row or {}).get("hidden_risk_focus") or "").strip()
    if hidden_focus:
        notes = f"{notes} 隐患重点：{hidden_focus}"
    return {
        "ticket_key": preview.get("ticket_key"),
        "code": preview.get("code"),
        "execution_price": preview.get("execution_price"),
        "quantity": preview.get("quantity"),
        "position_pct": preview.get("position_pct"),
        "base_capital": base_capital,
        "source": "g3_day1_paper_review_pack",
        "notes": notes,
    }


def _load_observation_snapshots() -> list[dict[str, Any]]:
    data = _read_json(OBSERVATION_SNAPSHOTS_PATH)
    rows = data.get("snapshots") if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def _save_observation_snapshots(rows: list[dict[str, Any]]) -> None:
    rows = sorted(rows, key=lambda item: str(item.get("observation_date") or item.get("created_at") or ""))
    _write_json(OBSERVATION_SNAPSHOTS_PATH, {"snapshots": rows[-500:]})


def _accepted_observation_dates(rows: list[dict[str, Any]]) -> list[str]:
    """Count only observations produced under the frozen formal Score88 contract.

    Historical G3 snapshots are retained for audit, but must never advance the
    30-trading-day evidence window after a strategy-contract switch.
    """
    return sorted({
        str(item.get("observation_date") or "")[:10]
        for item in rows
        if (
            isinstance(item, dict)
            and item.get("accepted")
            and str(item.get("strategy_id") or "") == STRATEGY_ID
            and str(item.get("contract") or "") == STRATEGY_ID
            and str(item.get("observation_date") or "").strip()
        )
    })


def _formal_score88_observation_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item for item in rows
        if (
            isinstance(item, dict)
            and str(item.get("strategy_id") or "") == STRATEGY_ID
            and str(item.get("contract") or "") == STRATEGY_ID
        )
    ]


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
            str(PROJECT_ROOT / "scripts" / "qmt_xtquant_minute_backfill_validate.py"),
            "--phase",
            "all",
            "--start-date",
            target_date,
            "--end-date",
            target_date,
            "--periods",
            "30m",
            "--codes",
            ",".join(codes),
            "--batch-size",
            "30",
            "--reset-stage",
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
    paper_rows = _formal_score88_paper_rows(_load_paper_executions())
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
    paper_rows = _formal_score88_paper_rows(_load_paper_executions())
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
        paper_rows = _formal_score88_paper_rows(_load_paper_executions())
        smoke = _latest_pretrade_smoke()

    observation_date = _formal_observation_date(entry_date)
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
        return
    if state.get("paper_entry_enabled", True):
        entry_result = _run_shadow_entry_monitor_once(source="scheduler")
        state = _load_monitor_state()
        state["last_entry_result"] = entry_result
        _save_monitor_state(state)
    if state.get("trading_hours_only") and not _in_trading_window(now):
        state["last_run_at"] = now.isoformat(sep=" ", timespec="seconds")
        state["last_result"] = {
            "skipped": True,
            "reason": "非交易日或非交易时段",
            "checked_at": state["last_run_at"],
        }
        _save_monitor_state(state)
        return
    if not state.get("run_current_refresh", True):
        state["last_run_at"] = now.isoformat(sep=" ", timespec="seconds")
        state["last_result"] = {
            "skipped": True,
            "reason": "run_current_refresh_disabled",
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


def _exit_monitor_job_wrapper() -> None:
    state = _load_exit_monitor_state()
    if not state.get("enabled"):
        return
    now = datetime.now()
    if state.get("trading_hours_only") and not _in_trading_window(now):
        state["last_run_at"] = now.isoformat(sep=" ", timespec="seconds")
        state["last_result"] = {
            "ok": True,
            "skipped": True,
            "reason": "not_trading_window",
            "checked_at": state["last_run_at"],
        }
        _save_exit_monitor_state(state)
        return
    started_at = datetime.now()
    state["last_run_at"] = started_at.isoformat(sep=" ", timespec="seconds")
    _save_exit_monitor_state(state)
    try:
        result = _run_shadow_exit_monitor_once(
            source="scheduler",
            paper_exit_enabled=bool(state.get("paper_exit_enabled", True)),
            update_ledger_enabled=bool(state.get("update_ledger_enabled", True)),
        )
        finished_at = datetime.now()
        state = _load_exit_monitor_state()
        state["last_run_at"] = started_at.isoformat(sep=" ", timespec="seconds")
        state["last_result"] = result
        state["last_error"] = None if result.get("ok") else str(result.get("error") or result.get("reason") or "")
        if result.get("ok"):
            state["last_success_at"] = finished_at.isoformat(sep=" ", timespec="seconds")
        _save_exit_monitor_state(state)
    except Exception as exc:
        logger.exception("G3 shadow exit monitor scheduler failed.")
        state = _load_exit_monitor_state()
        state["last_run_at"] = started_at.isoformat(sep=" ", timespec="seconds")
        state["last_error"] = str(exc)
        state["last_result"] = {"ok": False, "error": str(exc)}
        _save_exit_monitor_state(state)
        _append_monitor_event(
            {
                "type": "shadow_exit",
                "status": "error",
                "source": "scheduler",
                "message": "G3 shadow exit monitor scheduler failed",
                "error": str(exc),
            }
        )
        _send_exception_notification("影子盘卖出监控失败", str(exc))


def _daily_trend_exit_monitor_job_wrapper() -> None:
    state = _load_daily_trend_exit_monitor_state()
    if not state.get("enabled"):
        return
    now = datetime.now()
    if state.get("trading_hours_only") and not _in_trading_window(now):
        return
    try:
        if not TradingCalendar.is_trading_day(now):
            return
    except Exception:
        logger.exception("Failed to check trading day for G3 daily trend exit monitor.")
    try:
        _run_daily_trend_exit_monitor_once(source="scheduler")
    except Exception as exc:
        logger.exception("G3 daily trend exit monitor scheduler failed.")
        state = _load_daily_trend_exit_monitor_state()
        state["last_run_at"] = now.isoformat(sep=" ", timespec="seconds")
        state["last_error"] = str(exc)
        state["last_result"] = {"ok": False, "error": str(exc)}
        _save_daily_trend_exit_monitor_state(state)
        _append_monitor_event(
            {
                "type": "daily_trend_exit",
                "status": "error",
                "source": "scheduler",
                "message": "G3 daily trend exit monitor scheduler failed",
                "error": str(exc),
            }
        )


def configure_shadow_monitor_scheduler() -> dict[str, Any]:
    state = _load_monitor_state()
    if not _startup_schedulers_enabled():
        if _monitor_scheduler is not None:
            try:
                if _monitor_scheduler.get_job(_monitor_job_id):
                    _monitor_scheduler.remove_job(_monitor_job_id)
            except Exception:
                logger.exception("Failed to remove existing G3 monitor job")
        return _scheduler_disabled_result(state, _monitor_job_id)
    scheduler = _ensure_monitor_scheduler()
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


def configure_shadow_exit_monitor_scheduler() -> dict[str, Any]:
    state = _load_exit_monitor_state()
    if not _startup_schedulers_enabled():
        if _monitor_scheduler is not None:
            try:
                if _monitor_scheduler.get_job(_exit_monitor_job_id):
                    _monitor_scheduler.remove_job(_exit_monitor_job_id)
            except Exception:
                logger.exception("Failed to remove existing G3 shadow exit monitor job")
        return _scheduler_disabled_result(state, _exit_monitor_job_id)
    scheduler = _ensure_monitor_scheduler()
    try:
        if scheduler.get_job(_exit_monitor_job_id):
            scheduler.remove_job(_exit_monitor_job_id)
    except Exception:
        logger.exception("Failed to remove existing G3 shadow exit monitor job")
    if state.get("enabled"):
        scheduler.add_job(
            _exit_monitor_job_wrapper,
            "interval",
            seconds=max(120, int(state.get("interval_seconds") or 120)),
            id=_exit_monitor_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    job = scheduler.get_job(_exit_monitor_job_id)
    return {
        **state,
        "scheduler_enabled": bool(job),
        "scheduler_wired": True,
        "job_id": _exit_monitor_job_id,
        "next_run_time": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


def configure_daily_trend_exit_monitor_scheduler() -> dict[str, Any]:
    state = _load_daily_trend_exit_monitor_state()
    if not _startup_schedulers_enabled():
        if _monitor_scheduler is not None:
            try:
                if _monitor_scheduler.get_job(_daily_trend_exit_monitor_job_id):
                    _monitor_scheduler.remove_job(_daily_trend_exit_monitor_job_id)
            except Exception:
                logger.exception("Failed to remove G3 daily trend exit monitor job")
        return _scheduler_disabled_result(state, _daily_trend_exit_monitor_job_id)
    scheduler = _ensure_monitor_scheduler()
    try:
        if scheduler.get_job(_daily_trend_exit_monitor_job_id):
            scheduler.remove_job(_daily_trend_exit_monitor_job_id)
    except Exception:
        logger.exception("Failed to remove G3 daily trend exit monitor job")
    if state.get("enabled"):
        scheduler.add_job(
            _daily_trend_exit_monitor_job_wrapper,
            "interval",
            seconds=int(state.get("interval_seconds") or 300),
            id=_daily_trend_exit_monitor_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    job = scheduler.get_job(_daily_trend_exit_monitor_job_id)
    return {
        **state,
        "scheduler_enabled": bool(job),
        "scheduler_wired": True,
        "job_id": _daily_trend_exit_monitor_job_id,
        "next_run_time": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


def configure_observation_scheduler() -> dict[str, Any]:
    state = _load_observation_scheduler_state()
    if not _startup_schedulers_enabled():
        if _monitor_scheduler is not None:
            for job_id in (_observation_job_id, _observation_retry_job_id):
                try:
                    if _monitor_scheduler.get_job(job_id):
                        _monitor_scheduler.remove_job(job_id)
                except Exception:
                    logger.exception("Failed to remove existing G3 observation job")
        return _scheduler_disabled_result(state, _observation_job_id)
    scheduler = _ensure_monitor_scheduler()
    for job_id in (_observation_job_id, _observation_retry_job_id):
        try:
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
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
        retry_minutes = _coerce_scheduler_minutes(state.get("retry_minutes"), [])
        if retry_minutes:
            scheduler.add_job(
                _observation_job_wrapper,
                "cron",
                day_of_week="mon-fri",
                hour=int(state.get("retry_hour") or 10),
                minute=",".join(str(item) for item in retry_minutes),
                id=_observation_retry_job_id,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
    jobs = [job for job in (scheduler.get_job(_observation_job_id), scheduler.get_job(_observation_retry_job_id)) if job]
    next_run_times = [job.next_run_time for job in jobs if job.next_run_time]
    next_run_time = min(next_run_times).isoformat() if next_run_times else None
    return {
        **state,
        "scheduler_enabled": bool(jobs),
        "scheduler_wired": True,
        "job_id": _observation_job_id,
        "job_ids": [job.id for job in jobs],
        "retry_job_id": _observation_retry_job_id,
        "next_run_time": next_run_time,
        "scheduled_jobs": [
            {
                "job_id": job.id,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in jobs
        ],
    }


def configure_broker_sync_scheduler() -> dict[str, Any]:
    state = _load_broker_sync_state()
    if not _startup_schedulers_enabled():
        if _monitor_scheduler is not None:
            try:
                if _monitor_scheduler.get_job(_broker_sync_job_id):
                    _monitor_scheduler.remove_job(_broker_sync_job_id)
            except Exception:
                logger.exception("Failed to remove existing G3 broker sync job")
        return _scheduler_disabled_result(state, _broker_sync_job_id)
    scheduler = _ensure_monitor_scheduler()
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


def _auto_order_30m_job_wrapper() -> None:
    """Refresh same-day G3 tickets, then run the closed-by-default executor."""
    try:
        now = datetime.now()
        hhmm = now.strftime("%H:%M")
        if not (("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "14:57")):
            return
        from execution.gen3_auto_order_executor import refresh_current_30m_router, run_exit_once, run_once

        exit_result = run_exit_once(source="scheduler_30m")
        refresh = refresh_current_30m_router()
        result = run_once(source="scheduler_30m") if refresh.get("ok") else {
            "ok": False,
            "skipped": True,
            "reason": "router_refresh_failed",
            "router_refresh": refresh,
        }
        result["exit_cycle"] = exit_result
        _append_monitor_event(
            {
                "type": "auto_order_30m",
                "status": "ok" if result.get("ok") else "error",
                "source": "scheduler",
                "message": "G3 30m auto-order cycle completed",
                "result": _compact_monitor_value(result),
            }
        )
    except Exception as exc:
        logger.exception("G3 30m auto-order scheduler failed.")
        _append_monitor_event(
            {
                "type": "auto_order_30m",
                "status": "error",
                "source": "scheduler",
                "message": "G3 30m auto-order scheduler failed",
                "error": str(exc),
            }
        )


def configure_auto_order_scheduler() -> dict[str, Any]:
    """Wire the post-bar execution cycle without arming it by default."""
    from execution.gen3_auto_order_executor import load_state

    state = load_state()
    if not _startup_schedulers_enabled():
        if _monitor_scheduler is not None:
            try:
                if _monitor_scheduler.get_job(_auto_order_job_id):
                    _monitor_scheduler.remove_job(_auto_order_job_id)
            except Exception:
                logger.exception("Failed to remove G3 auto-order job")
        return _scheduler_disabled_result(state, _auto_order_job_id)
    scheduler = _ensure_monitor_scheduler()
    try:
        if scheduler.get_job(_auto_order_job_id):
            scheduler.remove_job(_auto_order_job_id)
    except Exception:
        logger.exception("Failed to remove existing G3 auto-order job")
    if state.get("enabled"):
        # A 30m confirmation is usable only after the bar closes.  :01 avoids
        # treating a still-forming bar as confirmation and has no fixed 10:30 assumption.
        scheduler.add_job(
            _auto_order_30m_job_wrapper,
            "cron",
            day_of_week="mon-fri",
            hour="10-11,13-14",
            minute="1,31",
            id=_auto_order_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    job = scheduler.get_job(_auto_order_job_id)
    return {
        **state,
        "scheduler_enabled": bool(job),
        "scheduler_wired": True,
        "job_id": _auto_order_job_id,
        "schedule": "post_completed_30m_bar_at_minute_01_or_31",
        "next_run_time": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }


def init_gen3_state_alpha_monitor_scheduler_from_config() -> dict[str, Any]:
    return {
        "shadow_monitor": configure_shadow_monitor_scheduler(),
        "shadow_exit_monitor": configure_shadow_exit_monitor_scheduler(),
        "daily_trend_exit_monitor": configure_daily_trend_exit_monitor_scheduler(),
        "observation_scheduler": configure_observation_scheduler(),
        "broker_sync": configure_broker_sync_scheduler(),
        "auto_order_30m": configure_auto_order_scheduler(),
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
    "g2_g2_gap_supplement": "volume_runup_supplement",
    "g2_volume5_keep80_runup_sector_bonus": "volume_runup_supplement",
    "g2_volume5_keep80_runup": "volume_runup_supplement",
}

TRADE_STRATEGY_LABELS = {
    "institutional_mainwave_score88": "机构主升Score88",
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
    "institutional_institutional_mainwave": "institutional_mainwave_score88",
    "old_g3_strong_main": "old_g3_strong_breakout",
    "g2_g2_gap_supplement": "volume_runup_supplement",
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
    inferred_trade_strategy = strategy_key.map(ROUTE_STRATEGY_TO_TRADE_STRATEGY).fillna("other")
    existing_trade_strategy = _first_text_series(out, ["trade_strategy"], "")
    # A route is a portfolio bucket, not necessarily a single entry mode.
    # Preserve the two current G3 modes when rebuilding historical evidence.
    current_modes = {"institutional_mainwave_score88", "institutional_score120_mainwave", "mainwave_breakout_initiation"}
    trade_strategy = existing_trade_strategy.where(existing_trade_strategy.isin(current_modes), inferred_trade_strategy)
    out["trade_strategy"] = trade_strategy
    existing_trade_label = _first_text_series(out, ["trade_strategy_label"], "")
    out["trade_strategy_label"] = existing_trade_label.where(
        existing_trade_label.str.len() > 0,
        trade_strategy.map(TRADE_STRATEGY_LABELS).fillna("其他策略"),
    )
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
    for col in ["exit_datetime", "sell_datetime", "policy_exit_datetime"]:
        if col not in out.columns:
            out[col] = out["exit_ts"]
        else:
            out[col] = out[col].fillna("").astype(str)
            out.loc[out[col].str.strip().eq(""), col] = out.loc[out[col].str.strip().eq(""), "exit_ts"]
    if "exit_date" not in out.columns:
        out["exit_date"] = pd.to_datetime(out["exit_ts"], errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        out["exit_date"] = out["exit_date"].fillna("").astype(str)
        missing_exit_date = out["exit_date"].str.strip().eq("")
        out.loc[missing_exit_date, "exit_date"] = pd.to_datetime(
            out.loc[missing_exit_date, "exit_ts"],
            errors="coerce",
        ).dt.strftime("%Y-%m-%d")
    if "source_type" not in out.columns:
        out["source_type"] = "historical_closed_trade"
    else:
        out["source_type"] = out["source_type"].fillna("historical_closed_trade")
        blank_source = out["source_type"].astype(str).str.strip().eq("")
        out.loc[blank_source, "source_type"] = "historical_closed_trade"
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
    hard_stop_match = int(hard_stop.dropna().round(4).isin({0.04, 0.10, 0.12}).sum()) if len(hard_stop.dropna()) else 0
    take_profit_match = int(take_profit.dropna().round(4).isin({0.10, 0.12}).sum()) if len(take_profit.dropna()) else 0
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


def _current_broker_open_trade_rows() -> pd.DataFrame:
    try:
        snapshot = _broker_snapshot()
    except Exception:
        logger.exception("Failed to read broker open holdings for historical trades")
        return pd.DataFrame()
    holdings = snapshot.get("holdings") if isinstance(snapshot.get("holdings"), list) else []
    if not holdings:
        return pd.DataFrame()
    df = pd.DataFrame(holdings)
    if df.empty:
        return df
    shares = pd.to_numeric(df.get("shares"), errors="coerce")
    df = df[shares.fillna(0) > 0].copy()
    if df.empty:
        return df
    updated_at = str(snapshot.get("updated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    out = pd.DataFrame(index=df.index)
    entry_raw = df["entry_date"] if "entry_date" in df.columns else pd.Series(updated_at, index=df.index)
    out["entry_date"] = pd.to_datetime(entry_raw, errors="coerce").dt.strftime("%Y-%m-%d")
    out["entry_date"] = out["entry_date"].fillna(updated_at[:10])
    out["policy_exit_date"] = ""
    out["decision_date"] = ""
    out["confirm_datetime"] = ""
    out["entry_ts"] = ""
    out["exit_ts"] = ""
    out["exit_date"] = ""
    out["exit_datetime"] = ""
    out["code"] = df.get("code", "").map(_row_code) if "code" in df.columns else ""
    out["name"] = df.get("name", out["code"])
    out["route"] = "broker_real_position"
    out["route_label"] = "real broker holding"
    out["mode_label"] = "current open holding"
    out["score"] = pd.NA
    out["raw_score"] = pd.NA
    out["score_source"] = "broker_position"
    out["score_scale"] = "not_applicable"
    out["net_ret"] = pd.to_numeric(df.get("pnl_ratio"), errors="coerce")
    out["stress_net_ret"] = pd.NA
    out["policy_net_ret"] = pd.NA
    out["entry_price"] = pd.to_numeric(df.get("entry_price", df.get("cost_price")), errors="coerce")
    out["reference_close"] = pd.to_numeric(df.get("reference_close", df.get("current_price")), errors="coerce")
    out["stake"] = pd.to_numeric(df.get("market_value"), errors="coerce")
    out["exit_value"] = pd.NA
    out["realized_pnl"] = pd.NA
    out["market_style"] = ""
    out["policy"] = "broker_open_position"
    out["confirm_rule"] = ""
    out["live_ready"] = True
    out["shadow_action"] = "broker_open_holding"
    out["formal_buy_signal"] = False
    out["auto_order_allowed"] = False
    out["order_path_enabled"] = False
    out["block_reason"] = ""
    out["position_slots"] = pd.NA
    out["slot_pct"] = pd.to_numeric(df.get("position_pct"), errors="coerce")
    out["position_pct"] = pd.to_numeric(df.get("position_pct"), errors="coerce")
    out["trade_key"] = "broker_open|" + out["code"].astype(str)
    out["candidate_key"] = out["trade_key"]
    out["trade_status"] = "open_shadow"
    out["shadow_status"] = "broker_open"
    out["last_state"] = "open"
    out["planned_entry_ts"] = ""
    out["structure_stop"] = pd.NA
    out["hard_stop"] = pd.to_numeric(df.get("hard_stop"), errors="coerce")
    out["take_profit_1"] = pd.to_numeric(df.get("take_profit_1"), errors="coerce")
    out["exit_contract"] = "managed_by_g3_exit_contract"
    out["source_type"] = "broker_state_open_holding"
    return out.reset_index(drop=True)


def _current_open_trade_rows() -> pd.DataFrame:
    frames = [_current_shadow_trade_rows(), _current_broker_open_trade_rows()]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    if "trade_key" in out.columns:
        out = out.drop_duplicates(subset=["trade_key"], keep="last")
    return out


def _read_historical_trades(
    limit: int,
    route: str | None,
    window: str | None,
    sort_by: str,
    sort_order: str,
) -> dict[str, Any]:
    historical_path = _historical_trades_path()
    curve_df, curve_path = _read_equity_curve_df(window)
    shadow_df = _current_open_trade_rows()
    natural_policy_shadow = _read_natural_policy_shadow()
    if not historical_path.exists():
        if not shadow_df.empty and route and route != "all" and "route" in shadow_df.columns:
            shadow_df = shadow_df[shadow_df["route"].astype(str) == route]
        if not shadow_df.empty:
            shadow_df = shadow_df[_historical_window_mask(shadow_df, window)]
            shadow_df = _with_route_strategy_fields(shadow_df)
        replay_candidates = _historical_candidate_replay_rows(shadow_df, limit, route, window)
        page_df = shadow_df.head(limit).astype(object).where(pd.notna(shadow_df.head(limit)), None)
        return {
            "rows": json.loads(page_df.to_json(orient="records", force_ascii=False)) if not page_df.empty else [],
            "historical_replay_candidates": replay_candidates["rows"],
            "historical_replay_summary": replay_candidates["summary"],
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
                "closed_trades": _path_status(historical_path),
                "integrated_historical_trades": _path_status(HISTORICAL_TRADES_WITH_OPEN_PATH),
                "base_closed_trades": _path_status(HISTORICAL_TRADES_BASE_PATH),
                "runtime_shadow_ledger": _path_status(STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"),
                "equity_curve": _path_status(curve_path),
                "mtm_equity_curve": _path_status(MTM_EQUITY_CURVE_PATH),
                "summary": _path_status(PROMOTION_SUMMARY_PATH),
                "gates": _path_status(PROMOTION_GATES_PATH),
                **replay_candidates["artifacts"],
            },
            "natural_policy_shadow": natural_policy_shadow,
        }
    try:
        df = pd.read_csv(historical_path, low_memory=False)
    except pd.errors.EmptyDataError:
        df = pd.DataFrame()
    except Exception:
        logger.exception("Failed to read historical trades: %s", historical_path)
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
    replay_candidates = _historical_candidate_replay_rows(df, limit, route, window)
    slim_columns = [
        "entry_date",
        "policy_exit_date",
        "decision_date",
        "confirm_datetime",
        "entry_ts",
        "exit_date",
        "exit_ts",
        "exit_datetime",
        "sell_datetime",
        "policy_exit_datetime",
        "code",
        "name",
        "route",
        "route_strategy",
        "route_strategy_label",
        "trade_strategy",
        "trade_strategy_label",
        "entry_mode",
        "review_contract",
        "evidence_level",
        "historical_status",
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
        "hard_stop_pct",
        "take_profit_1",
        "take_profit_1_pct",
        "take_profit_1_sell_ratio",
        "exit_contract",
        "source_type",
        "tdx_topic_names",
        "sector_families",
        "tdx_topic_count",
        "historical_sector_for_distinct",
        "sector_family",
        "replay_contract",
        "replay_price_granularity",
        "replay_source",
        "trade_key",
        "candidate_key",
    ]
    available = [col for col in slim_columns if col in df.columns]
    page_df = df.head(limit)[available] if available else df.head(limit)
    page_df = page_df.astype(object).where(pd.notna(page_df), None)
    return {
        "rows": json.loads(page_df.to_json(orient="records", force_ascii=False)),
        "historical_replay_candidates": replay_candidates["rows"],
        "historical_replay_summary": replay_candidates["summary"],
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
            "closed_trades": _path_status(historical_path),
            "integrated_historical_trades": _path_status(HISTORICAL_TRADES_WITH_OPEN_PATH),
            "base_closed_trades": _path_status(HISTORICAL_TRADES_BASE_PATH),
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
            **replay_candidates["artifacts"],
        },
        "natural_policy_shadow": natural_policy_shadow,
    }


def _historical_replay_axis_for_trade(row: pd.Series) -> tuple[str, str, str]:
    net_ret = _to_float_or_none(row.get("net_ret"))
    account_ret = _to_float_or_none(row.get("account_ret"))
    exit_reason = _clean_review_text(row.get("exit_reason"))
    route = _clean_review_text(row.get("route") or row.get("mode"))
    market_style = _clean_review_text(row.get("market_style"))
    confirm_rule = _clean_review_text(row.get("confirm_rule"))
    if "hard_stop" in exit_reason or (net_ret is not None and net_ret <= -0.10):
        return (
            "sell_point",
            "loss_or_hard_stop_exit",
            "Replay whether the exit contract cut risk naturally, or whether entry quality/route switch made the stop inevitable.",
        )
    if net_ret is not None and net_ret < 0:
        return (
            "buy_point",
            "negative_closed_trade",
            "Replay the buy point: confirmation timing, market state, score source, and whether the entry was naturally executable.",
        )
    if "panic" in route and market_style not in {"standard_range", "weak_rebound", ""}:
        return (
            "model_switch",
            "panic_route_in_non_panic_context",
            "Replay whether the panic repair route was still the natural model for this market state.",
        )
    if "policy_exit_remaining" in exit_reason and net_ret is not None and net_ret > 0.08:
        return (
            "sell_point",
            "profitable_policy_exit",
            "Replay whether the staged sell logic preserved trend participation without turning into hindsight profit chasing.",
        )
    if route in {"institutional_mainwave", "score120_core", "strong_main"} and "30m" not in confirm_rule.lower():
        return (
            "buy_point",
            "mainwave_confirmation_trace",
            "Replay the mainwave confirmation evidence and score scale consistency before trusting this buy sample.",
        )
    if account_ret is not None and abs(account_ret) >= 0.04:
        return (
            "selection",
            "large_account_impact",
            "Replay whether selected stock, sector exposure, and slot usage were the natural representatives of the route.",
        )
    return (
        "natural_trade_consistency",
        "ordinary_closed_trade_sample",
        "Replay this sample only as supporting evidence; do not optimize rules from one ordinary trade.",
    )


def _build_g3_historical_decision_replay_tasks(
    limit: int = 80,
    route: str | None = "all",
    window: str | None = "all",
) -> dict[str, Any]:
    artifacts = {
        "closed_trades": _path_status(HISTORICAL_TRADES_PATH),
        "strategy_tuning_task_reviews": _path_status(STRATEGY_TUNING_TASK_REVIEWS_PATH),
    }
    if not HISTORICAL_TRADES_PATH.exists():
        return {
            "ok": False,
            "mode": "g3_historical_decision_replay_tasks",
            "error": "missing_historical_closed_trades",
            "tasks": [],
            "summary": {
                "task_count": 0,
                "trade_count": 0,
                "can_execute_trade": False,
                "can_change_strategy_contract": False,
                "profit_only_optimization_allowed": False,
            },
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": artifacts,
        }
    try:
        df = pd.read_csv(HISTORICAL_TRADES_PATH, low_memory=False)
    except pd.errors.EmptyDataError:
        df = pd.DataFrame()
    except Exception:
        logger.exception("Failed to build historical decision replay tasks: %s", HISTORICAL_TRADES_PATH)
        df = pd.DataFrame()
    df = _normalize_latest_g3_closed_trades(df)
    if not df.empty:
        df = _with_route_strategy_fields(df)
        if route and route != "all" and "route" in df.columns:
            df = df[df["route"].astype(str).eq(route)]
        df = df[_historical_window_mask(df, window)]
    if df.empty:
        return {
            "ok": True,
            "mode": "g3_historical_decision_replay_tasks",
            "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "tasks": [],
            "summary": {
                "task_count": 0,
                "trade_count": 0,
                "can_execute_trade": False,
                "can_change_strategy_contract": False,
                "profit_only_optimization_allowed": False,
            },
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": artifacts,
        }

    review_map = _load_strategy_tuning_task_reviews()
    net_ret = pd.to_numeric(df.get("net_ret"), errors="coerce")
    account_ret = pd.to_numeric(df.get("account_ret"), errors="coerce")
    df["_abs_account_ret"] = account_ret.abs().fillna(0)
    df["_loss_rank"] = net_ret.fillna(0).mul(-1)
    df["_priority_score"] = df["_loss_rank"] + df["_abs_account_ret"]
    if "exit_reason" in df.columns:
        df["_priority_score"] += df["exit_reason"].astype(str).str.contains("hard_stop|prev_low_break", case=False, na=False).astype(float) * 0.12
    df = df.sort_values(["_priority_score", "entry_date"], ascending=[False, False], kind="mergesort")

    candidate_items = list(df.iterrows())
    available_axis_counts: dict[str, int] = {}
    for _, row in candidate_items:
        axis, _, _ = _historical_replay_axis_for_trade(row)
        available_axis_counts[axis] = available_axis_counts.get(axis, 0) + 1

    selected_items: list[tuple[Any, pd.Series]] = []
    selected_indices: set[Any] = set()
    for target_axis in ("sell_point", "buy_point", "selection", "model_switch", "natural_trade_consistency"):
        for index, row in candidate_items:
            if index in selected_indices:
                continue
            axis, _, _ = _historical_replay_axis_for_trade(row)
            if axis != target_axis:
                continue
            selected_items.append((index, row))
            selected_indices.add(index)
            break

    max_candidates = max(limit * 3, limit)
    for index, row in candidate_items:
        if len(selected_items) >= max_candidates:
            break
        if index in selected_indices:
            continue
        selected_items.append((index, row))
        selected_indices.add(index)

    rows: list[dict[str, Any]] = []
    axis_counts: dict[str, int] = {}
    for _, row in selected_items:
        axis, problem_type, replay_focus = _historical_replay_axis_for_trade(row)
        if axis == "natural_trade_consistency" and len(rows) >= max(10, limit // 4):
            continue
        code = _clean_review_text(row.get("code"))
        name = _clean_review_text(row.get("name"))
        entry_date = _date_text(row.get("entry_date"))
        route_text = _clean_review_text(row.get("route") or row.get("mode"))
        task_key = f"historical_decision|{axis}|{entry_date}|{code}|{problem_type}"
        review = review_map.get(task_key) if task_key else None
        review = review if isinstance(review, dict) else {}
        trade_ret = _to_float_or_none(row.get("net_ret"))
        acct_ret = _to_float_or_none(row.get("account_ret"))
        severity = "watch"
        if trade_ret is not None and trade_ret <= -0.10:
            severity = "high"
        elif trade_ret is not None and trade_ret < 0:
            severity = "medium"
        elif acct_ret is not None and abs(acct_ret) >= 0.05:
            severity = "medium"
        axis_counts[axis] = axis_counts.get(axis, 0) + 1
        rows.append(
            {
                "priority": len(rows) + 1,
                "task_key": task_key,
                "axis": axis,
                "axis_label": _g3_strategy_tuning_axis_meta(axis).get("axis_label"),
                "task_status": "reviewed" if review.get("review_status") == "reviewed" else ("followup" if review.get("review_status") == "needs_followup" else ("watch" if review.get("review_status") == "watch" else "todo")),
                "severity": severity,
                "problem_type": problem_type,
                "origin": "historical_closed_trades",
                "object": f"{code} {name}".strip(),
                "entry_date": entry_date,
                "exit_date": _date_text(row.get("exit_date") or row.get("policy_exit_date")),
                "route": route_text,
                "market_style": _clean_review_text(row.get("market_style")),
                "exit_reason": _clean_review_text(row.get("exit_reason")),
                "net_ret": trade_ret,
                "account_ret": acct_ret,
                "score": _to_float_or_none(row.get("score")),
                "raw_score": _to_float_or_none(row.get("raw_score")),
                "score_source": _clean_review_text(row.get("score_source")),
                "confirm_rule": _clean_review_text(row.get("confirm_rule")),
                "problem_signal": f"{problem_type}; net_ret={trade_ret if trade_ret is not None else '--'}; exit={_clean_review_text(row.get('exit_reason')) or '--'}",
                "replay_focus": replay_focus,
                "review_method": _g3_tuning_task_review_method(axis),
                "evidence": f"entry={entry_date}, exit={_date_text(row.get('exit_date') or row.get('policy_exit_date'))}, route={route_text}, market={_clean_review_text(row.get('market_style'))}, confirm={_clean_review_text(row.get('confirm_rule'))}",
                "completion_evidence": _g3_tuning_task_completion_evidence(axis),
                "next_action": "Write a historical replay note before changing any strategy contract.",
                "optimization_boundary": _g3_strategy_tuning_axis_meta(axis).get("optimization_boundary"),
                "natural_trade_boundary": "Historical replay is evidence only; do not tune for return if the behavior becomes unnatural or hard to execute.",
                "review_result": _clean_review_text(review.get("review_result")),
                "review_status": _clean_review_text(review.get("review_status")),
                "review_note": _clean_review_text(review.get("review_note")),
                "can_execute_trade": False,
                "can_change_strategy_contract": False,
                "profit_only_optimization_allowed": False,
            }
        )
        if len(rows) >= limit:
            break

    return {
        "ok": True,
        "mode": "g3_historical_decision_replay_tasks",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "summary": {
            "task_count": len(rows),
            "trade_count": int(len(df)),
            "axis_counts": axis_counts,
            "available_axis_counts": available_axis_counts,
            "selection_policy": "axis_coverage_first_then_risk_priority",
            "loss_trade_count": int((net_ret < 0).sum()) if len(net_ret) else 0,
            "hard_stop_count": int(df.get("exit_reason", pd.Series("", index=df.index)).astype(str).str.contains("hard_stop", case=False, na=False).sum()) if not df.empty else 0,
            "route": route or "all",
            "window": window or "all",
            "can_execute_trade": False,
            "can_change_strategy_contract": False,
            "profit_only_optimization_allowed": False,
            "natural_trade_boundary": "Replay historical decisions from evidence first; do not optimize only for historical return.",
        },
        "tasks": rows,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": artifacts,
    }


def _build_g3_historical_decision_replay_audit(
    route: str | None = "all",
    window: str | None = "all",
) -> dict[str, Any]:
    replay = _build_g3_historical_decision_replay_tasks(limit=500, route=route, window=window)
    summary = replay.get("summary") if isinstance(replay.get("summary"), dict) else {}
    tasks = replay.get("tasks") if isinstance(replay.get("tasks"), list) else []
    review_map = _load_strategy_tuning_task_reviews()

    required_axes = [
        axis
        for axis in ("buy_point", "sell_point", "selection", "model_switch", "natural_trade_consistency")
        if _to_int_or_zero((summary.get("available_axis_counts") or {}).get(axis)) > 0
    ]
    if not required_axes:
        required_axes = ["buy_point", "sell_point", "selection", "model_switch"]

    axis_rows: list[dict[str, Any]] = []
    for axis in required_axes:
        axis_tasks = [row for row in tasks if isinstance(row, dict) and row.get("axis") == axis]
        reviewed_count = 0
        followup_count = 0
        watch_count = 0
        todo_count = 0
        issue_count = 0
        for row in axis_tasks:
            key = _clean_review_text(row.get("task_key"))
            review = review_map.get(key) if key else None
            review = review if isinstance(review, dict) else {}
            status = _clean_review_text(review.get("review_status") or row.get("review_status"))
            result = _clean_review_text(review.get("review_result") or row.get("review_result"))
            if status == "reviewed":
                reviewed_count += 1
            elif status == "needs_followup":
                followup_count += 1
            elif status == "watch":
                watch_count += 1
            else:
                todo_count += 1
            if result in {"issue_found", "data_gap", "evidence_pending", "defer_contract_review"}:
                issue_count += 1
        sample = axis_tasks[0] if axis_tasks else {}
        if not axis_tasks:
            axis_status = "missing_axis_sample"
        elif todo_count or followup_count:
            axis_status = "blocked"
        elif watch_count and not reviewed_count:
            axis_status = "watch_only"
        else:
            axis_status = "reviewed_or_watch"
        axis_rows.append(
            {
                "axis": axis,
                "axis_label": _g3_strategy_tuning_axis_meta(axis).get("axis_label"),
                "axis_status": axis_status,
                "task_count": len(axis_tasks),
                "todo_count": todo_count,
                "followup_count": followup_count,
                "watch_count": watch_count,
                "reviewed_count": reviewed_count,
                "issue_count": issue_count,
                "available_count": _to_int_or_zero((summary.get("available_axis_counts") or {}).get(axis)),
                "next_review_object": sample.get("object") if isinstance(sample, dict) else "",
                "next_review_focus": sample.get("replay_focus") if isinstance(sample, dict) else _g3_strategy_tuning_axis_meta(axis).get("recommended_review"),
                "completion_evidence": _g3_tuning_task_completion_evidence(axis),
                "optimization_boundary": _g3_strategy_tuning_axis_meta(axis).get("optimization_boundary"),
            }
        )

    hard_gaps: list[dict[str, Any]] = []
    missing_axes = [row for row in axis_rows if row.get("axis_status") == "missing_axis_sample"]
    unreviewed_axes = [row for row in axis_rows if _to_int_or_zero(row.get("todo_count")) > 0]
    followup_axes = [row for row in axis_rows if _to_int_or_zero(row.get("followup_count")) > 0]
    if missing_axes:
        hard_gaps.append(
            {
                "gap": "historical_axis_sample_missing",
                "count": len(missing_axes),
                "next_action": "rebuild or inspect historical replay samples before using history as launch evidence",
                "trade_impact": "hold_manual_live_review",
            }
        )
    if unreviewed_axes:
        hard_gaps.append(
            {
                "gap": "historical_replay_unreviewed_axes",
                "count": sum(_to_int_or_zero(row.get("todo_count")) for row in unreviewed_axes),
                "next_action": "review historical buy/sell/selection/model-switch tasks axis by axis",
                "trade_impact": "hold_strategy_learning_claim",
            }
        )
    if followup_axes:
        hard_gaps.append(
            {
                "gap": "historical_replay_followup_axes",
                "count": sum(_to_int_or_zero(row.get("followup_count")) for row in followup_axes),
                "next_action": "resolve issue_found/data_gap/evidence_pending historical replay notes",
                "trade_impact": "hold_contract_change_and_live_review",
            }
        )

    watch_gaps: list[dict[str, Any]] = []
    watch_only_axes = [row for row in axis_rows if row.get("axis_status") == "watch_only"]
    if watch_only_axes:
        watch_gaps.append(
            {
                "gap": "historical_replay_watch_only_axes",
                "count": len(watch_only_axes),
                "next_action": "keep these axes under paper/live observation until evidence becomes decisive",
                "trade_impact": "observe_only",
            }
        )

    if hard_gaps:
        audit_status = "blocked"
        audit_label = "historical_replay_not_complete"
    elif watch_gaps:
        audit_status = "observe_only"
        audit_label = "historical_replay_watch_only"
    else:
        audit_status = "ready_for_manual_review"
        audit_label = "historical_replay_closed_for_now"

    reviewed_total = sum(_to_int_or_zero(row.get("reviewed_count")) for row in axis_rows)
    followup_total = sum(_to_int_or_zero(row.get("followup_count")) for row in axis_rows)
    watch_total = sum(_to_int_or_zero(row.get("watch_count")) for row in axis_rows)
    todo_total = sum(_to_int_or_zero(row.get("todo_count")) for row in axis_rows)

    return {
        "ok": bool(replay.get("ok")),
        "mode": "g3_historical_decision_replay_audit",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "audit": {
            "audit_status": audit_status,
            "audit_label": audit_label,
            "can_enter_live_manual_review": False,
            "can_buy": False,
            "primary_gap": hard_gaps[0].get("gap") if hard_gaps else (watch_gaps[0].get("gap") if watch_gaps else ""),
            "next_action": hard_gaps[0].get("next_action") if hard_gaps else (watch_gaps[0].get("next_action") if watch_gaps else "keep historical replay evidence under observation"),
            "natural_trade_boundary": "Historical replay can support learning only after axis evidence is reviewed; it never creates buy signals or profit-only contract changes.",
        },
        "hard_gaps": hard_gaps,
        "watch_gaps": watch_gaps,
        "axis_audit_rows": axis_rows,
        "top_open_tasks": [row for row in tasks if isinstance(row, dict) and row.get("task_status") in {"todo", "followup"}][:12],
        "summary": {
            "task_count": summary.get("task_count"),
            "trade_count": summary.get("trade_count"),
            "required_axis_count": len(required_axes),
            "blocked_axis_count": len([row for row in axis_rows if row.get("axis_status") == "blocked"]),
            "watch_only_axis_count": len(watch_only_axes),
            "todo_count": todo_total,
            "followup_count": followup_total,
            "watch_count": watch_total,
            "reviewed_count": reviewed_total,
            "hard_gap_count": len(hard_gaps),
            "watch_gap_count": len(watch_gaps),
            "axis_counts": summary.get("axis_counts"),
            "available_axis_counts": summary.get("available_axis_counts"),
            "selection_policy": summary.get("selection_policy"),
            "profit_only_optimization_allowed": False,
            "can_change_strategy_contract": False,
            "can_execute_trade": False,
        },
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": replay.get("artifacts") if isinstance(replay.get("artifacts"), dict) else {},
    }


def _archived_router_candidate_replay_rows(limit: int, route: str | None, window: str | None) -> dict[str, Any]:
    frames: list[pd.DataFrame] = []
    selected_frames: list[pd.DataFrame] = []
    archive_sources: list[tuple[str, Path]] = []
    archive_dirs = (
        sorted(
            [path for path in STATE_ROUTER_DAILY_ARCHIVE_DIR.iterdir() if path.is_dir()],
            key=lambda path: path.name,
            reverse=True,
        )
        if STATE_ROUTER_DAILY_ARCHIVE_DIR.exists()
        else []
    )
    archive_sources.extend((path.name, path) for path in archive_dirs)
    latest_summary = _read_json(STATE_ROUTER_REPORT_DIR / "summary.json")
    latest_entry_date = _date_text((latest_summary or {}).get("entry_date"))
    latest_all_path = STATE_ROUTER_REPORT_DIR / "g3_state_router_all_source_candidates.csv"
    archived_dates = {date for date, _ in archive_sources}
    if latest_entry_date and latest_entry_date not in archived_dates and latest_all_path.exists():
        archive_sources.insert(0, (latest_entry_date, STATE_ROUTER_REPORT_DIR))

    for archive_date, archive_dir in archive_sources:
        all_path = archive_dir / "g3_state_router_all_source_candidates.csv"
        selected_path = archive_dir / "g3_state_router_selected_candidates.csv"
        all_df = _read_csv_df(all_path)
        if not all_df.empty:
            all_df = all_df.copy()
            all_df["archive_entry_date"] = archive_date
            all_df["archive_source_dir"] = str(archive_dir)
            frames.append(all_df)
        selected_df = _read_csv_df(selected_path)
        if not selected_df.empty:
            selected_df = selected_df.copy()
            selected_df["archive_entry_date"] = archive_date
            selected_df["archive_source_dir"] = str(archive_dir)
            selected_frames.append(selected_df)

    if not frames:
        return {
            "rows": [],
            "summary": {
                "source": "state_router_daily_archive",
                "candidate_count": 0,
                "bought_count": 0,
                "blocked_count": 0,
                "note": "逐日归档或当前报告目录存在，但尚无全源候选 CSV。",
            },
            "artifacts": {
                "state_router_daily_archive": _path_status(STATE_ROUTER_DAILY_ARCHIVE_DIR),
                "state_router_latest_report": _path_status(STATE_ROUTER_REPORT_DIR),
            },
        }

    candidates = pd.concat(frames, ignore_index=True, sort=False)
    if "entry_date" in candidates.columns:
        candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    else:
        candidates["entry_date"] = candidates.get("archive_entry_date", "")
    if "decision_date" in candidates.columns:
        candidates["decision_date"] = pd.to_datetime(candidates["decision_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "code" in candidates.columns:
        candidates["code"] = candidates["code"].astype(str)
    candidates = _enrich_trade_strategy_records(
        json.loads(candidates.astype(object).where(pd.notna(candidates), None).to_json(orient="records", force_ascii=False))
    )
    candidates = pd.DataFrame(candidates)
    candidates = _with_route_strategy_fields(candidates)
    excluded_by_current_contract = 0
    latest_full_g3_only = str((latest_summary or {}).get("strategy_system_mode") or "").strip() == "full_g3_only"
    if latest_full_g3_only and "route" in candidates.columns:
        disabled_mask = candidates["route"].astype(str).isin(FULL_G3_DISABLED_REPLAY_ROUTES)
        excluded_by_current_contract = int(disabled_mask.sum())
        candidates = candidates[~disabled_mask].copy()

    selected_keys: set[tuple[str, str, str]] = set()
    selected_count_by_date: dict[str, int] = {}
    if selected_frames:
        selected = pd.concat(selected_frames, ignore_index=True, sort=False)
        if "entry_date" in selected.columns:
            selected["entry_date"] = pd.to_datetime(selected["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        else:
            selected["entry_date"] = selected.get("archive_entry_date", "")
        selected["code"] = selected.get("code", pd.Series("", index=selected.index)).astype(str)
        selected["route"] = selected.get("route", pd.Series("", index=selected.index)).astype(str)
        if latest_full_g3_only:
            selected = selected[~selected["route"].isin(FULL_G3_DISABLED_REPLAY_ROUTES)].copy()
        selected_keys = set(zip(selected["entry_date"], selected["code"], selected["route"]))
        selected_count_by_date = selected.groupby("entry_date")["code"].count().to_dict()

    def is_bought(row: pd.Series) -> bool:
        return (
            str(row.get("entry_date") or ""),
            str(row.get("code") or ""),
            str(row.get("route") or ""),
        ) in selected_keys

    def archive_block_reason(row: pd.Series) -> str:
        existing = str(row.get("block_reason") or row.get("block_detail") or "").strip()
        if existing:
            return _candidate_block_reason_with_zh(existing)
        if bool(row.get("bought_in_replay")):
            return "已进入当日正式路由候选/影子买入样本。"
        entry_date = str(row.get("entry_date") or "")
        reasons: list[str] = []
        if not _truthy(row.get("router_eligible")):
            reasons.append("路由准入未通过")
        if row.get("route") == "g2_gap_supplement" and not _truthy(row.get("source_fresh")) and str(row.get("source_quality") or "").startswith("ledger_fallback"):
            reasons.append("G2补位源非当日新鲜重建")
        selected_count = int(selected_count_by_date.get(entry_date, 0) or 0)
        if selected_count >= 2:
            reasons.append("当日2槽已占满")
        elif selected_count == 1:
            reasons.append("当日已有更高优先级票入选")
        return _candidate_block_reason_with_zh("；".join(reasons)) or "未进入当日正式买入样本，需复核路由排序、容量或风控状态。"

    candidates["bought_in_replay"] = candidates.apply(is_bought, axis=1)
    if "qualified_shadow_buy" not in candidates.columns:
        candidates["qualified_shadow_buy"] = candidates["bought_in_replay"]
    candidates["replay_decision"] = candidates["bought_in_replay"].map(lambda value: "bought" if value else "not_bought")
    candidates["block_reason"] = candidates.apply(archive_block_reason, axis=1)
    if "source_type" not in candidates.columns:
        candidates["source_type"] = "state_router_daily_archive"
    else:
        candidates["source_type"] = candidates["source_type"].fillna("state_router_daily_archive")

    if route and route != "all" and "route" in candidates.columns:
        candidates = candidates[candidates["route"].astype(str).eq(route)]
    if not candidates.empty:
        candidates = candidates[_historical_window_mask(candidates, window)]
        score_col = "score" if "score" in candidates.columns else None
        sort_cols = ["entry_date"] + ([score_col] if score_col else [])
        candidates = candidates.sort_values(sort_cols, ascending=[False] * len(sort_cols), kind="mergesort")

    slim_columns = [
        "entry_date",
        "decision_date",
        "code",
        "name",
        "route",
        "route_label",
        "trade_strategy",
        "trade_strategy_label",
        "score",
        "wave_style_score",
        "sector_diffusion_score",
        "index_mom60",
        "m30_confirmed",
        "m30_status",
        "m30_close_above_ma20",
        "confirm_datetime",
        "confirm_rule",
        "reference_close",
        "entry_price",
        "candidate_key",
        "source_type",
        "router_eligible",
        "qualified_shadow_buy",
        "replay_decision",
        "bought_in_replay",
        "shadow_status",
        "block_reason",
        "archive_entry_date",
    ]
    available = [col for col in slim_columns if col in candidates.columns]
    page_df = candidates.head(limit)[available].astype(object).where(pd.notna(candidates.head(limit)[available]), None)
    rows = json.loads(page_df.to_json(orient="records", force_ascii=False)) if not page_df.empty else []
    bought_count = int(candidates["bought_in_replay"].sum()) if "bought_in_replay" in candidates.columns else 0
    return {
        "rows": rows,
        "summary": {
            "source": "state_router_daily_archive",
            "archive_day_count": len(archive_sources),
            "candidate_count": int(len(candidates)),
            "bought_count": bought_count,
            "blocked_count": int(len(candidates) - bought_count),
            "excluded_by_current_contract": excluded_by_current_contract,
            "current_contract_filter": "full_g3_only" if latest_full_g3_only else "",
            "note": "优先使用 state-router 每日全源候选归档；若当天尚未迁入 daily_archive，则读取最新报告快照。未买原因来自当日路由输出或按槽位/准入状态补充。",
        },
        "artifacts": {
            "state_router_daily_archive": _path_status(STATE_ROUTER_DAILY_ARCHIVE_DIR),
            "state_router_latest_report": _path_status(STATE_ROUTER_REPORT_DIR),
        },
    }


def _candidate_block_reason_with_zh(reason: Any) -> str:
    text = str(reason or "").strip()
    if not text:
        return ""
    labels = {
        "panic_wait_intraday_confirm_or_no_intraday_data": "恐慌修复等待盘中确认或缺少分钟确认数据",
        "no_router_eligible_source_candidates": "没有通过路由准入的来源候选",
        "mainwave_index_mom60_over_5pct": "机构主升指数60日动量超过5%，按当前合同阻断",
        "institutional_mainwave_cooldown_active": "机构主升连续亏损动态冷却中",
        "m30_not_confirmed": "30m确认未通过",
        "same_sector_guard": "板块暴露规则阻断",
        "duplicate_code_guard": "同一股票重复候选阻断",
        "g2_gap_supplement_source_not_fresh": "G2补位来源不是当日新鲜数据",
        "source_not_fresh": "来源数据不新鲜",
        "route_not_selected": "当日未被路由选中",
        "account_risk_pause_new_buy": "账户风控暂停新买入",
    }
    if "（" in text:
        return text
    if text in labels:
        return f"{text}（{labels[text]}）"
    for key, label in labels.items():
        if key in text:
            return text.replace(key, f"{key}（{label}）")
    return text


def _historical_candidate_replay_rows(
    historical_df: pd.DataFrame,
    limit: int,
    route: str | None,
    window: str | None,
) -> dict[str, Any]:
    archived = _archived_router_candidate_replay_rows(limit, route, window)
    if archived.get("rows"):
        return archived

    source = _read_csv_df(SCORE120_ENTRY_SOURCE_SIGNALS_PATH)
    if source.empty:
        return {
            "rows": [],
            "summary": {
                "source": "score120_entry_timing_source_signals",
                "candidate_count": 0,
                "bought_count": 0,
                "blocked_count": 0,
                "note": "未找到 Score120 源信号候选归档。",
            },
            "artifacts": {
                "score120_source_signals": _path_status(SCORE120_ENTRY_SOURCE_SIGNALS_PATH),
            },
        }

    candidates = source.copy()
    if "entry_date" in candidates.columns:
        candidates["entry_date"] = pd.to_datetime(candidates["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "trade_date" in candidates.columns and "decision_date" not in candidates.columns:
        candidates["decision_date"] = pd.to_datetime(candidates["trade_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if "code" in candidates.columns:
        candidates["code"] = candidates["code"].astype(str)
    if "stock_name" in candidates.columns and "name" not in candidates.columns:
        candidates["name"] = candidates["stock_name"]

    candidates["route"] = "institutional_mainwave"
    candidates["route_label"] = "机构主升浪"
    candidates["trade_strategy"] = "institutional_mainwave_score88"
    candidates["trade_strategy_label"] = "机构主升Score88"
    candidates["source_type"] = "historical_score120_source_signal"
    candidates["confirm_rule"] = "score>=120 + sector_diffusion>=65 + 30m MA20 + index_mom60<=5%"
    candidates["score"] = pd.to_numeric(candidates.get("wave_style_score"), errors="coerce")
    candidates["score_source"] = "wave_style_score"
    candidates["score_scale"] = "mainwave_0_140"
    candidates["m30_confirmed"] = candidates.get("m30_ok", False)
    candidates["m30_status"] = candidates.get("m30_ok", False).map(lambda value: "ok" if _truthy(value) else "blocked") if "m30_ok" in candidates.columns else "unknown"
    candidates["reference_close"] = pd.to_numeric(candidates.get("signal_close"), errors="coerce")
    candidates["candidate_key"] = candidates.apply(
        lambda row: f"score120_source|{row.get('entry_date') or ''}|{row.get('code') or ''}",
        axis=1,
    )

    selected_keys: set[tuple[str, str]] = set()
    selected_count_by_date: dict[str, int] = {}
    selected_frames: list[pd.DataFrame] = []
    if not historical_df.empty and {"entry_date", "code"}.issubset(historical_df.columns):
        selected_frames.append(historical_df)
    score120_selected = _read_csv_df(SCORE120_CORE_TRADES_PATH)
    if not score120_selected.empty and {"entry_date", "code"}.issubset(score120_selected.columns):
        selected_frames.append(score120_selected)
    if selected_frames:
        selected = pd.concat(selected_frames, ignore_index=True, sort=False)
        selected["entry_date"] = pd.to_datetime(selected["entry_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        selected["code"] = selected["code"].astype(str)
        selected_keys = set(zip(selected["entry_date"], selected["code"]))
        selected_count_by_date = selected.groupby("entry_date")["code"].count().to_dict()

    def gate_block_reason(row: pd.Series) -> str:
        entry_date = str(row.get("entry_date") or "")
        code = str(row.get("code") or "")
        if (entry_date, code) in selected_keys:
            return "已进入正式路由成交样本。"
        diffusion = _to_float_or_none(row.get("sector_diffusion_score"))
        index_mom60 = _to_float_or_none(row.get("index_mom60"))
        m30_ok = _truthy(row.get("m30_ok"))
        m30_ma20 = _to_float_or_none(row.get("m30_close_above_ma20"))
        reasons: list[str] = []
        if diffusion is not None and diffusion < 65:
            reasons.append(f"板块扩散 {diffusion:.1f} < 65")
        if index_mom60 is not None and index_mom60 > 0.05:
            reasons.append(f"指数60日动量 {index_mom60 * 100:.1f}% > 5%")
        if not m30_ok or (m30_ma20 is not None and m30_ma20 < 0):
            reasons.append("30m 未确认站上 MA20")
        if reasons:
            return "；".join(reasons)
        selected_count = int(selected_count_by_date.get(entry_date, 0) or 0)
        if selected_count >= 2:
            return "当日 2 槽已占满，源信号未进入正式路由成交。"
        if selected_count == 1:
            return "当日已有更高优先级票入选，源信号未进入正式路由成交。"
        return "源信号通过基础门槛，但未进入正式路由成交样本；需复核动态冷却、持仓容量或历史路由口径。"

    candidates["bought_in_replay"] = candidates.apply(
        lambda row: (str(row.get("entry_date") or ""), str(row.get("code") or "")) in selected_keys,
        axis=1,
    )
    candidates["router_eligible"] = candidates["bought_in_replay"]
    candidates["qualified_shadow_buy"] = candidates["bought_in_replay"]
    candidates["replay_decision"] = candidates["bought_in_replay"].map(lambda value: "bought" if value else "not_bought")
    candidates["block_reason"] = candidates.apply(lambda row: _candidate_block_reason_with_zh(gate_block_reason(row)), axis=1)
    candidates["shadow_status"] = candidates["replay_decision"].map(
        {"bought": "historical_replay_bought", "not_bought": "historical_replay_not_bought"}
    )

    if route and route != "all":
        candidates = candidates[candidates["route"].astype(str).eq(route)]
    if not candidates.empty:
        candidates = candidates[_historical_window_mask(candidates, window)]
        candidates = candidates.sort_values(["entry_date", "score"], ascending=[False, False], kind="mergesort")

    slim_columns = [
        "entry_date",
        "decision_date",
        "code",
        "name",
        "route",
        "route_label",
        "trade_strategy",
        "trade_strategy_label",
        "score",
        "wave_style_score",
        "sector_diffusion_score",
        "index_mom60",
        "m30_confirmed",
        "m30_status",
        "m30_close_above_ma20",
        "confirm_rule",
        "reference_close",
        "candidate_key",
        "source_type",
        "router_eligible",
        "qualified_shadow_buy",
        "replay_decision",
        "bought_in_replay",
        "shadow_status",
        "block_reason",
        "net_ret",
        "policy_exit_date",
        "hold_days",
    ]
    available = [col for col in slim_columns if col in candidates.columns]
    page_df = candidates.head(limit)[available].astype(object).where(pd.notna(candidates.head(limit)[available]), None)
    rows = json.loads(page_df.to_json(orient="records", force_ascii=False)) if not page_df.empty else []
    bought_count = int(candidates["bought_in_replay"].sum()) if "bought_in_replay" in candidates.columns else 0
    return {
        "rows": rows,
        "summary": {
            "source": "score120_entry_timing_source_signals",
            "candidate_count": int(len(candidates)),
            "bought_count": bought_count,
            "blocked_count": int(len(candidates) - bought_count),
            "note": "当前历史候选复盘先覆盖 Score120 买点审计源信号；该样本与正式路由成交底稿并非完全同源，未进入成交样本的原因按可确认硬门槛与口径差异保守标注。",
        },
        "artifacts": {
            "score120_source_signals": _path_status(SCORE120_ENTRY_SOURCE_SIGNALS_PATH),
        },
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
    if strategy in {"institutional_mainwave_score88", "institutional_score120_mainwave"} or route in {"institutional_mainwave", "score120_core"}:
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
            out.at[idx, "natural_action"] = "skip"
            out.at[idx, "natural_action_label"] = "跳过"
            out.at[idx, "natural_position_pct"] = 0.0
            out.at[idx, "natural_rule_hits"] = "N3"
            out.at[idx, "natural_reason"] = "G2 空档补位已因历史回放负贡献退出实盘交易；仅保留观察和历史归因"
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


def _natural_trade_key_from_record(row: dict[str, Any]) -> str:
    for key in ("trade_key", "candidate_key", "ticket_key"):
        text = str(row.get(key) or "").strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split("|") if part.strip()]
        if len(parts) >= 4 and parts[0].startswith(("g3_", "G3")):
            return "|".join(parts[-3:])
        if len(parts) >= 3:
            return "|".join(parts[-3:])
        return text
    code = str(row.get("code") or row.get("code_raw") or "").strip()
    entry_date = _date_text(row.get("entry_date") or row.get("planned_entry_ts") or row.get("confirm_datetime"))
    route = str(row.get("route") or row.get("mode") or "").strip()
    strategy = str(row.get("trade_strategy") or "").strip()
    if not route and strategy in {"institutional_mainwave_score88", "institutional_score120_mainwave"}:
        route = "institutional_mainwave"
    if route == "score120_core":
        route = "institutional_mainwave"
    if route and entry_date and code:
        return f"{route}|{entry_date}|{code}"
    return ""


def _attach_natural_policy_shadow_to_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not records:
        return []
    df = pd.DataFrame(records)
    if df.empty:
        return records
    if "trade_key" not in df.columns:
        df["trade_key"] = ""
    missing = df["trade_key"].isna() | df["trade_key"].astype(str).eq("")
    if missing.any():
        df.loc[missing, "trade_key"] = [
            _natural_trade_key_from_record(row) for row in df.loc[missing].to_dict("records")
        ]
    df = _attach_natural_policy_shadow_fields(df)
    df = df.astype(object).where(pd.notna(df), None)
    return json.loads(df.to_json(orient="records", force_ascii=False))


def _open_position_code_entries(rows: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or row.get("code_raw") or "").strip()
        if not code:
            continue
        entry_date = _date_text(row.get("entry_date") or row.get("planned_entry_ts") or row.get("confirm_datetime"))
        out.setdefault(code, entry_date)
    return out


def _apply_natural_same_stock_open_guard(
    records: list[dict[str, Any]],
    open_code_entries: dict[str, str],
) -> list[dict[str, Any]]:
    if not records or not open_code_entries:
        return records
    out: list[dict[str, Any]] = []
    for row in records:
        item = dict(row)
        code = str(item.get("code") or item.get("code_raw") or "").strip()
        open_entry = open_code_entries.get(code)
        entry_date = _date_text(item.get("entry_date") or item.get("planned_entry_ts") or item.get("confirm_datetime"))
        if code and open_entry and entry_date and entry_date != open_entry:
            existing_rules = str(item.get("natural_rule_hits") or "").strip()
            rules = [part for part in existing_rules.split(",") if part]
            if "N1" not in rules:
                rules.insert(0, "N1")
            item["natural_action"] = "skip"
            item["natural_action_label"] = "跳过"
            item["natural_position_pct"] = 0.0
            item["natural_rule_hits"] = ",".join(rules)
            item["natural_reason"] = f"同一股票已有未退出仓位（入场日 {open_entry}）；当前候选不应伪装成独立二槽"
        out.append(item)
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


def _read_realtime_readiness_review() -> dict[str, Any]:
    return {
        "summary": _read_json(REALTIME_READINESS_REVIEW_DIR / "summary.json"),
        "readiness_gates": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "readiness_gates.csv"),
        "pretrade_action_checklist": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "pretrade_action_checklist.csv"),
        "formal_launch_checklist": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "formal_launch_checklist.csv"),
        "execution_mode_matrix": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "execution_mode_matrix.csv"),
        "formal_launch_action_queue": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "formal_launch_action_queue.csv"),
        "pretrade_review_evidence": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "pretrade_review_evidence.csv"),
        "paper_watch_followup": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "paper_watch_followup.csv"),
        "candidate_omission_review_ledger": list(_load_candidate_omission_reviews().values()),
        "premarket_execution_playbook": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "premarket_execution_playbook.csv"),
        "first_live_decision_card": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "first_live_decision_card.csv"),
        "live_review_task_queue": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_review_task_queue.csv"),
        "review_coverage_dashboard": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "review_coverage_dashboard.csv"),
        "live_review_evidence_rubric": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_review_evidence_rubric.csv"),
        "live_premarket_command_sheet": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_premarket_command_sheet.csv"),
        "live_admission_snapshot": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_admission_snapshot.csv"),
        "live_blocker_resolution_plan": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_blocker_resolution_plan.csv"),
        "live_blocker_evidence_ledger": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_blocker_evidence_ledger.csv"),
        "live_premarket_action_sequence": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_premarket_action_sequence.csv"),
        "live_premarket_execution_recheck": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_premarket_execution_recheck.csv"),
        "live_premarket_action_attempts": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_premarket_action_attempts.csv"),
        "premarket_action_attempt_ledger": _load_premarket_action_attempts(),
        "live_manual_launch_acceptance": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_manual_launch_acceptance.csv"),
        "live_day1_review_journal": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_day1_review_journal.csv"),
        "daily_live_review_board": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "daily_live_review_board.csv"),
        "strategy_learning_backlog": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "strategy_learning_backlog.csv"),
        "live_hidden_risk_watchlist": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_hidden_risk_watchlist.csv"),
        "live_daily_review_execution_checklist": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_daily_review_execution_checklist.csv"),
        "live_daily_review_action_layers": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "live_daily_review_action_layers.csv"),
        "daily_review_checklist_reviews": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "daily_review_checklist_reviews.csv"),
        "daily_review_checklist_review_ledger": list(_load_daily_review_checklist_reviews().values()),
        "formal_action_reviews": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "formal_action_reviews.csv"),
        "formal_action_review_ledger": list(_load_formal_action_reviews().values()),
        "no_trade_day_review": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "no_trade_day_review.csv"),
        "day1_paper_review_pack": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "day1_paper_review_pack.csv"),
        "day1_after_close_review_queue": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "day1_after_close_review_queue.csv"),
        "next_trade_ticket_review": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "next_trade_ticket_review.csv"),
        "ticket_review_checklist": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "ticket_review_checklist.csv"),
        "holding_exit_review": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "holding_exit_review.csv"),
        "holding_exit_checklist": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "holding_exit_checklist.csv"),
        "holding_refresh_evidence": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "holding_refresh_evidence.csv"),
        "candidate_hidden_risk_review": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "candidate_hidden_risk_review.csv"),
        "candidate_omission_checklist": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "candidate_omission_checklist.csv"),
        "natural_trade_consistency_review": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "natural_trade_consistency_review.csv"),
        "natural_execution_decision_matrix": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "natural_execution_decision_matrix.csv"),
        "hazard_register": _read_csv_records(REALTIME_READINESS_REVIEW_DIR / "hazard_register.csv"),
        "artifacts": {
            "summary": _path_status(REALTIME_READINESS_REVIEW_DIR / "summary.json"),
            "report": _path_status(REALTIME_READINESS_REVIEW_DIR / "REPORT_CN.md"),
            "pretrade_action_checklist_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "PRETRADE_ACTION_CHECKLIST_CN.md"),
            "ticket_review_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "TICKET_REVIEW_BRIEF_CN.md"),
            "holding_exit_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "HOLDING_EXIT_BRIEF_CN.md"),
            "candidate_omission_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "CANDIDATE_OMISSION_BRIEF_CN.md"),
            "readiness_gates": _path_status(REALTIME_READINESS_REVIEW_DIR / "readiness_gates.csv"),
            "pretrade_action_checklist": _path_status(REALTIME_READINESS_REVIEW_DIR / "pretrade_action_checklist.csv"),
            "formal_launch_checklist": _path_status(REALTIME_READINESS_REVIEW_DIR / "formal_launch_checklist.csv"),
            "execution_mode_matrix": _path_status(REALTIME_READINESS_REVIEW_DIR / "execution_mode_matrix.csv"),
            "formal_launch_action_queue": _path_status(REALTIME_READINESS_REVIEW_DIR / "formal_launch_action_queue.csv"),
            "pretrade_review_evidence": _path_status(REALTIME_READINESS_REVIEW_DIR / "pretrade_review_evidence.csv"),
            "paper_watch_followup": _path_status(REALTIME_READINESS_REVIEW_DIR / "paper_watch_followup.csv"),
            "candidate_omission_reviews": _path_status(CANDIDATE_OMISSION_REVIEWS_PATH),
            "premarket_execution_playbook": _path_status(REALTIME_READINESS_REVIEW_DIR / "premarket_execution_playbook.csv"),
            "first_live_decision_card": _path_status(REALTIME_READINESS_REVIEW_DIR / "first_live_decision_card.csv"),
            "first_live_decision_card_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "FIRST_LIVE_DECISION_CARD_CN.md"),
            "live_review_task_queue": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_review_task_queue.csv"),
            "live_review_task_queue_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_REVIEW_TASK_QUEUE_CN.md"),
            "review_coverage_dashboard": _path_status(REALTIME_READINESS_REVIEW_DIR / "review_coverage_dashboard.csv"),
            "review_coverage_dashboard_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "REVIEW_COVERAGE_DASHBOARD_CN.md"),
            "live_review_evidence_rubric": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_review_evidence_rubric.csv"),
            "live_review_evidence_rubric_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_REVIEW_EVIDENCE_RUBRIC_CN.md"),
            "live_premarket_command_sheet": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_premarket_command_sheet.csv"),
            "live_premarket_command_sheet_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_PREMARKET_COMMAND_SHEET_CN.md"),
            "live_admission_snapshot": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_admission_snapshot.csv"),
            "live_admission_snapshot_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_ADMISSION_SNAPSHOT_CN.md"),
            "live_blocker_resolution_plan": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_blocker_resolution_plan.csv"),
            "live_blocker_resolution_plan_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_BLOCKER_RESOLUTION_PLAN_CN.md"),
            "live_blocker_evidence_ledger": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_blocker_evidence_ledger.csv"),
            "live_blocker_evidence_ledger_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_BLOCKER_EVIDENCE_LEDGER_CN.md"),
            "live_premarket_action_sequence": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_premarket_action_sequence.csv"),
            "live_premarket_action_sequence_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_PREMARKET_ACTION_SEQUENCE_CN.md"),
            "live_premarket_execution_recheck": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_premarket_execution_recheck.csv"),
            "live_premarket_execution_recheck_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_PREMARKET_EXECUTION_RECHECK_CN.md"),
            "live_premarket_action_attempts": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_premarket_action_attempts.csv"),
            "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
            "live_manual_launch_acceptance": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_manual_launch_acceptance.csv"),
            "live_manual_launch_acceptance_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_MANUAL_LAUNCH_ACCEPTANCE_CN.md"),
            "live_day1_review_journal": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_day1_review_journal.csv"),
            "live_day1_review_journal_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_DAY1_REVIEW_JOURNAL_CN.md"),
            "daily_live_review_board": _path_status(REALTIME_READINESS_REVIEW_DIR / "daily_live_review_board.csv"),
            "daily_live_review_board_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "DAILY_LIVE_REVIEW_BOARD_CN.md"),
            "strategy_learning_backlog": _path_status(REALTIME_READINESS_REVIEW_DIR / "strategy_learning_backlog.csv"),
            "strategy_learning_backlog_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "STRATEGY_LEARNING_BACKLOG_CN.md"),
            "live_hidden_risk_watchlist": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_hidden_risk_watchlist.csv"),
            "live_hidden_risk_watchlist_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_HIDDEN_RISK_WATCHLIST_CN.md"),
            "live_daily_review_execution_checklist": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_daily_review_execution_checklist.csv"),
            "live_daily_review_execution_checklist_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "LIVE_DAILY_REVIEW_EXECUTION_CHECKLIST_CN.md"),
            "live_daily_review_action_layers": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_daily_review_action_layers.csv"),
            "daily_review_checklist_reviews_report": _path_status(REALTIME_READINESS_REVIEW_DIR / "daily_review_checklist_reviews.csv"),
            "daily_review_checklist_reviews": _path_status(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH),
            "formal_action_reviews_report": _path_status(REALTIME_READINESS_REVIEW_DIR / "formal_action_reviews.csv"),
            "formal_action_reviews": _path_status(FORMAL_ACTION_REVIEWS_PATH),
            "no_trade_day_review": _path_status(REALTIME_READINESS_REVIEW_DIR / "no_trade_day_review.csv"),
            "no_trade_day_review_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "NO_TRADE_DAY_REVIEW_CN.md"),
            "no_trade_day_reviews": _path_status(NO_TRADE_DAY_REVIEWS_PATH),
            "day1_paper_review_pack": _path_status(REALTIME_READINESS_REVIEW_DIR / "day1_paper_review_pack.csv"),
            "day1_paper_review_pack_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "DAY1_PAPER_REVIEW_PACK_CN.md"),
            "day1_after_close_review_queue": _path_status(REALTIME_READINESS_REVIEW_DIR / "day1_after_close_review_queue.csv"),
            "day1_after_close_review_queue_brief": _path_status(REALTIME_READINESS_REVIEW_DIR / "DAY1_AFTER_CLOSE_REVIEW_QUEUE_CN.md"),
            "next_trade_ticket_review": _path_status(REALTIME_READINESS_REVIEW_DIR / "next_trade_ticket_review.csv"),
            "ticket_review_checklist": _path_status(REALTIME_READINESS_REVIEW_DIR / "ticket_review_checklist.csv"),
            "holding_exit_review": _path_status(REALTIME_READINESS_REVIEW_DIR / "holding_exit_review.csv"),
            "holding_exit_checklist": _path_status(REALTIME_READINESS_REVIEW_DIR / "holding_exit_checklist.csv"),
            "holding_refresh_evidence": _path_status(REALTIME_READINESS_REVIEW_DIR / "holding_refresh_evidence.csv"),
            "candidate_hidden_risk_review": _path_status(REALTIME_READINESS_REVIEW_DIR / "candidate_hidden_risk_review.csv"),
            "candidate_omission_checklist": _path_status(REALTIME_READINESS_REVIEW_DIR / "candidate_omission_checklist.csv"),
            "natural_trade_consistency_review": _path_status(REALTIME_READINESS_REVIEW_DIR / "natural_trade_consistency_review.csv"),
            "natural_execution_decision_matrix": _path_status(REALTIME_READINESS_REVIEW_DIR / "natural_execution_decision_matrix.csv"),
            "hazard_register": _path_status(REALTIME_READINESS_REVIEW_DIR / "hazard_register.csv"),
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
    score88_runtime_ledger = [
        item for item in runtime_ledger
        if isinstance(item, dict) and str(item.get("strategy_id") or "") == STRATEGY_ID
    ]
    score88_paper_execution_count = len(_formal_score88_paper_rows(_load_paper_executions()))
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
            "historical_reference_closed_trade_count": closed_count,
            "score88_formal_paper_execution_count": score88_paper_execution_count,
            "score88_formal_shadow_ledger_count": len(score88_runtime_ledger),
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
            "trade_strategy_framework": "4_live_strategies_plus_g2_observation",
            "trade_strategy_count": 4,
            "observation_strategy_count": 1,
            "institutional_mainwave_position_pct": 0.50,
            "panic_repair_position_pct": 0.50,
            "old_g3_route_position_pct": 0.50,
            "max_single_name_position_pct": 0.50,
            "final_profile": FINAL_G3_PROFILE,
            "final_profile_name": FINAL_G3_PROFILE_NAME,
            "legacy_base_profile": FINAL_G3_LEGACY_BASE_PROFILE,
            "g2_gap_supplement_position_pct": 0.0,
            "g2_gap_supplement_role": "retired from live trading; observation and historical attribution only",
            "g2_gap_supplement_live_enabled": False,
            "g2_gap_supplement_retire_reason": G2_GAP_SUPPLEMENT_RETIRE_REASON,
            "same_sector_policy": "allow same sector only when both selected candidates are institutional_mainwave; otherwise skip duplicated sector exposure",
        },
        "trade_strategy_policy": [
            {
                "trade_strategy": "institutional_mainwave_score88",
                "label_cn": "机构主升Score88",
                "source_routes": ["institutional_mainwave"],
                "source_strategies": ["机构主升浪Score120核心"],
                "trading_assumption": "机构主线扩散和 Score120 强确认后的主升延续。",
                "default_position_pct": 0.50,
                "risk_note": "允许主线/板块共振；institutional_mainwave 必须满足 index_mom60<=5%，>5% 只观察不进入影子盘/买入候选。",
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
                "trading_assumption": "已退出实盘交易；仅保留源新鲜度、候选质量和历史归因观察。",
                "default_position_pct": 0.0,
                "live_enabled": False,
                "retire_reason": G2_GAP_SUPPLEMENT_RETIRE_REASON,
                "risk_note": "历史回放显示负贡献且未改善组合回撤；不再进入影子票据、纸面交易或正式买入候选。",
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
                "loss_pct": 0.10,
                "action": "sell_all",
            },
            "take_profit": {
                "type": "m30_take_profit_partial",
                "profit_pct": 0.10,
                "sell_ratio": 0.50,
            },
            "structure_exit": {
                "type": "previous_day_low_break_after_take_profit",
                "confirm_bar": "30m",
                "action": "sell_remaining",
            },
            "cooldown": {
                "policy": "institutional_mainwave_consecutive_loss_dynamic_recovery",
                "trigger": "consecutive_closed_institutional_mainwave_loss_count>=2",
                "min_cooldown_trading_days": 3,
                "release_condition": "index_mom60<=5% and (index_close>=index_ma20 or index_mom20>=0) and a current institutional_mainwave candidate still passes sector diffusion plus 30m confirmation",
                "max_recheck_trading_days": 15,
                "scope": "institutional_mainwave_new_buys",
            },
            "risk_limits": {
                "max_single_trade_account_loss_pct": 0.065,
                "max_mtm_drawdown_pct": 0.18,
                "max_consecutive_realized_loss_pct": 0.20,
                "mtm_drawdown_reduce_risk_pct": 0.15,
                "mtm_drawdown_pause_new_buy_pct": 0.18,
                "institutional_mainwave_cooldown_trigger": "consecutive_closed_institutional_mainwave_loss_count>=2",
            },
            "entry_mode_overrides": {
                "mainwave_continuation": {"hard_stop_pct": 0.10, "take_profit_pct": 0.10},
                "breakout_initiation": {"hard_stop_pct": 0.04, "take_profit_pct": 0.10, "entry_requires": "first_completed_entry_day_30m_close_ge_ma20"},
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
                "router_eligible": "score>=120 && sector_diffusion>=65 && 30m_confirmed && index_mom60<=5% && mainwave_dynamic_cooldown_active=false; >5% observation only, no shadow/buy ticket",
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
    contract["strategy_id"] = STRATEGY_ID
    contract["strategy_name"] = STRATEGY_NAME
    contract["strategy_name_cn"] = STRATEGY_NAME_CN
    contract["profile"] = FINAL_G3_PROFILE
    contract["profile_name"] = FINAL_G3_PROFILE_NAME
    contract["trade_strategy_policy"] = [{
        "trade_strategy": FORMAL_G3_CONTRACT["trade_strategy"],
        "label_cn": FORMAL_G3_CONTRACT["display_name"],
        "source_routes": ["institutional_mainwave"],
        "entry_contract": "wave_style_score>=88 && index_mom60<=5% && same_day_industry_mainwave_count>=2 && first_completed_30m_volume_breakout_prior20_high",
        "position_contract": "two slots; 50% per slot; at most two new buys per day; -10% hard stop; +10% sell half; remaining exits on previous-day low break confirmed by 30m",
        "shadow_only": True,
    }]
    contract["route_priority"] = [
        item for item in contract.get("route_priority", [])
        if str(item.get("route") or "") == "institutional_mainwave"
    ]
    portfolio = contract.setdefault("portfolio_contract", {})
    default_portfolio = default.get("portfolio_contract") or {}
    portfolio["trade_strategy_framework"] = "institutional_mainwave_score88_plus_cash"
    portfolio["trade_strategy_count"] = 1
    portfolio["observation_strategy_count"] = 0
    portfolio["g2_gap_supplement_position_pct"] = 0.0
    portfolio["cash_is_valid_decision"] = True
    portfolio["cash_policy"] = "no qualified institutional_mainwave candidate means no new position"
    if False and not any(str(item.get("trade_strategy")) == "mainwave_breakout_initiation" for item in contract["trade_strategy_policy"]):
        contract["trade_strategy_policy"].append({
            "trade_strategy": "mainwave_breakout_initiation",
            "label_cn": "前高突破启动",
            "source_routes": ["institutional_mainwave"],
            "entry_contract": "前高突破、主线扩散>=65、突破后首根满足条件的已完成30m收盘不低于MA20、index_mom60<=5%。",
            "exit_contract": "-4%硬止损、+10%先减半、余仓以前一日低点30m确认退出。",
            "default_position_pct": 0.50,
            "shadow_only": True,
        })
    contract["canonical_contract"] = formal_g3_score88_contract_metadata()
    return contract


def _wrap_guardrails(payload: dict[str, Any]) -> dict[str, Any]:
    runtime_health = read_snapshot(runtime_path("health", "latest.json"))
    payload["runtime_health"] = runtime_health
    payload["data_freshness_blocked"] = not bool(runtime_health.get("strategy_actionable"))
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
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
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
    available_shares = int(
        _parse_broker_number(
            row.get("available_shares") if row.get("available_shares") is not None else row.get("can_use_volume")
        )
        or 0
    )
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
        "available_shares": available_shares,
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


def _attach_latest_market_prices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows
    codes = sorted({
        str(row.get("code") or row.get("code_raw") or "").strip().upper()
        for row in rows
        if isinstance(row, dict) and str(row.get("code") or row.get("code_raw") or "").strip()
    })
    codes = [code for code in codes if re.fullmatch(r"[0-9A-Z.]+", code)]
    if not codes:
        return rows
    quoted_codes = ", ".join(f"'{code}'" for code in codes)
    try:
        from utils.market_warehouse import clickhouse_query_df

        prices = clickhouse_query_df(
            f"""
            SELECT k.code, k.trade_date, k.close
            FROM kline_daily AS k
            INNER JOIN (
                SELECT code, max(trade_date) AS max_date
                FROM kline_daily
                WHERE code IN ({quoted_codes})
                GROUP BY code
            ) AS latest
                ON k.code = latest.code AND k.trade_date = latest.max_date
            """
        )
    except Exception:
        logger.exception("Failed to attach latest market prices for shadow ledger rows.")
        return rows
    if prices.empty:
        return rows
    price_by_code: dict[str, dict[str, Any]] = {}
    for item in prices.to_dict(orient="records"):
        code = str(item.get("code") or "").strip().upper()
        close = _as_float(item.get("close"), None)
        if not code or close is None or close <= 0:
            continue
        price_by_code[code] = {
            "latest_price": close,
            "latest_price_date": _date_text(item.get("trade_date")),
            "latest_price_source": "clickhouse:kline_daily",
        }
    if not price_by_code:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        code = str(row.get("code") or row.get("code_raw") or "").strip().upper()
        latest = price_by_code.get(code)
        if not latest:
            out.append(row)
            continue
        item = dict(row)
        current_price = _as_float(item.get("current_price"), None)
        latest_price = _as_float(item.get("latest_price"), None)
        last_price = _as_float(item.get("last_price"), None)
        close = _as_float(item.get("close"), None)
        if current_price is None or current_price <= 0:
            item["current_price"] = latest["latest_price"]
        if latest_price is None or latest_price <= 0:
            item["latest_price"] = latest["latest_price"]
        if last_price is None or last_price <= 0:
            item["last_price"] = latest["latest_price"]
        if close is None or close <= 0:
            item["close"] = latest["latest_price"]
        item["latest_price_date"] = latest["latest_price_date"]
        item["latest_price_source"] = latest["latest_price_source"]
        out.append(item)
    return out


def _attach_exit_advice(
    rows: list[dict[str, Any]],
    *,
    source: str,
    updated_at: Any = None,
    broker_trades: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if source == "shadow_ledger":
        rows = _attach_latest_market_prices(rows)
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


def _load_daily_trend_bars(codes: list[str], lookback_days: int) -> dict[str, list[dict[str, Any]]]:
    clean_codes = sorted({str(code or "").strip().upper() for code in codes if str(code or "").strip()})
    clean_codes = [code for code in clean_codes if re.fullmatch(r"[0-9A-Z.]+", code)]
    if not clean_codes:
        return {}
    quoted_codes = ", ".join(f"'{code}'" for code in clean_codes)
    try:
        from utils.market_warehouse import clickhouse_query_df

        frame = clickhouse_query_df(
            f"""
            SELECT code, trade_date, low, close
            FROM kline_daily FINAL
            WHERE code IN ({quoted_codes})
              AND trade_date >= today() - INTERVAL {int(lookback_days)} DAY
            ORDER BY code, trade_date
            """
        )
    except Exception:
        logger.exception("Failed to load daily bars for G3 daily trend exit monitor.")
        return {}
    rows_by_code: dict[str, list[dict[str, Any]]] = {}
    for item in frame.to_dict(orient="records") if not frame.empty else []:
        code = str(item.get("code") or "").strip().upper()
        if code:
            rows_by_code.setdefault(code, []).append(item)
    return rows_by_code


def _daily_trend_monitor_positions() -> list[dict[str, Any]]:
    state = _load_broker_state()
    broker_rows = state.get("holdings") if isinstance(state.get("holdings"), list) else []
    ledger_rows = _filter_open_shadow_ledger_records(_read_shadow_ledger_records(STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv", limit=None))
    rows: list[dict[str, Any]] = []
    # Broker holdings take priority.  A matching shadow ticket is supporting
    # evidence, not a second position that should receive a duplicate alert.
    seen: set[str] = set()
    for source, source_rows in (("broker_real_position", broker_rows), ("shadow_ledger", ledger_rows)):
        for item in source_rows:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or item.get("code_raw") or "").strip().upper()
            if not code:
                continue
            if code in seen:
                continue
            seen.add(code)
            rows.append({
                "code": code,
                "name": item.get("name") or item.get("stock_name") or "",
                "position_source": source,
                "ticket_key": item.get("ticket_key") or item.get("candidate_key") or item.get("trade_key"),
                "shares": item.get("shares") or item.get("quantity") or item.get("available_shares"),
                "entry_price": item.get("entry_price") or item.get("cost_price") or item.get("execution_price"),
            })
    return rows


def _send_daily_trend_exit_email(signals: list[dict[str, Any]]) -> dict[str, Any]:
    lines = ["以下持仓已在日线收盘有效跌破上升趋势线，下一交易日执行卖出计划；本通知不发送真实委托。", ""]
    for item in signals:
        lines.append(
            f"- {item.get('code')} {item.get('name') or ''}: 收盘 {_fmt_num(item.get('daily_close'))}，"
            f"趋势线 {_fmt_num(item.get('trend_line_price'))}，"
            f"跌破幅度 {_fmt_num((_to_float(item.get('distance_to_trend_pct')) or 0.0) * 100, 2, '%')}，"
            f"锚点 {((item.get('anchor_low_1') or {}).get('trade_date') or '--')} / {((item.get('anchor_low_2') or {}).get('trade_date') or '--')}"
        )
    return _send_shadow_email(
        f"G3 日线趋势跌破卖出计划 {datetime.now().strftime('%Y-%m-%d')}",
        "\n".join(lines),
    )


def _run_daily_trend_exit_monitor_once(source: str = "manual") -> dict[str, Any]:
    started_at = datetime.now()
    state = _load_daily_trend_exit_monitor_state()
    positions = _daily_trend_monitor_positions()
    bars_by_code = _load_daily_trend_bars([item["code"] for item in positions], state["lookback_days"])
    tick_source: dict[str, Any] = {}
    ticks: dict[str, Any] = {}
    if positions:
        try:
            ticks = _read_qmt_ticks([item["code"] for item in positions], tick_source)
        except Exception as exc:
            logger.warning("G3 intraday trend monitor tick unavailable: {}", exc)
    rows: list[dict[str, Any]] = []
    for position in positions:
        raw_tick = ticks.get(position["code"], {}) if isinstance(ticks, dict) else {}
        last_price = _parse_broker_number(raw_tick.get("lastPrice")) if isinstance(raw_tick, dict) else None
        analysis = evaluate_intraday_rising_trend_line(
            bars_by_code.get(position["code"], []),
            last_price,
            pivot_window=state["pivot_window"],
            min_pivot_separation=state["min_pivot_separation"],
            break_buffer_pct=state["break_buffer_pct"],
        )
        rows.append({**position, **analysis})

    triggered = [item for item in rows if bool(item.get("intraday_trend_broken"))]
    known = set(state.get("alerted_signal_keys") or [])
    new_signals = [
        item for item in triggered
        if f"{item.get('position_source')}|{item.get('code')}|{item.get('signal_date')}" not in known
    ]
    email_result: dict[str, Any] | None = None
    if new_signals and state.get("email_enabled"):
        try:
            email_result = _send_daily_trend_exit_email(new_signals)
        except Exception as exc:
            logger.exception("G3 daily trend exit email failed.")
            email_result = {"sent": False, "error": str(exc)}
    if new_signals:
        known.update(f"{item.get('position_source')}|{item.get('code')}|{item.get('signal_date')}" for item in new_signals)
    result = {
        "ok": True,
        "mode": "g3_daily_trend_exit_monitor",
        "contract": "intraday_daily_rising_trend_line_exit_v1",
        "source": source,
        "checked_at": started_at.isoformat(sep=" ", timespec="seconds"),
        "position_count": len(positions),
        "triggered_count": len(triggered),
        "new_triggered_count": len(new_signals),
        "signals": rows,
        "next_trade_action": "sell_or_switch_review_for_triggered_positions" if triggered else "hold_or_wait_for_qualified_replacement",
        "formal_order_status": "manual_or_paper_only",
        "email": email_result,
    }
    state["alerted_signal_keys"] = list(known)[-500:]
    state["last_run_at"] = result["checked_at"]
    state["last_success_at"] = result["checked_at"]
    state["last_error"] = None
    state["last_result"] = result
    _save_daily_trend_exit_monitor_state(state)
    for item in new_signals:
        _append_monitor_event({
            "type": "daily_trend_exit",
            "status": "sell_next_open",
            "source": source,
            "code": item.get("code"),
            "signal_date": item.get("signal_date"),
            "message": "Daily close broke the confirmed rising trend line; sell is planned for next open.",
        })
    return result


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


def _broker_holdings_sync_preflight() -> dict[str, Any]:
    gateway_url = str(os.environ.get("AISTOCK_TDX_GATEWAY_URL") or "").strip().rstrip("/")
    source_preference = _preferred_broker_sync_source()
    gateway_required = source_preference in {"tdx", "ths", "tdx_gateway", "gateway"}
    qmtmini_configured = source_preference in {"qmtmini", "qmt", "qmtmini_first"}
    sync_state = _load_broker_sync_state()
    broker = _broker_snapshot()
    updated_at = broker.get("updated_at")
    staleness = _staleness_minutes(updated_at)
    holdings = broker.get("holdings") if isinstance(broker.get("holdings"), list) else []
    capital = broker.get("capital") if isinstance(broker.get("capital"), dict) else {}
    review = _read_or_run_realtime_readiness_review(refresh=False)
    command = _current_premarket_action_card(review)
    current_action_group = _clean_review_text(command.get("action_group"))
    is_current_action = current_action_group == "sync_broker_holding_price"
    needs_confirmation = bool(command.get("requires_confirmation") or current_action_group == "sync_broker_holding_price")
    blockers: list[str] = []
    warnings: list[str] = []
    if gateway_required and not gateway_url:
        blockers.append("missing_gateway_url")
    if current_action_group and not is_current_action:
        blockers.append("current_action_not_broker_sync")
    if not current_action_group:
        warnings.append("no_current_premarket_action")
    if staleness is None:
        warnings.append("no_cached_broker_holding_snapshot")
    elif staleness > 30:
        warnings.append("cached_broker_holding_snapshot_stale")
    if bool(capital.get("holdings_stale")):
        warnings.append("capital_holdings_value_mismatch")

    sync_source_ready = qmtmini_configured or bool(gateway_url)
    can_prompt_manual_sync = sync_source_ready and (is_current_action or not current_action_group)
    return {
        "ok": True,
        "mode": "g3_state_alpha_broker_holdings_sync_preflight",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "preflight_status": "ready_for_manual_confirmation" if can_prompt_manual_sync and not blockers else "blocked_or_watch",
        "can_prompt_manual_sync": can_prompt_manual_sync,
        "requires_confirmation": needs_confirmation,
        "broker_sync_source": source_preference,
        "qmtmini_configured": qmtmini_configured,
        "gateway_configured": bool(gateway_url),
        "gateway_required": gateway_required,
        "gateway_url_hint": gateway_url[:24] + "..." if len(gateway_url) > 24 else gateway_url,
        "current_action_group": current_action_group,
        "current_action_label": command.get("action_label") or command.get("next_action"),
        "current_action_matches_sync": is_current_action,
        "holdings_count": len(holdings),
        "broker_updated_at": updated_at,
        "broker_staleness_minutes": round(staleness, 2) if staleness is not None else None,
        "capital": {
            "total_capital": capital.get("total_capital"),
            "available_cash": capital.get("available_cash"),
            "market_value": capital.get("market_value"),
            "holdings_stale": bool(capital.get("holdings_stale")),
        },
        "sync_config": {
            "enabled": bool(sync_state.get("enabled")),
            "sync_holdings": bool(sync_state.get("sync_holdings")),
            "sync_trades": bool(sync_state.get("sync_trades")),
            "scheduled_time": f"{int(sync_state.get('hour') or 17):02d}:{int(sync_state.get('minute') or 30):02d}",
            "last_success_at": sync_state.get("last_success_at"),
            "last_holdings_success_at": sync_state.get("last_holdings_success_at"),
            "last_error": sync_state.get("last_error"),
        },
        "blockers": blockers,
        "warnings": warnings,
        "next_manual_action": (
            "确认后执行同步真实持仓/价格并复审；同步失败则保持禁止真实买入并记录卡点。"
            if can_prompt_manual_sync
            else "先处理当前盘前动作或补齐网关配置，再进入真实持仓同步。"
        ),
        "execution_boundary": "预检只读取本地配置、缓存和当前盘前动作；不读取同花顺、不写 attempts、不写快照、不打开下单。",
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "broker_state": _path_status(BROKER_STATE_PATH),
            "broker_sync_state": _path_status(BROKER_SYNC_STATE_PATH),
        },
    }


def _broker_holdings_sync_confirmation_packet() -> dict[str, Any]:
    preflight = _broker_holdings_sync_preflight()
    review = _read_or_run_realtime_readiness_review(refresh=False)
    command = _current_premarket_action_card(review)
    current_action = command.get("current_action") if isinstance(command.get("current_action"), dict) else {}
    action_group = _clean_review_text(command.get("action_group"))
    action_label = _clean_review_text(command.get("action_label") or command.get("next_action"))
    blockers = preflight.get("blockers") if isinstance(preflight.get("blockers"), list) else []
    warnings = preflight.get("warnings") if isinstance(preflight.get("warnings"), list) else []
    safe_to_prompt = bool(preflight.get("can_prompt_manual_sync")) and not blockers and action_group == "sync_broker_holding_price"
    action_fingerprint = {
        "entry_date": _clean_review_text(command.get("entry_date")),
        "action_group": action_group,
        "action_label": action_label,
        "primary_blocking_key": _clean_review_text(current_action.get("primary_blocking_key")),
        "object": _clean_review_text(current_action.get("object")),
        "completion_check": _clean_review_text(current_action.get("completion_check")),
    }
    confirmation_items = [
        "current action is sync_broker_holding_price",
        "broker sync source is ready",
        "broker holding cache is stale or needs refresh",
        "sync failure keeps live buy locked",
        "after sync, rerun readiness review before any manual live review",
    ]
    return {
        "ok": True,
        "mode": "g3_broker_holdings_sync_confirmation_packet",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "packet_status": "ready_for_manual_confirmation" if safe_to_prompt else "not_ready_for_confirmation",
        "safe_to_prompt": safe_to_prompt,
        "requires_confirmation": True,
        "action_fingerprint": action_fingerprint,
        "confirmation_items": confirmation_items,
        "expected_request": {
            "endpoint": "POST /api/gen3-state-alpha/premarket-control/execute-next",
            "body": {
                "action_group": action_group,
                "confirm": True,
                "source": "g3_manual_broker_sync_confirmation",
            },
        },
        "direct_sync_request": {
            "endpoint": "POST /api/gen3-state-alpha/broker/holdings/sync-ths-and-review",
            "body": {
                "confirm": True,
                "source": "g3_manual_broker_sync_confirmation",
            },
        },
        "preflight": preflight,
        "blockers": blockers,
        "warnings": warnings,
        "expected_effect": _clean_review_text(
            current_action.get("expected_effect"),
            "broker holdings and prices refreshed, then readiness review rerun",
        ),
        "completion_check": _clean_review_text(
            current_action.get("completion_check"),
            "related holding/price blockers disappear after readiness review",
        ),
        "stop_if_fail": _clean_review_text(
            current_action.get("stop_if_fail"),
            "keep live buy locked and record the sync failure evidence",
        ),
        "natural_trade_boundary": _clean_review_text(
            current_action.get("natural_trade_boundary"),
            "clear execution evidence first; do not change strategy rules from stale account data",
        ),
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "broker_state": _path_status(BROKER_STATE_PATH),
            "broker_sync_state": _path_status(BROKER_SYNC_STATE_PATH),
            "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
        },
    }


def _broker_holdings_sync_outcome_verifier() -> dict[str, Any]:
    attempts = _load_premarket_action_attempts()
    broker = _broker_snapshot()
    preflight = _broker_holdings_sync_preflight()
    blocker_board = _build_live_blocker_evidence_board(refresh=False)
    readiness_audit = _build_live_launch_readiness_audit(refresh=False)
    sync_attempts = [
        row
        for row in attempts
        if isinstance(row, dict) and _clean_review_text(row.get("action_group")) == "sync_broker_holding_price"
    ]
    latest_attempt = sync_attempts[-1] if sync_attempts else {}
    latest_attempt = latest_attempt if isinstance(latest_attempt, dict) else {}
    board_summary = blocker_board.get("summary") if isinstance(blocker_board.get("summary"), dict) else {}
    audit_summary = readiness_audit.get("summary") if isinstance(readiness_audit.get("summary"), dict) else {}
    audit_payload = readiness_audit.get("audit") if isinstance(readiness_audit.get("audit"), dict) else {}
    staleness = _staleness_minutes(broker.get("updated_at"))
    broker_fresh = staleness is not None and staleness <= 30
    blocking_count = _to_int_or_zero(audit_summary.get("blocking_command_count"))
    pending_evidence_count = _to_int_or_zero(audit_summary.get("pending_evidence_count"))

    if not latest_attempt:
        outcome_status = "pending_manual_sync"
        outcome_label = "waiting_for_first_sync_attempt"
        next_action = "confirm and run broker holding/price sync, then rerun readiness review"
    elif not bool(latest_attempt.get("ok")):
        outcome_status = "sync_attempt_failed"
        outcome_label = "sync_or_review_failed_keep_blocked"
        next_action = latest_attempt.get("sync_message") or "fix sync failure, then retry manual sync"
    elif blocking_count > 0 or pending_evidence_count > 0 or not broker_fresh:
        outcome_status = "synced_but_still_blocked"
        outcome_label = "sync_completed_but_evidence_not_clear"
        next_action = board_summary.get("next_action") or audit_payload.get("next_action") or "continue blocker evidence review"
    else:
        outcome_status = "ready_for_manual_review"
        outcome_label = "sync_cleared_hard_gaps_manual_review_only"
        next_action = "enter manual live review; auto order remains locked"

    return {
        "ok": True,
        "mode": "g3_broker_holdings_sync_outcome_verifier",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "outcome": {
            "outcome_status": outcome_status,
            "outcome_label": outcome_label,
            "can_enter_live_manual_review": outcome_status == "ready_for_manual_review",
            "can_buy": False,
            "next_action": next_action,
            "natural_trade_boundary": "verify account facts before changing buy point, sell point, selection, or model switch rules",
        },
        "latest_sync_attempt": latest_attempt,
        "sync_attempt_count": len(sync_attempts),
        "broker": {
            "updated_at": broker.get("updated_at"),
            "staleness_minutes": round(staleness, 2) if staleness is not None else None,
            "is_fresh": broker_fresh,
            "holdings_count": len(broker.get("holdings") if isinstance(broker.get("holdings"), list) else []),
        },
        "preflight": {
            "preflight_status": preflight.get("preflight_status"),
            "warnings": preflight.get("warnings"),
            "blockers": preflight.get("blockers"),
        },
        "blocker_summary": board_summary,
        "audit_summary": audit_summary,
        "remaining_blockers": blocker_board.get("rows") if isinstance(blocker_board.get("rows"), list) else [],
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "broker_state": _path_status(BROKER_STATE_PATH),
            "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
            "live_blocker_evidence_ledger": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_blocker_evidence_ledger.csv"),
        },
    }


def _build_broker_post_sync_acceptance_packet(refresh: bool = False) -> dict[str, Any]:
    if refresh:
        _read_or_run_realtime_readiness_review(refresh=True)
    outcome = _broker_holdings_sync_outcome_verifier()
    outcome_payload = outcome.get("outcome") if isinstance(outcome.get("outcome"), dict) else {}
    broker = outcome.get("broker") if isinstance(outcome.get("broker"), dict) else {}
    latest_attempt = outcome.get("latest_sync_attempt") if isinstance(outcome.get("latest_sync_attempt"), dict) else {}
    completion = _build_g3_strategy_tuning_current_step_completion_packet(refresh=False)
    completion_packet = completion.get("packet") if isinstance(completion.get("packet"), dict) else {}
    current_step = completion_packet.get("current_step") if isinstance(completion_packet.get("current_step"), dict) else {}
    completion_gaps = completion_packet.get("gaps") if isinstance(completion_packet.get("gaps"), list) else []
    readiness_audit = _build_live_launch_readiness_audit(refresh=False)
    readiness_payload = readiness_audit.get("audit") if isinstance(readiness_audit.get("audit"), dict) else {}
    readiness_summary = readiness_audit.get("summary") if isinstance(readiness_audit.get("summary"), dict) else {}

    broker_fresh = bool(broker.get("is_fresh"))
    sync_attempt_count = _to_int_or_zero(outcome.get("sync_attempt_count"))
    latest_attempt_ok = bool(latest_attempt.get("ok")) if latest_attempt else False
    readiness_blocked = _clean_review_text(readiness_payload.get("audit_status")) == "blocked"

    acceptance_gaps: list[dict[str, Any]] = []
    if sync_attempt_count <= 0:
        acceptance_gaps.append(
            {
                "gap": "no_confirmed_sync_attempt",
                "next_action": "run the manual broker holding/price sync confirmation first",
                "trade_impact": "hold_manual_live_review",
            }
        )
    elif not latest_attempt_ok:
        acceptance_gaps.append(
            {
                "gap": "latest_sync_attempt_failed",
                "next_action": latest_attempt.get("sync_message") or latest_attempt.get("error") or "fix sync failure and retry",
                "trade_impact": "hold_manual_live_review",
            }
        )
    if not broker_fresh:
        acceptance_gaps.append(
            {
                "gap": "broker_snapshot_still_stale",
                "next_action": "refresh real holdings/prices until broker staleness is within 30 minutes",
                "trade_impact": "hold_manual_live_review",
            }
        )
    if readiness_blocked:
        acceptance_gaps.append(
            {
                "gap": "readiness_audit_still_blocked",
                "next_action": readiness_payload.get("next_action") or "rerun readiness audit and clear hard blockers",
                "trade_impact": "hold_manual_live_review",
            }
        )
    for gap in completion_gaps:
        if isinstance(gap, dict):
            acceptance_gaps.append(
                {
                    "gap": _clean_review_text(gap.get("gap"), "current_step_gap"),
                    "next_action": gap.get("next_action") or "complete current replay step evidence",
                    "trade_impact": gap.get("trade_impact") or "hold_replay_progress",
                }
            )

    can_record_execution_evidence = (
        bool(completion_packet.get("can_mark_step_done"))
        and sync_attempt_count > 0
        and latest_attempt_ok
        and broker_fresh
        and not readiness_blocked
    )
    if can_record_execution_evidence:
        acceptance_status = "ready_to_record_execution_evidence"
        next_action = "record current execution evidence as validated, then rerun tuning completion audit"
    elif sync_attempt_count <= 0:
        acceptance_status = "pending_manual_sync"
        next_action = "confirm and run manual broker holding/price sync"
    elif not latest_attempt_ok:
        acceptance_status = "sync_failed"
        next_action = latest_attempt.get("sync_message") or "fix latest sync failure and retry"
    else:
        acceptance_status = "post_sync_still_blocked"
        next_action = acceptance_gaps[0].get("next_action") if acceptance_gaps else "continue readiness review"

    suggested_review_payload = {}
    if can_record_execution_evidence and current_step:
        suggested_review_payload = {
            "task_key": current_step.get("task_key"),
            "axis": current_step.get("axis"),
            "axis_label": current_step.get("axis_label"),
            "origin": current_step.get("origin"),
            "object": current_step.get("object"),
            "problem_signal": current_step.get("problem_signal"),
            "source_status": current_step.get("source_status"),
            "review_result": "validated",
            "evidence": current_step.get("evidence"),
            "completion_evidence": current_step.get("evidence_required"),
            "review_note": "post-sync acceptance: broker snapshot fresh, confirmed sync attempt exists, readiness blockers cleared",
            "optimization_suggestion": current_step.get("suggested_learning"),
            "next_action": "rerun tuning completion audit after recording this evidence",
            "optimization_boundary": "execution evidence only; no buy signal, no auto order, no strategy contract change",
            "can_execute_trade": False,
            "can_change_strategy_contract": False,
            "profit_only_optimization_allowed": False,
        }

    return {
        "ok": True,
        "mode": "g3_broker_post_sync_acceptance_packet",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "acceptance": {
            "acceptance_status": acceptance_status,
            "can_record_execution_evidence": can_record_execution_evidence,
            "manual_live_review_allowed": False,
            "can_buy": False,
            "next_action": next_action,
            "natural_trade_boundary": "Post-sync acceptance only validates execution facts; it does not optimize for profit, change the contract, or unlock order paths.",
        },
        "gaps": acceptance_gaps,
        "suggested_review_payload": suggested_review_payload,
        "outcome": outcome_payload,
        "broker": broker,
        "latest_sync_attempt": latest_attempt,
        "sync_attempt_count": sync_attempt_count,
        "current_step_completion": completion_packet,
        "readiness_audit": {
            "audit_status": readiness_payload.get("audit_status"),
            "primary_blocker": readiness_payload.get("primary_blocker"),
            "next_action": readiness_payload.get("next_action"),
        },
        "readiness_summary": readiness_summary,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "broker_state": _path_status(BROKER_STATE_PATH),
            "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
            "strategy_tuning_task_reviews": _path_status(STRATEGY_TUNING_TASK_REVIEWS_PATH),
        },
    }


def _record_broker_post_sync_execution_evidence(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    refresh = _truthy(payload.get("refresh"))
    acceptance_packet = _build_broker_post_sync_acceptance_packet(refresh=refresh)
    acceptance = acceptance_packet.get("acceptance") if isinstance(acceptance_packet.get("acceptance"), dict) else {}
    suggested = acceptance_packet.get("suggested_review_payload") if isinstance(acceptance_packet.get("suggested_review_payload"), dict) else {}
    gaps = acceptance_packet.get("gaps") if isinstance(acceptance_packet.get("gaps"), list) else []
    if not bool(acceptance.get("can_record_execution_evidence")) or not suggested.get("task_key"):
        return {
            "ok": False,
            "mode": "g3_broker_post_sync_execution_evidence_record",
            "error": "post_sync_acceptance_not_ready",
            "acceptance": acceptance,
            "gaps": gaps,
            "review": None,
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
        }

    review_payload = {
        **suggested,
        "review_result": "validated",
        "review_note": _clean_review_text(
            payload.get("review_note") or suggested.get("review_note"),
            "post-sync acceptance: execution evidence validated",
        ),
        "source": _clean_review_text(payload.get("source"), "g3_post_sync_acceptance"),
        "can_execute_trade": False,
        "can_change_strategy_contract": False,
        "profit_only_optimization_allowed": False,
    }
    result = _save_strategy_tuning_task_review(review_payload)
    queue = _build_g3_strategy_tuning_review_queue(refresh=False) if result.get("ok") else {}
    completion_audit = _build_g3_strategy_tuning_completion_audit(refresh=False) if result.get("ok") else {}
    return {
        "ok": bool(result.get("ok")),
        "mode": "g3_broker_post_sync_execution_evidence_record",
        "error": result.get("error"),
        "acceptance": acceptance,
        "gaps": gaps,
        "review": result.get("review"),
        "key": result.get("key"),
        "queue_summary": queue.get("summary") if isinstance(queue.get("summary"), dict) else {},
        "completion_summary": completion_audit.get("summary") if isinstance(completion_audit.get("summary"), dict) else {},
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "strategy_tuning_task_reviews": _path_status(STRATEGY_TUNING_TASK_REVIEWS_PATH),
        },
    }


def _build_g3_live_action_console(refresh: bool = False) -> dict[str, Any]:
    review = _read_or_run_realtime_readiness_review(refresh=refresh)
    command = _current_premarket_action_card(review)
    preflight = _broker_holdings_sync_preflight()
    confirmation = _broker_holdings_sync_confirmation_packet()
    outcome = _broker_holdings_sync_outcome_verifier()
    outcome_payload = outcome.get("outcome") if isinstance(outcome.get("outcome"), dict) else {}
    acceptance_packet = _build_broker_post_sync_acceptance_packet(refresh=False)
    acceptance = acceptance_packet.get("acceptance") if isinstance(acceptance_packet.get("acceptance"), dict) else {}
    completion = _build_g3_strategy_tuning_current_step_completion_packet(refresh=False)
    completion_packet = completion.get("packet") if isinstance(completion.get("packet"), dict) else {}
    tuning_audit = _build_g3_strategy_tuning_completion_audit(refresh=False)
    tuning_audit_payload = tuning_audit.get("audit") if isinstance(tuning_audit.get("audit"), dict) else {}
    readiness_audit = _build_live_launch_readiness_audit(refresh=False)
    readiness_payload = readiness_audit.get("audit") if isinstance(readiness_audit.get("audit"), dict) else {}

    def _step_status(ready: bool, done: bool, blocked: bool = False) -> str:
        if done:
            return "done"
        if blocked:
            return "blocked"
        if ready:
            return "ready"
        return "locked"

    sync_done = _clean_review_text(outcome_payload.get("outcome_status")) == "ready_for_manual_review"
    sync_ready = bool(confirmation.get("safe_to_prompt"))
    acceptance_ready = bool(acceptance.get("can_record_execution_evidence"))
    completion_done = bool(completion_packet.get("can_mark_step_done"))
    tuning_ready = _clean_review_text(tuning_audit_payload.get("audit_status")) == "ready_for_manual_review"
    live_ready = _clean_review_text(readiness_payload.get("audit_status")) == "manual_review_ready"

    steps = [
        {
            "step": 1,
            "action_key": "sync_broker_holding_price",
            "action_label": "sync broker holding/price",
            "status": _step_status(sync_ready, sync_done, blocked=not sync_ready and bool(preflight.get("blockers"))),
            "ui_action": "click sync and review",
            "api_action": "POST /api/gen3-state-alpha/premarket-control/execute-next",
            "required_before": "safe_to_prompt=true and action_group=sync_broker_holding_price",
            "done_when": "sync outcome becomes ready_for_manual_review",
            "current_evidence": f"preflight={preflight.get('preflight_status')}, outcome={outcome_payload.get('outcome_status')}",
            "next_action": preflight.get("next_manual_action") or outcome_payload.get("next_action"),
            "can_execute_now": sync_ready and not sync_done,
            "trade_impact": "real buy locked until this clears",
        },
        {
            "step": 2,
            "action_key": "post_sync_acceptance",
            "action_label": "verify post-sync acceptance",
            "status": _step_status(acceptance_ready, acceptance_ready, blocked=not acceptance_ready),
            "ui_action": "refresh post-sync acceptance",
            "api_action": "GET /api/gen3-state-alpha/broker/holdings/post-sync-acceptance",
            "required_before": "confirmed sync attempt and fresh broker snapshot",
            "done_when": "can_record_execution_evidence=true",
            "current_evidence": f"acceptance={acceptance.get('acceptance_status')}, gaps={len(acceptance_packet.get('gaps') if isinstance(acceptance_packet.get('gaps'), list) else [])}",
            "next_action": acceptance.get("next_action"),
            "can_execute_now": False,
            "trade_impact": "observation only; no order path",
        },
        {
            "step": 3,
            "action_key": "record_execution_evidence",
            "action_label": "record execution evidence",
            "status": _step_status(acceptance_ready, completion_done, blocked=not acceptance_ready),
            "ui_action": "record execution evidence",
            "api_action": "POST /api/gen3-state-alpha/broker/holdings/post-sync-acceptance/record-execution-evidence",
            "required_before": "post-sync acceptance ready",
            "done_when": "current step completion can_mark_step_done=true or replay session advances",
            "current_evidence": f"completion={completion_packet.get('completion_status')}",
            "next_action": completion_packet.get("after_done_check"),
            "can_execute_now": acceptance_ready and not completion_done,
            "trade_impact": "writes review evidence only; no strategy contract change",
        },
        {
            "step": 4,
            "action_key": "rerun_tuning_completion",
            "action_label": "rerun tuning completion audit",
            "status": _step_status(completion_done, tuning_ready, blocked=not completion_done),
            "ui_action": "refresh tuning audit",
            "api_action": "GET /api/gen3-state-alpha/strategy-tuning-completion-audit?refresh=true",
            "required_before": "execution evidence recorded",
            "done_when": "tuning completion audit is ready_for_manual_review",
            "current_evidence": f"tuning_audit={tuning_audit_payload.get('audit_status')}",
            "next_action": tuning_audit_payload.get("next_action"),
            "can_execute_now": completion_done and not tuning_ready,
            "trade_impact": "review gate only",
        },
        {
            "step": 5,
            "action_key": "manual_live_review_gate",
            "action_label": "enter manual live review gate",
            "status": _step_status(tuning_ready, live_ready, blocked=not tuning_ready),
            "ui_action": "rerun live readiness audit",
            "api_action": "GET /api/gen3-state-alpha/live-launch-readiness-audit?refresh=true",
            "required_before": "all tuning and readiness hard blockers cleared",
            "done_when": "live readiness audit becomes manual_review_ready",
            "current_evidence": f"readiness={readiness_payload.get('audit_status')}",
            "next_action": readiness_payload.get("next_action"),
            "can_execute_now": tuning_ready and not live_ready,
            "trade_impact": "manual review only; auto order remains locked",
        },
    ]
    current_step = next((row for row in steps if row.get("status") in {"ready", "blocked", "locked"}), steps[-1])
    blocking_steps = [row for row in steps if row.get("status") in {"blocked", "locked"}]
    ready_steps = [row for row in steps if row.get("status") == "ready"]
    done_steps = [row for row in steps if row.get("status") == "done"]
    return {
        "ok": True,
        "mode": "g3_live_action_console",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "summary": {
            "console_status": "manual_review_ready" if live_ready else "blocked_or_in_progress",
            "current_action_key": current_step.get("action_key"),
            "current_action_label": current_step.get("action_label"),
            "current_action_status": current_step.get("status"),
            "ready_step_count": len(ready_steps),
            "done_step_count": len(done_steps),
            "blocking_step_count": len(blocking_steps),
            "live_buy_allowed": False,
            "manual_live_review_allowed": live_ready,
            "auto_order_allowed": False,
            "natural_trade_boundary": "Follow factual execution evidence first; do not tune buy/sell/selection/model switching from stale account data.",
        },
        "steps": steps,
        "current_step": current_step,
        "premarket_command": command,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
            "strategy_tuning_task_reviews": _path_status(STRATEGY_TUNING_TASK_REVIEWS_PATH),
        },
    }


def _save_g3_live_action_console_step_review(payload: dict[str, Any]) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    action_key = _clean_review_text(source.get("action_key"))
    if not action_key:
        return {"ok": False, "error": "missing action_key"}
    result = _clean_review_text(source.get("review_result") or source.get("result"), "issue_found")
    allowed_results = {"issue_found", "continue_watch", "data_gap", "evidence_pending", "defer_contract_review"}
    if result not in allowed_results:
        return {"ok": False, "error": f"unsupported review_result: {result}", "allowed_results": sorted(allowed_results)}

    console = _build_g3_live_action_console(refresh=False)
    steps = console.get("steps") if isinstance(console.get("steps"), list) else []
    step = next((row for row in steps if isinstance(row, dict) and _clean_review_text(row.get("action_key")) == action_key), None)
    if not step:
        return {"ok": False, "error": f"unknown action_key: {action_key}", "allowed_action_keys": [row.get("action_key") for row in steps if isinstance(row, dict)]}

    current_evidence = _clean_review_text(step.get("current_evidence"))
    status = _clean_review_text(step.get("status"))
    review_payload = {
        "task_key": f"live_action_console|{action_key}",
        "axis": "execution_evidence",
        "axis_label": "execution evidence",
        "origin": "live_action_console",
        "object": _clean_review_text(step.get("action_label")) or action_key,
        "problem_signal": _clean_review_text(source.get("problem_signal")) or f"{action_key} status={status}",
        "source_status": status,
        "review_result": result,
        "evidence": current_evidence,
        "completion_evidence": f"{current_evidence}; done_when={_clean_review_text(step.get('done_when'))}",
        "review_note": _clean_review_text(source.get("review_note") or source.get("note")) or "recorded from live action console",
        "hidden_risk": _clean_review_text(source.get("hidden_risk")) or _clean_review_text(step.get("trade_impact")),
        "decision": _clean_review_text(source.get("decision")) or "keep live trading locked until factual evidence clears this step",
        "optimization_suggestion": _clean_review_text(source.get("optimization_suggestion")) or "use this blocker as buy/sell/selection/model-switch replay evidence; do not optimize only for return",
        "next_action": _clean_review_text(source.get("next_action")) or _clean_review_text(step.get("next_action")),
        "optimization_boundary": "This review is evidence-only: no buy unlock, no auto order, no strategy contract change, no profit-only optimization.",
    }
    saved = _save_strategy_tuning_task_review(review_payload)
    queue = _build_g3_strategy_tuning_review_queue(refresh=False) if saved.get("ok") else {}
    completion = _build_g3_strategy_tuning_completion_audit(refresh=False) if saved.get("ok") else {}
    return {
        **saved,
        "mode": "g3_live_action_console_step_review",
        "action_key": action_key,
        "step": step,
        "console_summary": console.get("summary") if isinstance(console.get("summary"), dict) else {},
        "queue_summary": queue.get("summary") if isinstance(queue.get("summary"), dict) else {},
        "completion_summary": completion.get("summary") if isinstance(completion.get("summary"), dict) else {},
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "strategy_tuning_task_reviews": _path_status(STRATEGY_TUNING_TASK_REVIEWS_PATH),
        },
    }


def _build_g3_live_replay_cockpit(refresh: bool = False) -> dict[str, Any]:
    console = _build_g3_live_action_console(refresh=refresh)
    queue = _build_g3_strategy_tuning_review_queue(refresh=False)
    session = _build_g3_strategy_tuning_replay_session(refresh=False, limit=12)
    completion = _build_g3_strategy_tuning_current_step_completion_packet(refresh=False)
    acceptance = _build_broker_post_sync_acceptance_packet(refresh=False)

    console_summary = console.get("summary") if isinstance(console.get("summary"), dict) else {}
    queue_summary = queue.get("summary") if isinstance(queue.get("summary"), dict) else {}
    session_payload = session.get("session") if isinstance(session.get("session"), dict) else {}
    session_summary = session.get("summary") if isinstance(session.get("summary"), dict) else {}
    completion_packet = completion.get("packet") if isinstance(completion.get("packet"), dict) else {}
    acceptance_payload = acceptance.get("acceptance") if isinstance(acceptance.get("acceptance"), dict) else {}
    current_action = console.get("current_step") if isinstance(console.get("current_step"), dict) else {}
    current_review = session_payload.get("current_step") if isinstance(session_payload.get("current_step"), dict) else {}
    task_rows = queue.get("task_rows") if isinstance(queue.get("task_rows"), list) else []
    axis_rows = queue.get("axis_task_summary") if isinstance(queue.get("axis_task_summary"), list) else []

    todo_tasks = [row for row in task_rows if isinstance(row, dict) and row.get("task_status") in {"todo", "followup"}]
    watch_tasks = [row for row in task_rows if isinstance(row, dict) and row.get("task_status") == "watch"]
    next_task = todo_tasks[0] if todo_tasks else (watch_tasks[0] if watch_tasks else {})
    next_task = next_task if isinstance(next_task, dict) else {}

    action_items: list[dict[str, Any]] = []
    if current_action:
        action_items.append(
            {
                "priority": 1,
                "kind": "live_action",
                "axis": "execution_evidence",
                "object": current_action.get("action_label") or current_action.get("action_key"),
                "status": current_action.get("status"),
                "next_action": current_action.get("next_action") or current_action.get("ui_action"),
                "evidence": current_action.get("current_evidence"),
                "done_when": current_action.get("done_when"),
                "trade_impact": current_action.get("trade_impact"),
                "can_execute_now": bool(current_action.get("can_execute_now")),
                "review_boundary": "Do not evaluate buy/sell/selection/model switching from stale broker evidence.",
            }
        )
    if next_task:
        action_items.append(
            {
                "priority": 2,
                "kind": "strategy_replay_task",
                "axis": next_task.get("axis"),
                "object": next_task.get("object"),
                "status": next_task.get("task_status"),
                "next_action": next_task.get("next_action"),
                "evidence": next_task.get("completion_evidence"),
                "done_when": "Record a replay review result, then rerun completion audit.",
                "trade_impact": "review only; no order path",
                "can_execute_now": False,
                "review_boundary": next_task.get("optimization_boundary"),
                "task_key": next_task.get("task_key"),
            }
        )

    action_items.append(
        {
            "priority": 3,
            "kind": "completion_gate",
            "axis": "natural_trade_consistency",
            "object": "manual live review gate",
            "status": completion_packet.get("completion_status"),
            "next_action": completion_packet.get("after_done_check"),
            "evidence": f"todo={queue_summary.get('todo_count')}, followup={queue_summary.get('followup_count')}, broker_acceptance={acceptance_payload.get('acceptance_status')}",
            "done_when": "No hard tuning gap remains and post-sync acceptance is ready.",
            "trade_impact": "manual live review only",
            "can_execute_now": False,
            "review_boundary": "Manual live review is allowed only after factual evidence and replay tasks are coherent.",
        }
    )

    axis_progress: list[dict[str, Any]] = []
    for row in axis_rows:
        if not isinstance(row, dict):
            continue
        task_count = _to_int_or_zero(row.get("task_count"))
        reviewed_count = _to_int_or_zero(row.get("reviewed_count"))
        watch_count = _to_int_or_zero(row.get("watch_count"))
        done_like = reviewed_count + watch_count
        progress_pct = round((done_like / task_count) * 100, 1) if task_count else 100.0
        axis_progress.append(
            {
                **row,
                "progress_pct": progress_pct,
                "next_review_object": next((task.get("object") for task in todo_tasks if isinstance(task, dict) and task.get("axis") == row.get("axis")), ""),
                "review_boundary": row.get("optimization_boundary"),
            }
        )

    natural_trade_checks = [
        {
            "check": "broker_evidence_first",
            "status": "blocked" if not bool(acceptance_payload.get("can_record_execution_evidence")) else "ready",
            "evidence": acceptance_payload.get("acceptance_status"),
            "required_behavior": "Fresh real holding/price evidence must come before strategy tuning.",
        },
        {
            "check": "profit_only_disabled",
            "status": "pass" if queue_summary.get("profit_only_optimization_allowed") is False else "issue",
            "evidence": queue_summary.get("profit_only_optimization_allowed"),
            "required_behavior": "Do not tune only for historical return.",
        },
        {
            "check": "contract_change_locked",
            "status": "pass" if queue_summary.get("can_change_strategy_contract") is False else "issue",
            "evidence": queue_summary.get("can_change_strategy_contract"),
            "required_behavior": "Replay can produce evidence, not immediate contract changes.",
        },
        {
            "check": "order_path_locked",
            "status": "pass",
            "evidence": False,
            "required_behavior": "No auto order path while live launch review is incomplete.",
        },
    ]

    return {
        "ok": True,
        "mode": "g3_live_replay_cockpit",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "summary": {
            "cockpit_status": "manual_review_ready" if bool(console_summary.get("manual_live_review_allowed")) else "blocked_or_replay_in_progress",
            "current_action_key": console_summary.get("current_action_key"),
            "current_action_status": console_summary.get("current_action_status"),
            "next_review_axis": next_task.get("axis"),
            "next_review_object": next_task.get("object"),
            "todo_count": queue_summary.get("todo_count"),
            "followup_count": queue_summary.get("followup_count"),
            "watch_count": queue_summary.get("watch_count"),
            "progress_pct": session_summary.get("progress_pct"),
            "broker_acceptance_status": acceptance_payload.get("acceptance_status"),
            "can_execute_trade": False,
            "can_change_strategy_contract": False,
            "profit_only_optimization_allowed": False,
            "natural_trade_boundary": "Evidence -> replay -> manual review. No buy unlock, no order path, no profit-only tuning.",
        },
        "action_items": action_items,
        "axis_progress": axis_progress,
        "natural_trade_checks": natural_trade_checks,
        "current_action": current_action,
        "current_review_task": current_review,
        "completion_packet": completion_packet,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "strategy_tuning_task_reviews": _path_status(STRATEGY_TUNING_TASK_REVIEWS_PATH),
            "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
        },
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


@router.get("/realtime-readiness-review")
async def get_gen3_state_alpha_realtime_readiness_review() -> dict[str, Any]:
    review = _read_realtime_readiness_review()
    return _wrap_guardrails(
        {
            "ok": bool(review.get("summary")),
            "mode": "g3_realtime_readiness_review_v1",
            **review,
        }
    )


def _run_realtime_readiness_review_once() -> dict[str, Any]:
    started = datetime.now()
    proc = subprocess.run(
        [sys.executable, str(REALTIME_READINESS_REVIEW_SCRIPT)],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=180,
    )
    review = _read_realtime_readiness_review()
    return {
        "ok": proc.returncode == 0 and bool(review.get("summary")),
        "mode": "g3_realtime_readiness_review_v1",
        "run": {
            "returncode": proc.returncode,
            "duration_seconds": round((datetime.now() - started).total_seconds(), 1),
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        },
        **review,
    }


def _read_live_launch_packet() -> dict[str, Any]:
    return {
        "packet": _read_json(LIVE_LAUNCH_PACKET_PATH),
        "premarket_step_cards": _read_csv_records(LIVE_LAUNCH_PACKET_DIR / "premarket_step_cards.csv"),
        "review_axis_matrix": _read_csv_records(LIVE_LAUNCH_PACKET_DIR / "review_axis_matrix.csv"),
        "launch_day_playbook": _read_csv_records(LIVE_LAUNCH_PACKET_DIR / "launch_day_playbook.csv"),
        "launch_day_learning_queue": _read_csv_records(LIVE_LAUNCH_PACKET_DIR / "launch_day_learning_queue.csv"),
        "launch_day_playbook_reviews": list(_load_launch_day_playbook_reviews().values()),
        "artifacts": {
            "launch_packet": _path_status(LIVE_LAUNCH_PACKET_PATH),
            "launch_packet_cn": _path_status(LIVE_LAUNCH_PACKET_DIR / "LAUNCH_PACKET_CN.md"),
            "premarket_step_cards": _path_status(LIVE_LAUNCH_PACKET_DIR / "premarket_step_cards.csv"),
            "review_axis_matrix": _path_status(LIVE_LAUNCH_PACKET_DIR / "review_axis_matrix.csv"),
            "launch_day_playbook": _path_status(LIVE_LAUNCH_PACKET_DIR / "launch_day_playbook.csv"),
            "launch_day_learning_queue": _path_status(LIVE_LAUNCH_PACKET_DIR / "launch_day_learning_queue.csv"),
            "launch_day_playbook_reviews": _path_status(LAUNCH_DAY_PLAYBOOK_REVIEWS_PATH),
        },
    }


def _run_live_launch_packet_once() -> dict[str, Any]:
    started = datetime.now()
    proc = subprocess.run(
        [sys.executable, str(LIVE_LAUNCH_PACKET_SCRIPT)],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=180,
    )
    packet = _read_live_launch_packet()
    return {
        "ok": proc.returncode == 0 and bool(packet.get("packet")),
        "mode": "g3_live_launch_packet_v1",
        "run": {
            "returncode": proc.returncode,
            "duration_seconds": round((datetime.now() - started).total_seconds(), 1),
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        },
        **packet,
    }


def _learning_status_rank(status: str, risk_level: str = "") -> int:
    status_text = _clean_review_text(status).lower()
    risk_text = _clean_review_text(risk_level).lower()
    if status_text in {"needs_review", "issue_found", "blocked"} or risk_text in {"high", "block", "danger"}:
        return 0
    if status_text in {"watch_more", "continue_watch", "pending_evidence"} or risk_text in {"medium", "watch", "warn"}:
        return 1
    if status_text in {"data_gap", "process_gap"}:
        return 2
    return 3


def _normalize_live_learning_row(row: dict[str, Any], *, origin: str, priority: int) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    learning_status = _clean_review_text(
        row.get("learning_status")
        or row.get("watch_status")
        or row.get("review_status")
        or row.get("status"),
        "pending_review",
    )
    risk_level = _clean_review_text(row.get("risk_level"))
    if not risk_level:
        risk_level = "high" if _learning_status_rank(learning_status) == 0 else "watch"
    issue_area = _clean_review_text(row.get("issue_area") or row.get("review_axis"), "unknown")
    evidence = _clean_review_text(row.get("evidence") or row.get("evidence_gap") or row.get("review_note"))
    problem_signal = _clean_review_text(row.get("problem_signal") or row.get("risk_signal") or row.get("hidden_risk"))
    suggested_learning = _clean_review_text(
        row.get("suggested_learning")
        or row.get("optimization_direction")
        or row.get("next_review_action")
        or row.get("required_action")
    )
    return {
        "priority": priority,
        "rank_key": _learning_status_rank(learning_status, risk_level),
        "origin": origin,
        "learning_status": learning_status,
        "risk_level": risk_level,
        "issue_area": issue_area,
        "review_scope": _clean_review_text(row.get("review_scope") or row.get("action_type") or origin),
        "object": _clean_review_text(row.get("object") or row.get("name") or row.get("code") or issue_area),
        "code": _clean_review_text(row.get("code")),
        "name": _clean_review_text(row.get("name")),
        "entry_date": _clean_review_text(row.get("entry_date")),
        "problem_signal": problem_signal,
        "evidence": evidence,
        "suggested_learning": suggested_learning,
        "natural_trade_boundary": _clean_review_text(row.get("natural_trade_boundary")),
        "source": _clean_review_text(row.get("source"), origin),
        "review_key": _clean_review_text(row.get("review_key")),
        "ticket_key": _clean_review_text(row.get("ticket_key")),
        "updated_at": _clean_review_text(row.get("updated_at") or row.get("review_updated_at")),
    }


def _build_live_learning_ledger(refresh: bool = False) -> dict[str, Any]:
    review = _read_or_run_realtime_readiness_review(refresh=refresh)
    packet_result = _read_or_run_live_launch_packet(refresh=refresh)
    rows: list[dict[str, Any]] = []
    for origin, source_rows in (
        ("launch_day_learning_queue", packet_result.get("launch_day_learning_queue")),
        ("strategy_learning_backlog", review.get("strategy_learning_backlog")),
        ("live_hidden_risk_watchlist", review.get("live_hidden_risk_watchlist")),
    ):
        if not isinstance(source_rows, list):
            continue
        for row in source_rows:
            rows.append(_normalize_live_learning_row(row, origin=origin, priority=len(rows) + 1))

    rows = sorted(rows, key=lambda item: (item.get("rank_key", 9), item.get("priority", 9999)))
    for index, row in enumerate(rows, start=1):
        row["priority"] = index
        row.pop("rank_key", None)

    counts_by_status = _count_status(rows, "learning_status")
    counts_by_origin = _count_status(rows, "origin")
    counts_by_issue_area = _count_status(rows, "issue_area")
    return {
        "ok": bool(review.get("summary")) or bool(packet_result.get("packet")),
        "mode": "g3_live_learning_ledger",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "summary": {
            "learning_item_count": len(rows),
            "needs_review_count": sum(
                1
                for row in rows
                if _learning_status_rank(row.get("learning_status"), row.get("risk_level")) == 0
            ),
            "watch_more_count": sum(
                1
                for row in rows
                if _learning_status_rank(row.get("learning_status"), row.get("risk_level")) == 1
            ),
            "origin_counts": counts_by_origin,
            "status_counts": counts_by_status,
            "issue_area_counts": counts_by_issue_area,
            "live_admission_status": (review.get("summary") or {}).get("live_admission_status") if isinstance(review.get("summary"), dict) else None,
            "live_admission_buy_allowed": bool((review.get("summary") or {}).get("live_admission_buy_allowed")) if isinstance(review.get("summary"), dict) else False,
        },
        "learning_ledger": rows,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "strategy_learning_backlog": _path_status(REALTIME_READINESS_REVIEW_DIR / "strategy_learning_backlog.csv"),
            "live_hidden_risk_watchlist": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_hidden_risk_watchlist.csv"),
            "launch_day_learning_queue": _path_status(LIVE_LAUNCH_PACKET_DIR / "launch_day_learning_queue.csv"),
        },
    }


def _build_live_launch_decision_card(refresh: bool = False) -> dict[str, Any]:
    review = _read_or_run_realtime_readiness_review(refresh=refresh)
    if refresh:
        _read_or_run_live_launch_packet(refresh=True)
    command = _current_premarket_action_card(review)
    learning = _build_live_learning_ledger(refresh=False)
    learning_rows = learning.get("learning_ledger") if isinstance(learning.get("learning_ledger"), list) else []
    learning_summary = learning.get("summary") if isinstance(learning.get("summary"), dict) else {}
    summary = review.get("summary") if isinstance(review.get("summary"), dict) else {}
    blocking_count = _to_int_or_zero(summary.get("live_admission_blocking_command_count"))
    pending_evidence_count = _to_int_or_zero(summary.get("live_blocker_pending_evidence_count"))
    live_buy_allowed = bool(summary.get("live_admission_buy_allowed"))
    formal_launch_ready = bool(summary.get("formal_launch_ready"))
    manual_launch_ready = bool(summary.get("live_manual_launch_ready") or formal_launch_ready)
    can_enter_manual_review = live_buy_allowed and blocking_count <= 0

    if not live_buy_allowed or blocking_count > 0:
        decision_status = "blocked"
        decision_label = "不放行：先处理盘前阻断"
        tone = "block"
    elif not manual_launch_ready:
        decision_status = "manual_review_ready"
        decision_label = "仅可进入人工复核：等待启动验收闭环"
        tone = "watch"
    else:
        decision_status = "ready_for_manual_live_review"
        decision_label = "可进入人工实盘复核：仍不自动下单"
        tone = "ready"

    manual_acceptance_rows = review.get("live_manual_launch_acceptance")
    if not isinstance(manual_acceptance_rows, list):
        manual_acceptance_rows = []
    required_actions = review.get("live_premarket_action_sequence")
    if not isinstance(required_actions, list):
        required_actions = []
    top_learning_risks = [
        row
        for row in learning_rows
        if _learning_status_rank(row.get("learning_status"), row.get("risk_level")) <= 1
    ][:5]
    current_action = command.get("current_action") if isinstance(command.get("current_action"), dict) else {}
    primary_blocker = (
        command.get("action_label")
        or command.get("next_action")
        or summary.get("live_premarket_next_action")
        or current_action.get("action")
        or ""
    )

    decision = {
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "decision_status": decision_status,
        "decision_label": decision_label,
        "risk_tone": tone,
        "can_enter_live_manual_review": can_enter_manual_review,
        "can_buy": False,
        "live_admission_status": summary.get("live_admission_status"),
        "live_admission_label": summary.get("live_admission_label"),
        "live_admission_buy_allowed": live_buy_allowed,
        "formal_launch_ready": formal_launch_ready,
        "live_manual_launch_ready": manual_launch_ready,
        "primary_blocker": primary_blocker,
        "next_action": summary.get("live_premarket_next_action") or command.get("next_action"),
        "blocking_command_count": blocking_count,
        "pending_evidence_count": pending_evidence_count,
        "live_learning_ledger_count": len(learning_rows),
        "live_learning_needs_review_count": learning_summary.get("needs_review_count"),
        "live_learning_watch_more_count": learning_summary.get("watch_more_count"),
        "natural_trade_boundary": "先清执行证据，再判断交易逻辑；无票日不补票，不按单日收益倒推放宽规则。",
        "execution_boundary": "决策卡只放行人工复核，不生成正式买点，不开启自动下单或订单路由。",
    }
    return {
        "ok": bool(summary),
        "mode": "g3_live_launch_decision_card",
        "generated_at": decision["generated_at"],
        "decision": decision,
        "premarket_command": command,
        "manual_acceptance_rows": manual_acceptance_rows[:8],
        "required_actions": required_actions[:8],
        "top_learning_risks": top_learning_risks,
        "summary": {
            "decision_status_counts": _count_status([decision], "decision_status"),
            "learning_item_count": len(learning_rows),
            "needs_review_count": learning_summary.get("needs_review_count"),
            "watch_more_count": learning_summary.get("watch_more_count"),
            "blocking_command_count": blocking_count,
            "pending_evidence_count": pending_evidence_count,
            "live_admission_status": summary.get("live_admission_status"),
            "live_admission_buy_allowed": live_buy_allowed,
        },
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "realtime_readiness_review": _path_status(REALTIME_READINESS_REVIEW_DIR / "summary.json"),
            "live_launch_packet": _path_status(LIVE_LAUNCH_PACKET_DIR / "launch_packet.json"),
        },
    }


def _live_blocker_axis(resolution_type: str, review_scope: str) -> str:
    resolution_type = _clean_review_text(resolution_type)
    review_scope = _clean_review_text(review_scope)
    if resolution_type in {"broker_holding_price_refresh", "rerun_readiness_audit", "manual_formal_action_review"}:
        return "execution_evidence"
    if resolution_type == "no_trade_context_review":
        return "selection/model_switch"
    if review_scope == "no_trade_day":
        return "natural_trade_consistency"
    return "execution_evidence"


def _live_blocker_trade_impact(resolution_type: str, review_scope: str) -> str:
    axis = _live_blocker_axis(resolution_type, review_scope)
    if axis == "execution_evidence":
        return "先补执行证据，不改买点、卖点、选股或策略切换参数。"
    if axis == "selection/model_switch":
        return "先区分自然空仓、误杀、数据缺口和流程缺口，再决定是否进入策略学习。"
    return "只记录自然交易边界，不因无票日或单日收益压力放宽准入。"


def _build_live_blocker_evidence_board(refresh: bool = False) -> dict[str, Any]:
    review = _read_or_run_realtime_readiness_review(refresh=refresh)
    summary = review.get("summary") if isinstance(review.get("summary"), dict) else {}
    resolution_rows = review.get("live_blocker_resolution_plan")
    evidence_rows = review.get("live_blocker_evidence_ledger")
    sequence_rows = review.get("live_premarket_action_sequence")
    command_rows = review.get("live_premarket_command_sheet")
    attempts = review.get("premarket_action_attempt_ledger")
    resolution_rows = resolution_rows if isinstance(resolution_rows, list) else []
    evidence_rows = evidence_rows if isinstance(evidence_rows, list) else []
    sequence_rows = sequence_rows if isinstance(sequence_rows, list) else []
    command_rows = command_rows if isinstance(command_rows, list) else []
    attempts = attempts if isinstance(attempts, list) else []

    evidence_by_key: dict[str, dict[str, Any]] = {}
    for row in evidence_rows:
        if not isinstance(row, dict):
            continue
        for key_field in ("review_key", "blocking_key"):
            key = _clean_review_text(row.get(key_field))
            if key:
                evidence_by_key[key] = row

    command_by_key: dict[str, dict[str, Any]] = {}
    for row in command_rows:
        if not isinstance(row, dict):
            continue
        for key_field in ("ledger_key", "review_key"):
            key = _clean_review_text(row.get(key_field))
            if key:
                command_by_key[key] = row

    ready_sequence = next(
        (row for row in sequence_rows if isinstance(row, dict) and _truthy(row.get("can_execute_now"))),
        sequence_rows[0] if sequence_rows else {},
    )
    ready_sequence = ready_sequence if isinstance(ready_sequence, dict) else {}
    ready_resolution_types = {
        _clean_review_text(part)
        for part in _clean_review_text(ready_sequence.get("resolution_types")).split("/")
        if _clean_review_text(part)
    }
    if not ready_resolution_types and ready_sequence.get("resolution_types"):
        ready_resolution_types = {_clean_review_text(ready_sequence.get("resolution_types"))}

    board_rows: list[dict[str, Any]] = []
    for index, row in enumerate(resolution_rows, start=1):
        if not isinstance(row, dict):
            continue
        blocking_key = _clean_review_text(row.get("blocking_key") or row.get("review_key"))
        evidence = evidence_by_key.get(blocking_key, {})
        command = command_by_key.get(blocking_key, {})
        resolution_type = _clean_review_text(row.get("resolution_type") or evidence.get("resolution_type"))
        review_scope = _clean_review_text(row.get("review_scope") or evidence.get("review_scope") or command.get("review_scope"))
        evidence_status = _clean_review_text(evidence.get("evidence_status"), "pending_evidence")
        can_execute_now = bool(
            ready_sequence
            and _truthy(ready_sequence.get("can_execute_now"))
            and (
                resolution_type in ready_resolution_types
                or resolution_type == _clean_review_text(ready_sequence.get("resolution_types"))
            )
        )
        axis = _live_blocker_axis(resolution_type, review_scope)
        board_rows.append(
            {
                "priority": _to_int_or_zero(row.get("priority")) or index,
                "blocking_key": blocking_key,
                "review_key": _clean_review_text(evidence.get("review_key") or blocking_key),
                "review_scope": review_scope,
                "object": _clean_review_text(row.get("object") or evidence.get("object") or command.get("object")),
                "entry_date": _clean_review_text(row.get("entry_date") or evidence.get("entry_date") or command.get("entry_date")),
                "resolution_type": resolution_type,
                "action": _clean_review_text(row.get("action") or evidence.get("action") or command.get("action")),
                "evidence_status": evidence_status,
                "review_result_label": _clean_review_text(evidence.get("review_result_label")),
                "review_note": _clean_review_text(evidence.get("review_note")),
                "can_execute_now": can_execute_now,
                "requires_manual_confirmation": bool(_truthy(row.get("requires_manual_confirmation"))),
                "recommended_ui_action": _clean_review_text(row.get("recommended_ui_action") or evidence.get("recommended_ui_action")),
                "recommended_api_action": _clean_review_text(row.get("recommended_api_action")),
                "evidence_required": _clean_review_text(row.get("evidence_required") or evidence.get("evidence_required") or command.get("evidence_required")),
                "completion_check": _clean_review_text(row.get("completion_check") or evidence.get("completion_check") or command.get("post_action_check")),
                "fallback": _clean_review_text(row.get("fallback") or command.get("fallback")),
                "natural_trade_boundary": _clean_review_text(row.get("natural_trade_boundary") or evidence.get("natural_trade_boundary")),
                "learning_axis": axis,
                "trade_impact": _live_blocker_trade_impact(resolution_type, review_scope),
                "current_action_group": _clean_review_text(ready_sequence.get("action_group")),
                "current_action_label": _clean_review_text(ready_sequence.get("action_label")),
            }
        )

    board_rows = sorted(board_rows, key=lambda item: item.get("priority", 9999))
    pending_rows = [row for row in board_rows if row.get("evidence_status") == "pending_evidence"]
    executable_rows = [row for row in board_rows if _truthy(row.get("can_execute_now"))]
    last_attempt = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
    next_row = executable_rows[0] if executable_rows else (pending_rows[0] if pending_rows else (board_rows[0] if board_rows else {}))
    return {
        "ok": bool(summary) or bool(board_rows),
        "mode": "g3_live_blocker_evidence_board",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "summary": {
            "blocker_count": len(board_rows),
            "pending_evidence_count": len(pending_rows),
            "executable_count": len(executable_rows),
            "validated_count": sum(1 for row in board_rows if row.get("evidence_status") == "validated"),
            "issue_found_count": sum(1 for row in board_rows if row.get("evidence_status") == "issue_found"),
            "next_action": next_row.get("recommended_ui_action") or next_row.get("action") if isinstance(next_row, dict) else "",
            "next_resolution_type": next_row.get("resolution_type") if isinstance(next_row, dict) else "",
            "live_admission_status": summary.get("live_admission_status"),
            "live_admission_buy_allowed": bool(summary.get("live_admission_buy_allowed")),
            "last_attempt_at": last_attempt.get("attempted_at"),
            "last_attempt_ok": last_attempt.get("ok"),
        },
        "rows": board_rows,
        "next_blocker": next_row,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "live_blocker_resolution_plan": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_blocker_resolution_plan.csv"),
            "live_blocker_evidence_ledger": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_blocker_evidence_ledger.csv"),
            "live_premarket_action_sequence": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_premarket_action_sequence.csv"),
        },
    }


G3_STRATEGY_TUNING_AXES = [
    {
        "axis": "buy_point",
        "axis_label": "Buy point",
        "recommended_review": "Review signal freshness, confirmation chain, and whether the entry follows the original setup instead of chasing a single-day result.",
        "optimization_boundary": "Do not loosen buy gates only because missed trades later rose.",
    },
    {
        "axis": "sell_point",
        "axis_label": "Sell point",
        "recommended_review": "Review stop, partial profit, previous-low protection, and whether exits match holding evidence.",
        "optimization_boundary": "Do not move exits only to improve historical profit if risk behavior becomes inconsistent.",
    },
    {
        "axis": "selection",
        "axis_label": "Selection",
        "recommended_review": "Review candidate omission, same-theme exposure, route role, and whether the selected stock is the natural representative.",
        "optimization_boundary": "Do not add factors only because they fit one historical window.",
    },
    {
        "axis": "model_switch",
        "axis_label": "Model switch",
        "recommended_review": "Review route switching, no-trade days, G2 gap supplement role, and whether strategy transitions are smooth.",
        "optimization_boundary": "Do not switch models just to chase the best hindsight route.",
    },
    {
        "axis": "execution_evidence",
        "axis_label": "Execution evidence",
        "recommended_review": "Refresh real holdings, prices, blocker evidence, and premarket command state before changing trading logic.",
        "optimization_boundary": "Execution evidence must be cleared before any strategy rule adjustment.",
    },
    {
        "axis": "natural_trade_consistency",
        "axis_label": "Natural trade consistency",
        "recommended_review": "Review whether no-trade, paper-watch, buy, sell, and hold decisions feel coherent and reproducible.",
        "optimization_boundary": "Do not optimize for profit if the resulting behavior is unnatural, hard to execute, or hard to explain.",
    },
]


def _g3_strategy_tuning_axis(*parts: Any) -> str:
    text = " ".join(_clean_review_text(part) for part in parts if part is not None).lower()
    keyword_groups = [
        ("buy_point", ["buy", "entry", "pretrade", "\u4e70\u70b9", "\u4e70\u5165", "\u5165\u573a", "\u9010\u7968"]),
        ("sell_point", ["sell", "exit", "stop", "holding_exit", "risk_exit", "\u5356\u70b9", "\u9000\u51fa", "\u6b62\u635f", "\u6b62\u76c8", "\u6301\u4ed3"]),
        ("selection", ["selection", "candidate", "omission", "stock", "theme", "sector", "\u9009\u80a1", "\u5019\u9009", "\u9057\u6f0f", "\u4e3b\u7ebf", "\u677f\u5757"]),
        ("model_switch", ["model_switch", "switch", "route", "g2", "gap", "no_trade", "\u7b56\u7565\u5207\u6362", "\u8def\u7ebf", "\u8865\u4f4d", "\u65e0\u7968"]),
        ("execution_evidence", ["execution", "broker", "holding", "price", "evidence", "sync", "preflight", "\u6267\u884c", "\u8bc1\u636e", "\u6301\u4ed3", "\u4ef7\u683c", "\u540c\u82b1\u987a"]),
        ("natural_trade_consistency", ["natural", "consistency", "paper_watch", "manual", "\u81ea\u7136", "\u4e00\u81f4", "\u7eb8\u9762", "\u4eba\u5de5"]),
    ]
    for axis, keywords in keyword_groups:
        if any(keyword in text for keyword in keywords):
            return axis
    return "execution_evidence"


def _g3_strategy_tuning_axis_meta(axis: str) -> dict[str, Any]:
    return next((item for item in G3_STRATEGY_TUNING_AXES if item["axis"] == axis), G3_STRATEGY_TUNING_AXES[-1])


def _build_g3_strategy_tuning_axis_board(refresh: bool = False) -> dict[str, Any]:
    learning = _build_live_learning_ledger(refresh=refresh)
    blocker_board = _build_live_blocker_evidence_board(refresh=False)
    decision_result = _build_live_launch_decision_card(refresh=False)
    learning_rows = learning.get("learning_ledger") if isinstance(learning.get("learning_ledger"), list) else []
    blocker_rows = blocker_board.get("rows") if isinstance(blocker_board.get("rows"), list) else []
    learning_summary = learning.get("summary") if isinstance(learning.get("summary"), dict) else {}
    decision = decision_result.get("decision") if isinstance(decision_result.get("decision"), dict) else {}

    risk_rows: list[dict[str, Any]] = []
    for row in learning_rows:
        if not isinstance(row, dict):
            continue
        axis = _g3_strategy_tuning_axis(
            row.get("issue_area"),
            row.get("origin"),
            row.get("object"),
            row.get("problem_signal"),
            row.get("suggested_learning"),
            row.get("natural_trade_boundary"),
        )
        risk_rows.append(
            {
                "priority": _to_int_or_zero(row.get("priority")) or len(risk_rows) + 1,
                "axis": axis,
                "axis_label": _g3_strategy_tuning_axis_meta(axis).get("axis_label"),
                "origin": _clean_review_text(row.get("origin")),
                "learning_status": _clean_review_text(row.get("learning_status") or row.get("risk_level"), "watch_more"),
                "issue_area": _clean_review_text(row.get("issue_area")),
                "object": _clean_review_text(row.get("object")),
                "problem_signal": _clean_review_text(row.get("problem_signal") or row.get("review_note")),
                "evidence": _clean_review_text(row.get("evidence") or row.get("review_evidence") or row.get("evidence_required")),
                "suggested_learning": _clean_review_text(row.get("suggested_learning") or row.get("learning_direction")),
                "natural_trade_boundary": _clean_review_text(row.get("natural_trade_boundary")),
                "can_change_strategy_contract": False,
                "profit_only_optimization_allowed": False,
            }
        )

    for row in blocker_rows:
        if not isinstance(row, dict):
            continue
        axis = _g3_strategy_tuning_axis(
            row.get("learning_axis"),
            row.get("resolution_type"),
            row.get("review_scope"),
            row.get("object"),
            row.get("action"),
            row.get("trade_impact"),
        )
        risk_rows.append(
            {
                "priority": len(risk_rows) + 1,
                "axis": axis,
                "axis_label": _g3_strategy_tuning_axis_meta(axis).get("axis_label"),
                "origin": "live_blocker_evidence_board",
                "learning_status": _clean_review_text(row.get("evidence_status"), "pending_evidence"),
                "issue_area": _clean_review_text(row.get("resolution_type") or row.get("learning_axis")),
                "object": _clean_review_text(row.get("object") or row.get("blocking_key")),
                "problem_signal": _clean_review_text(row.get("action") or row.get("review_note")),
                "evidence": _clean_review_text(row.get("evidence_required") or row.get("completion_check")),
                "suggested_learning": _clean_review_text(row.get("trade_impact")),
                "natural_trade_boundary": _clean_review_text(row.get("natural_trade_boundary") or row.get("fallback")),
                "can_change_strategy_contract": False,
                "profit_only_optimization_allowed": False,
            }
        )

    axis_rows: list[dict[str, Any]] = []
    for meta in G3_STRATEGY_TUNING_AXES:
        axis = meta["axis"]
        rows = [row for row in risk_rows if row.get("axis") == axis]
        needs_review_rows = [
            row
            for row in rows
            if _learning_status_rank(row.get("learning_status"), row.get("learning_status")) == 0
            or row.get("learning_status") in {"pending_evidence", "issue_found"}
        ]
        watch_rows = [
            row
            for row in rows
            if row not in needs_review_rows and row.get("learning_status") in {"watch_more", "continue_watch", "pending_observation"}
        ]
        top_objects = []
        for row in rows:
            obj = _clean_review_text(row.get("object"))
            if obj and obj not in top_objects:
                top_objects.append(obj)
            if len(top_objects) >= 3:
                break
        axis_rows.append(
            {
                "axis": axis,
                "axis_label": meta["axis_label"],
                "risk_count": len(rows),
                "needs_review_count": len(needs_review_rows),
                "watch_count": len(watch_rows),
                "top_objects": " / ".join(top_objects),
                "first_problem_signal": rows[0].get("problem_signal") if rows else "",
                "recommended_review": meta["recommended_review"],
                "optimization_boundary": meta["optimization_boundary"],
                "can_change_strategy_contract": False,
                "profit_only_optimization_allowed": False,
            }
        )

    axis_counts = {row["axis"]: row["risk_count"] for row in axis_rows}
    needs_review_count = sum(row.get("needs_review_count", 0) for row in axis_rows)
    return {
        "ok": bool(learning.get("ok")) or bool(blocker_board.get("ok")),
        "mode": "g3_strategy_tuning_axis_board",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "summary": {
            "risk_item_count": len(risk_rows),
            "axis_counts": axis_counts,
            "needs_review_count": needs_review_count,
            "watch_count": sum(row.get("watch_count", 0) for row in axis_rows),
            "profit_only_optimization_allowed": False,
            "can_change_strategy_contract": False,
            "live_admission_status": learning_summary.get("live_admission_status") or decision.get("live_admission_status"),
            "live_admission_buy_allowed": False,
            "primary_blocker": decision.get("primary_blocker"),
            "next_action": decision.get("next_action"),
        },
        "axis_rows": axis_rows,
        "risk_rows": risk_rows[:200],
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "live_learning_ledger": learning.get("artifacts", {}),
            "live_blocker_evidence_board": blocker_board.get("artifacts", {}),
        },
    }


def _g3_tuning_task_review_method(axis: str) -> str:
    methods = {
        "buy_point": "Replay the entry setup from signal date, 30m confirmation, market state, route role, and missed-trade evidence before changing any buy gate.",
        "sell_point": "Replay current holding price freshness, stop/profit contract, previous-low protection, and whether exit evidence is newer than the last decision.",
        "selection": "Replay candidate pool, omitted candidates, sector/theme exposure, and whether selected stocks are natural representatives of the route.",
        "model_switch": "Replay route decision, no-trade reason, G2 gap supplement role, and whether switching logic is smooth rather than hindsight-driven.",
        "execution_evidence": "Refresh factual evidence first: real holding, price, blocker ledger, and premarket command state. Do not touch strategy rules before evidence is current.",
        "natural_trade_consistency": "Compare buy, sell, hold, no-trade, and paper-watch behavior for coherence, explainability, and repeatability.",
    }
    return methods.get(axis, methods["natural_trade_consistency"])


def _g3_tuning_task_completion_evidence(axis: str) -> str:
    evidence = {
        "buy_point": "A replay note states whether the buy point was valid, late, missing confirmation, or intentionally blocked, with evidence timestamp.",
        "sell_point": "A holding/exit note states whether sell logic stayed consistent after fresh price and position evidence.",
        "selection": "A candidate omission note explains selected/omitted stocks and confirms whether selection logic needs a future contract review.",
        "model_switch": "A route-switch note separates natural no-trade, data gap, execution blocker, and true model-switch weakness.",
        "execution_evidence": "Broker/preflight/readiness evidence is fresh and the related blocker is validated or explicitly marked issue_found.",
        "natural_trade_consistency": "A natural-trade note explains why the decision is coherent without optimizing only for historical profit.",
    }
    return evidence.get(axis, evidence["natural_trade_consistency"])


def _g3_tuning_task_next_action(axis: str, status: str) -> str:
    if axis == "execution_evidence":
        return "Clear factual evidence first, then rerun readiness review."
    if status in {"pending_evidence", "issue_found", "needs_review"}:
        return "Open the source ledger row and write a replay note before changing any strategy contract."
    return "Keep watching until a new live/paper sample updates the evidence."


def _g3_tuning_task_replay_suggestion(row: dict[str, Any]) -> dict[str, Any]:
    axis = _clean_review_text(row.get("axis"), "natural_trade_consistency")
    status = _clean_review_text(row.get("source_status"), "watch_more")
    origin = _clean_review_text(row.get("origin"))
    problem = _clean_review_text(row.get("problem_signal"))
    natural_boundary = _clean_review_text(row.get("natural_trade_boundary"))

    if status in {"pending_evidence", "needs_review", "must_review"}:
        suggested_result = "evidence_pending"
    elif status == "issue_found":
        suggested_result = "issue_found"
    elif status in {"watch_more", "continue_watch", "pending_observation"}:
        suggested_result = "continue_watch"
    else:
        suggested_result = "data_gap"

    if axis == "execution_evidence":
        suggested_result = "evidence_pending"
        replay_focus = "先更新真实持仓、价格、阻断证据和盘前动作状态，再评价买卖点或选股逻辑。"
        hidden_risk = "如果用过期账户/价格证据复盘，系统可能把执行缺口误判成策略缺口。"
    elif axis == "sell_point":
        replay_focus = "复核当前持仓、止损/止盈、前低保护和最新价格证据是否同一时间口径。"
        hidden_risk = "卖点证据不新鲜时，继续优化买点会掩盖真实退出风险。"
    elif axis == "buy_point":
        replay_focus = "从信号日、30m确认、市场状态、路线角色和被拦截原因倒推买点是否自然。"
        hidden_risk = "不能因为后验上涨就放宽买点；只记录是否存在可复现的漏买证据。"
    elif axis == "selection":
        replay_focus = "复核候选池、遗漏候选、板块/主线暴露和入选股票是否是路线的自然代表。"
        hidden_risk = "选股优化如果只追历史强票，会破坏二槽暴露和补位角色一致性。"
    elif axis == "model_switch":
        replay_focus = "区分自然空仓、数据缺口、执行阻断和真实策略切换迟滞。"
        hidden_risk = "不能把无票日或执行阻断误改成策略切换规则。"
    else:
        replay_focus = "检查买、卖、持有、空仓和观察动作是否在同一交易逻辑下自洽。"
        hidden_risk = "如果解释只服务于收益曲线，就不应进入合同变更。"

    evidence_required = _clean_review_text(row.get("completion_evidence"))
    review_note = " / ".join(
        part
        for part in [
            replay_focus,
            f"source={origin}" if origin else "",
            f"problem={problem}" if problem else "",
        ]
        if part
    )
    return {
        "task_key": row.get("task_key"),
        "axis": axis,
        "axis_label": row.get("axis_label"),
        "object": row.get("object"),
        "problem_signal": problem,
        "source_status": status,
        "task_status": row.get("task_status"),
        "suggested_review_result": suggested_result,
        "suggested_review_result_label": _strategy_tuning_task_result_label(suggested_result),
        "suggested_review_note": review_note,
        "suggested_hidden_risk": hidden_risk,
        "suggested_decision": "先完成证据复盘；当前建议不改变策略合同，不生成买点，不进入订单路径。",
        "suggested_optimization": row.get("suggested_learning") or "Only consider a future contract review after repeated, timestamped replay evidence.",
        "replay_focus": replay_focus,
        "evidence_required": evidence_required,
        "next_action": row.get("next_action"),
        "optimization_boundary": row.get("optimization_boundary"),
        "natural_trade_boundary": natural_boundary
        or "不按单日收益倒推放宽规则；只在事实证据闭环后讨论策略合同。",
        "can_apply_as_review": suggested_result in {"continue_watch", "evidence_pending", "data_gap", "issue_found"},
        "can_execute_trade": False,
        "can_change_strategy_contract": False,
        "profit_only_optimization_allowed": False,
    }


def _build_g3_strategy_tuning_replay_suggestions(refresh: bool = False, limit: int = 50) -> dict[str, Any]:
    queue = _build_g3_strategy_tuning_review_queue(refresh=refresh)
    task_rows = queue.get("task_rows") if isinstance(queue.get("task_rows"), list) else []
    summary = queue.get("summary") if isinstance(queue.get("summary"), dict) else {}

    open_rows = [
        row
        for row in task_rows
        if isinstance(row, dict) and row.get("task_status") in {"todo", "followup", "watch"}
    ]
    suggestions = [_g3_tuning_task_replay_suggestion(row) for row in open_rows[: max(1, min(limit, 200))]]
    result_counts: dict[str, int] = {}
    axis_counts: dict[str, int] = {}
    for row in suggestions:
        result = _clean_review_text(row.get("suggested_review_result"), "unknown")
        axis = _clean_review_text(row.get("axis"), "unknown")
        result_counts[result] = result_counts.get(result, 0) + 1
        axis_counts[axis] = axis_counts.get(axis, 0) + 1

    return {
        "ok": bool(queue.get("ok")),
        "mode": "g3_strategy_tuning_replay_suggestions",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "summary": {
            "suggestion_count": len(suggestions),
            "open_task_count": len(open_rows),
            "queue_task_count": summary.get("task_count"),
            "todo_count": summary.get("todo_count"),
            "followup_count": summary.get("followup_count"),
            "watch_count": summary.get("watch_count"),
            "result_counts": result_counts,
            "axis_counts": axis_counts,
            "profit_only_optimization_allowed": False,
            "can_change_strategy_contract": False,
            "can_execute_trade": False,
            "live_admission_status": summary.get("live_admission_status"),
            "live_admission_buy_allowed": False,
        },
        "suggestions": suggestions,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": queue.get("artifacts") if isinstance(queue.get("artifacts"), dict) else {},
    }


def _g3_strategy_tuning_session_stage(axis: str) -> str:
    stages = {
        "execution_evidence": "01_fact_evidence",
        "sell_point": "02_sell_point",
        "buy_point": "03_buy_point",
        "selection": "04_selection",
        "model_switch": "05_model_switch",
        "natural_trade_consistency": "06_natural_consistency",
    }
    return stages.get(axis, "06_natural_consistency")


def _build_g3_strategy_tuning_replay_session(refresh: bool = False, limit: int = 12) -> dict[str, Any]:
    completion = _build_g3_strategy_tuning_completion_audit(refresh=refresh)
    suggestions_payload = _build_g3_strategy_tuning_replay_suggestions(refresh=False, limit=max(limit, 50))
    completion_summary = completion.get("summary") if isinstance(completion.get("summary"), dict) else {}
    completion_audit = completion.get("audit") if isinstance(completion.get("audit"), dict) else {}
    suggestions = suggestions_payload.get("suggestions") if isinstance(suggestions_payload.get("suggestions"), list) else []

    ordered_steps: list[dict[str, Any]] = []
    for index, row in enumerate(suggestions[: max(1, min(limit, 50))], start=1):
        if not isinstance(row, dict):
            continue
        axis = _clean_review_text(row.get("axis"), "natural_trade_consistency")
        ordered_steps.append(
            {
                "step": index,
                "session_stage": _g3_strategy_tuning_session_stage(axis),
                "task_key": row.get("task_key"),
                "axis": axis,
                "axis_label": row.get("axis_label"),
                "task_status": row.get("task_status"),
                "suggested_review_result": row.get("suggested_review_result"),
                "object": row.get("object"),
                "problem_signal": row.get("problem_signal"),
                "replay_focus": row.get("replay_focus"),
                "hidden_risk": row.get("suggested_hidden_risk"),
                "evidence_required": row.get("evidence_required"),
                "next_action": row.get("next_action"),
                "natural_trade_boundary": row.get("natural_trade_boundary"),
                "can_execute_trade": False,
                "can_change_strategy_contract": False,
                "profit_only_optimization_allowed": False,
            }
        )

    current_step = ordered_steps[0] if ordered_steps else {}
    total_count = _to_int_or_zero(completion_summary.get("task_count"))
    todo_count = _to_int_or_zero(completion_summary.get("todo_count"))
    followup_count = _to_int_or_zero(completion_summary.get("followup_count"))
    watch_count = _to_int_or_zero(completion_summary.get("watch_count"))
    reviewed_count = _to_int_or_zero(completion_summary.get("reviewed_count"))
    remaining_count = todo_count + followup_count
    progress_pct = round((reviewed_count / total_count) * 100, 2) if total_count else 0.0

    session_blockers: list[dict[str, Any]] = []
    if todo_count:
        session_blockers.append(
            {
                "blocker": "todo_replay_tasks",
                "count": todo_count,
                "next_action": "process replay session steps from top to bottom",
            }
        )
    if followup_count:
        session_blockers.append(
            {
                "blocker": "followup_replay_tasks",
                "count": followup_count,
                "next_action": "resolve issue/data-gap/evidence-pending reviews before manual live review",
            }
        )
    if watch_count:
        session_blockers.append(
            {
                "blocker": "watch_replay_tasks",
                "count": watch_count,
                "next_action": "keep observing; do not convert watch-only tasks into contract changes",
            }
        )

    session_status = "blocked"
    if not remaining_count and watch_count:
        session_status = "observe_only"
    elif not remaining_count:
        session_status = "ready_for_manual_readiness_review"

    return {
        "ok": bool(completion.get("ok")) and bool(suggestions_payload.get("ok")),
        "mode": "g3_strategy_tuning_replay_session",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "session": {
            "session_status": session_status,
            "audit_status": completion_audit.get("audit_status"),
            "audit_label": completion_audit.get("audit_label"),
            "current_step": current_step,
            "current_step_label": current_step.get("replay_focus") if current_step else "",
            "progress_pct": progress_pct,
            "remaining_count": remaining_count,
            "next_action": current_step.get("next_action") if current_step else completion_audit.get("next_action"),
            "manual_live_review_allowed": False,
            "natural_trade_boundary": "Replay session organizes evidence only; it never creates buy signals, order routes, or profit-only contract changes.",
        },
        "summary": {
            "task_count": total_count,
            "todo_count": todo_count,
            "followup_count": followup_count,
            "watch_count": watch_count,
            "reviewed_count": reviewed_count,
            "remaining_count": remaining_count,
            "progress_pct": progress_pct,
            "session_step_count": len(ordered_steps),
            "profit_only_optimization_allowed": False,
            "can_change_strategy_contract": False,
            "can_execute_trade": False,
            "live_admission_buy_allowed": False,
        },
        "session_steps": ordered_steps,
        "session_blockers": session_blockers,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": suggestions_payload.get("artifacts") if isinstance(suggestions_payload.get("artifacts"), dict) else {},
    }


def _build_g3_strategy_tuning_current_step_completion_packet(refresh: bool = False) -> dict[str, Any]:
    session_result = _build_g3_strategy_tuning_replay_session(refresh=refresh, limit=12)
    session = session_result.get("session") if isinstance(session_result.get("session"), dict) else {}
    current_step = session.get("current_step") if isinstance(session.get("current_step"), dict) else {}
    axis = _clean_review_text(current_step.get("axis"), "natural_trade_consistency")

    required_evidence = [
        _clean_review_text(current_step.get("evidence_required")),
        "Record a replay review result in strategy_tuning_task_reviews.",
        "Rerun completion audit after evidence changes.",
    ]
    current_evidence: dict[str, Any] = {
        "task_key": current_step.get("task_key"),
        "task_status": current_step.get("task_status"),
        "suggested_review_result": current_step.get("suggested_review_result"),
        "replay_focus": current_step.get("replay_focus"),
        "hidden_risk": current_step.get("hidden_risk"),
    }
    gaps: list[dict[str, Any]] = []
    after_done_check = "Rerun /api/gen3-state-alpha/strategy-tuning-replay-session and confirm current_step advances."

    broker_packet: dict[str, Any] = {}
    if axis == "execution_evidence":
        preflight = _broker_holdings_sync_preflight()
        confirmation = _broker_holdings_sync_confirmation_packet()
        outcome = _broker_holdings_sync_outcome_verifier()
        outcome_payload = outcome.get("outcome") if isinstance(outcome.get("outcome"), dict) else {}
        broker_state = outcome.get("broker") if isinstance(outcome.get("broker"), dict) else {}
        broker_packet = {
            "preflight_status": preflight.get("preflight_status"),
            "can_prompt_manual_sync": preflight.get("can_prompt_manual_sync"),
            "requires_confirmation": preflight.get("requires_confirmation"),
            "packet_status": confirmation.get("packet_status"),
            "safe_to_prompt": confirmation.get("safe_to_prompt"),
            "expected_request": confirmation.get("expected_request"),
            "action_fingerprint": confirmation.get("action_fingerprint"),
            "outcome_status": outcome_payload.get("outcome_status"),
            "outcome_label": outcome_payload.get("outcome_label"),
            "sync_attempt_count": outcome.get("sync_attempt_count"),
            "broker": broker_state,
            "warnings": preflight.get("warnings"),
            "blockers": preflight.get("blockers"),
        }
        current_evidence["broker_sync"] = broker_packet
        required_evidence.extend(
            [
                "Broker holding/price cache is fresh after manual confirmation.",
                "Broker sync outcome is ready_for_manual_review or remaining blocker is explicitly recorded.",
                "Readiness audit is rerun after sync.",
            ]
        )
        if not bool(broker_state.get("is_fresh")):
            gaps.append(
                {
                    "gap": "broker_snapshot_not_fresh",
                    "next_action": "manual broker holding/price sync with confirmation, then rerun readiness review",
                    "trade_impact": "keep_live_buy_locked",
                }
            )
        if not bool(confirmation.get("safe_to_prompt")):
            gaps.append(
                {
                    "gap": "broker_sync_confirmation_not_ready",
                    "next_action": "resolve preflight blockers before prompting manual sync",
                    "trade_impact": "keep_live_buy_locked",
                }
            )
        if _to_int_or_zero(outcome.get("sync_attempt_count")) <= 0:
            gaps.append(
                {
                    "gap": "no_sync_attempt_recorded",
                    "next_action": "do not mark execution evidence reviewed until a confirmed sync attempt or explicit failure evidence exists",
                    "trade_impact": "hold_replay_progress",
                }
            )
        after_done_check = "After manual sync, rerun readiness audit and confirm broker staleness <= 30 minutes and related holding/price blockers clear or are recorded as issue_found."
    elif current_step:
        gaps.append(
            {
                "gap": "manual_replay_note_required",
                "next_action": current_step.get("next_action") or "write replay evidence before changing any strategy contract",
                "trade_impact": "hold_contract_change",
            }
        )

    completion_status = "blocked" if gaps else "ready_to_record_review"
    return {
        "ok": bool(session_result.get("ok")),
        "mode": "g3_strategy_tuning_current_step_completion_packet",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "packet": {
            "completion_status": completion_status,
            "current_step": current_step,
            "required_evidence": [item for item in required_evidence if item],
            "current_evidence": current_evidence,
            "gaps": gaps,
            "after_done_check": after_done_check,
            "can_mark_step_done": completion_status == "ready_to_record_review",
            "manual_live_review_allowed": False,
            "natural_trade_boundary": "Completion packet verifies evidence only; it never creates buy signals, order routes, or profit-only contract changes.",
        },
        "session_summary": session_result.get("summary") if isinstance(session_result.get("summary"), dict) else {},
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": session_result.get("artifacts") if isinstance(session_result.get("artifacts"), dict) else {},
    }


def _build_g3_strategy_tuning_review_queue(refresh: bool = False) -> dict[str, Any]:
    board = _build_g3_strategy_tuning_axis_board(refresh=refresh)
    axis_rows = board.get("axis_rows") if isinstance(board.get("axis_rows"), list) else []
    risk_rows = board.get("risk_rows") if isinstance(board.get("risk_rows"), list) else []
    summary = board.get("summary") if isinstance(board.get("summary"), dict) else {}
    review_map = _load_strategy_tuning_task_reviews()

    axis_priority = {
        "execution_evidence": 0,
        "sell_point": 1,
        "buy_point": 2,
        "selection": 3,
        "model_switch": 4,
        "natural_trade_consistency": 5,
    }
    status_priority = {
        "pending_evidence": 0,
        "issue_found": 0,
        "needs_review": 1,
        "must_review": 1,
        "watch_more": 2,
        "continue_watch": 3,
        "pending_observation": 3,
    }

    task_rows: list[dict[str, Any]] = []
    for row in risk_rows:
        if not isinstance(row, dict):
            continue
        axis = _clean_review_text(row.get("axis"), "natural_trade_consistency")
        status = _clean_review_text(row.get("learning_status"), "watch_more")
        task_key = "|".join(
            [
                axis,
                _clean_review_text(row.get("origin"), "unknown_origin"),
                _clean_review_text(row.get("object"), "unknown_object"),
                _clean_review_text(row.get("problem_signal"), "unknown_problem")[:80],
            ]
        )
        review = review_map.get(task_key) if task_key else None
        review = review if isinstance(review, dict) else {}
        base_task_status = "todo" if status_priority.get(status, 4) <= 1 else "watch"
        if review.get("review_status") == "reviewed":
            task_status = "reviewed"
        elif review.get("review_status") == "needs_followup":
            task_status = "followup"
        elif review.get("review_status") == "watch":
            task_status = "watch"
        else:
            task_status = base_task_status
        task_rows.append(
            {
                "priority": 0,
                "task_key": task_key,
                "axis": axis,
                "axis_label": _g3_strategy_tuning_axis_meta(axis).get("axis_label"),
                "task_status": task_status,
                "source_status": status,
                "review_result": _clean_review_text(review.get("review_result")),
                "review_status": _clean_review_text(review.get("review_status")),
                "review_note": _clean_review_text(review.get("review_note")),
                "review_updated_at": _clean_review_text(review.get("updated_at")),
                "origin": _clean_review_text(row.get("origin")),
                "object": _clean_review_text(row.get("object")),
                "problem_signal": _clean_review_text(row.get("problem_signal")),
                "review_method": _g3_tuning_task_review_method(axis),
                "completion_evidence": _g3_tuning_task_completion_evidence(axis),
                "next_action": _g3_tuning_task_next_action(axis, status),
                "optimization_boundary": _g3_strategy_tuning_axis_meta(axis).get("optimization_boundary"),
                "suggested_learning": _clean_review_text(row.get("suggested_learning")),
                "natural_trade_boundary": _clean_review_text(row.get("natural_trade_boundary")),
                "can_execute_trade": False,
                "can_change_strategy_contract": False,
                "profit_only_optimization_allowed": False,
            }
        )

    task_rows = sorted(
        task_rows,
        key=lambda item: (
            status_priority.get(item.get("source_status"), 4),
            axis_priority.get(item.get("axis"), 9),
            item.get("origin") or "",
            item.get("object") or "",
        ),
    )
    for index, row in enumerate(task_rows, start=1):
        row["priority"] = index

    axis_task_summary: list[dict[str, Any]] = []
    for axis_row in axis_rows:
        if not isinstance(axis_row, dict):
            continue
        axis = _clean_review_text(axis_row.get("axis"))
        rows = [row for row in task_rows if row.get("axis") == axis]
        axis_task_summary.append(
            {
                "axis": axis,
                "axis_label": axis_row.get("axis_label"),
                "task_count": len(rows),
                "todo_count": sum(1 for row in rows if row.get("task_status") == "todo"),
                "reviewed_count": sum(1 for row in rows if row.get("task_status") == "reviewed"),
                "followup_count": sum(1 for row in rows if row.get("task_status") == "followup"),
                "watch_count": sum(1 for row in rows if row.get("task_status") == "watch"),
                "first_next_action": rows[0].get("next_action") if rows else "",
                "completion_evidence": _g3_tuning_task_completion_evidence(axis),
                "optimization_boundary": axis_row.get("optimization_boundary"),
            }
        )

    return {
        "ok": bool(board.get("ok")),
        "mode": "g3_strategy_tuning_review_queue",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "summary": {
            "task_count": len(task_rows),
            "todo_count": sum(1 for row in task_rows if row.get("task_status") == "todo"),
            "reviewed_count": sum(1 for row in task_rows if row.get("task_status") == "reviewed"),
            "followup_count": sum(1 for row in task_rows if row.get("task_status") == "followup"),
            "watch_count": sum(1 for row in task_rows if row.get("task_status") == "watch"),
            "axis_count": len(axis_task_summary),
            "review_count": len(review_map),
            "profit_only_optimization_allowed": False,
            "can_change_strategy_contract": False,
            "can_execute_trade": False,
            "live_admission_status": summary.get("live_admission_status"),
            "live_admission_buy_allowed": False,
            "primary_blocker": summary.get("primary_blocker"),
            "next_action": summary.get("next_action"),
        },
        "axis_task_summary": axis_task_summary,
        "task_rows": task_rows[:200],
        "review_map": review_map,
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            **(board.get("artifacts") if isinstance(board.get("artifacts"), dict) else {}),
            "strategy_tuning_task_reviews": _path_status(STRATEGY_TUNING_TASK_REVIEWS_PATH),
        },
    }


def _build_g3_strategy_tuning_completion_audit(refresh: bool = False) -> dict[str, Any]:
    queue = _build_g3_strategy_tuning_review_queue(refresh=refresh)
    summary = queue.get("summary") if isinstance(queue.get("summary"), dict) else {}
    task_rows = queue.get("task_rows") if isinstance(queue.get("task_rows"), list) else []
    axis_rows = queue.get("axis_task_summary") if isinstance(queue.get("axis_task_summary"), list) else []

    todo_rows = [row for row in task_rows if isinstance(row, dict) and row.get("task_status") == "todo"]
    followup_rows = [row for row in task_rows if isinstance(row, dict) and row.get("task_status") == "followup"]
    watch_rows = [row for row in task_rows if isinstance(row, dict) and row.get("task_status") == "watch"]
    reviewed_rows = [row for row in task_rows if isinstance(row, dict) and row.get("task_status") == "reviewed"]

    hard_gaps: list[dict[str, Any]] = []
    if todo_rows:
        hard_gaps.append(
            {
                "gap": "tuning_tasks_unreviewed",
                "count": len(todo_rows),
                "next_action": "review every todo tuning task before manual live review",
                "trade_impact": "hold_manual_live_review",
            }
        )
    if followup_rows:
        hard_gaps.append(
            {
                "gap": "tuning_tasks_need_followup",
                "count": len(followup_rows),
                "next_action": "resolve issue_found/data_gap/evidence_pending tuning reviews",
                "trade_impact": "hold_contract_change_and_live_review",
            }
        )

    watch_gaps: list[dict[str, Any]] = []
    if watch_rows:
        watch_gaps.append(
            {
                "gap": "tuning_tasks_watch",
                "count": len(watch_rows),
                "next_action": "keep paper/live observation until evidence changes",
                "trade_impact": "observe_only",
            }
        )

    axis_completion_rows: list[dict[str, Any]] = []
    for row in axis_rows:
        if not isinstance(row, dict):
            continue
        todo_count = _to_int_or_zero(row.get("todo_count"))
        followup_count = _to_int_or_zero(row.get("followup_count"))
        reviewed_count = _to_int_or_zero(row.get("reviewed_count"))
        watch_count = _to_int_or_zero(row.get("watch_count"))
        task_count = _to_int_or_zero(row.get("task_count"))
        if todo_count or followup_count:
            axis_status = "blocked"
        elif watch_count and not reviewed_count:
            axis_status = "watch_only"
        elif task_count:
            axis_status = "reviewed_or_watch"
        else:
            axis_status = "no_current_task"
        axis_completion_rows.append(
            {
                "axis": row.get("axis"),
                "axis_label": row.get("axis_label"),
                "axis_status": axis_status,
                "task_count": task_count,
                "todo_count": todo_count,
                "followup_count": followup_count,
                "reviewed_count": reviewed_count,
                "watch_count": watch_count,
                "completion_evidence": row.get("completion_evidence"),
                "optimization_boundary": row.get("optimization_boundary"),
            }
        )

    if hard_gaps:
        audit_status = "blocked"
        audit_label = "tuning_review_not_complete"
        can_enter_live_manual_review = False
    elif watch_gaps:
        audit_status = "observe_only"
        audit_label = "tuning_review_watch_only"
        can_enter_live_manual_review = False
    else:
        audit_status = "ready_for_manual_review"
        audit_label = "tuning_review_closed_for_now"
        can_enter_live_manual_review = False

    return {
        "ok": bool(queue.get("ok")),
        "mode": "g3_strategy_tuning_completion_audit",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "audit": {
            "audit_status": audit_status,
            "audit_label": audit_label,
            "can_enter_live_manual_review": can_enter_live_manual_review,
            "can_buy": False,
            "primary_gap": hard_gaps[0].get("gap") if hard_gaps else (watch_gaps[0].get("gap") if watch_gaps else ""),
            "next_action": hard_gaps[0].get("next_action") if hard_gaps else (watch_gaps[0].get("next_action") if watch_gaps else "keep the tuning ledger under observation"),
            "natural_trade_boundary": "Tuning completion can only support manual review; it never creates buy signals, order routes, or profit-only contract changes.",
        },
        "hard_gaps": hard_gaps,
        "watch_gaps": watch_gaps,
        "axis_completion_rows": axis_completion_rows,
        "top_open_tasks": [*todo_rows[:8], *followup_rows[:8]][:8],
        "summary": {
            "task_count": summary.get("task_count"),
            "todo_count": len(todo_rows),
            "followup_count": len(followup_rows),
            "watch_count": len(watch_rows),
            "reviewed_count": len(reviewed_rows),
            "hard_gap_count": len(hard_gaps),
            "watch_gap_count": len(watch_gaps),
            "review_count": summary.get("review_count"),
            "profit_only_optimization_allowed": False,
            "can_change_strategy_contract": False,
            "can_execute_trade": False,
            "live_admission_status": summary.get("live_admission_status"),
            "live_admission_buy_allowed": False,
        },
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": queue.get("artifacts") if isinstance(queue.get("artifacts"), dict) else {},
    }


def _build_live_launch_readiness_audit(refresh: bool = False) -> dict[str, Any]:
    decision_result = _build_live_launch_decision_card(refresh=refresh)
    blocker_board = _build_live_blocker_evidence_board(refresh=False)
    learning = _build_live_learning_ledger(refresh=False)
    broker_preflight = _broker_holdings_sync_preflight()

    decision = decision_result.get("decision") if isinstance(decision_result.get("decision"), dict) else {}
    blocker_summary = blocker_board.get("summary") if isinstance(blocker_board.get("summary"), dict) else {}
    learning_summary = learning.get("summary") if isinstance(learning.get("summary"), dict) else {}
    blockers = blocker_board.get("rows") if isinstance(blocker_board.get("rows"), list) else []
    learning_rows = learning.get("learning_ledger") if isinstance(learning.get("learning_ledger"), list) else []

    blocking_count = _to_int_or_zero(decision.get("blocking_command_count"))
    pending_evidence_count = _to_int_or_zero(decision.get("pending_evidence_count"))
    learning_needs_review = _to_int_or_zero(learning_summary.get("needs_review_count"))
    broker_blockers = broker_preflight.get("blockers") if isinstance(broker_preflight.get("blockers"), list) else []
    broker_warnings = broker_preflight.get("warnings") if isinstance(broker_preflight.get("warnings"), list) else []
    broker_stale = "cached_broker_holding_snapshot_stale" in broker_warnings or "no_cached_broker_holding_snapshot" in broker_warnings

    hard_gaps: list[dict[str, Any]] = []
    if blocking_count > 0:
        hard_gaps.append(
            {
                "gap": "premarket_blockers_pending",
                "count": blocking_count,
                "next_action": decision.get("next_action") or decision.get("primary_blocker"),
                "trade_impact": "hold_live_launch",
            }
        )
    if pending_evidence_count > 0:
        hard_gaps.append(
            {
                "gap": "evidence_pending",
                "count": pending_evidence_count,
                "next_action": blocker_summary.get("next_action"),
                "trade_impact": "review_before_live",
            }
        )
    if broker_blockers or broker_stale:
        hard_gaps.append(
            {
                "gap": "broker_holding_price_not_fresh",
                "count": len(broker_blockers) or 1,
                "next_action": broker_preflight.get("next_manual_action"),
                "trade_impact": "sync_real_position_before_decision",
            }
        )

    watch_gaps: list[dict[str, Any]] = []
    if learning_needs_review > 0:
        watch_gaps.append(
            {
                "gap": "learning_items_need_review",
                "count": learning_needs_review,
                "next_action": "review top learning risks before changing rules",
                "trade_impact": "do_not_optimize_by_profit_only",
            }
        )

    audit_status = "blocked"
    audit_label = "blocked_before_live_launch"
    if not hard_gaps and decision.get("decision_status") == "ready_for_manual_live_review":
        audit_status = "manual_review_ready"
        audit_label = "manual_live_review_ready"
    elif not hard_gaps:
        audit_status = "watch"
        audit_label = "manual_review_watch"

    top_blockers = [
        row
        for row in blockers
        if isinstance(row, dict)
        and (
            _clean_review_text(row.get("evidence_status")) == "pending_evidence"
            or _truthy(row.get("can_execute_now"))
        )
    ][:5]
    top_learning = [
        row
        for row in learning_rows
        if _learning_status_rank(row.get("learning_status"), row.get("risk_level")) <= 1
    ][:5]

    return {
        "ok": True,
        "mode": "g3_live_launch_readiness_audit",
        "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "audit": {
            "audit_status": audit_status,
            "audit_label": audit_label,
            "can_enter_live_manual_review": audit_status == "manual_review_ready",
            "can_buy": False,
            "primary_blocker": decision.get("primary_blocker") or (hard_gaps[0].get("next_action") if hard_gaps else ""),
            "next_action": (hard_gaps[0].get("next_action") if hard_gaps else decision.get("next_action")),
            "natural_trade_conclusion": (
                "clear_execution_evidence_first"
                if hard_gaps
                else "manual_review_only_no_auto_order"
            ),
            "optimization_boundary": "review buy point, sell point, selection, and model switch only after evidence is fresh; never tune only for return.",
        },
        "hard_gaps": hard_gaps,
        "watch_gaps": watch_gaps,
        "top_blockers": top_blockers,
        "top_learning_risks": top_learning,
        "broker_sync_preflight": broker_preflight,
        "summary": {
            "hard_gap_count": len(hard_gaps),
            "watch_gap_count": len(watch_gaps),
            "blocking_command_count": blocking_count,
            "pending_evidence_count": pending_evidence_count,
            "learning_needs_review_count": learning_needs_review,
            "broker_preflight_status": broker_preflight.get("preflight_status"),
            "broker_staleness_minutes": broker_preflight.get("broker_staleness_minutes"),
            "decision_status": decision.get("decision_status"),
            "live_admission_status": decision.get("live_admission_status"),
        },
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
        "artifacts": {
            "realtime_readiness_review": _path_status(REALTIME_READINESS_REVIEW_DIR / "summary.json"),
            "live_launch_packet": _path_status(LIVE_LAUNCH_PACKET_DIR / "launch_packet.json"),
            "live_blocker_evidence_ledger": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_blocker_evidence_ledger.csv"),
            "live_hidden_risk_watchlist": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_hidden_risk_watchlist.csv"),
        },
    }


def _load_live_launch_review_snapshots() -> list[dict[str, Any]]:
    data = _read_json(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH)
    rows = data.get("snapshots") if isinstance(data, dict) else []
    return rows if isinstance(rows, list) else []


def _load_live_launch_review_snapshot_detail(snapshot_id: str) -> dict[str, Any]:
    snapshot_id = _clean_review_text(snapshot_id)
    if not snapshot_id or Path(snapshot_id).name != snapshot_id or not re.fullmatch(r"[0-9T_A-Za-z-]+", snapshot_id):
        return {"ok": False, "error": "invalid snapshot_id"}
    detail_path = LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / f"{snapshot_id}.json"
    detail = _read_json(detail_path)
    if detail:
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "detail": detail,
            "artifacts": {
                "detail_json": _path_status(detail_path),
                "timeline_csv": _path_status(LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / "timeline.csv"),
                "live_launch_review_snapshots": _path_status(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH),
            },
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
        }
    rows = _load_live_launch_review_snapshots()
    snapshot = next((row for row in rows if isinstance(row, dict) and _clean_review_text(row.get("snapshot_id")) == snapshot_id), None)
    if snapshot:
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "detail": snapshot,
            "detail_missing": True,
            "artifacts": {
                "detail_json": _path_status(detail_path),
                "timeline_csv": _path_status(LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / "timeline.csv"),
                "live_launch_review_snapshots": _path_status(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH),
            },
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
        }
    return {"ok": False, "error": "snapshot_not_found", "snapshot_id": snapshot_id}


def _save_live_launch_review_snapshots(rows: list[dict[str, Any]]) -> None:
    rows = rows[-500:]
    _write_json(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH, {"snapshots": rows})
    LIVE_LAUNCH_REVIEW_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / "timeline.csv", index=False, encoding="utf-8-sig")


def _count_status(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _clean_review_text(row.get(field), "blank")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _build_live_launch_review_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    refresh = bool(payload.get("refresh"))
    phase = _clean_review_text(payload.get("phase"), "manual_checkpoint")
    note = _clean_review_text(payload.get("note") or payload.get("review_note"))
    source = _clean_review_text(payload.get("source"), "g3_live_launch_review_snapshot")
    review = _read_or_run_realtime_readiness_review(refresh=refresh)
    packet_result = _read_or_run_live_launch_packet(refresh=refresh)
    packet = packet_result.get("packet") if isinstance(packet_result.get("packet"), dict) else {}
    readiness_summary = review.get("summary") if isinstance(review.get("summary"), dict) else {}
    launch_summary = packet.get("summary") if isinstance(packet.get("summary"), dict) else {}
    command = _current_premarket_action_card(review)
    playbook_rows = packet_result.get("launch_day_playbook") if isinstance(packet_result.get("launch_day_playbook"), list) else []
    if not playbook_rows and isinstance(packet.get("launch_day_playbook"), list):
        playbook_rows = packet.get("launch_day_playbook")
    learning_rows = packet_result.get("launch_day_learning_queue") if isinstance(packet_result.get("launch_day_learning_queue"), list) else []
    if not learning_rows and isinstance(packet.get("launch_day_learning_queue"), list):
        learning_rows = packet.get("launch_day_learning_queue")
    live_learning = _build_live_learning_ledger(refresh=False)
    live_learning_rows = live_learning.get("learning_ledger") if isinstance(live_learning.get("learning_ledger"), list) else []
    live_learning_summary = live_learning.get("summary") if isinstance(live_learning.get("summary"), dict) else {}

    generated_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    snapshot_id = f"{generated_at.replace('-', '').replace(':', '').replace(' ', 'T')}_{uuid4().hex[:8]}"
    record = {
        "snapshot_id": snapshot_id,
        "generated_at": generated_at,
        "phase": phase,
        "source": source,
        "note": note,
        "entry_date": readiness_summary.get("next_trade_entry_date") or launch_summary.get("next_trade_entry_date"),
        "live_admission_status": readiness_summary.get("live_admission_status") or launch_summary.get("live_admission_status"),
        "live_admission_buy_allowed": bool(readiness_summary.get("live_admission_buy_allowed") or launch_summary.get("live_admission_buy_allowed")),
        "blocking_command_count": readiness_summary.get("live_admission_blocking_command_count") or launch_summary.get("live_admission_blocking_command_count"),
        "next_action": readiness_summary.get("live_premarket_next_action") or launch_summary.get("live_premarket_next_action"),
        "first_live_decision_status": readiness_summary.get("first_live_decision_status"),
        "formal_launch_ready": bool(readiness_summary.get("formal_launch_ready") or launch_summary.get("formal_launch_ready")),
        "launch_packet_status": packet.get("status"),
        "launch_day_playbook_count": len(playbook_rows),
        "launch_day_learning_queue_count": len(learning_rows),
        "live_learning_ledger_count": len(live_learning_rows),
        "live_learning_needs_review_count": live_learning_summary.get("needs_review_count"),
        "live_learning_watch_more_count": live_learning_summary.get("watch_more_count"),
        "playbook_review_status_counts": _count_status(playbook_rows, "review_status"),
        "learning_status_counts": _count_status(learning_rows, "learning_status"),
        "live_learning_status_counts": live_learning_summary.get("status_counts") or {},
        "current_action_group": command.get("action_group"),
        "current_action_label": command.get("action_label"),
        "formal_buy_signal": False,
        "auto_order_allowed": False,
        "order_path_enabled": False,
    }
    detail_path = LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / f"{snapshot_id}.json"
    detail = {
        **record,
        "readiness_summary": readiness_summary,
        "launch_summary": launch_summary,
        "premarket_command": command,
        "launch_day_playbook": playbook_rows,
        "launch_day_learning_queue": learning_rows,
        "live_learning_ledger": live_learning_rows,
        "live_learning_summary": live_learning_summary,
        "artifacts": {
            "live_launch_review_snapshots": _path_status(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH),
            "timeline_csv": _path_status(LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / "timeline.csv"),
            "detail_json": _path_status(LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / f"{snapshot_id}.json"),
        },
    }
    LIVE_LAUNCH_REVIEW_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    detail_path.write_text(
        json.dumps(detail, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    rows = _load_live_launch_review_snapshots()
    rows.append(record)
    _save_live_launch_review_snapshots(rows)
    detail["artifacts"] = {
        "live_launch_review_snapshots": _path_status(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH),
        "timeline_csv": _path_status(LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / "timeline.csv"),
        "detail_json": _path_status(detail_path),
    }
    detail_path.write_text(
        json.dumps(detail, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return {"ok": True, "snapshot": record, "detail": detail}


def _try_record_live_launch_action_snapshot(
    *,
    source: str,
    phase: str,
    note: str,
    refresh_packet: bool = True,
) -> dict[str, Any]:
    try:
        if refresh_packet:
            _run_live_launch_packet_once()
        return _build_live_launch_review_snapshot(
            {
                "source": source,
                "phase": phase,
                "note": note,
                "refresh": False,
            }
        )
    except Exception as exc:
        logger.exception("Failed to record G3 live launch action snapshot.")
        return {"ok": False, "error": str(exc)}


def _read_or_run_live_launch_packet(refresh: bool = False) -> dict[str, Any]:
    if refresh:
        return _run_live_launch_packet_once()
    packet = _read_live_launch_packet()
    if packet.get("packet"):
        return {
            "ok": True,
            "mode": "g3_live_launch_packet_v1",
            **packet,
        }
    return _run_live_launch_packet_once()


def _readiness_review_admission_summary(review: dict[str, Any]) -> dict[str, Any]:
    summary = review.get("summary") if isinstance(review.get("summary"), dict) else {}
    return {
        "live_admission_status": summary.get("live_admission_status"),
        "live_admission_label": summary.get("live_admission_label"),
        "live_admission_buy_allowed": bool(summary.get("live_admission_buy_allowed")),
        "live_admission_blocking_command_count": summary.get("live_admission_blocking_command_count"),
        "live_premarket_next_action": summary.get("live_premarket_next_action"),
        "live_blocker_pending_evidence_count": summary.get("live_blocker_pending_evidence_count"),
        "first_live_decision_status": summary.get("first_live_decision_status"),
        "formal_launch_ready": bool(summary.get("formal_launch_ready")),
    }


def _current_premarket_action_card(review: dict[str, Any]) -> dict[str, Any]:
    summary = review.get("summary") if isinstance(review.get("summary"), dict) else {}
    sequence_rows = review.get("live_premarket_action_sequence")
    if not isinstance(sequence_rows, list):
        sequence_rows = []
    recheck_rows = review.get("live_premarket_execution_recheck")
    if not isinstance(recheck_rows, list):
        recheck_rows = []
    admission_rows = review.get("live_admission_snapshot")
    if not isinstance(admission_rows, list):
        admission_rows = []

    ready_rows = [row for row in sequence_rows if isinstance(row, dict) and _truthy(row.get("can_execute_now"))]
    current = ready_rows[0] if ready_rows else (sequence_rows[0] if sequence_rows else {})
    current = current if isinstance(current, dict) else {}
    action_group = _clean_review_text(current.get("action_group"))
    recommended_api_action = _clean_review_text(current.get("recommended_api_action"))
    safe_api_groups = {"sync_broker_holding_price", "rerun_readiness_audit"}
    requires_confirmation = action_group == "sync_broker_holding_price" or _truthy(current.get("requires_manual_confirmation"))
    manual_only = bool(action_group and action_group not in safe_api_groups)
    can_execute_now = bool(current) and _truthy(current.get("can_execute_now"))
    can_execute_by_api = can_execute_now and action_group in safe_api_groups

    return {
        "generated_at": summary.get("generated_at"),
        "entry_date": summary.get("next_trade_entry_date"),
        "status": summary.get("live_admission_status"),
        "status_label": summary.get("live_admission_label"),
        "live_buy_allowed": bool(summary.get("live_admission_buy_allowed")),
        "blocking_command_count": summary.get("live_admission_blocking_command_count"),
        "pending_evidence_count": summary.get("live_blocker_pending_evidence_count"),
        "next_action": summary.get("live_premarket_next_action"),
        "current_action": current,
        "current_recheck": recheck_rows[0] if recheck_rows else {},
        "admission": admission_rows[0] if admission_rows else {},
        "action_group": action_group,
        "action_label": current.get("action_label") or summary.get("live_premarket_next_action"),
        "recommended_api_action": recommended_api_action,
        "recommended_ui_action": current.get("recommended_ui_action"),
        "can_execute_now": can_execute_now,
        "can_execute_by_api": can_execute_by_api,
        "requires_confirmation": requires_confirmation,
        "manual_only": manual_only,
        "confirmation_hint": (
            "confirm=true is required because this action may touch the broker/THS refresh path."
            if requires_confirmation
            else ""
        ),
        "execution_boundary": "This endpoint never enables auto order routing or formal buy signals.",
    }


def _read_or_run_realtime_readiness_review(refresh: bool = False) -> dict[str, Any]:
    if refresh:
        return _run_realtime_readiness_review_once()
    review = _read_realtime_readiness_review()
    if review.get("summary"):
        return {
            "ok": True,
            "mode": "g3_realtime_readiness_review_v1",
            **review,
        }
    return _run_realtime_readiness_review_once()


def _append_review_action_attempt(
    *,
    attempted_at: str,
    action_group: str,
    action_label: str,
    source: str,
    ok: bool,
    review_result: dict[str, Any],
    admission: dict[str, Any],
    response: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    run = review_result.get("run") if isinstance(review_result.get("run"), dict) else {}
    attempt = {
        "attempted_at": attempted_at,
        "action_group": action_group,
        "action_label": action_label,
        "source": source,
        "ok": ok,
        "review_ok": bool(review_result.get("ok")),
        "review_returncode": run.get("returncode"),
        "live_admission_status": admission.get("live_admission_status"),
        "live_admission_buy_allowed": admission.get("live_admission_buy_allowed"),
        "live_admission_blocking_command_count": admission.get("live_admission_blocking_command_count"),
        "live_premarket_next_action": admission.get("live_premarket_next_action"),
        "formal_buy_signal": response.get("formal_buy_signal"),
        "auto_order_allowed": response.get("auto_order_allowed"),
        "order_path_enabled": response.get("order_path_enabled"),
    }
    if extra:
        attempt.update(extra)
    _append_premarket_action_attempt(attempt)


@router.post("/realtime-readiness-review/run")
async def run_gen3_state_alpha_realtime_readiness_review() -> dict[str, Any]:
    return _wrap_guardrails(_run_realtime_readiness_review_once())


@router.get("/live-launch-packet")
async def get_gen3_state_alpha_live_launch_packet(
    refresh: bool = Query(default=False, description="Rebuild the G3 live launch packet before reading it."),
) -> dict[str, Any]:
    packet = _read_or_run_live_launch_packet(refresh=refresh)
    return _wrap_guardrails(packet)


@router.post("/live-launch-packet/run")
async def run_gen3_state_alpha_live_launch_packet() -> dict[str, Any]:
    return _wrap_guardrails(_run_live_launch_packet_once())


@router.get("/live-learning-ledger")
async def get_gen3_state_alpha_live_learning_ledger(
    refresh: bool = Query(default=False, description="Refresh readiness review and launch packet before building the learning ledger."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_live_learning_ledger(refresh=refresh))


@router.get("/live-launch-decision")
async def get_gen3_state_alpha_live_launch_decision(
    refresh: bool = Query(default=False, description="Refresh readiness review before building the live launch decision card."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_live_launch_decision_card(refresh=refresh))


@router.get("/live-blocker-evidence-board")
async def get_gen3_state_alpha_live_blocker_evidence_board(
    refresh: bool = Query(default=False, description="Refresh readiness review before building the live blocker evidence board."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_live_blocker_evidence_board(refresh=refresh))


@router.get("/live-launch-readiness-audit")
async def get_gen3_state_alpha_live_launch_readiness_audit(
    refresh: bool = Query(default=False, description="Refresh readiness review before building the live launch readiness audit."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_live_launch_readiness_audit(refresh=refresh))


@router.get("/strategy-tuning-axis-board")
async def get_gen3_state_alpha_strategy_tuning_axis_board(
    refresh: bool = Query(default=False, description="Refresh readiness review and launch packet before building the read-only G3 tuning axis board."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_g3_strategy_tuning_axis_board(refresh=refresh))


@router.get("/strategy-tuning-review-queue")
async def get_gen3_state_alpha_strategy_tuning_review_queue(
    refresh: bool = Query(default=False, description="Refresh readiness review and launch packet before building the read-only G3 tuning review queue."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_g3_strategy_tuning_review_queue(refresh=refresh))


@router.get("/strategy-tuning-completion-audit")
async def get_gen3_state_alpha_strategy_tuning_completion_audit(
    refresh: bool = Query(default=False, description="Refresh readiness review and launch packet before auditing G3 tuning review completion."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_g3_strategy_tuning_completion_audit(refresh=refresh))


@router.get("/strategy-tuning-replay-suggestions")
async def get_gen3_state_alpha_strategy_tuning_replay_suggestions(
    refresh: bool = Query(default=False, description="Refresh readiness review and launch packet before generating G3 tuning replay suggestions."),
    limit: int = Query(default=50, ge=1, le=200, description="Maximum replay suggestions returned."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_g3_strategy_tuning_replay_suggestions(refresh=refresh, limit=limit))


@router.get("/strategy-tuning-replay-session")
async def get_gen3_state_alpha_strategy_tuning_replay_session(
    refresh: bool = Query(default=False, description="Refresh readiness review before building the ordered G3 tuning replay session."),
    limit: int = Query(default=12, ge=1, le=50, description="Maximum replay session steps returned."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_g3_strategy_tuning_replay_session(refresh=refresh, limit=limit))


@router.get("/strategy-tuning-current-step-completion-packet")
async def get_gen3_state_alpha_strategy_tuning_current_step_completion_packet(
    refresh: bool = Query(default=False, description="Refresh readiness review before building the current replay step completion packet."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_g3_strategy_tuning_current_step_completion_packet(refresh=refresh))


@router.get("/broker/holdings/post-sync-acceptance")
async def get_gen3_state_alpha_broker_post_sync_acceptance(
    refresh: bool = Query(default=False, description="Refresh readiness review before checking post-sync acceptance."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_broker_post_sync_acceptance_packet(refresh=refresh))


@router.post("/broker/holdings/post-sync-acceptance/record-execution-evidence")
async def record_gen3_state_alpha_broker_post_sync_execution_evidence(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    return _wrap_guardrails(_record_broker_post_sync_execution_evidence(payload))


@router.get("/live-action-console")
async def get_gen3_state_alpha_live_action_console(
    refresh: bool = Query(default=False, description="Refresh readiness review before building the live action console."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_g3_live_action_console(refresh=refresh))


@router.post("/live-action-console/step-review")
async def save_gen3_state_alpha_live_action_console_step_review(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    return _wrap_guardrails(_save_g3_live_action_console_step_review(payload if isinstance(payload, dict) else {}))


@router.get("/live-replay-cockpit")
async def get_gen3_state_alpha_live_replay_cockpit(
    refresh: bool = Query(default=False, description="Refresh readiness review before building the live replay cockpit."),
) -> dict[str, Any]:
    return _wrap_guardrails(_build_g3_live_replay_cockpit(refresh=refresh))


@router.get("/strategy-tuning-task-reviews")
async def get_gen3_state_alpha_strategy_tuning_task_reviews() -> dict[str, Any]:
    reviews = _load_strategy_tuning_task_reviews()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_strategy_tuning_task_reviews",
            "reviews": list(reviews.values()),
            "review_map": reviews,
            "count": len(reviews),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "strategy_tuning_task_reviews": _path_status(STRATEGY_TUNING_TASK_REVIEWS_PATH),
            },
        }
    )


@router.post("/strategy-tuning-task-review")
async def save_gen3_state_alpha_strategy_tuning_task_review(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _save_strategy_tuning_task_review(payload if isinstance(payload, dict) else {})
    queue = _build_g3_strategy_tuning_review_queue(refresh=False) if result.get("ok") else {}
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_strategy_tuning_task_review",
            "queue_summary": queue.get("summary") if isinstance(queue.get("summary"), dict) else {},
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "strategy_tuning_task_reviews": _path_status(STRATEGY_TUNING_TASK_REVIEWS_PATH),
            },
        }
    )


@router.get("/live-launch-review-snapshots")
async def get_gen3_state_alpha_live_launch_review_snapshots(
    limit: int = Query(default=100, ge=1, le=500, description="Maximum G3 live launch review snapshots returned."),
) -> dict[str, Any]:
    rows = _load_live_launch_review_snapshots()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_live_launch_review_snapshots",
            "snapshots": rows[-limit:][::-1],
            "count": len(rows),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "live_launch_review_snapshots": _path_status(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH),
                "timeline_csv": _path_status(LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / "timeline.csv"),
            },
        }
    )


@router.get("/live-launch-review-snapshot/{snapshot_id}")
async def get_gen3_state_alpha_live_launch_review_snapshot_detail(snapshot_id: str) -> dict[str, Any]:
    result = _load_live_launch_review_snapshot_detail(snapshot_id)
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_live_launch_review_snapshot_detail",
        }
    )


@router.post("/live-launch-review-snapshot")
async def record_gen3_state_alpha_live_launch_review_snapshot(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _build_live_launch_review_snapshot(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_live_launch_review_snapshot",
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "live_launch_review_snapshots": _path_status(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH),
                "timeline_csv": _path_status(LIVE_LAUNCH_REVIEW_ARCHIVE_DIR / "timeline.csv"),
            },
        }
    )


@router.get("/launch-day-playbook-reviews")
async def get_gen3_state_alpha_launch_day_playbook_reviews() -> dict[str, Any]:
    reviews = _load_launch_day_playbook_reviews()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_launch_day_playbook_reviews",
            "reviews": list(reviews.values()),
            "review_map": reviews,
            "count": len(reviews),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "launch_day_playbook_reviews": _path_status(LAUNCH_DAY_PLAYBOOK_REVIEWS_PATH),
            },
        }
    )


@router.post("/launch-day-playbook-review-and-run")
async def save_gen3_state_alpha_launch_day_playbook_review_and_run(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    request_payload = payload if isinstance(payload, dict) else {}
    save_result = _save_launch_day_playbook_review(request_payload)
    if not save_result.get("ok"):
        return _wrap_guardrails(
            {
                **save_result,
                "mode": "g3_state_alpha_launch_day_playbook_review_and_packet",
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "artifacts": {
                    "launch_day_playbook_reviews": _path_status(LAUNCH_DAY_PLAYBOOK_REVIEWS_PATH),
                },
            }
        )
    packet = _run_live_launch_packet_once()
    saved_review = save_result.get("review") if isinstance(save_result.get("review"), dict) else {}
    review_label = saved_review.get("review_result_label") or saved_review.get("review_result") or "review_saved"
    snapshot = _try_record_live_launch_action_snapshot(
        source=_clean_review_text(request_payload.get("source"), "launch_day_playbook"),
        phase="launch_day_playbook_review",
        note=(
            f"launch day playbook review saved: "
            f"{saved_review.get('review_axis') or request_payload.get('review_axis') or 'unknown_axis'} / "
            f"{saved_review.get('action_type') or request_payload.get('action_type') or 'unknown_action'} / "
            f"{review_label}. "
            f"{saved_review.get('review_note') or request_payload.get('review_note') or ''}"
        ),
        refresh_packet=False,
    )
    return _wrap_guardrails(
        {
            "ok": bool(save_result.get("ok")) and bool(packet.get("ok")),
            "mode": "g3_state_alpha_launch_day_playbook_review_and_packet",
            "save": save_result,
            "packet": packet,
            "live_launch_review_snapshot": snapshot.get("snapshot") if snapshot.get("ok") else None,
            "live_launch_review_snapshot_error": snapshot.get("error") if not snapshot.get("ok") else None,
            "message": "launch day playbook review saved and packet rebuilt",
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                **(packet.get("artifacts") if isinstance(packet.get("artifacts"), dict) else {}),
                "launch_day_playbook_reviews": _path_status(LAUNCH_DAY_PLAYBOOK_REVIEWS_PATH),
                "live_launch_review_snapshots": _path_status(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH),
            },
        }
    )


@router.get("/premarket-control")
async def get_gen3_state_alpha_premarket_control(
    refresh: bool = Query(default=False, description="Run the realtime readiness review before reading the command card."),
) -> dict[str, Any]:
    review = _read_or_run_realtime_readiness_review(refresh=refresh)
    return _wrap_guardrails(
        {
            "ok": bool(review.get("summary")),
            "mode": "g3_state_alpha_premarket_control",
            "review_ok": bool(review.get("ok")),
            "command": _current_premarket_action_card(review),
            "summary": review.get("summary") if isinstance(review.get("summary"), dict) else {},
            "artifacts": {
                "summary": _path_status(REALTIME_READINESS_REVIEW_DIR / "summary.json"),
                "live_premarket_action_sequence": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_premarket_action_sequence.csv"),
                "live_premarket_execution_recheck": _path_status(REALTIME_READINESS_REVIEW_DIR / "live_premarket_execution_recheck.csv"),
                "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
            },
        }
    )


@router.post("/premarket-control/execute-next")
async def execute_gen3_state_alpha_premarket_next_action(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    confirm = bool(payload.get("confirm"))
    source = _clean_review_text(payload.get("source"), "premarket_control")
    refresh_first = bool(payload.get("refresh_first"))
    expected_action_group = _clean_review_text(payload.get("action_group"))
    review = _read_or_run_realtime_readiness_review(refresh=refresh_first)
    command = _current_premarket_action_card(review)
    action_group = _clean_review_text(command.get("action_group"))

    if expected_action_group and action_group and expected_action_group != action_group:
        return _wrap_guardrails(
            {
                "ok": False,
                "mode": "g3_state_alpha_premarket_control_execute_next",
                "error": "action_group_changed",
                "expected_action_group": expected_action_group,
                "actual_action_group": action_group,
                "command": command,
            }
        )

    if not action_group:
        return _wrap_guardrails(
            {
                "ok": True,
                "mode": "g3_state_alpha_premarket_control_execute_next",
                "executed": False,
                "message": "no premarket action is currently required",
                "command": command,
            }
        )

    if not command.get("can_execute_now"):
        return _wrap_guardrails(
            {
                "ok": False,
                "mode": "g3_state_alpha_premarket_control_execute_next",
                "executed": False,
                "error": "current_action_not_executable_yet",
                "command": command,
            }
        )

    if action_group == "sync_broker_holding_price":
        if not confirm:
            return _wrap_guardrails(
                {
                    "ok": False,
                    "mode": "g3_state_alpha_premarket_control_execute_next",
                    "executed": False,
                    "error": "confirmation_required",
                    "command": command,
                }
            )
        response = await sync_gen3_state_alpha_broker_holdings_from_ths_and_review(
            {
                "source": source,
                "action_group": action_group,
                "premarket_control": True,
            }
        )
        return response

    if action_group == "rerun_readiness_audit":
        started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        review_result = _run_realtime_readiness_review_once()
        admission = _readiness_review_admission_summary(review_result)
        response = _wrap_guardrails(
            {
                "ok": bool(review_result.get("ok")),
                "mode": "g3_state_alpha_premarket_control_execute_next",
                "executed": True,
                "action_group": action_group,
                "action_label": command.get("action_label") or "rerun readiness audit",
                "review": review_result,
                "admission": admission,
                "command": _current_premarket_action_card(review_result),
            }
        )
        _append_review_action_attempt(
            attempted_at=started_at,
            action_group=action_group,
            action_label=str(command.get("action_label") or "rerun readiness audit"),
            source=source,
            ok=bool(review_result.get("ok")),
            review_result=review_result,
            admission=admission,
            response=response,
            extra={
                "sync_ok": None,
                "sync_mode": "not_applicable",
                "sync_message": "",
            },
        )
        snapshot = _try_record_live_launch_action_snapshot(
            source=source,
            phase="premarket_action_executed",
            note=(
                f"盘前动作已执行：{command.get('action_label') or action_group}；"
                f"ok={bool(review_result.get('ok'))}；"
                f"阻断数={admission.get('live_admission_blocking_command_count')}；"
                f"下一步={admission.get('live_premarket_next_action') or '--'}"
            ),
        )
        response["live_launch_review_snapshot"] = snapshot.get("snapshot") if snapshot.get("ok") else None
        if not snapshot.get("ok"):
            response["live_launch_review_snapshot_error"] = snapshot.get("error")
        response["artifacts"] = {
            **(response.get("artifacts") if isinstance(response.get("artifacts"), dict) else {}),
            "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
            "live_launch_review_snapshots": _path_status(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH),
        }
        return response

    return _wrap_guardrails(
        {
            "ok": False,
            "mode": "g3_state_alpha_premarket_control_execute_next",
            "executed": False,
            "error": "manual_review_required",
            "command": command,
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
    natural_policy_shadow = _read_natural_policy_shadow()
    tickets = _retire_g2_gap_supplement_records(
        _attach_natural_policy_shadow_to_records(
            _enrich_trade_strategy_records(_read_csv_records(tickets_path, limit=limit))
        )
    )
    afterhours_tickets = _retire_g2_gap_supplement_records(
        _attach_natural_policy_shadow_to_records(
            _enrich_trade_strategy_records(_read_csv_records(afterhours_tickets_path, limit=limit))
        )
    )
    broker_snapshot = _broker_snapshot()
    broker_trades = broker_snapshot.get("broker_trades") if isinstance(broker_snapshot.get("broker_trades"), list) else []
    ledger_audit = _attach_exit_advice(
        _enrich_trade_strategy_records(_read_shadow_ledger_records(ledger_path, limit=limit)),
        source="shadow_ledger",
        updated_at=summary.get("generated_at") if isinstance(summary, dict) else None,
        broker_trades=broker_trades,
    )
    ledger = _filter_open_shadow_ledger_records(ledger_audit)
    selected_candidates = _retire_g2_gap_supplement_records(
        _attach_natural_policy_shadow_to_records(
            _enrich_trade_strategy_records(_read_csv_records(selected_path, limit=limit))
        )
    )
    all_source_candidates = _retire_g2_gap_supplement_records(
        _attach_natural_policy_shadow_to_records(
            _enrich_trade_strategy_records(_read_csv_records(all_path, limit=limit))
        )
    )
    route_diagnostics = _retire_g2_gap_supplement_diagnostics(_read_csv_records(diagnostics_path, limit=50))
    real_exit_rows = broker_snapshot.get("holdings") if isinstance(broker_snapshot.get("holdings"), list) else []
    qualified_tickets = [item for item in tickets if _truthy(item.get("qualified_shadow_buy"))]
    if isinstance(summary, dict):
        summary["shadow_ticket_rows"] = len(tickets)
        summary["qualified_shadow_buy_rows"] = len(qualified_tickets)
        summary["g2_gap_supplement_enabled"] = bool(G2_GAP_SUPPLEMENT_LIVE_ENABLED)
        summary["g2_gap_supplement_live_enabled"] = bool(G2_GAP_SUPPLEMENT_LIVE_ENABLED)
        summary["g2_gap_supplement_retire_reason"] = G2_GAP_SUPPLEMENT_RETIRE_REASON
    open_code_entries = _open_position_code_entries([*real_exit_rows, *ledger])
    afterhours_tickets = _apply_natural_same_stock_open_guard(afterhours_tickets, open_code_entries)
    selected_candidates = _apply_natural_same_stock_open_guard(selected_candidates, open_code_entries)
    all_source_candidates = _apply_natural_same_stock_open_guard(all_source_candidates, open_code_entries)
    blocked_candidates = [
        item for item in all_source_candidates
        if not _truthy(item.get("router_eligible")) or item.get("block_reason")
    ][:limit]
    next_trade_buy_tickets = _apply_natural_same_stock_open_guard(
        _filter_next_trade_buy_tickets(afterhours_tickets, summary, tickets),
        open_code_entries,
    )
    next_trade_buy_tickets = _attach_pretrade_reviews_to_records(next_trade_buy_tickets)
    afterhours_tickets = _attach_pretrade_reviews_to_records(afterhours_tickets)
    selected_candidates = _attach_pretrade_reviews_to_records(selected_candidates)
    all_source_candidates = _attach_pretrade_reviews_to_records(all_source_candidates)
    date_display_analysis = _build_date_display_analysis(
        summary,
        tickets,
        afterhours_summary,
        afterhours_tickets,
        next_trade_buy_tickets,
    )
    exit_rows = [*real_exit_rows, *ledger]
    daily_trend_exit_monitor = _load_daily_trend_exit_monitor_state()

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
            "selected_ticket": _first_record(qualified_tickets),
            "shadow_ledger": ledger,
            "shadow_ledger_audit": ledger_audit,
            "exit_management": {
                "real_holdings": real_exit_rows,
                "shadow_holdings": ledger,
                "shadow_ledger_audit_rows": len(ledger_audit),
                "summary": _exit_advice_summary(exit_rows),
                "contract": "realtime_exit_advice_v1",
                "daily_trend_exit_monitor": daily_trend_exit_monitor.get("last_result"),
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
            "natural_policy_shadow": natural_policy_shadow,
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
                "natural_policy_shadow_summary": _path_status(NATURAL_POLICY_SHADOW_DIR / "summary.json"),
                "natural_policy_shadow_contract": _path_status(NATURAL_POLICY_SHADOW_DIR / "natural_policy_contract.json"),
                "natural_candidate_shadow_labels": _path_status(NATURAL_POLICY_SHADOW_DIR / "candidate_shadow_labels.csv"),
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
    watch_candidates = _build_mainwave_watch_candidates(limit=max(limit * 3, 120))
    sectors = _build_mainwave_sector_opportunities_v2(candidates, watch_candidates)
    _enrich_mainwave_sector_codes(sectors)
    sector_kline_by_name = _build_mainwave_sector_kline_map(sectors, days=260)
    sector_components_by_code = _build_mainwave_sector_component_map(sectors)
    for sector in sectors:
        name = str(sector.get("sector_name") or "")
        code = str(sector.get("sector_code") or "")
        sector["kline_daily"] = sector_kline_by_name.get(name, [])
        sector["kline_day_count"] = len(sector["kline_daily"])
        sector["component_stocks"] = sector_components_by_code.get(code, [])
        sector["component_stock_count"] = len(sector["component_stocks"])
    _apply_mainwave_sector_trend_adjustment(sectors)
    recommended = [item for item in candidates if item.get("is_recommended")]
    sector_watch_candidates = [item for item in watch_candidates if _to_int_or_zero(item.get("sector_signal_count")) >= 2]
    m30_ok_count = len([item for item in candidates if bool(item.get("m30_confirmed"))])
    entry_date = _date_text(summary.get("entry_date") or source_meta.get("entry_date") or (recommended[0].get("entry_date") if recommended else None))
    if recommended:
        entry_date = _date_text(recommended[0].get("entry_date") or entry_date)
    decision_date = _date_text(summary.get("decision_date") or source_meta.get("decision_date") or (recommended[0].get("decision_date") if recommended else None))
    index_mom60 = _to_float_or_none(source_meta.get("index_mom60") or (source_meta.get("index") or {}).get("index_mom60"))
    mainwave_dynamic_cooldown = source_meta.get("mainwave_dynamic_cooldown") if isinstance(source_meta.get("mainwave_dynamic_cooldown"), dict) else {}
    scan = source_meta.get("scan") if isinstance(source_meta.get("scan"), dict) else {}
    strongest_sector = sectors[0] if sectors else {}

    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_institutional_mainwave_opportunities",
            "strategy": {
                "route": "institutional_mainwave",
                "route_label": "机构主升浪",
                "trade_strategy": "institutional_mainwave_score88",
                "trade_strategy_label": "机构主升Score88",
                "source": "institutional_mainwave_current_builder_v1",
                "source_script": "scripts/gen3_institutional_mainwave_current_v1.py",
                "purpose": "识别机构集体主升、板块扩散和重要行业机会，并为下一交易日候选提供来源。",
                "ranking_basis": "wave_style_score 主升骨架 + 同日同业主升共振计数 + 30m放量突破前20根高点确认 + 指数门槛。",
            },
            "summary": {
                "entry_date": entry_date,
                "decision_date": decision_date,
                "source_label": source_label,
                "source_path": str(source_path) if source_path else "",
                "candidate_count": len(candidates),
                "watch_candidate_count": len(watch_candidates),
                "sector_watch_candidate_count": len(sector_watch_candidates),
                "sector_count": len(sectors),
                "recommended_count": len(recommended),
                "m30_ok_count": m30_ok_count,
                "strong_sector_count": len([item for item in sectors if item.get("state") == "strong_mainwave"]),
                "important_sector_count": len([item for item in sectors if item.get("state") in {"strong_mainwave", "important_industry"}]),
                "strongest_sector": strongest_sector.get("sector_name") or "",
                "strongest_sector_state": strongest_sector.get("state_label") or "",
                "index_mom60": index_mom60,
                "mainwave_dynamic_cooldown": mainwave_dynamic_cooldown,
                "mainwave_dynamic_cooldown_active": bool(mainwave_dynamic_cooldown.get("cooldown_active", False)),
                "mainwave_dynamic_cooldown_reason": mainwave_dynamic_cooldown.get("cooldown_reason") or "",
                "mainwave_recovery_signal_ok": bool(mainwave_dynamic_cooldown.get("recovery_signal_ok", False)),
                "index_heat_label": "超过主升门槛，仅观察" if index_mom60 is not None and index_mom60 > 0.05 else "正常热度",
                "min_score": _to_float_or_none(source_meta.get("min_score")) or 88.0,
                "min_sector_signal_count": _to_int_or_zero(source_meta.get("min_sector_signal_count")) or 2,
                "max_index_mom60": _to_float_or_none(source_meta.get("max_index_mom60")) or 0.05,
                "scan_target_date": scan.get("target_date") or "",
                "scan_source_mode": scan.get("source_mode") or "",
                "template_pool_rows": _to_int_or_zero(source_meta.get("template_pool_rows")),
                "pre_confirm_rows": _to_int_or_zero(source_meta.get("pre_confirm_rows") or source_meta.get("rows")),
            },
            "sector_opportunities": sectors,
            "sector_kline_by_name": sector_kline_by_name,
            "candidates": candidates,
            "watch_candidates": watch_candidates,
            "sector_watch_candidates": sector_watch_candidates,
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
                "mainwave_watch_pool": _path_status(MAINWAVE_REPORT_DIR / "current_wave_pool_top500.csv"),
                "current_wave_top_candidates": _path_status(CURRENT_WAVE_SCAN_DIR / "top_candidates.csv"),
                "current_wave_template_pass": _path_status(CURRENT_WAVE_SCAN_DIR / "template_pass_candidates.csv"),
            },
        }
    )


def _historical_reference_provenance(
    rows: list[dict[str, Any]],
    formal_contract: dict[str, Any],
) -> dict[str, Any]:
    """Keep legacy backtest samples distinct from the frozen formal contract."""
    canonical = formal_contract.get("canonical_contract") or {}
    formal_trade_strategy = str(canonical.get("trade_strategy") or "").strip()
    strategy_counts: dict[str, int] = {}
    for row in rows:
        strategy = str(row.get("trade_strategy") or row.get("route_strategy") or row.get("route") or "unknown").strip()
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

    total_count = len(rows)
    formal_trade_count = strategy_counts.get(formal_trade_strategy, 0) if formal_trade_strategy else 0
    formal_history_available = formal_trade_count > 0
    source_strategies = sorted(strategy_counts)
    return {
        "classification": "formal_contract_history" if formal_history_available and formal_trade_count == total_count else "legacy_reference_history",
        "formal_trade_strategy": formal_trade_strategy or None,
        "formal_trade_count": formal_trade_count,
        "total_trade_count": total_count,
        "formal_contract_history_available": formal_history_available,
        "source_strategies": source_strategies,
        "source_strategy_counts": strategy_counts,
        "message": (
            "当前筛选结果全部来自冻结的正式合同。"
            if formal_history_available and formal_trade_count == total_count
            else "当前历史样本用于参考归因，包含非 Score88 的旧策略记录；不得将其收益、胜率或夏普表述为 Score88 正式合同实绩。"
        ),
    }


def _score88_replay_summary() -> dict[str, Any]:
    """Expose the frozen Score88 research replay without treating it as live performance."""
    payload = _read_json(SCORE88_REPLAY_SUMMARY_PATH) or {}
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        return {
            "available": False,
            "classification": "research_replay_not_available",
            "message": "Score88 独立历史复现工件不可用；不展示替代收益指标。",
            "artifacts": {"summary": _path_status(SCORE88_REPLAY_SUMMARY_PATH)},
        }
    metrics = payload.get("two_slot") or {}
    audit: dict[str, Any] = {"available": False}
    closed_trades_path = SCORE88_REPLAY_DIR / "two_slot_closed_trades.csv"
    try:
        trades = pd.read_csv(closed_trades_path, usecols=["entry_date", "net_ret", "exit_reason"])
        trades["entry_date"] = pd.to_datetime(trades["entry_date"], errors="coerce")
        trades["net_ret"] = pd.to_numeric(trades["net_ret"], errors="coerce")
        trades = trades.dropna(subset=["entry_date", "net_ret"])

        def summarize(frame: pd.DataFrame) -> dict[str, Any]:
            returns = frame["net_ret"]
            wins = returns[returns > 0]
            losses = returns[returns < 0]
            return {
                "trade_count": int(len(frame)),
                "win_rate": float((returns > 0).mean()) if len(frame) else None,
                "avg_trade_return": float(returns.mean()) if len(frame) else None,
                "payoff_ratio": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
                "worst_trade": float(returns.min()) if len(frame) else None,
            }

        audit = {
            "available": True,
            "windows": {
                "2020_2021": summarize(trades[trades["entry_date"] < "2022-01-01"]),
                "2022_bear": summarize(trades[trades["entry_date"].between("2022-01-01", "2022-12-31")]),
                "2023_2024": summarize(trades[trades["entry_date"].between("2023-01-01", "2024-12-31")]),
                "2025_2026ytd": summarize(trades[trades["entry_date"] >= "2025-01-01"]),
                "blind_2026ytd": summarize(trades[trades["entry_date"] >= "2026-01-01"]),
            },
            "exit_counts": {str(key): int(value) for key, value in trades["exit_reason"].value_counts().items()},
        }
    except Exception as exc:
        logger.warning(f"Score88 replay audit unavailable: {exc}")
    return {
        "available": True,
        "classification": "research_only_ohlc_proxy",
        "strategy_id": STRATEGY_ID,
        "trade_strategy": "institutional_mainwave_score88",
        "contract": payload.get("contract"),
        "metrics": {
            "trade_count": metrics.get("trades"),
            "win_rate": metrics.get("win_rate"),
            "avg_trade_return": metrics.get("avg_ret"),
            "payoff_ratio": metrics.get("payoff_ratio"),
            "worst_trade": metrics.get("worst_trade"),
            "slot_skipped": payload.get("slot_skipped"),
        },
        "limitations": payload.get("limitations"),
        "audit": audit,
        "message": "Score88 独立复现仅用于研究验证；采用 OHLC 代理，未包含逐笔滑点、涨跌停成交和真实订单回报。",
        "artifacts": {
            "summary": _path_status(SCORE88_REPLAY_SUMMARY_PATH),
            "closed_trades": _path_status(SCORE88_REPLAY_DIR / "two_slot_closed_trades.csv"),
            "confirmed_tickets": _path_status(SCORE88_REPLAY_DIR / "m30_confirmed_tickets.csv"),
        },
    }


def _read_score88_replay_trades(limit: int, sort_order: str) -> dict[str, Any]:
    """Rows for the history page; always labelled research-only, never live proof."""
    path = SCORE88_REPLAY_DIR / "two_slot_closed_trades.csv"
    if not path.exists():
        return {"rows": [], "metrics": {"trade_count": 0}, "artifacts": {"closed_trades": _path_status(path)}}
    frame = pd.read_csv(path, low_memory=False)
    for column in ("entry_date", "exit_date"):
        frame[column] = pd.to_datetime(frame.get(column), errors="coerce").dt.strftime("%Y-%m-%d")
    frame["name"] = frame.get("stock_name", frame.get("code", "")).fillna("")
    frame["policy_exit_date"] = frame["exit_date"]
    frame["route"] = "institutional_mainwave_score88"
    frame["route_label"] = "Score88 主升复现"
    frame["route_strategy"] = "institutional_mainwave_score88"
    frame["route_strategy_label"] = "Score88（研究复现）"
    frame["score"] = pd.to_numeric(frame.get("wave_style_score"), errors="coerce")
    frame["slot_pct"] = 0.50
    frame["trade_status"] = "closed"
    frame["formal_buy_signal"] = False
    frame["live_ready"] = False
    frame["block_reason"] = "Score88 历史 OHLC 研究复现；非实盘、非纸面成交"
    frame["confirm_datetime"] = frame.get("entry_datetime")
    frame["realized_pnl"] = None
    frame = frame.sort_values("entry_date", ascending=sort_order == "asc")
    returns = pd.to_numeric(frame.get("net_ret"), errors="coerce")
    rows = frame.head(limit).where(pd.notna(frame), None).to_dict("records")
    return {
        "rows": rows,
        "metrics": {
            "trade_count": int(len(frame)),
            "win_rate": float((returns > 0).mean()) if len(frame) else None,
            "avg_trade_return": float(returns.mean()) if len(frame) else None,
            "payoff_ratio": _score88_replay_summary().get("metrics", {}).get("payoff_ratio"),
        },
        "artifacts": {"closed_trades": _path_status(path)},
    }


@router.get("/historical-trades")
async def get_gen3_state_alpha_historical_trades(
    limit: int = Query(default=200, ge=1, le=2000, description="Maximum closed trade rows returned."),
    route: str | None = Query(default="all", description="Route filter: all, institutional_mainwave, panic_repair, old_g3_route_v3."),
    window: str | None = Query(default="all", description="Window filter: all, pre_2024_09, post_2024_09, weak_2022, valid_2024, blind_2026ytd."),
    sort_by: str = Query(default="entry_date", description="Column used for sorting."),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$", description="Sort order."),
    dataset: str = Query(default="reference", pattern="^(reference|score88_replay)$"),
) -> dict[str, Any]:
    if dataset == "score88_replay":
        replay = _read_score88_replay_trades(limit, sort_order)
        return _wrap_guardrails({
            "ok": True,
            "mode": "g3_state_alpha_score88_research_replay",
            "dataset": "score88_replay",
            "dataset_label": "Score88 全历史研究复现（OHLC代理）",
            "research_only": True,
            "historical_trades": replay["rows"],
            "metrics": replay["metrics"],
            "route_metrics": [], "market_style_metrics": [], "equity_curve": [],
            "exposure_metrics": {}, "future_leak_audit": {}, "window_metrics": [],
            "filters": {"dataset": dataset, "limit": limit, "sort_order": sort_order},
            "score88_replay": _score88_replay_summary(),
            "artifacts": replay["artifacts"],
            "message": "仅研究复现：359笔闭合成交可逐笔查看，不代表实盘、纸面或正式Score88观察。",
        })
    historical = _read_historical_trades(
        limit=limit,
        route=route.strip() if isinstance(route, str) and route.strip() else "all",
        window=window.strip() if isinstance(window, str) and window.strip() else "all",
        sort_by=sort_by.strip() if isinstance(sort_by, str) and sort_by.strip() else "entry_date",
        sort_order=sort_order,
    )
    formal_contract = _contract()
    historical_reference = _historical_reference_provenance(historical["rows"], formal_contract)
    replacement_assessment = _build_replacement_assessment(historical)
    strategy_reduction_recovery = _read_strategy_reduction_recovery()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_historical_closed_trades",
            "historical_trades": historical["rows"],
            "historical_replay_candidates": historical.get("historical_replay_candidates") or [],
            "historical_replay_summary": historical.get("historical_replay_summary") or {},
            "metrics": historical["metrics"],
            "route_metrics": historical["route_metrics"],
            "market_style_metrics": historical["market_style_metrics"],
            "equity_curve": historical["equity_curve"],
            "exposure_metrics": historical["exposure_metrics"],
            "future_leak_audit": historical["future_leak_audit"],
            "window_metrics": historical["window_metrics"],
            "filters": historical["filters"],
            "strategy_contract": formal_contract,
            "historical_reference": historical_reference,
            "score88_replay": _score88_replay_summary(),
            "artifacts": historical["artifacts"],
            "replacement_assessment": replacement_assessment,
            "strategy_reduction_recovery": strategy_reduction_recovery,
        }
    )


@router.get("/historical-decision-replay-tasks")
async def get_gen3_state_alpha_historical_decision_replay_tasks(
    limit: int = Query(default=80, ge=1, le=500, description="Maximum historical replay tasks returned."),
    route: str | None = Query(default="all", description="Route filter."),
    window: str | None = Query(default="all", description="Window filter."),
) -> dict[str, Any]:
    return _wrap_guardrails(
        _build_g3_historical_decision_replay_tasks(
            limit=limit,
            route=route.strip() if isinstance(route, str) and route.strip() else "all",
            window=window.strip() if isinstance(window, str) and window.strip() else "all",
        )
    )


@router.get("/historical-decision-replay-audit")
async def get_gen3_state_alpha_historical_decision_replay_audit(
    route: str | None = Query(default="all", description="Route filter."),
    window: str | None = Query(default="all", description="Window filter."),
) -> dict[str, Any]:
    return _wrap_guardrails(
        _build_g3_historical_decision_replay_audit(
            route=route.strip() if isinstance(route, str) and route.strip() else "all",
            window=window.strip() if isinstance(window, str) and window.strip() else "all",
        )
    )


@router.get("/broker/holdings")
async def get_gen3_state_alpha_broker_holdings() -> dict[str, Any]:
    return _wrap_guardrails(_broker_snapshot())


def _normalize_tick_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    code = _normalize_code6(text)
    if len(code) != 6:
        return ""
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"


def _tick_time_text(value: Any) -> str:
    try:
        numeric = float(value)
        if numeric > 1_000_000_000_000:
            return pd.to_datetime(numeric, unit="ms", utc=True).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        pass
    return str(value or "")


def _tick_age_seconds(value: Any) -> float | None:
    try:
        numeric = float(value)
        if numeric > 1_000_000_000_000:
            tick_at = pd.to_datetime(numeric, unit="ms", utc=True)
            return round(float((pd.Timestamp.now(tz="UTC") - tick_at).total_seconds()), 1)
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _load_holding_tick_sector_members(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Load a small, liquid QMT-canonical industry peer sample for each holding."""
    if not codes:
        return {}
    try:
        from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists

        if not (clickhouse_table_exists("sectors") and clickhouse_table_exists("sector_stocks")):
            return {}
        quoted_codes = _quote_sql_list(codes)
        memberships = clickhouse_query_df(
            f"""
            SELECT ss.stock_code AS holding_code, ss.sector_code AS sector_code,
                   any(s.name) AS sector_name, any(s.level) AS sector_level
            FROM sector_stocks AS ss FINAL
            INNER JOIN sectors AS s FINAL ON s.code = ss.sector_code
            WHERE ss.stock_code IN ({quoted_codes})
              AND s.type = 'industry'
            GROUP BY ss.stock_code, ss.sector_code
            ORDER BY holding_code, sector_level DESC, sector_name ASC
            """
        )
        if memberships.empty:
            return {}
        picked: dict[str, dict[str, Any]] = {}
        for _, row in memberships.iterrows():
            holding_code = _normalize_tick_code(row.get("holding_code"))
            sector_code = str(row.get("sector_code") or "").strip()
            if holding_code and sector_code and holding_code not in picked:
                picked[holding_code] = {
                    "sector_code": sector_code,
                    "sector_name": str(row.get("sector_name") or "").strip(),
                    "sector_level": int(_to_float_or_none(row.get("sector_level")) or 0),
                    "peer_codes": [],
                    "peer_names": {},
                }
        sector_codes = sorted({str(item["sector_code"]) for item in picked.values()})
        if not sector_codes:
            return picked
        member_counts = clickhouse_query_df(
            f"""
            SELECT sector_code, count() AS member_count
            FROM sector_stocks FINAL
            WHERE sector_code IN ({_quote_sql_list(sector_codes)})
            GROUP BY sector_code
            """
        )
        member_count_by_sector = {
            str(row.get("sector_code") or ""): int(_to_float_or_none(row.get("member_count")) or 0)
            for _, row in member_counts.iterrows()
        }
        peer_rows = clickhouse_query_df(
            f"""
            SELECT ss.sector_code AS sector_code, ss.stock_code AS code,
                   anyOrNull(st.name) AS name,
                   argMax(kd.amount, kd.trade_date) AS recent_amount
            FROM sector_stocks AS ss FINAL
            LEFT JOIN stocks AS st FINAL ON st.code = ss.stock_code
            LEFT JOIN kline_daily AS kd FINAL ON kd.code = ss.stock_code
            WHERE ss.sector_code IN ({_quote_sql_list(sector_codes)})
            GROUP BY ss.sector_code, ss.stock_code
            ORDER BY sector_code, recent_amount DESC, code ASC
            LIMIT 12 BY sector_code
            """
        )
        peers_by_sector: dict[str, list[dict[str, str]]] = {}
        for _, row in peer_rows.iterrows():
            sector_code = str(row.get("sector_code") or "").strip()
            code = _normalize_tick_code(row.get("code"))
            if sector_code and code:
                peers_by_sector.setdefault(sector_code, []).append(
                    {"code": code, "name": str(row.get("name") or "").strip()}
                )
        for holding_code, item in picked.items():
            peers = [peer for peer in peers_by_sector.get(str(item["sector_code"]), []) if peer["code"] != holding_code]
            item["peer_codes"] = [peer["code"] for peer in peers[:10]]
            item["peer_names"] = {peer["code"]: peer["name"] for peer in peers[:10]}
            item["sector_member_count"] = member_count_by_sector.get(str(item["sector_code"]), 0)
        return picked
    except Exception as exc:
        logger.warning("Failed to load holding tick sector peers: %s", exc)
        return {}


def _tick_change_pct(raw: Any) -> float | None:
    if not isinstance(raw, dict):
        return None
    last = _parse_broker_number(raw.get("lastPrice"))
    previous_close = _parse_broker_number(raw.get("lastClose"))
    if last is None or last <= 0 or previous_close is None or previous_close <= 0:
        return None
    return (last / previous_close - 1.0) * 100.0


def _summarize_holding_tick_sector(
    holding_code: str,
    context: dict[str, Any],
    ticks: dict[str, Any],
) -> dict[str, Any]:
    peer_codes = list(context.get("peer_codes") or [])
    changes = [(code, _tick_change_pct(ticks.get(code))) for code in peer_codes]
    usable = [(code, value) for code, value in changes if value is not None]
    if len(usable) < 4:
        return {
            "sector_name": context.get("sector_name") or "未识别行业",
            "sector_code": context.get("sector_code") or "",
            "peer_sample_size": len(usable),
            "sector_member_count": int(_to_float_or_none(context.get("sector_member_count")) or 0),
            "sector_follow_state": "样本不足",
            "sector_follow_label": "板块样本不足，不能用跟随关系判断",
            "peer_changes": [],
        }
    values = [value for _, value in usable]
    median_change = float(pd.Series(values).median())
    positive_ratio = sum(1 for value in values if value > 0) / len(values)
    leader_rows = sorted(usable, key=lambda item: item[1], reverse=True)[:3]
    peer_names = context.get("peer_names") or {}
    peer_changes = [
        {"code": code, "name": peer_names.get(code) or code, "change_pct": value}
        for code, value in leader_rows
    ]
    holding_change = _tick_change_pct(ticks.get(holding_code))
    relative_strength = holding_change - median_change if holding_change is not None else None
    if positive_ratio >= 0.6 and median_change >= 0:
        state, label = "同步增强", "板块扩散偏强，可作为个股走势的确认"
    elif positive_ratio <= 0.4 and median_change <= 0:
        state, label = "同步走弱", "板块扩散偏弱，不支持逆势补仓"
    else:
        state, label = "分化", "板块内部不一致，跟随证据不足"
    return {
        "sector_name": context.get("sector_name") or "未识别行业",
        "sector_code": context.get("sector_code") or "",
        "peer_sample_size": len(usable),
        "sector_member_count": int(_to_float_or_none(context.get("sector_member_count")) or 0),
        "peer_positive_ratio": positive_ratio,
        "peer_median_change_pct": median_change,
        "relative_strength_pct": relative_strength,
        "sector_follow_state": state,
        "sector_follow_label": label,
        "peer_changes": peer_changes,
    }


def _read_qmt_ticks(codes: list[str], source: dict[str, Any]) -> dict[str, Any]:
    """Read ticks through the host bridge when the API runs inside Docker."""
    bridge_url = str(os.getenv("AISTOCK_QMT_TICK_BRIDGE_URL", "http://host.docker.internal:8766/full-tick")).strip()
    if bridge_url:
        try:
            response = requests.get(bridge_url, params={"codes": ",".join(codes)}, timeout=8)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or not body.get("ok") or not isinstance(body.get("ticks"), dict):
                raise RuntimeError(str((body or {}).get("message") or "invalid bridge response"))
            source["provider"] = "QMT xtdata host bridge"
            source["bridge_url"] = bridge_url
            return body["ticks"]
        except Exception as exc:
            source["bridge_error"] = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(f"QMT Tick host bridge unavailable: {source['bridge_error']}") from exc
    try:
        from xtquant import xtdata  # type: ignore

        host = source["host"]
        port = source["port"]
        xtdata.enable_hello = False
        xtdata.connect(host, port)
        source["provider"] = "QMT xtdata local runtime"
        return xtdata.get_full_tick(codes) or {}
    except Exception as exc:
        raise RuntimeError(f"QMT Tick local runtime unavailable: {type(exc).__name__}: {exc}") from exc


def _rsi_from_closes(closes: list[Any], period: int = 14) -> float | None:
    values = pd.to_numeric(pd.Series(closes), errors="coerce").dropna()
    if len(values) < period + 1:
        return None
    delta = values.diff()
    gains = delta.clip(lower=0)
    losses = (-delta.clip(upper=0))
    average_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    average_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    if pd.isna(average_gain) or pd.isna(average_loss):
        return None
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return round(float(100 - (100 / (1 + (average_gain / average_loss)))), 2)


def _load_holding_tick_minute_metrics(codes: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, float | None]]]:
    minute_by_code: dict[str, dict[str, Any]] = {}
    rsi_by_code: dict[str, dict[str, float | None]] = {code: {"rsi_5m_14": None, "rsi_15m_14": None} for code in codes}
    if not codes:
        return minute_by_code, rsi_by_code
    try:
        from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists

        quoted = _quote_sql_list(codes)
        if clickhouse_table_exists("kline_minute_5"):
            frame = clickhouse_query_df(
                f"""
                SELECT code, max(datetime) AS latest_5m_at, argMax(close, datetime) AS last_5m_close,
                       sum(amount) / nullIf(sum(volume), 0) AS vwap_5m
                FROM kline_minute_5 FINAL
                WHERE code IN ({quoted}) AND toDate(datetime) = today()
                GROUP BY code
                """
            )
            minute_by_code = {str(row.code).upper(): row._asdict() for row in frame.itertuples(index=False)}

        for period_name, table_name, result_key in [
            ("5m", "kline_minute_5", "rsi_5m_14"),
            ("15m", "kline_minute_15", "rsi_15m_14"),
        ]:
            if not clickhouse_table_exists(table_name):
                continue
            raw = clickhouse_query_df(
                f"""
                SELECT code, datetime, close
                FROM {table_name} FINAL
                WHERE code IN ({quoted})
                ORDER BY code, datetime DESC
                LIMIT 80 BY code
                """
            )
            if raw.empty:
                continue
            for code, rows in raw.groupby(raw["code"].astype(str).str.upper(), dropna=False):
                rsi_by_code.setdefault(str(code), {})[result_key] = _rsi_from_closes(rows.sort_values("datetime")["close"].tolist())
    except Exception as exc:
        logger.warning("Failed to load holding tick minute metrics: %s", exc)
    return minute_by_code, rsi_by_code


def _load_stock_names(codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    try:
        from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists

        if not clickhouse_table_exists("stocks"):
            return {}
        raw = clickhouse_query_df(
            f"""
            SELECT code, any(name) AS name
            FROM stocks
            WHERE code IN ({_quote_sql_list(codes)})
            GROUP BY code
            """
        )
        return {
            _normalize_tick_code(row.get("code")): str(row.get("name") or "").strip()
            for _, row in raw.iterrows()
            if _normalize_tick_code(row.get("code")) and str(row.get("name") or "").strip()
        }
    except Exception as exc:
        logger.warning("Failed to load holding tick stock names: %s", exc)
        return {}


def _holding_tick_display_name(code: str, holding: dict[str, Any], stock_names: dict[str, str]) -> str:
    """Ignore broker placeholder names that merely repeat the stock code."""
    candidate = str(holding.get("name") or holding.get("stock_name") or "").strip()
    if candidate and _normalize_code6(candidate) == _normalize_code6(code):
        candidate = ""
    return candidate or str(stock_names.get(code) or "").strip()


def _read_holding_tick_watchlist() -> dict[str, Any]:
    """Return the user-entered holding Tick watchlist kept outside the repository."""
    payload = _read_json(HOLDING_TICK_WATCHLIST_PATH)
    codes = payload.get("codes") if isinstance(payload, dict) else []
    normalized = list(dict.fromkeys(_normalize_tick_code(code) for code in codes if _normalize_tick_code(code))) if isinstance(codes, list) else []
    return {
        "codes": normalized,
        "updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
    }


def _save_holding_tick_watchlist(raw_codes: Any) -> dict[str, Any]:
    if isinstance(raw_codes, str):
        candidates = re.split(r"[,，\s]+", raw_codes)
    elif isinstance(raw_codes, list):
        candidates = raw_codes
    else:
        candidates = []
    codes = list(dict.fromkeys(_normalize_tick_code(code) for code in candidates if _normalize_tick_code(code)))
    if len(codes) > 100:
        raise ValueError("holding Tick watchlist supports at most 100 stock codes")
    payload = {
        "codes": codes,
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
    }
    _write_json(HOLDING_TICK_WATCHLIST_PATH, payload)
    return payload


def _read_holding_tick_analysis(codes_text: str | None = None) -> dict[str, Any]:
    broker = _broker_snapshot()
    holdings = broker.get("holdings") if isinstance(broker.get("holdings"), list) else []
    holding_by_code = {
        _normalize_tick_code(row.get("code")): row
        for row in holdings
        if isinstance(row, dict) and _normalize_tick_code(row.get("code"))
    }
    requested = [item.strip() for item in str(codes_text or "").split(",") if item.strip()]
    codes = list(dict.fromkeys(_normalize_tick_code(item) for item in requested)) if requested else list(holding_by_code)
    codes = [code for code in codes if code]
    payload: dict[str, Any] = {
        "ok": True,
        "mode": "read_only_holding_tick_analysis",
        "order_path_enabled": False,
        "broker_updated_at": broker.get("updated_at"),
        "requested_codes": codes,
        "items": [],
        "warnings": [],
        "source": {"provider": "QMT xtdata", "host": os.getenv("AISTOCK_QMT_TICK_HOST", "127.0.0.1"), "port": int(os.getenv("AISTOCK_QMT_TICK_PORT", "58610"))},
    }
    if not codes:
        payload["warnings"].append("账户持仓快照为空；可在页面手动输入以逗号分隔的股票代码。")
        return payload

    stock_name_by_code = _load_stock_names(codes)
    sector_context_by_code = _load_holding_tick_sector_members(codes)
    peer_codes = sorted(
        {
            peer_code
            for context in sector_context_by_code.values()
            for peer_code in context.get("peer_codes") or []
            if peer_code not in codes
        }
    )

    try:
        ticks = _read_qmt_ticks(codes + peer_codes, payload["source"])
        _mark_holding_tick_qmt_available(codes, payload["source"])
    except Exception as exc:
        payload["ok"] = False
        payload["warnings"].append(f"QMT Tick 不可用：{type(exc).__name__}: {exc}")
        payload["alert"] = _notify_holding_tick_qmt_unavailable(codes, payload["source"], exc)
        return payload

    minute_by_code, rsi_by_code = _load_holding_tick_minute_metrics(codes)
    daily_bars_by_code = _load_daily_trend_bars(codes, 180)
    today_text = datetime.now().strftime("%Y-%m-%d")

    for code in codes:
        raw = ticks.get(code) if isinstance(ticks, dict) else None
        if not isinstance(raw, dict):
            payload["items"].append({"code": code, "available": False, "diagnosis": "行情缺失", "anomalies": ["未收到 Tick 快照"]})
            continue
        last = _parse_broker_number(raw.get("lastPrice")) or 0.0
        open_price = _parse_broker_number(raw.get("open")) or 0.0
        high = _parse_broker_number(raw.get("high")) or 0.0
        low = _parse_broker_number(raw.get("low")) or 0.0
        previous_close = _parse_broker_number(raw.get("lastClose")) or 0.0
        bid_volumes = [float(item or 0) for item in (raw.get("bidVol") or [])[:5]]
        ask_volumes = [float(item or 0) for item in (raw.get("askVol") or [])[:5]]
        bid_total, ask_total = sum(bid_volumes), sum(ask_volumes)
        imbalance = (bid_total - ask_total) / (bid_total + ask_total) if bid_total + ask_total else None
        bid1 = _parse_broker_number((raw.get("bidPrice") or [None])[0])
        ask1 = _parse_broker_number((raw.get("askPrice") or [None])[0])
        change_pct = (last / previous_close - 1.0) * 100 if last > 0 and previous_close > 0 else None
        tick_age_seconds = _tick_age_seconds(raw.get("time"))
        minute = minute_by_code.get(code, {})
        rsi = rsi_by_code.get(code, {})
        daily_bars = daily_bars_by_code.get(code, [])
        trend_line = evaluate_intraday_rising_trend_line(
            daily_bars,
            last,
            current_session_in_daily_bars=bool(daily_bars and str(daily_bars[-1].get("trade_date") or "")[:10] == today_text),
        )
        sector = _summarize_holding_tick_sector(code, sector_context_by_code[code], ticks) if code in sector_context_by_code else {
            "sector_name": "未识别行业",
            "sector_code": "",
            "peer_sample_size": 0,
            "sector_follow_state": "未映射",
            "sector_follow_label": "未找到QMT行业成分，不能判断板块跟随",
            "peer_changes": [],
        }
        vwap = _parse_broker_number(minute.get("vwap_5m"))
        near_low = last > 0 and low > 0 and last <= low * 1.003
        below_vwap = last > 0 and vwap is not None and last < vwap
        anomalies: list[str] = []
        if near_low:
            anomalies.append("价格贴近日内低点")
        if below_vwap:
            anomalies.append("低于5分钟VWAP")
        if imbalance is not None and imbalance <= -0.35:
            anomalies.append("五档卖盘显著占优")
        if bid1 is not None and ask1 is not None and last > 0 and (ask1 - bid1) / last > 0.003:
            anomalies.append("买卖价差异常扩大")
        if trend_line.get("intraday_trend_broken"):
            anomalies.append("盘中跌破日线主升趋势线")
        sector_state = str(sector.get("sector_follow_state") or "")
        relative_strength = _to_float_or_none(sector.get("relative_strength_pct"))
        if tick_age_seconds is not None and tick_age_seconds > 20:
            anomalies.append(f"Tick 已延迟 {int(tick_age_seconds)} 秒")
            diagnosis, level = "行情过期：仅保留观察，不生成动作", "info"
        elif sector_state == "同步走弱" and (below_vwap or near_low):
            diagnosis, level = "风险复核：个股与板块同步偏弱", "danger"
        elif sector_state == "同步增强" and relative_strength is not None and relative_strength >= 0.8 and last > open_price > 0:
            diagnosis, level = "共振偏强：不因单笔Tick急于高抛", "success"
        elif sector_state in {"分化", "同步走弱"} and relative_strength is not None and relative_strength >= 1.0:
            diagnosis, level = "个股领先但板块未确认：关注冲高回落", "warning"
        elif sector_state == "同步增强" and relative_strength is not None and relative_strength <= -0.8:
            diagnosis, level = "板块偏强但个股落后：等待重新跟随", "warning"
        elif near_low and below_vwap and (imbalance is None or imbalance <= 0):
            diagnosis, level = "风控观察：破低后缺少承接确认", "danger"
        elif below_vwap or (imbalance is not None and imbalance <= -0.35):
            diagnosis, level = "弱势观察：等待5分钟结构确认", "warning"
        elif imbalance is not None and imbalance >= 0.35 and last > open_price > 0:
            diagnosis, level = "承接观察：尚需5分钟确认", "success"
        else:
            diagnosis, level = "中性观察：Tick未形成单独交易动作", "info"
        if trend_line.get("intraday_trend_broken") and not (tick_age_seconds is not None and tick_age_seconds > 20):
            diagnosis, level = "盘中跌破日线主升趋势线：卖出/换股复核", "danger"
        holding = holding_by_code.get(code, {})
        payload["items"].append(
            {
                "code": code,
                "name": _holding_tick_display_name(code, holding, stock_name_by_code),
                "available": last > 0,
                "tick_time": _tick_time_text(raw.get("time") or raw.get("timetag")),
                "tick_age_seconds": tick_age_seconds,
                "tick_fresh": tick_age_seconds is None or tick_age_seconds <= 20,
                "last_price": last or None,
                "open": open_price or None,
                "high": high or None,
                "low": low or None,
                "previous_close": previous_close or None,
                "change_pct": change_pct,
                "vwap_5m": vwap,
                "last_5m_at": str(minute.get("latest_5m_at") or ""),
                "rsi_5m_14": rsi.get("rsi_5m_14"),
                "rsi_15m_14": rsi.get("rsi_15m_14"),
                "bid1": bid1,
                "ask1": ask1,
                "bid5_volume": bid_total,
                "ask5_volume": ask_total,
                "order_book_imbalance": imbalance,
                "speed_1m": _parse_broker_number(raw.get("speed1Min")),
                "speed_5m": _parse_broker_number(raw.get("speed5Min")),
                "daily_trend": trend_line,
                **sector,
                "shares": holding.get("shares"),
                "cost_price": holding.get("cost_price"),
                "diagnosis": diagnosis,
                "level": level,
                "anomalies": anomalies,
            }
        )
    return payload


@router.get("/holding-tick-watchlist")
async def get_gen3_state_alpha_holding_tick_watchlist() -> dict[str, Any]:
    """Load manually entered Tick symbols; quote data itself is never replayed."""
    return _wrap_guardrails({"ok": True, **_read_holding_tick_watchlist(), "order_path_enabled": False})


@router.put("/holding-tick-watchlist")
async def put_gen3_state_alpha_holding_tick_watchlist(
    payload: dict[str, Any] = Body(..., description="Manual holding Tick watchlist"),
) -> dict[str, Any]:
    try:
        saved = _save_holding_tick_watchlist(payload.get("codes"))
    except ValueError as exc:
        return _wrap_guardrails({"ok": False, "message": str(exc), "order_path_enabled": False})
    return _wrap_guardrails({"ok": True, **saved, "order_path_enabled": False})


@router.get("/holding-tick-analysis")
async def get_gen3_state_alpha_holding_tick_analysis(
    codes: str | None = Query(default=None, description="Comma-separated QMT stock codes; defaults to the broker holdings snapshot."),
) -> dict[str, Any]:
    return _wrap_guardrails(_read_holding_tick_analysis(codes))


@router.get("/holding-tick-analysis/chart/{code}")
async def get_gen3_state_alpha_holding_tick_chart(code: str) -> dict[str, Any]:
    normalized = _normalize_tick_code(code)
    if not normalized:
        return _wrap_guardrails({"ok": False, "message": "invalid stock code", "items": {}})
    try:
        from utils.market_warehouse import clickhouse_query_df, clickhouse_table_exists

        specs = {
            "1d": ("kline_daily", "trade_date", 180),
            "60m": ("kline_minute_60", "datetime", 160),
            "15m": ("kline_minute_15", "datetime", 160),
            "5m": ("kline_minute_5", "datetime", 160),
        }
        charts: dict[str, list[dict[str, Any]]] = {}
        for period, (table, time_col, limit) in specs.items():
            if not clickhouse_table_exists(table):
                charts[period] = []
                continue
            source_table = f"{table} FINAL" if period == "1d" else table
            frame = clickhouse_query_df(
                f"SELECT {time_col} AS time, open, high, low, close, volume FROM {source_table} "
                f"WHERE code = '{normalized}' ORDER BY {time_col} DESC LIMIT {limit}"
            )
            records = list(reversed(frame.to_dict(orient="records"))) if not frame.empty else []
            for record in records:
                stamp = pd.Timestamp(record["time"])
                # The chart client consumes an explicit epoch value for minute
                # bars, avoiding browser-specific parsing of ISO DateTime.
                record["unix_time"] = int(stamp.value // 1_000_000_000)
                record["time"] = stamp.strftime("%Y-%m-%d" if period == "1d" else "%Y-%m-%d %H:%M:%S")
            charts[period] = records
        daily_trend = evaluate_intraday_rising_trend_line(charts["1d"], None)
        return _wrap_guardrails({
            "ok": True,
            "mode": "read_only_holding_tick_chart",
            "code": normalized,
            "charts": charts,
            "daily_trend": daily_trend,
            "order_path_enabled": False,
        })
    except Exception as exc:
        logger.warning("Failed to load holding tick charts for %s: %s", normalized, exc)
        return _wrap_guardrails({"ok": False, "code": normalized, "charts": {}, "message": str(exc), "order_path_enabled": False})


@router.get("/holding-t/review")
async def get_gen3_holding_t_review(
    trade_date: str | None = Query(default=None, description="Trading date in YYYY-MM-DD; defaults to today."),
) -> dict[str, Any]:
    """Return the persisted after-close review, or a read-only preview before 16:00."""
    date_text = str(trade_date or datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat())
    path = HOLDING_T_REVIEW_DIR / f"{date_text}.json"
    review = _read_json(path) if path.exists() else run_daily_review(date_text)
    return _wrap_guardrails({"ok": True, **review, "order_path_enabled": False})


@router.post("/holding-t/manual-execution")
async def post_gen3_holding_t_manual_execution(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Store a user-entered fill for attribution; it never talks to a broker."""
    try:
        saved = record_manual_execution(payload)
        review = run_daily_review(str(saved.get("trade_date") or ""))
        return _wrap_guardrails({"ok": True, "execution": saved, "review": review, "order_path_enabled": False})
    except ValueError as exc:
        return _wrap_guardrails({"ok": False, "message": str(exc), "order_path_enabled": False})


@router.get("/holding-t/portfolio-state")
async def get_gen3_holding_t_portfolio_state() -> dict[str, Any]:
    return _wrap_guardrails({"ok": True, "state": load_holding_t_portfolio_state(), "order_path_enabled": False})


@router.post("/holding-t/portfolio-confirmation")
async def post_gen3_holding_t_portfolio_confirmation(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        result = confirm_position_action(payload)
        return _wrap_guardrails({"ok": True, **result, "order_path_enabled": False})
    except ValueError as exc:
        return _wrap_guardrails({"ok": False, "message": str(exc), "order_path_enabled": False})

@router.put("/holding-t/portfolio-state")
async def put_gen3_holding_t_portfolio_state(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else None
    if state is None: return _wrap_guardrails({"ok": False, "message": "missing state", "order_path_enabled": False})
    return _wrap_guardrails({"ok": True, "state": save_holding_t_portfolio_state(state), "order_path_enabled": False})


@router.get("/broker/holdings/sync-ths-preflight")
async def get_gen3_state_alpha_broker_holdings_sync_preflight() -> dict[str, Any]:
    return _wrap_guardrails(_broker_holdings_sync_preflight())


@router.get("/broker/holdings/sync-ths-confirmation-packet")
async def get_gen3_state_alpha_broker_holdings_sync_confirmation_packet() -> dict[str, Any]:
    return _wrap_guardrails(_broker_holdings_sync_confirmation_packet())


@router.get("/broker/holdings/sync-ths-outcome")
async def get_gen3_state_alpha_broker_holdings_sync_outcome() -> dict[str, Any]:
    return _wrap_guardrails(_broker_holdings_sync_outcome_verifier())


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


def _qmtmini_position_to_broker_holding(row: dict[str, Any]) -> dict[str, Any]:
    code = str(row.get("stock_code") or row.get("code") or "").strip().upper()
    code6 = _normalize_code6(code)
    shares = int(_parse_broker_number(row.get("volume")) or 0)
    available_shares = int(_parse_broker_number(row.get("can_use_volume")) or 0)
    cost_price = (
        _parse_broker_number(row.get("avg_price"))
        or _parse_broker_number(row.get("cost_price"))
        or _parse_broker_number(row.get("open_price"))
    )
    current_price = _parse_broker_number(row.get("last_price"))
    market_value = _parse_broker_number(row.get("market_value"))
    if current_price is None and market_value is not None and shares > 0:
        current_price = market_value / shares
    return {
        "code": code6,
        "code_raw": code,
        "name": row.get("stock_name") or code6,
        "shares": shares,
        "available_shares": available_shares,
        "cost_price": cost_price,
        "current_price": current_price,
        "market_value": market_value,
        "position_cost": _parse_broker_number(row.get("position_cost")),
        "route": "broker_real_position",
        "route_label": "QMT Mini real position",
        "trade_status": "broker_open",
        "management_action": "check_exit_contract",
        "exit_contract": "QMT Mini read-only position snapshot; managed by G3 12% hard stop, 12% half take-profit, and remaining-position protection.",
        "source": "qmtmini_readonly_snapshot",
    }


def _read_qmtmini_capital_holdings() -> dict[str, Any]:
    try:
        from data_fetcher.sources.qmtmini_client import QmtMiniTradingClient
    except Exception as exc:
        return {
            "ok": False,
            "source": "qmtmini_readonly_snapshot",
            "message": f"QMT Mini client unavailable: {exc}",
        }
    client = QmtMiniTradingClient()
    try:
        connect_status = client.connect()
        if not connect_status.get("ok"):
            return {
                "ok": False,
                "source": "qmtmini_readonly_snapshot",
                "message": f"QMT Mini trading connect failed: {connect_status}",
                "connect": connect_status,
            }
        snapshot = client.account_snapshot(include_sensitive=True)
    except Exception as exc:
        return {
            "ok": False,
            "source": "qmtmini_readonly_snapshot",
            "message": f"Failed to read QMT Mini account snapshot: {exc}",
        }
    finally:
        client.close()

    asset = snapshot.get("asset") if isinstance(snapshot.get("asset"), dict) else {}
    raw_positions = [
        item
        for item in (snapshot.get("positions") or [])
        if isinstance(item, dict) and _normalize_code6(item.get("stock_code") or item.get("code"))
    ]
    holdings = [
        item
        for item in (_qmtmini_position_to_broker_holding(row) for row in raw_positions)
        if item.get("code") and int(item.get("shares") or 0) > 0
    ]
    capital = {
        "fund_balance": _parse_broker_number(asset.get("total_asset")),
        "available_cash": _parse_broker_number(asset.get("cash")),
        "withdrawable_cash": _parse_broker_number(asset.get("cash")),
        "frozen_cash": _parse_broker_number(asset.get("frozen_cash")),
        "market_value": _parse_broker_number(asset.get("market_value")),
        "total_capital": _parse_broker_number(asset.get("total_asset")),
        "holding_market_value": _parse_broker_number(asset.get("market_value")),
    }
    return {
        "ok": bool(snapshot.get("ok")),
        "source": "qmtmini_readonly_snapshot",
        "message": "QMT Mini read-only account snapshot synced.",
        "account_id": snapshot.get("account_id"),
        "positions_count": snapshot.get("positions_count"),
        "orders_count": snapshot.get("orders_count"),
        "trades_count": snapshot.get("trades_count"),
        "holdings": holdings,
        "capital": capital,
        "qmtmini_snapshot": {
            "asset_available": snapshot.get("asset_available"),
            "positions_count": snapshot.get("positions_count"),
            "orders_count": snapshot.get("orders_count"),
            "trades_count": snapshot.get("trades_count"),
        },
    }


def _normalize_qmtmini_trade_time(value: Any) -> str | None:
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    try:
        if len(digits) >= 14:
            return pd.Timestamp(
                f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:{digits[12:14]}"
            ).strftime("%Y-%m-%d %H:%M:%S")
        if len(digits) == 8:
            return pd.Timestamp(f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}").strftime("%Y-%m-%d 00:00:00")
        if text:
            return pd.Timestamp(text).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None
    return None


def _parse_qmtmini_trade_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"23", "stock_buy"} or "buy" in text or "买" in text:
        return "BUY"
    if text in {"24", "stock_sell"} or "sell" in text or "卖" in text:
        return "SELL"
    return _parse_trade_side(value)


def _read_qmtmini_trades() -> dict[str, Any]:
    try:
        from data_fetcher.sources.qmtmini_client import QmtMiniTradingClient
    except Exception as exc:
        return {
            "ok": False,
            "source": "qmtmini_readonly_snapshot",
            "message": f"QMT Mini client unavailable: {exc}",
        }
    client = QmtMiniTradingClient()
    try:
        connect_status = client.connect()
        if not connect_status.get("ok"):
            return {
                "ok": False,
                "source": "qmtmini_readonly_snapshot",
                "message": f"QMT Mini trading connect failed: {connect_status}",
                "connect": connect_status,
            }
        snapshot = client.account_snapshot(include_sensitive=True)
    except Exception as exc:
        return {
            "ok": False,
            "source": "qmtmini_readonly_snapshot",
            "message": f"Failed to read QMT Mini trades: {exc}",
        }
    finally:
        client.close()

    rows: list[dict[str, Any]] = []
    for item in snapshot.get("trades") or []:
        if not isinstance(item, dict):
            continue
        code = _normalize_code6(item.get("stock_code") or item.get("code"))
        trade_time = _normalize_qmtmini_trade_time(item.get("traded_time") or item.get("trade_time"))
        side = _parse_qmtmini_trade_side(item.get("order_type"))
        shares = int(abs(_parse_broker_number(item.get("traded_volume") or item.get("volume")) or 0))
        price = _parse_broker_number(item.get("traded_price") or item.get("price"))
        amount = _parse_broker_number(item.get("traded_amount") or item.get("amount"))
        if not code or not trade_time or side not in {"BUY", "SELL"} or shares <= 0 or not price or price <= 0:
            continue
        rows.append(
            {
                "trade_time": trade_time,
                "trade_date": trade_time[:10],
                "side": side,
                "side_label": "买入" if side == "BUY" else "卖出",
                "code": code,
                "name": "",
                "shares": shares,
                "price": round(float(price), 3),
                "amount": round(float(amount), 3) if amount is not None else round(float(price) * shares, 3),
                "remark": str(item.get("traded_id") or item.get("order_id") or "").strip(),
                "source": "qmtmini_trade_snapshot",
            }
        )
    rows.sort(key=lambda item: str(item.get("trade_time") or ""), reverse=True)
    return {
        "ok": bool(snapshot.get("ok")),
        "source": "qmtmini_readonly_snapshot",
        "message": "QMT Mini read-only trades synced.",
        "rows": rows,
        "parsed_count": len(rows),
        "raw_trades_count": len(snapshot.get("trades") or []),
        "account_id": snapshot.get("account_id"),
    }


def _preferred_broker_sync_source() -> str:
    return str(os.environ.get("AISTOCK_BROKER_SYNC_SOURCE") or "qmtmini").strip().lower()


def _sync_broker_holdings_from_ths() -> dict[str, Any]:
    source_preference = _preferred_broker_sync_source()
    result: dict[str, Any]
    fallback_result: dict[str, Any] | None = None
    if source_preference in {"qmtmini", "qmt", "qmtmini_first"}:
        result = _read_qmtmini_capital_holdings()
        if not result.get("ok"):
            fallback_result = result
            try:
                result = _read_ths_capital_holdings_via_gateway()
            except Exception as gateway_exc:
                result = {
                    "ok": False,
                    "source": "tdx_gateway_ths_bridge",
                    "message": f"Failed to read THS capital/holdings via TDX Gateway: {gateway_exc}",
                    "fallback_from": fallback_result,
                }
    else:
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
                "broker_account_id": result.get("account_id"),
                "qmtmini_snapshot": result.get("qmtmini_snapshot"),
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
            "broker_account_id": result.get("account_id"),
            "qmtmini_snapshot": result.get("qmtmini_snapshot"),
            "fallback_cache": bool(result.get("fallback_cache")),
            "sync_message": result.get("message"),
            "capital_only": False,
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
    source_preference = _preferred_broker_sync_source()
    if raw_text.strip():
        raw_result = {"ok": True, "raw_text": raw_text, "source": "payload_raw_text"}
    elif source_preference in {"qmtmini", "qmt", "qmtmini_first"}:
        raw_result = _read_qmtmini_trades()
        if not raw_result.get("ok"):
            fallback_result = raw_result
            try:
                raw_result = _read_ths_trades_via_gateway()
            except Exception as gateway_exc:
                raw_result = {
                    "ok": False,
                    "source": "tdx_gateway_ths_bridge",
                    "message": f"Failed to read THS trades via TDX Gateway: {gateway_exc}",
                    "fallback_from": fallback_result,
                }
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


@router.post("/broker/holdings/sync-ths-and-review")
async def sync_gen3_state_alpha_broker_holdings_from_ths_and_review(
    payload: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    source = _clean_review_text((payload or {}).get("source"), "manual")
    started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    sync_result = _sync_broker_holdings_from_ths()
    try:
        review_result = _run_realtime_readiness_review_once()
    except Exception as exc:
        logger.exception("Failed to run readiness review after broker holdings sync.")
        review_result = {
            "ok": False,
            "mode": "g3_realtime_readiness_review_v1",
            "error": str(exc),
            "summary": {},
        }
    admission = _readiness_review_admission_summary(review_result)
    ok = bool(sync_result.get("ok")) and bool(review_result.get("ok"))
    response = _wrap_guardrails(
        {
            "ok": ok,
            "mode": "g3_state_alpha_broker_holdings_sync_and_readiness_review",
            "source": source,
            "sync": sync_result,
            "review": review_result,
            "admission": admission,
            "message": (
                "真实持仓/价格已同步并完成实战前复审"
                if ok
                else "真实持仓/价格同步或实战前复审未完成；保持禁止真实买入"
            ),
        }
    )
    _append_review_action_attempt(
        attempted_at=started_at,
        action_group="sync_broker_holding_price",
        action_label="同步真实持仓/价格并复审",
        source=source,
        ok=ok,
        review_result=review_result,
        admission=admission,
        response=response,
        extra={
            "sync_ok": bool(sync_result.get("ok")),
            "sync_mode": sync_result.get("mode"),
            "sync_message": sync_result.get("message") or sync_result.get("error"),
            "holdings_count": len(sync_result.get("holdings") or []),
        },
    )
    snapshot = _try_record_live_launch_action_snapshot(
        source=source,
        phase="premarket_action_executed",
        note=(
            f"盘前动作已执行：同步真实持仓/价格并复审；"
            f"sync_ok={bool(sync_result.get('ok'))}；"
            f"review_ok={bool(review_result.get('ok'))}；"
            f"阻断数={admission.get('live_admission_blocking_command_count')}；"
            f"下一步={admission.get('live_premarket_next_action') or '--'}"
        ),
    )
    response["live_launch_review_snapshot"] = snapshot.get("snapshot") if snapshot.get("ok") else None
    if not snapshot.get("ok"):
        response["live_launch_review_snapshot_error"] = snapshot.get("error")
    response["artifacts"] = {
        **(response.get("artifacts") if isinstance(response.get("artifacts"), dict) else {}),
        "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
        "live_launch_review_snapshots": _path_status(LIVE_LAUNCH_REVIEW_SNAPSHOTS_PATH),
    }
    return response


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
async def get_gen3_state_alpha_shadow_monitor_status(
    include_workflow: bool = Query(default=False),
) -> dict[str, Any]:
    state = configure_shadow_monitor_scheduler()
    payload = {
        "ok": True,
        "mode": "g3_state_alpha_shadow_monitor",
        "monitor": state,
        "workflow_included": bool(include_workflow),
        "scheduler": {
            "enabled": bool(state.get("scheduler_enabled")),
            "wired": bool(state.get("scheduler_wired")),
            "next_run_time": state.get("next_run_time"),
            "message": (
                "G3 monitor scheduler disabled by runtime configuration."
                if state.get("scheduler_disabled_reason")
                else "G3 monitor scheduler wired."
            ),
        },
    }
    if include_workflow:
        payload["workflow"] = _build_workflow_status()
    return _wrap_guardrails(payload)


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
        if "paper_entry_enabled" in payload:
            state["paper_entry_enabled"] = bool(payload.get("paper_entry_enabled"))
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


@router.post("/shadow-entry-monitor/run-once")
async def run_gen3_state_alpha_shadow_entry_monitor_once() -> dict[str, Any]:
    return _wrap_guardrails(_run_shadow_entry_monitor_once(source="manual"))


@router.get("/shadow-exit-monitor/status")
async def get_gen3_state_alpha_shadow_exit_monitor_status() -> dict[str, Any]:
    state = configure_shadow_exit_monitor_scheduler()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_shadow_exit_monitor",
            "monitor": state,
            "scheduler": {
                "enabled": bool(state.get("scheduler_enabled")),
                "wired": bool(state.get("scheduler_wired")),
                "next_run_time": state.get("next_run_time"),
                "message": (
                    "G3 shadow exit monitor scheduler disabled by runtime configuration."
                    if state.get("scheduler_disabled_reason")
                    else "G3 shadow exit monitor scheduler wired."
                ),
            },
            "artifacts": {
                "exit_monitor_state": _path_status(EXIT_MONITOR_STATE_PATH),
                "shadow_ledger": _path_status(STATE_ALPHA_RUNTIME_DIR / "shadow_ledger.csv"),
                "paper_executions": _path_status(PAPER_EXECUTIONS_PATH),
            },
        }
    )


@router.post("/shadow-exit-monitor/config")
async def set_gen3_state_alpha_shadow_exit_monitor_config(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    state = _load_exit_monitor_state()
    if isinstance(payload, dict):
        if "enabled" in payload:
            state["enabled"] = bool(payload.get("enabled"))
        if "interval_seconds" in payload:
            state["interval_seconds"] = max(120, int(payload.get("interval_seconds") or 120))
        if "trading_hours_only" in payload:
            state["trading_hours_only"] = bool(payload.get("trading_hours_only"))
        if "paper_exit_enabled" in payload:
            state["paper_exit_enabled"] = bool(payload.get("paper_exit_enabled"))
        if "update_ledger_enabled" in payload:
            state["update_ledger_enabled"] = bool(payload.get("update_ledger_enabled"))
    _save_exit_monitor_state(state)
    state = configure_shadow_exit_monitor_scheduler()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_shadow_exit_monitor_config",
            "monitor": state,
        }
    )


@router.post("/shadow-exit-monitor/run-once")
async def run_gen3_state_alpha_shadow_exit_monitor_once(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = dict(payload) if isinstance(payload, dict) else {}
    state = _load_exit_monitor_state()
    source = str(data.get("source") or "manual").strip() or "manual"
    paper_enabled_value = data.get("paper_exit_enabled", state.get("paper_exit_enabled", True))
    ledger_enabled_value = data.get("update_ledger_enabled", state.get("update_ledger_enabled", True))
    paper_exit_enabled = paper_enabled_value if isinstance(paper_enabled_value, bool) else _truthy(paper_enabled_value)
    update_ledger_enabled = ledger_enabled_value if isinstance(ledger_enabled_value, bool) else _truthy(ledger_enabled_value)
    result = _run_shadow_exit_monitor_once(
        source=source,
        paper_exit_enabled=bool(paper_exit_enabled),
        update_ledger_enabled=bool(update_ledger_enabled),
    )
    now_text = datetime.now().isoformat(sep=" ", timespec="seconds")
    state["last_run_at"] = result.get("started_at") or now_text
    state["last_result"] = result
    state["last_error"] = None if result.get("ok") else str(result.get("error") or result.get("reason") or "")
    if result.get("ok"):
        state["last_success_at"] = result.get("finished_at") or now_text
    _save_exit_monitor_state(state)
    return _wrap_guardrails(
        {
            "ok": bool(result.get("ok")),
            "mode": "g3_state_alpha_shadow_exit_monitor_run_once",
            "result": result,
            "monitor": configure_shadow_exit_monitor_scheduler(),
        }
    )


@router.get("/daily-trend-exit-monitor/status")
async def get_gen3_state_alpha_daily_trend_exit_monitor_status() -> dict[str, Any]:
    state = configure_daily_trend_exit_monitor_scheduler()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_daily_trend_exit_monitor",
            "monitor": state,
            "contract": "intraday_daily_rising_trend_line_exit_v1",
            "execution_policy": "five_minute_intraday_trend_break_review_manual_or_paper_only",
            "artifacts": {"daily_trend_exit_monitor_state": _path_status(DAILY_TREND_EXIT_MONITOR_STATE_PATH)},
        }
    )


@router.post("/daily-trend-exit-monitor/config")
async def set_gen3_state_alpha_daily_trend_exit_monitor_config(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    state = _load_daily_trend_exit_monitor_state()
    data = payload if isinstance(payload, dict) else {}
    for key in ("enabled", "email_enabled"):
        if key in data:
            state[key] = bool(data.get(key))
    for key in ("hour", "minute", "lookback_days", "pivot_window", "min_pivot_separation"):
        if key in data:
            state[key] = data.get(key)
    if "break_buffer_pct" in data:
        state["break_buffer_pct"] = data.get("break_buffer_pct")
    _save_daily_trend_exit_monitor_state(_load_daily_trend_exit_monitor_state() | state)
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_daily_trend_exit_monitor_config",
            "monitor": configure_daily_trend_exit_monitor_scheduler(),
        }
    )


@router.post("/daily-trend-exit-monitor/run-once")
async def run_gen3_state_alpha_daily_trend_exit_monitor_once(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    result = _run_daily_trend_exit_monitor_once(source=str(data.get("source") or "manual").strip() or "manual")
    return _wrap_guardrails({**result, "monitor": configure_daily_trend_exit_monitor_scheduler()})


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


@router.get("/pretrade-ticket-reviews")
async def get_gen3_state_alpha_pretrade_ticket_reviews() -> dict[str, Any]:
    reviews = _load_pretrade_ticket_reviews()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_pretrade_ticket_reviews",
            "reviews": list(reviews.values()),
            "review_map": reviews,
            "count": len(reviews),
            "artifacts": {
                "pretrade_ticket_reviews": _path_status(PRETRADE_TICKET_REVIEWS_PATH),
            },
        }
    )


@router.post("/pretrade-ticket-review")
async def save_gen3_state_alpha_pretrade_ticket_review(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _save_pretrade_ticket_review(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_state_alpha_pretrade_ticket_review",
            "artifacts": {
                "pretrade_ticket_reviews": _path_status(PRETRADE_TICKET_REVIEWS_PATH),
            },
        }
    )


@router.post("/pretrade-paper-watch-batch")
async def start_gen3_state_alpha_pretrade_paper_watch_batch(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    include_existing = _truthy(data.get("include_existing"))
    dry_run = _truthy(data.get("dry_run"))
    run_review = data.get("run_readiness_review")
    run_review = True if run_review is None else _truthy(run_review)
    if dry_run:
        run_review = False
    reason = str(data.get("reason") or "").strip()
    batch_id = f"g3paperwatch_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"

    current = await get_gen3_state_alpha_current(limit=100, refresh=False, entry_date=None)
    tickets = current.get("next_trade_buy_tickets") if isinstance(current, dict) else []
    tickets = tickets if isinstance(tickets, list) else []
    saved: list[dict[str, Any]] = []
    preview: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for ticket in tickets:
        if not isinstance(ticket, dict):
            continue
        action = str(ticket.get("pretrade_review_action") or "").strip()
        if action and not include_existing:
            skipped.append(
                {
                    "code": ticket.get("code"),
                    "name": ticket.get("name"),
                    "entry_date": ticket.get("entry_date"),
                    "reason": f"已有复盘状态 {ticket.get('pretrade_review_label') or action}",
                }
            )
            continue
        review_payload = _pretrade_paper_watch_payload_from_ticket(ticket, batch_id=batch_id, reason=reason)
        if dry_run:
            preview.append(
                {
                    "ticket_key": review_payload.get("ticket_key"),
                    "code": review_payload.get("code"),
                    "name": review_payload.get("name"),
                    "entry_date": review_payload.get("entry_date"),
                    "route": review_payload.get("route"),
                    "review_action": review_payload.get("review_action"),
                    "review_decision_reason": review_payload.get("review_decision_reason"),
                    "next_review_trigger": review_payload.get("next_review_trigger"),
                }
            )
            continue
        result = _save_pretrade_ticket_review(review_payload)
        if result.get("ok"):
            saved.append(result.get("review") or {})
        else:
            skipped.append(
                {
                    "code": ticket.get("code"),
                    "name": ticket.get("name"),
                    "entry_date": ticket.get("entry_date"),
                    "reason": result.get("error") or "save_failed",
                }
            )

    review: dict[str, Any] = {}
    run: dict[str, Any] = {"ran": False}
    if run_review:
        started = datetime.now()
        proc = subprocess.run(
            [sys.executable, str(REALTIME_READINESS_REVIEW_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=180,
        )
        run = {
            "ran": True,
            "returncode": proc.returncode,
            "duration_seconds": round((datetime.now() - started).total_seconds(), 1),
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }
        review = _read_realtime_readiness_review()

    return _wrap_guardrails(
        {
            "ok": (bool(preview) if dry_run else (bool(saved) and (not run_review or run.get("returncode") == 0))),
            "mode": "g3_state_alpha_pretrade_paper_watch_batch",
            "dry_run": dry_run,
            "batch_id": batch_id,
            "saved_count": len(saved),
            "would_save_count": len(preview),
            "skipped_count": len(skipped),
            "ticket_count": len(tickets),
            "preview_reviews": preview,
            "saved_reviews": saved,
            "skipped": skipped,
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "next_action": "盘后或次日逐票填写纸面观察后评估，归因到买点、选股、策略切换或卖点合同",
            "run": run,
            "review_summary": review.get("summary") if isinstance(review, dict) else {},
            "artifacts": {
                "pretrade_ticket_reviews": _path_status(PRETRADE_TICKET_REVIEWS_PATH),
                "paper_watch_followup": _path_status(REALTIME_READINESS_REVIEW_DIR / "paper_watch_followup.csv"),
            },
        }
    )


@router.get("/paper-watch-reviews")
async def get_gen3_state_alpha_paper_watch_reviews() -> dict[str, Any]:
    reviews = _load_paper_watch_reviews()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_paper_watch_reviews",
            "reviews": list(reviews.values()),
            "review_map": reviews,
            "count": len(reviews),
            "artifacts": {
                "paper_watch_reviews": _path_status(PAPER_WATCH_REVIEWS_PATH),
            },
        }
    )


@router.post("/paper-watch-review")
async def save_gen3_state_alpha_paper_watch_review(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _save_paper_watch_review(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_state_alpha_paper_watch_review",
            "artifacts": {
                "paper_watch_reviews": _path_status(PAPER_WATCH_REVIEWS_PATH),
            },
        }
    )


@router.get("/candidate-omission-reviews")
async def get_gen3_state_alpha_candidate_omission_reviews() -> dict[str, Any]:
    reviews = _load_candidate_omission_reviews()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_candidate_omission_reviews",
            "reviews": list(reviews.values()),
            "review_map": reviews,
            "count": len(reviews),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "candidate_omission_reviews": _path_status(CANDIDATE_OMISSION_REVIEWS_PATH),
            },
        }
    )


@router.post("/candidate-omission-review")
async def save_gen3_state_alpha_candidate_omission_review(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _save_candidate_omission_review(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_state_alpha_candidate_omission_review",
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "candidate_omission_reviews": _path_status(CANDIDATE_OMISSION_REVIEWS_PATH),
            },
        }
    )


@router.post("/candidate-omission-review-and-run")
async def save_gen3_state_alpha_candidate_omission_review_and_run(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    request_payload = payload if isinstance(payload, dict) else {}
    source = _clean_review_text(request_payload.get("source"), "candidate_omission_checklist")
    started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    save_result = _save_candidate_omission_review(request_payload)
    if not save_result.get("ok"):
        return _wrap_guardrails(
            {
                **save_result,
                "mode": "g3_state_alpha_candidate_omission_review_and_readiness_review",
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "artifacts": {
                    "candidate_omission_reviews": _path_status(CANDIDATE_OMISSION_REVIEWS_PATH),
                },
            }
        )
    if save_result.get("removed"):
        return _wrap_guardrails(
            {
                **save_result,
                "mode": "g3_state_alpha_candidate_omission_review_and_readiness_review",
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "artifacts": {
                    "candidate_omission_reviews": _path_status(CANDIDATE_OMISSION_REVIEWS_PATH),
                },
            }
        )
    try:
        review_result = _run_realtime_readiness_review_once()
    except Exception as exc:
        logger.exception("Failed to run readiness review after candidate omission review.")
        review_result = {
            "ok": False,
            "mode": "g3_realtime_readiness_review_v1",
            "error": str(exc),
            "summary": {},
        }
    admission = _readiness_review_admission_summary(review_result)
    ok = bool(save_result.get("ok")) and bool(review_result.get("ok"))
    review_item = save_result.get("review") if isinstance(save_result.get("review"), dict) else {}
    result_code = _clean_review_text(request_payload.get("review_result") or request_payload.get("watch_result") or request_payload.get("result"))
    action_label = f"候选遗漏复盘：{_candidate_omission_result_label(result_code)}" if result_code else "候选遗漏复盘并复审"
    response = _wrap_guardrails(
        {
            "ok": ok,
            "mode": "g3_state_alpha_candidate_omission_review_and_readiness_review",
            "source": source,
            "save": save_result,
            "review": review_result,
            "admission": admission,
            "message": (
                "候选遗漏复盘已保存并完成实战前复审"
                if ok
                else "候选遗漏复盘已保存，但实战前复审未完成；保持禁止真实买入"
            ),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
        }
    )
    _append_review_action_attempt(
        attempted_at=started_at,
        action_group="candidate_omission_review",
        action_label=action_label,
        source=source,
        ok=ok,
        review_result=review_result,
        admission=admission,
        response=response,
        extra={
            "sync_ok": None,
            "sync_mode": "not_applicable",
            "sync_message": review_item.get("review_note") or save_result.get("key"),
            "holdings_count": None,
            "review_key": save_result.get("key"),
            "review_result": result_code,
            "review_status": review_item.get("review_status"),
        },
    )
    response["artifacts"] = {
        **(response.get("artifacts") if isinstance(response.get("artifacts"), dict) else {}),
        "candidate_omission_reviews": _path_status(CANDIDATE_OMISSION_REVIEWS_PATH),
        "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
    }
    return response


@router.get("/no-trade-day-reviews")
async def get_gen3_state_alpha_no_trade_day_reviews() -> dict[str, Any]:
    reviews = _load_no_trade_day_reviews()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_no_trade_day_reviews",
            "reviews": list(reviews.values()),
            "review_map": reviews,
            "count": len(reviews),
            "artifacts": {
                "no_trade_day_reviews": _path_status(NO_TRADE_DAY_REVIEWS_PATH),
            },
        }
    )


@router.post("/no-trade-day-review")
async def save_gen3_state_alpha_no_trade_day_review(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _save_no_trade_day_review(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_state_alpha_no_trade_day_review",
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "no_trade_day_reviews": _path_status(NO_TRADE_DAY_REVIEWS_PATH),
            },
        }
    )


@router.post("/no-trade-day-review-and-run")
async def save_gen3_state_alpha_no_trade_day_review_and_run(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    request_payload = payload if isinstance(payload, dict) else {}
    source = _clean_review_text(request_payload.get("source"), "no_trade_day_review")
    started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    save_result = _save_no_trade_day_review(request_payload)
    if not save_result.get("ok"):
        return _wrap_guardrails(
            {
                **save_result,
                "mode": "g3_state_alpha_no_trade_day_review_and_readiness_review",
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "artifacts": {
                    "no_trade_day_reviews": _path_status(NO_TRADE_DAY_REVIEWS_PATH),
                },
            }
        )
    if save_result.get("removed"):
        return _wrap_guardrails(
            {
                **save_result,
                "mode": "g3_state_alpha_no_trade_day_review_and_readiness_review",
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "artifacts": {
                    "no_trade_day_reviews": _path_status(NO_TRADE_DAY_REVIEWS_PATH),
                },
            }
        )
    try:
        review_result = _run_realtime_readiness_review_once()
    except Exception as exc:
        logger.exception("Failed to run readiness review after no-trade-day review.")
        review_result = {
            "ok": False,
            "mode": "g3_realtime_readiness_review_v1",
            "error": str(exc),
            "summary": {},
        }
    admission = _readiness_review_admission_summary(review_result)
    ok = bool(save_result.get("ok")) and bool(review_result.get("ok"))
    review_item = save_result.get("review") if isinstance(save_result.get("review"), dict) else {}
    result_code = _clean_review_text(request_payload.get("review_result") or request_payload.get("result"))
    action_label = f"无票日复盘：{_no_trade_day_result_label(result_code)}" if result_code else "无票日复盘并复审"
    response = _wrap_guardrails(
        {
            "ok": ok,
            "mode": "g3_state_alpha_no_trade_day_review_and_readiness_review",
            "source": source,
            "save": save_result,
            "review": review_result,
            "admission": admission,
            "message": (
                "无票日复盘已保存并完成实战前复审"
                if ok
                else "无票日复盘已保存，但实战前复审未完成；保持禁止真实买入"
            ),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
        }
    )
    _append_review_action_attempt(
        attempted_at=started_at,
        action_group="no_trade_day_review",
        action_label=action_label,
        source=source,
        ok=ok,
        review_result=review_result,
        admission=admission,
        response=response,
        extra={
            "sync_ok": None,
            "sync_mode": "not_applicable",
            "sync_message": review_item.get("review_note") or save_result.get("key"),
            "holdings_count": None,
            "review_key": save_result.get("key"),
            "review_result": result_code,
            "review_status": review_item.get("review_status"),
        },
    )
    response["artifacts"] = {
        **(response.get("artifacts") if isinstance(response.get("artifacts"), dict) else {}),
        "no_trade_day_reviews": _path_status(NO_TRADE_DAY_REVIEWS_PATH),
        "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
    }
    return response


@router.get("/formal-action-reviews")
async def get_gen3_state_alpha_formal_action_reviews() -> dict[str, Any]:
    reviews = _load_formal_action_reviews()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_formal_action_reviews",
            "reviews": list(reviews.values()),
            "review_map": reviews,
            "count": len(reviews),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "formal_action_reviews": _path_status(FORMAL_ACTION_REVIEWS_PATH),
            },
        }
    )


@router.post("/formal-action-review")
async def save_gen3_state_alpha_formal_action_review(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _save_formal_action_review(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_state_alpha_formal_action_review",
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "formal_action_reviews": _path_status(FORMAL_ACTION_REVIEWS_PATH),
            },
        }
    )


@router.post("/formal-action-review-and-run")
async def save_gen3_state_alpha_formal_action_review_and_run(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    request_payload = payload if isinstance(payload, dict) else {}
    source = _clean_review_text(request_payload.get("source"), "formal_action")
    started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    save_result = _save_formal_action_review(request_payload)
    if not save_result.get("ok"):
        return _wrap_guardrails(
            {
                **save_result,
                "mode": "g3_state_alpha_formal_action_review_and_readiness_review",
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "artifacts": {
                    "formal_action_reviews": _path_status(FORMAL_ACTION_REVIEWS_PATH),
                },
            }
        )
    if save_result.get("removed"):
        return _wrap_guardrails(
            {
                **save_result,
                "mode": "g3_state_alpha_formal_action_review_and_readiness_review",
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "artifacts": {
                    "formal_action_reviews": _path_status(FORMAL_ACTION_REVIEWS_PATH),
                },
            }
        )
    try:
        review_result = _run_realtime_readiness_review_once()
    except Exception as exc:
        logger.exception("Failed to run readiness review after formal action review.")
        review_result = {
            "ok": False,
            "mode": "g3_realtime_readiness_review_v1",
            "error": str(exc),
            "summary": {},
        }
    admission = _readiness_review_admission_summary(review_result)
    ok = bool(save_result.get("ok")) and bool(review_result.get("ok"))
    review_item = save_result.get("review") if isinstance(save_result.get("review"), dict) else {}
    result_code = _clean_review_text(request_payload.get("review_result") or request_payload.get("result"))
    action_label = f"正式动作复核：{_formal_action_result_label(result_code)}" if result_code else "正式动作复核并复审"
    response = _wrap_guardrails(
        {
            "ok": ok,
            "mode": "g3_state_alpha_formal_action_review_and_readiness_review",
            "source": source,
            "save": save_result,
            "review": review_result,
            "admission": admission,
            "message": (
                "正式动作复核已保存并完成实战前复审"
                if ok
                else "正式动作复核已保存，但实战前复审未完成；保持禁止真实买入"
            ),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
        }
    )
    _append_review_action_attempt(
        attempted_at=started_at,
        action_group="formal_action_review",
        action_label=action_label,
        source=source,
        ok=ok,
        review_result=review_result,
        admission=admission,
        response=response,
        extra={
            "sync_ok": None,
            "sync_mode": "not_applicable",
            "sync_message": review_item.get("review_note") or save_result.get("key"),
            "holdings_count": None,
            "review_key": save_result.get("key"),
            "review_result": result_code,
            "review_status": review_item.get("review_status"),
        },
    )
    response["artifacts"] = {
        **(response.get("artifacts") if isinstance(response.get("artifacts"), dict) else {}),
        "formal_action_reviews": _path_status(FORMAL_ACTION_REVIEWS_PATH),
        "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
    }
    return response


@router.get("/daily-review-checklist-reviews")
async def get_gen3_state_alpha_daily_review_checklist_reviews() -> dict[str, Any]:
    reviews = _load_daily_review_checklist_reviews()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_daily_review_checklist_reviews",
            "reviews": list(reviews.values()),
            "review_map": reviews,
            "count": len(reviews),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "daily_review_checklist_reviews": _path_status(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH),
            },
        }
    )


@router.post("/daily-review-checklist-review")
async def save_gen3_state_alpha_daily_review_checklist_review(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = _save_daily_review_checklist_review(payload if isinstance(payload, dict) else {})
    return _wrap_guardrails(
        {
            **result,
            "mode": "g3_state_alpha_daily_review_checklist_review",
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "artifacts": {
                "daily_review_checklist_reviews": _path_status(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH),
            },
        }
    )


@router.post("/daily-review-checklist-review-and-run")
async def save_gen3_state_alpha_daily_review_checklist_review_and_run(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    request_payload = payload if isinstance(payload, dict) else {}
    source = _clean_review_text(request_payload.get("source"), "live_daily_review_execution_checklist")
    started_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    save_result = _save_daily_review_checklist_review(request_payload)
    if not save_result.get("ok"):
        return _wrap_guardrails(
            {
                **save_result,
                "mode": "g3_state_alpha_daily_review_checklist_review_and_readiness_review",
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "artifacts": {
                    "daily_review_checklist_reviews": _path_status(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH),
                },
            }
        )
    if save_result.get("removed"):
        return _wrap_guardrails(
            {
                **save_result,
                "mode": "g3_state_alpha_daily_review_checklist_review_and_readiness_review",
                "formal_buy_signal": False,
                "auto_order_allowed": False,
                "order_path_enabled": False,
                "artifacts": {
                    "daily_review_checklist_reviews": _path_status(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH),
                },
            }
        )
    try:
        review_result = _run_realtime_readiness_review_once()
    except Exception as exc:
        logger.exception("Failed to run readiness review after daily checklist review.")
        review_result = {
            "ok": False,
            "mode": "g3_realtime_readiness_review_v1",
            "error": str(exc),
            "summary": {},
        }
    admission = _readiness_review_admission_summary(review_result)
    ok = bool(save_result.get("ok")) and bool(review_result.get("ok"))
    review_item = save_result.get("review") if isinstance(save_result.get("review"), dict) else {}
    result_code = _clean_review_text(request_payload.get("review_result") or request_payload.get("result"))
    axis = _clean_review_text(request_payload.get("review_axis"), "daily_review")
    action_label = f"逐项复盘：{axis}/{_daily_review_checklist_result_label(result_code)}" if result_code else "逐项复盘并复审"
    response = _wrap_guardrails(
        {
            "ok": ok,
            "mode": "g3_state_alpha_daily_review_checklist_review_and_readiness_review",
            "source": source,
            "save": save_result,
            "review": review_result,
            "admission": admission,
            "message": (
                "逐项复盘已保存并完成实战前复审"
                if ok
                else "逐项复盘已保存，但实战前复审未完成；保持禁止真实买入"
            ),
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
        }
    )
    _append_review_action_attempt(
        attempted_at=started_at,
        action_group="daily_review_checklist_review",
        action_label=action_label,
        source=source,
        ok=ok,
        review_result=review_result,
        admission=admission,
        response=response,
        extra={
            "sync_ok": None,
            "sync_mode": "not_applicable",
            "sync_message": review_item.get("review_note") or save_result.get("key"),
            "holdings_count": None,
            "review_key": save_result.get("key"),
            "review_result": result_code,
            "review_status": review_item.get("review_status"),
            "review_axis": review_item.get("review_axis"),
            "review_scope": review_item.get("review_scope"),
            "issue_area": review_item.get("issue_area"),
        },
    )
    response["artifacts"] = {
        **(response.get("artifacts") if isinstance(response.get("artifacts"), dict) else {}),
        "daily_review_checklist_reviews": _path_status(DAILY_REVIEW_CHECKLIST_REVIEWS_PATH),
        "premarket_action_attempts": _path_status(PREMARKET_ACTION_ATTEMPTS_PATH),
    }
    return response


@router.get("/paper-executions")
async def get_gen3_state_alpha_paper_executions(
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum paper execution rows returned."),
) -> dict[str, Any]:
    all_rows = _load_paper_executions()
    rows = _formal_score88_paper_rows(all_rows)
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_paper_executions",
            "paper_executions": rows[-limit:][::-1],
            "count": len(rows),
            "legacy_execution_count": len(all_rows) - len(rows),
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


@router.post("/day1-paper-execution-batch")
async def start_gen3_state_alpha_day1_paper_execution_batch(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    dry_run = _truthy(data.get("dry_run"))
    include_existing = _truthy(data.get("include_existing"))
    run_review = data.get("run_readiness_review")
    run_review = True if run_review is None else _truthy(run_review)
    if dry_run:
        run_review = False
    base_capital = data.get("base_capital")
    batch_id = f"g3day1paper_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:8]}"

    current = await get_gen3_state_alpha_current(limit=100, refresh=False, entry_date=None)
    tickets = current.get("next_trade_buy_tickets") if isinstance(current, dict) else []
    tickets = [item for item in tickets if isinstance(item, dict)]
    pack_by_code = _day1_pack_by_code()
    paper_rows = _load_paper_executions()
    preview: list[dict[str, Any]] = []
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for ticket in tickets:
        code = str(ticket.get("code") or ticket.get("code_raw") or "").strip().upper()
        pack_row = pack_by_code.get(code) if pack_by_code else None
        if pack_by_code and pack_row is None:
            skipped.append({"code": ticket.get("code"), "name": ticket.get("name"), "reason": "not_in_day1_review_pack"})
            continue
        if _day1_launch_posture_blocks_execution(pack_row):
            skipped.append(
                {
                    "code": ticket.get("code"),
                    "name": ticket.get("name"),
                    "entry_date": ticket.get("entry_date"),
                    "reason": "day1_contract_block_observation",
                    "launch_posture": (pack_row or {}).get("launch_posture"),
                    "contract_block_reason": (pack_row or {}).get("contract_block_reason"),
                }
            )
            continue
        if not _truthy(ticket.get("qualified_shadow_buy")):
            skipped.append({"code": ticket.get("code"), "name": ticket.get("name"), "reason": "not_qualified_shadow_buy"})
            continue
        if not _truthy(ticket.get("m30_confirmed")):
            skipped.append({"code": ticket.get("code"), "name": ticket.get("name"), "reason": "missing_m30_confirmation"})
            continue
        existing = next((row for row in paper_rows if _paper_execution_matches_ticket(ticket, row)), None)
        if existing and not include_existing:
            skipped.append(
                {
                    "code": ticket.get("code"),
                    "name": ticket.get("name"),
                    "entry_date": ticket.get("entry_date"),
                    "reason": "paper_execution_exists",
                    "execution_id": existing.get("execution_id"),
                }
            )
            continue

        order_payload = _day1_paper_order_payload(ticket, pack_row, base_capital=base_capital)
        order_payload["batch_id"] = batch_id
        item = _day1_paper_execution_preview(ticket, pack_row, base_capital=base_capital)
        item["batch_id"] = batch_id
        if dry_run:
            preview.append(item)
            continue
        result = _submit_paper_order(order_payload)
        if result.get("ok"):
            record = result.get("record") or {}
            record["batch_id"] = batch_id
            saved.append(record)
            paper_rows.append(record)
        else:
            skipped.append(
                {
                    "code": ticket.get("code"),
                    "name": ticket.get("name"),
                    "entry_date": ticket.get("entry_date"),
                    "reason": result.get("message") or result.get("error") or "paper_order_failed",
                }
            )

    review: dict[str, Any] = {}
    run: dict[str, Any] = {"ran": False}
    if run_review:
        started = datetime.now()
        proc = subprocess.run(
            [sys.executable, str(REALTIME_READINESS_REVIEW_SCRIPT)],
            cwd=str(PROJECT_ROOT),
            text=True,
            capture_output=True,
            timeout=180,
        )
        run = {
            "ran": True,
            "returncode": proc.returncode,
            "duration_seconds": round((datetime.now() - started).total_seconds(), 1),
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }
        review = _read_realtime_readiness_review()

    ok = bool(preview) if dry_run else bool(saved) and (not run_review or run.get("returncode") == 0)
    return _wrap_guardrails(
        {
            "ok": ok,
            "mode": "g3_state_alpha_day1_paper_execution_batch",
            "dry_run": dry_run,
            "batch_id": batch_id,
            "ticket_count": len(tickets),
            "would_save_count": len(preview),
            "saved_count": len(saved),
            "skipped_count": len(skipped),
            "preview_executions": preview,
            "saved_executions": saved,
            "skipped": skipped,
            "formal_buy_signal": False,
            "auto_order_allowed": False,
            "order_path_enabled": False,
            "next_action": "收盘后逐票填写纸面观察后评估，并将问题归因到买点、选股、策略切换或卖点合同。",
            "run": run,
            "review_summary": review.get("summary") if isinstance(review, dict) else {},
            "artifacts": {
                "paper_executions": _path_status(PAPER_EXECUTIONS_PATH),
                "day1_paper_review_pack": _path_status(REALTIME_READINESS_REVIEW_DIR / "day1_paper_review_pack.csv"),
            },
        }
    )


@router.get("/observation-snapshots")
async def get_gen3_state_alpha_observation_snapshots(
    limit: int = Query(default=100, ge=1, le=500, description="Maximum observation snapshots returned."),
) -> dict[str, Any]:
    rows = _load_observation_snapshots()
    formal_rows = _formal_score88_observation_rows(rows)
    accepted_dates = _accepted_observation_dates(formal_rows)
    scheduler_state = configure_observation_scheduler()
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_observation_snapshots",
            "snapshots": formal_rows[-limit:][::-1],
            "count": len(formal_rows),
            "legacy_snapshot_count": len(rows) - len(formal_rows),
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
    formal_rows = _formal_score88_observation_rows(rows)
    accepted_dates = _accepted_observation_dates(formal_rows)
    return _wrap_guardrails(
        {
            "ok": True,
            "mode": "g3_state_alpha_observation_scheduler",
            "scheduler": configure_observation_scheduler(),
            "accepted_observation_days": len(accepted_dates),
            "accepted_observation_dates": accepted_dates[-30:],
            "latest_snapshot": formal_rows[-1] if formal_rows else None,
            "legacy_snapshot_count": len(rows) - len(formal_rows),
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
        if "retry_hour" in payload:
            state["retry_hour"] = min(23, max(0, int(payload.get("retry_hour") or 10)))
        if "retry_minutes" in payload:
            state["retry_minutes"] = _coerce_scheduler_minutes(payload.get("retry_minutes"), [])
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

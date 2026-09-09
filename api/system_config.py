"""
系统配置API

提供系统配置相关的接口，包括?
1. 同步丢二三行业板块
2. 修复历史K线数?
"""

from collections import defaultdict
from typing import Optional, Dict, Any, List, Union
from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel
from datetime import datetime, time as dt_time, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import csv
import os
import re
import requests
import ssl
import smtplib
import threading
import subprocess
import sys
import importlib.util
from pathlib import Path
from pathlib import PureWindowsPath
from sqlalchemy import desc, func, text
import yaml

from utils.logger import get_logger
from utils.config import config as app_config
from utils.paths import report_path, runtime_path

router = APIRouter(prefix="/system", tags=["系统配置"])
logger = get_logger("system_config")
REPO_ROOT = Path(__file__).resolve().parents[1]

# 情绪周期自动修复调度器（按需启动?
_emotion_scheduler = None
_emotion_scheduler_lock = threading.Lock()
_emotion_job_id = "emotion_cycle_auto_fix_5m"
_core_maintenance_scheduler = None
_core_maintenance_scheduler_lock = threading.Lock()
_homepage_integrity_scheduler = None
_homepage_integrity_scheduler_lock = threading.Lock()
_core_maintenance_job_ids = {
    "trade_calendar": "core_trade_calendar_sync",
    "stock_list_sync": "core_stock_list_sync",
    "index_list_sync": "core_index_list_sync",
    "sector_list_sync": "core_sector_list_sync",
    "stock_intraday": "core_market_intraday_daily_snapshot",
    "market_intraday_minutes": "core_market_intraday_minute_snapshot",
    "market_sentiment_snapshot": "core_market_sentiment_snapshot",
    "market_sentiment_after_close": "core_market_sentiment_after_close",
    "market_intraday_kline_refresh": "core_market_intraday_kline_refresh",
    "minute_kline_daily_repair_validate": "core_minute_kline_daily_repair_validate",
    "sector_intraday_stats_refresh": "core_sector_intraday_stats_refresh",
    "official_daily": "core_official_daily_close_sync",
    "emotion_cycle_after_close": "core_emotion_cycle_after_close_repair",
    "emotion_cycle_overnight": "core_emotion_cycle_overnight_repair",
    "repair_daily": "core_repair_previous_daily_kline",
    "daily_coverage": "core_daily_kline_coverage_maintenance",
}
_core_maintenance_task_map = {
    "trade_calendar": "update_trade_calendar",
    "stock_list_sync": "update_stock_list",
    "index_list_sync": "update_index_list",
    "sector_list_sync": "sync_sectors",
    "stock_intraday": "update_stock_today_data",
    "market_intraday_minutes": "update_market_today_minute_data",
    "market_sentiment_snapshot": "refresh_market_sentiment_snapshot",
    "market_sentiment_after_close": "refresh_market_sentiment_snapshot",
    "market_intraday_kline_refresh": "sync_today_intraday_kline",
    "minute_kline_daily_repair_validate": "minute_kline_daily_repair_validate",
    "market_minute_history_repair": "market_minute_history_repair",
    "data_source_date_repair": "data_source_date_repair",
    "sector_intraday_stats_refresh": "update_sector_intraday_stats",
    "official_daily": "official_daily_close_sync",
    "emotion_cycle_after_close": "repair_emotion_cycle_latest",
    "emotion_cycle_overnight": "repair_emotion_cycle_latest",
    "repair_daily": "repair_previous_daily_kline",
    "daily_coverage": "daily_kline_coverage_maintenance",
}
_core_maintenance_bootstrap_lock = threading.Lock()
_core_maintenance_bootstrap_thread = None
_trade_calendar_preflight_lock = threading.Lock()
_trade_calendar_preflight_date = None
_core_data_manual_sync_lock = threading.Lock()
_core_data_manual_sync_thread = None
_gen2_v4_event_refresh_lock = threading.Lock()
_startup_reference_sync_setting_key = "startup.enable_reference_data_sync_on_launch"
_strategy_daily_scheduler = None
_strategy_daily_scheduler_lock = threading.Lock()
_strategy_daily_job_id = "strategy_daily_runner"
_strategy_daily_setting_key = "strategy_daily_runner"
MIN_EMOTION_KLINE_ROWS = 1000
EMOTION_KLINE_COMPLETE_RATIO = 0.8


def _now_iso() -> str:
    return datetime.now().isoformat()


def _read_json_file(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _latest_qmt_after_close_execution() -> Dict[str, Any]:
    """Return host-collector artifacts; never infer success from legacy scheduler state."""
    root = runtime_path()
    runs = [item for item in root.glob("qmt_xtquant_collector_after-close_*") if item.is_dir()]
    if not runs:
        return {"available": False, "source": "qmt_xtquant"}
    run_dir = max(runs, key=lambda item: item.stat().st_mtime)
    validation = _read_json_file(run_dir / "final_validation.json")
    collector = _read_json_file(run_dir / "collector_summary.json")
    fullpush = _read_json_file(run_dir / "after_close_daily_fullpush.json")
    day = (validation.get("days") or [{}])[0]
    queue_rows: List[Dict[str, str]] = []
    try:
        with (run_dir / "issues.csv").open("r", encoding="utf-8-sig", newline="") as handle:
            queue_rows = list(csv.DictReader(handle))
    except Exception:
        pass
    worker_summary = (collector.get("minute") or {}).get("worker_summary") or {}
    initial_records = collector.get("initial_issue_count", worker_summary.get("initial_issue_count", worker_summary.get("issue_count")))
    initial_unique = worker_summary.get("initial_unique_issue_codes")
    if initial_unique is None and queue_rows:
        initial_unique = len({str(row.get("code") or "") for row in queue_rows if row.get("code")})
    final_period_records = sum(int(item.get("issue_codes") or 0) for item in (day.get("periods") or []))
    return {
        "available": True,
        "source": "qmt_xtquant",
        "run_dir": str(run_dir),
        "updated_at": datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(),
        "closed": bool(validation.get("closed")),
        "validation_status": "closed" if validation.get("closed") else "open",
        "trade_date": day.get("trade_date"),
        "periods": day.get("periods") or [],
        "remaining_period_code_records": final_period_records,
        "initial_period_code_records": initial_records,
        "initial_unique_codes": initial_unique,
        "fullpush_ok": fullpush.get("ok"),
        "fullpush_started_at": fullpush.get("started_at"),
        "fullpush_finished_at": fullpush.get("finished_at"),
        "schedule": {"start": "16:00", "retry_minutes": 30, "active_end": "18:30"},
    }


def _latest_qmt_overnight_repair_execution() -> Dict[str, Any]:
    """Return the latest overnight rolling-repair artifact without inferring task success."""
    root = runtime_path()
    runs = [item for item in root.glob("qmt_xtquant_collector_history_*") if item.is_dir()]
    if not runs:
        return {
            "available": False,
            "source": "qmt_xtquant",
            "schedule": {"start": "00:40", "active_end": "06:30"},
        }
    run_dir = max(runs, key=lambda item: item.stat().st_mtime)
    collector = _read_json_file(run_dir / "collector_summary.json")
    validation = _read_json_file(run_dir / "final_validation.json")
    minute = collector.get("minute") or {}
    worker_summary = minute.get("worker_summary") or {}
    stderr_tail = str(minute.get("stderr_tail") or "")
    execution_ok = bool(collector.get("ok"))
    validation_closed = bool(validation.get("closed"))
    if "EmptyDataError" in stderr_tail:
        execution_message = "空修复队列被重复读取，执行器失败；数据校验仍已闭环。"
    elif execution_ok:
        execution_message = "执行与最终校验均已完成。"
    else:
        execution_message = str(collector.get("validation_status") or "最近一次执行未完成")
    return {
        "available": True,
        "source": "qmt_xtquant",
        "run_dir": str(run_dir),
        "updated_at": datetime.fromtimestamp(run_dir.stat().st_mtime).isoformat(),
        "trade_dates": validation.get("trade_dates") or worker_summary.get("trade_dates") or [],
        "execution_ok": execution_ok,
        "validation_closed": validation_closed,
        "remaining_issue_count": int(worker_summary.get("issue_count") or 0),
        "execution_message": execution_message,
        "schedule": {"start": "00:40", "active_end": "06:30"},
    }


def _to_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None
    return None


def _task_status_from_payload(task: Dict[str, Any]) -> str:
    if not task:
        return "idle"
    if task.get("paused") is True:
        return "paused"
    if task.get("is_running"):
        return "running"
    if task.get("error"):
        return "failed"
    if task.get("results") is not None:
        return "success"
    return "idle"


def _startup_reference_sync_default_enabled() -> bool:
    debug_enabled = bool(app_config.get("app.debug", False))
    return not debug_enabled


def get_startup_reference_sync_enabled() -> bool:
    configured = app_config.get(_startup_reference_sync_setting_key, None)
    if configured is None:
        return _startup_reference_sync_default_enabled()
    return bool(configured)


def _save_settings_yaml(mutator):
    settings_path = app_config._config_dir / "settings.yaml"
    with open(settings_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    mutator(data)
    with open(settings_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    app_config.reload("settings.yaml")


def _save_settings_local_yaml(mutator):
    settings_path = app_config._config_dir / "settings.local.yaml"
    if settings_path.exists():
        with open(settings_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}
    mutator(data)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    app_config.reload("settings.yaml")


def set_startup_reference_sync_enabled(enabled: bool) -> bool:
    enabled = bool(enabled)

    def _mutate(data: Dict[str, Any]):
        startup_cfg = data.setdefault("startup", {})
        startup_cfg["enable_reference_data_sync_on_launch"] = enabled

    _save_settings_yaml(_mutate)
    return enabled


def _ensure_emotion_scheduler():
    global _emotion_scheduler
    if _emotion_scheduler is not None:
        return _emotion_scheduler
    with _emotion_scheduler_lock:
        if _emotion_scheduler is not None:
            return _emotion_scheduler
        try:
            from services.operations.schedulers import ObservedScheduler as BackgroundScheduler
            _emotion_scheduler = BackgroundScheduler(timezone="Asia/Shanghai", owner="数据维护")
            _emotion_scheduler.start()
        except Exception as e:
            logger.error(f"启动情绪周期调度器失? {e}")
            raise
    return _emotion_scheduler


def _ensure_core_maintenance_scheduler():
    global _core_maintenance_scheduler
    if _core_maintenance_scheduler is not None:
        return _core_maintenance_scheduler
    with _core_maintenance_scheduler_lock:
        if _core_maintenance_scheduler is not None:
            return _core_maintenance_scheduler
        from services.operations.schedulers import ObservedScheduler as BackgroundScheduler

        _core_maintenance_scheduler = BackgroundScheduler(timezone="Asia/Shanghai", owner="数据维护")
        _core_maintenance_scheduler.start()
    return _core_maintenance_scheduler


def _set_core_maintenance_status(enabled: bool, message: str = ""):
    task_manager.tasks["core_data_maintenance"]["is_running"] = enabled
    task_manager.tasks["core_data_maintenance"]["paused"] = not enabled
    task_manager.tasks["core_data_maintenance"]["started_at"] = _now_iso() if enabled else None
    task_manager.tasks["core_data_maintenance"]["progress"] = {"current": 0, "total": len(_core_maintenance_task_map), "message": message}
    results = task_manager.tasks["core_data_maintenance"].get("results") or {}
    results["enabled"] = enabled
    task_manager.tasks["core_data_maintenance"]["results"] = results
    task_manager.tasks["core_data_maintenance"]["last_run_at"] = _now_iso()
    if enabled:
        task_manager.tasks["core_data_maintenance"]["last_success_at"] = _now_iso()
    task_manager._append_timeline(
        "core_data_maintenance",
        "success" if enabled else "paused",
        message=message,
    )
    if not enabled:
        task_manager.tasks["core_data_maintenance"]["error"] = None


def _core_scheduler_jobs_status():
    scheduler = _core_maintenance_scheduler
    jobs = {}
    if scheduler is not None:
        for key, job_id in _core_maintenance_job_ids.items():
            job = scheduler.get_job(job_id)
            jobs[key] = {
                "enabled": job is not None,
                "next_run_time": job.next_run_time.isoformat() if job and job.next_run_time else None,
            }
    return jobs


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


_SYSTEM_TASK_RUN_LOG_READY = False
_SYSTEM_TASK_RUN_LOG_LOCK = threading.Lock()
_SYSTEM_TASK_RUN_LOG_CACHE: Dict[str, Dict[str, Any]] = {}
_SYSTEM_TASK_RUN_LOG_CACHE_LOCK = threading.Lock()
_SYSTEM_TASK_RUN_LOG_LAST_FAILURE: Dict[str, Any] = {
    "ts": None,  # datetime
    "message": "",
    "signature": "",
}
_SYSTEM_TASK_RUN_LOG_LAST_FAILURE_LOCK = threading.Lock()


def _json_default(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _compact_json(value: Any) -> str:
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=_json_default)
    except Exception:
        return str(value)


def _result_payload_indicates_failure(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    validation_status = str(payload.get("validation_status") or "").strip().lower()
    if validation_status and validation_status not in {"passed", "success", "ok", "skipped_non_trading_day"}:
        return True
    status = str(payload.get("status") or "").strip().lower()
    if status in {"failed", "failure", "error"}:
        return True
    return False


def _results_json_indicates_failure(value: Any) -> bool:
    if not value:
        return False
    try:
        payload = json.loads(value) if isinstance(value, str) else value
    except Exception:
        return False
    return _result_payload_indicates_failure(payload)


def _normalize_task_run_log_error(exc: Any) -> str:
    msg = str(exc)
    normalized = re.sub(r"query_id=[a-zA-Z0-9-]+", "query_id=<redacted>", msg)
    normalized = re.sub(r"for url\s*:\s*[^\s]+", "for url <redacted>", normalized)
    lowered = normalized.lower()
    if "httpconnectionpool(" in lowered and "max retries exceeded" in lowered:
        return "http_connection_pool_max_retries_exceeded"
    if "new connection" in lowered and "connection refused" in lowered:
        return "connection_refused"
    if "connection aborted" in lowered and "remote end closed connection without response" in lowered:
        return "connection_remote_closed"
    if "max retries exceeded" in lowered:
        return "max_retries_exceeded"
    return normalized


def _warn_task_run_log_failure(context: str, exc: Any) -> None:
    now = datetime.now()
    signature = _normalize_task_run_log_error(exc)
    with _SYSTEM_TASK_RUN_LOG_LAST_FAILURE_LOCK:
        last_ts = _SYSTEM_TASK_RUN_LOG_LAST_FAILURE.get("ts")
        last_signature = _SYSTEM_TASK_RUN_LOG_LAST_FAILURE.get("signature", "")
        if (not last_ts) or (now - last_ts).total_seconds() >= 30 or signature != last_signature:
            logger.warning(f"{context}: {exc}")
            _SYSTEM_TASK_RUN_LOG_LAST_FAILURE["ts"] = now
            _SYSTEM_TASK_RUN_LOG_LAST_FAILURE["signature"] = signature


def _ch_datetime(value: Any) -> Optional[datetime]:
    parsed = _to_datetime(value)
    if not parsed:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _persisted_datetime_iso(value: Any) -> Optional[str]:
    parsed = _to_datetime(value)
    if not parsed:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    # ClickHouse DateTime values written from local naive datetimes are read
    # back by clickhouse-connect eight hours earlier in this project setup.
    # Restore the business-time convention expected by the UI.
    return (parsed + timedelta(hours=8)).isoformat()


def _ensure_system_task_run_log_table() -> bool:
    global _SYSTEM_TASK_RUN_LOG_READY
    if _SYSTEM_TASK_RUN_LOG_READY:
        return True
    with _SYSTEM_TASK_RUN_LOG_LOCK:
        if _SYSTEM_TASK_RUN_LOG_READY:
            return True
        try:
            from utils.market_warehouse import clickhouse_client

            clickhouse_client().command(
                """
                CREATE TABLE IF NOT EXISTS system_task_run_log
                (
                    run_id String,
                    task_name String,
                    status String,
                    message String,
                    trigger_source String,
                    started_at Nullable(DateTime),
                    finished_at Nullable(DateTime),
                    last_run_at Nullable(DateTime),
                    last_success_at Nullable(DateTime),
                    last_error_at Nullable(DateTime),
                    duration_sec Nullable(Float64),
                    results_json String,
                    error String,
                    created_at DateTime
                )
                ENGINE = MergeTree
                ORDER BY (task_name, created_at, status)
                """
            )
            _SYSTEM_TASK_RUN_LOG_READY = True
            return True
        except Exception as exc:
            _warn_task_run_log_failure("ensure system_task_run_log failed", exc)
            return False


def _persist_system_task_run(task_name: str, status: str, message: str, task: Dict[str, Any]) -> None:
    if not _ensure_system_task_run_log_table():
        return
    try:
        from utils.market_warehouse import clickhouse_client

        created_at = datetime.now()
        row = (
            f"{task_name}:{status}:{created_at:%Y%m%d%H%M%S%f}",
            task_name,
            status,
            str(message or ""),
            str(task.get("trigger_source") or "auto"),
            _ch_datetime(task.get("started_at")),
            created_at,
            _ch_datetime(task.get("last_run_at")),
            _ch_datetime(task.get("last_success_at")),
            _ch_datetime(task.get("last_error_at")),
            _safe_float(task.get("last_duration_sec")),
            _compact_json(task.get("results")),
            str(task.get("last_error") or task.get("error") or ""),
            created_at,
        )
        clickhouse_client().insert(
            "system_task_run_log",
            [row],
            column_names=[
                "run_id",
                "task_name",
                "status",
                "message",
                "trigger_source",
                "started_at",
                "finished_at",
                "last_run_at",
                "last_success_at",
                "last_error_at",
                "duration_sec",
                "results_json",
                "error",
                "created_at",
            ],
        )
    except Exception as exc:
        logger.warning(f"persist system task run failed task={task_name}, status={status}: {exc}")


def _load_persisted_task_runs(task_names: List[str], limit: int = 500) -> Dict[str, Any]:
    if not task_names:
        return {"by_task": {}, "timeline": []}
    cache_key = f"{'|'.join(sorted(task_names))}:{int(limit)}"
    if not _ensure_system_task_run_log_table():
        with _SYSTEM_TASK_RUN_LOG_CACHE_LOCK:
            cached = _SYSTEM_TASK_RUN_LOG_CACHE.get(cache_key)
        if cached:
            return {
                "by_task": dict(cached.get("by_task") or {}),
                "timeline": list(cached.get("timeline") or []),
            }
        return {"by_task": {}, "timeline": []}
    try:
        from utils.market_warehouse import clickhouse_client

        quoted = ", ".join("'" + name.replace("\\", "\\\\").replace("'", "\\'") + "'" for name in task_names)
        rows = clickhouse_client().query(
            f"""
            SELECT task_name, status, message, trigger_source, started_at, finished_at,
                   last_run_at, last_success_at, last_error_at, duration_sec,
                   results_json, error, created_at
            FROM system_task_run_log
            WHERE task_name IN ({quoted})
            ORDER BY created_at DESC
            LIMIT {int(limit)}
            """
        ).result_rows
    except Exception as exc:
        _warn_task_run_log_failure("load persisted task runs failed", exc)
        with _SYSTEM_TASK_RUN_LOG_CACHE_LOCK:
            cached = _SYSTEM_TASK_RUN_LOG_CACHE.get(cache_key)
        return (
            {
                "by_task": dict(cached.get("by_task") or {}),
                "timeline": list(cached.get("timeline") or []),
            }
            if cached
            else {"by_task": {}, "timeline": []}
        )

    by_task: Dict[str, Dict[str, Any]] = {}
    timeline: List[Dict[str, Any]] = []
    for row in rows:
        (
            task_name,
            status,
            message,
            trigger_source,
            started_at,
            finished_at,
            last_run_at,
            last_success_at,
            last_error_at,
            duration_sec,
            results_json,
            error,
            created_at,
        ) = row
        item = {
            "task_name": task_name,
            "status": "failed" if status == "success" and _results_json_indicates_failure(results_json) else status,
            "message": message,
            "trigger_source": trigger_source,
            "started_at": _persisted_datetime_iso(started_at),
            "finished_at": _persisted_datetime_iso(finished_at),
            "last_run_at": _persisted_datetime_iso(last_run_at),
            "last_success_at": _persisted_datetime_iso(last_success_at),
            "last_error_at": _persisted_datetime_iso(last_error_at),
            "duration_sec": round(float(duration_sec), 3) if duration_sec is not None else None,
            "duration_ms": int(round(float(duration_sec) * 1000)) if duration_sec is not None else None,
            "results_json": results_json,
            "error": error,
            "created_at": _persisted_datetime_iso(created_at),
        }
        timeline.append(item)
        bucket = by_task.setdefault(str(task_name), {})
        bucket.setdefault("latest", item)
        effective_status = item.get("status")
        if effective_status == "success":
            bucket.setdefault("last_success", item)
        elif effective_status == "failed":
            bucket.setdefault("last_failed", item)
    payload = {"by_task": by_task, "timeline": timeline}
    with _SYSTEM_TASK_RUN_LOG_CACHE_LOCK:
        _SYSTEM_TASK_RUN_LOG_CACHE[cache_key] = {
            "by_task": dict(by_task),
            "timeline": list(timeline),
        }
        with _SYSTEM_TASK_RUN_LOG_LAST_FAILURE_LOCK:
            _SYSTEM_TASK_RUN_LOG_LAST_FAILURE["ts"] = None
            _SYSTEM_TASK_RUN_LOG_LAST_FAILURE["message"] = ""
            _SYSTEM_TASK_RUN_LOG_LAST_FAILURE["signature"] = ""
    return payload


_SYSTEM_TASK_TERMINAL_STATUSES = {"success", "failed", "error", "completed"}


def _timeline_dt(item: Dict[str, Any], *keys: str) -> Optional[datetime]:
    for key in keys:
        parsed = _to_datetime(item.get(key))
        if parsed:
            return parsed
    return None


def _same_timeline_run(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    a_started = _timeline_dt(a, "started_at", "last_run_at")
    b_started = _timeline_dt(b, "started_at", "last_run_at")
    if a_started and b_started:
        return abs((a_started - b_started).total_seconds()) < 1
    return True


def _dedupe_timeline_events(timeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in timeline:
        task_name = str(item.get("task_name") or "")
        status = str(item.get("status") or "").lower()
        event_time = _timeline_dt(item, "started_at", "last_run_at", "created_at")
        event_key = event_time.isoformat(timespec="seconds") if event_time else str(item.get("created_at") or "")
        key = (task_name, status, event_key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _filter_stale_running_timeline(
    timeline: List[Dict[str, Any]],
    task_status_by_name: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Hide start markers once a terminal record for the same run is visible."""
    terminal_by_task: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in timeline:
        status = str(item.get("status") or "").lower()
        if status in _SYSTEM_TASK_TERMINAL_STATUSES:
            terminal_by_task[str(item.get("task_name") or "")].append(item)

    filtered: List[Dict[str, Any]] = []
    for item in timeline:
        task_name = str(item.get("task_name") or "")
        status = str(item.get("status") or "").lower()
        if status != "running":
            filtered.append(item)
            continue

        item_created = _timeline_dt(item, "created_at", "started_at", "last_run_at")
        has_later_terminal = False
        for terminal in terminal_by_task.get(task_name, []):
            terminal_created = _timeline_dt(terminal, "created_at", "finished_at", "last_success_at", "last_error_at")
            if item_created and terminal_created and terminal_created < item_created:
                continue
            if _same_timeline_run(item, terminal):
                has_later_terminal = True
                break
        if has_later_terminal:
            continue

        current_task = task_status_by_name.get(task_name) or {}
        if not current_task.get("is_running"):
            continue
        filtered.append(item)
    return filtered


def _load_system_alert_email_config() -> Dict[str, Any]:
    email_cfg = app_config.get("email", default=None, config_file="settings.yaml") or {}
    if email_cfg.get("enabled", True) is False:
        raise RuntimeError("email.enabled is not enabled")

    smtp_server = str(email_cfg.get("smtp_server") or "").strip()
    smtp_user = str(email_cfg.get("smtp_user") or "").strip()
    smtp_password = str(email_cfg.get("smtp_password") or "").strip()
    from_email = str(email_cfg.get("from_email") or "").strip()
    try:
        smtp_port = int(email_cfg.get("smtp_port") or 0)
    except Exception:
        smtp_port = 0

    to_emails = email_cfg.get("to_emails") or []
    if isinstance(to_emails, str):
        to_emails = [to_emails]
    to_emails = [str(item).strip() for item in to_emails if str(item).strip()]

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


def _system_smtp_attempt_plans(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    plans: List[Dict[str, Any]] = []
    port = int(cfg.get("port") or 0)
    use_ssl = bool(cfg.get("use_ssl"))
    if use_ssl:
        plans.append({"mode": "ssl", "port": port})
        if port == 465:
            plans.append({"mode": "starttls", "port": 587})
    else:
        if port == 587:
            plans.append({"mode": "starttls", "port": port})
        plans.append({"mode": "plain", "port": port})
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in plans:
        key = (item.get("mode"), item.get("port"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _open_system_alert_smtp_server(cfg: Dict[str, Any]):
    errors: List[str] = []
    for plan in _system_smtp_attempt_plans(cfg):
        server = None
        try:
            mode = str(plan.get("mode") or "")
            port = int(plan.get("port") or 0)
            if mode == "ssl":
                server = smtplib.SMTP_SSL(cfg["server"], port, timeout=30)
            else:
                server = smtplib.SMTP(cfg["server"], port, timeout=30)
                server.ehlo()
                if mode == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
            server.login(cfg["username"], cfg["password"])
            return server
        except Exception as exc:
            errors.append(f"{plan.get('mode')}:{plan.get('port')} -> {exc}")
            if server is not None:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
    raise RuntimeError("SMTP send failed: " + " | ".join(errors))


def _send_system_alert_email(subject: str, body: str) -> Dict[str, Any]:
    cfg = _load_system_alert_email_config()
    msg = MIMEMultipart()
    msg["From"] = cfg["from_email"]
    msg["To"] = ", ".join(cfg["to_emails"])
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    server = _open_system_alert_smtp_server(cfg)
    try:
        server.sendmail(cfg["from_email"], cfg["to_emails"], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return {"sent": True, "to": cfg["to_emails"], "subject": subject}


def _maybe_send_minute_kline_coverage_alert(result: Dict[str, Any], threshold: float = 0.95) -> Dict[str, Any]:
    after = result.get("after") or {}
    low_periods = []
    for period, summary in after.items():
        total = int(summary.get("total_codes") or 0)
        complete = int(summary.get("complete_codes") or 0)
        coverage = (complete / total) if total > 0 else 0.0
        summary["coverage_ratio"] = round(coverage, 6)
        if coverage < threshold:
            low_periods.append(
                {
                    "period": period,
                    "coverage_ratio": coverage,
                    "complete_codes": complete,
                    "total_codes": total,
                    "still_bad_codes": int(summary.get("still_bad_codes") or max(0, total - complete)),
                }
            )

    if not low_periods:
        return {"sent": False, "reason": "coverage_ok", "threshold": threshold, "low_periods": []}

    trade_dates = result.get("trade_dates") or []
    lines = [
        "AiStock minute K-line repair validation coverage is below threshold; please refresh QMT local history and rebuild.",
        "",
        f"trade_dates: {', '.join(map(str, trade_dates))}",
        f"threshold: {threshold:.0%}",
        "",
        "low coverage periods:",
    ]
    for item in low_periods:
        lines.append(
            f"- {item['period']}: {item['coverage_ratio']:.2%} "
            f"({item['complete_codes']}/{item['total_codes']} complete, bad={item['still_bad_codes']})"
        )

    qmt_result = result.get("qmt_xtquant_result") or {}
    if qmt_result:
        lines.extend(
            [
                "",
                "QMT minute repair result:",
                f"- returncode: {qmt_result.get('returncode')}",
                f"- report_path: {qmt_result.get('report_path')}",
                f"- repair_code_count: {qmt_result.get('repair_code_count')}",
            ]
        )

    try:
        email_result = _send_system_alert_email(
            subject="[AiStock] minute K-line coverage below 95%; please refresh QMT local history",
            body="\n".join(lines),
        )
        logger.warning("minute kline coverage alert email sent: %s", email_result)
        return {"sent": True, "threshold": threshold, "low_periods": low_periods, **email_result}
    except Exception as exc:
        logger.error(f"minute kline coverage alert email failed: {exc}")
        return {"sent": False, "reason": str(exc), "threshold": threshold, "low_periods": low_periods}


def _calc_duration_profile(task_name: str, recent_limit: int = 10) -> Dict[str, Any]:
    durations: List[float] = []
    for item in reversed(task_manager.timeline):
        if item.get("task_name") != task_name:
            continue
        if item.get("status") not in {"success", "failed"}:
            continue
        sec = _safe_float(item.get("duration_sec"))
        if sec is None:
            continue
        durations.append(sec)
        if len(durations) >= recent_limit:
            break

    if not durations:
        return {
            "recent_run_count": 0,
            "recent_avg_duration_sec": None,
            "recent_min_duration_sec": None,
            "recent_max_duration_sec": None,
        }

    return {
        "recent_run_count": len(durations),
        "recent_avg_duration_sec": round(sum(durations) / len(durations), 3),
        "recent_min_duration_sec": round(min(durations), 3),
        "recent_max_duration_sec": round(max(durations), 3),
    }


def _is_trading_day_today(session) -> bool:
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        row = session.execute(
            text(
                "SELECT is_trading FROM trade_calendar "
                "WHERE trade_date = :d AND market='SH' LIMIT 1"
            ),
            {"d": today},
        ).fetchone()
        if not row:
            return False
        return bool(row[0] == 1 or row[0] is True)
    except Exception:
        return False


def _ensure_trade_calendar_fresh_for_today(task_name: str) -> Dict[str, Any]:
    global _trade_calendar_preflight_date

    today = datetime.now().strftime("%Y-%m-%d")
    with _trade_calendar_preflight_lock:
        if _trade_calendar_preflight_date != today:
            try:
                logger.info(f"preflight trade calendar sync before {task_name}: {today}")
                update_trade_calendar_task()
                status = task_manager.get_task_status("update_trade_calendar")
                if status.get("error"):
                    raise RuntimeError(str(status.get("error")))
                _trade_calendar_preflight_date = today
            except Exception as exc:
                msg = f"trade_calendar_preflight_failed: {exc}"
                logger.error(msg)
                task_manager.update_progress(task_name, {"current": 0, "total": 0, "message": msg})
                task_manager.set_results(
                    task_name,
                    {
                        "message": msg,
                        "skipped": True,
                        "skip_reason": "trade_calendar_preflight_failed",
                        "validation_status": "failed",
                    },
                )
                task_manager.set_error(task_name, msg)
                return {"ok": False, "trading_day": False, "message": msg}

    try:
        from scheduler.trading_calendar import TradingCalendar

        now_dt = datetime.now()
        if not TradingCalendar.is_trading_day(now_dt):
            msg = f"skip auto data update for non-trading day after calendar sync: {today}"
            logger.info(msg)
            task_manager.update_progress(task_name, {"current": 0, "total": 0, "message": msg})
            task_manager.set_results(
                task_name,
                {
                    "message": msg,
                    "skipped": True,
                    "skip_reason": "non_trading_day",
                    "validation_status": "skipped_non_trading_day",
                },
                mark_success=True,
            )
            return {"ok": True, "trading_day": False, "message": msg}
        return {"ok": True, "trading_day": True, "message": "trading_day"}
    except Exception as exc:
        msg = f"trade_calendar_check_failed: {exc}"
        logger.error(msg)
        task_manager.update_progress(task_name, {"current": 0, "total": 0, "message": msg})
        task_manager.set_results(
            task_name,
            {
                "message": msg,
                "skipped": True,
                "skip_reason": "trade_calendar_check_failed",
                "validation_status": "failed",
            },
        )
        task_manager.set_error(task_name, msg)
        return {"ok": False, "trading_day": False, "message": msg}


def _run_core_data_update_after_trade_calendar(task_name: str, task_fn):
    preflight = _ensure_trade_calendar_fresh_for_today(task_name)
    if not preflight.get("ok") or not preflight.get("trading_day"):
        return
    return task_fn()


def _resolve_intraday_daily_trade_date(now_dt: Optional[datetime] = None) -> str:
    """Use previous trade date before 09:15; today's price is not reliable yet."""
    from scheduler.trading_calendar import TradingCalendar

    now_dt = now_dt or datetime.now()
    if TradingCalendar.is_trading_day(now_dt) and now_dt.time() >= dt_time(9, 15):
        return now_dt.strftime("%Y-%m-%d")
    return TradingCalendar.get_previous_trading_day(now_dt).strftime("%Y-%m-%d")


def _is_trading_preopen_before_0915(now_dt: Optional[datetime] = None) -> bool:
    from scheduler.trading_calendar import TradingCalendar

    now_dt = now_dt or datetime.now()
    return bool(TradingCalendar.is_trading_day(now_dt) and now_dt.time() < dt_time(9, 15))


def _is_intraday_auto_update_window(now_dt: Optional[datetime] = None) -> bool:
    from scheduler.trading_calendar import TradingCalendar

    now_dt = now_dt or datetime.now()
    if not TradingCalendar.is_trading_day(now_dt):
        return False
    current_time = now_dt.time()
    return (dt_time(9, 15) <= current_time <= dt_time(11, 35)) or (
        dt_time(13, 0) <= current_time <= dt_time(15, 5)
    )


def _parse_data_source_count_date(value: Optional[str]) -> str:
    raw = (value or datetime.now().strftime("%Y-%m-%d")).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise HTTPException(status_code=400, detail="trade_date must be YYYY-MM-DD")
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="trade_date is invalid")
    return raw


def _data_source_empty_row(key: str, label: str, category: str) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "category": category,
        "row_count": 0,
        "code_count": 0,
        "expected_row_count": 0,
        "missing_rows": 0,
        "extra_rows": 0,
        "extended_rows": 0,
        "coverage_rate": 0.0,
        "latest_date": None,
        "periods": [],
        "ok": False,
        "complete": False,
        "message": "no data",
    }


def _clickhouse_date_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _is_month_partitioned_minute_table(client, table_name: str) -> bool:
    rows = client.query(
        "SELECT partition_key FROM system.tables WHERE database = currentDatabase() AND name = %(table)s",
        parameters={"table": table_name},
    ).result_rows
    return bool(rows and "toYYYYMM(datetime)" in str(rows[0][0] or ""))


def _resolve_latest_data_source_count_date(client) -> str:
    from utils.market_warehouse import clickhouse_table_exists

    queries: List[str] = []
    if clickhouse_table_exists("kline_daily"):
        queries.append("SELECT max(trade_date) FROM kline_daily")
    if clickhouse_table_exists("sector_kline_daily"):
        queries.append("SELECT max(trade_date) FROM sector_kline_daily")
    for table_name in ("kline_minute_5", "kline_minute_15", "kline_minute_30", "kline_minute_60"):
        if clickhouse_table_exists(table_name):
            queries.append(f"SELECT max(toDate(datetime)) FROM {table_name}")

    latest_dates: List[str] = []
    for sql in queries:
        try:
            rows = client.query(sql).result_rows
            text_value = _clickhouse_date_text(rows[0][0] if rows else None)
            if text_value:
                latest_dates.append(text_value)
        except Exception as exc:
            logger.warning(f"resolve latest data source date failed: sql={sql}, error={exc}")

    return max(latest_dates) if latest_dates else datetime.now().strftime("%Y-%m-%d")


def _query_data_source_counts(
    client,
    table_name: str,
    date_expr: str,
    target_date: str,
    stock_side: bool,
) -> Dict[str, Any]:
    join_key = "k.code" if table_name == "kline_daily" else "assumeNotNull(k.code)"
    from utils.qmt_universe import bond_index_exclusion_sql

    side_filter = "s.type = 'stock'" if stock_side else f"s.type = 'index' AND {bond_index_exclusion_sql('s.name')}"
    listing_filter = f"AND (toDateOrNull(toString(s.list_date)) IS NULL OR toDateOrNull(toString(s.list_date)) <= toDate('{target_date}'))"
    rows = client.query(
        f"""
        SELECT count() AS row_count, uniqExact({join_key}) AS code_count
        FROM {table_name} k
        LEFT JOIN stocks s ON {join_key} = s.code
        WHERE {date_expr} = toDate('{target_date}')
          AND {join_key} != ''
          AND {side_filter}
          {listing_filter}
          AND (k.volume > 0 OR k.amount > 0)
        """
    ).result_rows
    row = rows[0] if rows else (0, 0)
    latest_expr = date_expr.replace("k.", "")
    latest_rows = client.query(f"SELECT max({latest_expr}) FROM {table_name}").result_rows
    return {
        "row_count": int(row[0] or 0),
        "code_count": int(row[1] or 0),
        "latest_date": _clickhouse_date_text(latest_rows[0][0] if latest_rows else None),
    }


def _query_minute_data_source_counts(
    client,
    table_name: str,
    target_date: str,
    stock_side: bool,
    expected_per_code: int,
) -> Dict[str, Any]:
    join_key = "assumeNotNull(k.code)"
    from utils.qmt_universe import bond_index_exclusion_sql

    side_filter = "s.type = 'stock'" if stock_side else f"s.type = 'index' AND {bond_index_exclusion_sql('s.name')}"
    rows = client.query(
        f"""
        WITH daily_codes AS (
            SELECT kd.code AS code
            FROM kline_daily kd
            INNER JOIN stocks s ON kd.code = s.code
            WHERE kd.trade_date = toDate('{target_date}')
              AND {side_filter}
              AND (toDateOrNull(toString(s.list_date)) IS NULL OR toDateOrNull(toString(s.list_date)) <= toDate('{target_date}'))
              AND (kd.volume > 0 OR kd.amount > 0)
            GROUP BY kd.code
        ), bars_by_code AS (
            SELECT {join_key} AS code, count() AS bars
            FROM {table_name} k
            WHERE k.datetime >= toDateTime('{target_date} 00:00:00')
              AND k.datetime < toDateTime('{target_date} 00:00:00') + INTERVAL 1 DAY
              AND {join_key} != ''
              AND {join_key} IN (SELECT code FROM daily_codes)
            GROUP BY {join_key}
        )
        SELECT
            sum(ifNull(b.bars, 0)) AS row_count,
            countIf(ifNull(b.bars, 0) > 0) AS code_count,
            sum(greatest({int(expected_per_code)} - ifNull(b.bars, 0), 0)) AS missing_rows,
            sum(greatest(ifNull(b.bars, 0) - {int(expected_per_code)}, 0)) AS extended_rows
        FROM daily_codes d
        LEFT JOIN bars_by_code b ON d.code = b.code
        """
    ).result_rows
    row = rows[0] if rows else (0, 0, 0, 0)
    latest_rows = client.query(f"SELECT max(toDate(datetime)) FROM {table_name}").result_rows
    return {
        "row_count": int(row[0] or 0),
        "code_count": int(row[1] or 0),
        "missing_rows": int(row[2] or 0),
        "extended_rows": int(row[3] or 0),
        "latest_date": _clickhouse_date_text(latest_rows[0][0] if latest_rows else None),
    }


def _build_daily_data_source_row(
    client,
    table_name: str,
    key: str,
    label: str,
    category: str,
    target_date: str,
    stock_side: Optional[bool] = None,
) -> Dict[str, Any]:
    from utils.market_warehouse import clickhouse_table_exists

    row = _data_source_empty_row(key, label, category)
    if not clickhouse_table_exists(table_name):
        row["message"] = f"{table_name} missing"
        return row

    if stock_side is None:
        result_rows = client.query(
            f"""
            SELECT count() AS row_count, uniqExact(code) AS code_count
            FROM {table_name}
            WHERE trade_date = toDate('{target_date}')
            """
        ).result_rows
        latest_rows = client.query(f"SELECT max(trade_date) FROM {table_name}").result_rows
        result = {
            "row_count": int((result_rows[0][0] if result_rows else 0) or 0),
            "code_count": int((result_rows[0][1] if result_rows else 0) or 0),
            "latest_date": _clickhouse_date_text(latest_rows[0][0] if latest_rows else None),
        }
    else:
        result = _query_data_source_counts(client, table_name, "k.trade_date", target_date, stock_side)

    row.update(result)
    row["ok"] = row["row_count"] > 0 and row["code_count"] > 0
    row["expected_row_count"] = None
    row["missing_rows"] = 0
    row["extra_rows"] = 0
    row["coverage_rate"] = None
    row["complete"] = False
    row["verification_status"] = "unverified"
    row["message"] = "ok" if row["ok"] else "missing for date"
    return row


def _build_minute_data_source_row(
    client,
    key: str,
    label: str,
    category: str,
    target_date: str,
    stock_side: bool,
) -> Dict[str, Any]:
    from utils.market_warehouse import clickhouse_table_exists

    row = _data_source_empty_row(key, label, category)
    periods = [
        ("5m", "kline_minute_5", 48),
        ("15m", "kline_minute_15", 16),
        ("30m", "kline_minute_30", 8),
        ("60m", "kline_minute_60", 4),
    ]
    total_rows = 0
    total_expected_rows = 0
    total_missing_rows = 0
    total_extended_rows = 0
    max_codes = 0
    latest_dates: List[str] = []
    detail_rows: List[Dict[str, Any]] = []
    expected_code_count = 0

    if clickhouse_table_exists("kline_daily"):
        daily_baseline = _query_data_source_counts(client, "kline_daily", "k.trade_date", target_date, stock_side)
        expected_code_count = int(daily_baseline.get("code_count") or 0)

    for period, table_name, expected_per_code in periods:
        if not clickhouse_table_exists(table_name):
            detail_rows.append({
                "period": period,
                "table": table_name,
                "row_count": 0,
                "code_count": 0,
                "expected_per_code": expected_per_code,
                "expected_row_count": 0,
                "missing_rows": 0,
                "extra_rows": 0,
                "extended_rows": 0,
                "coverage_rate": 0.0,
                "latest_date": None,
                "ok": False,
                "complete": False,
                "message": "table missing",
            })
            continue
        result = _query_minute_data_source_counts(client, table_name, target_date, stock_side, expected_per_code)
        expected_row_count = expected_code_count * expected_per_code
        missing_rows = int(result.get("missing_rows") or 0)
        extended_rows = int(result.get("extended_rows") or 0)
        coverage_rate = (float(expected_row_count - missing_rows) / expected_row_count) if expected_row_count > 0 else 0.0
        complete = (
            expected_row_count > 0
            and int(result["code_count"] or 0) == expected_code_count
            and missing_rows == 0
        )
        total_rows += result["row_count"]
        total_expected_rows += expected_row_count
        total_missing_rows += missing_rows
        total_extended_rows += extended_rows
        max_codes = max(max_codes, expected_code_count, result["code_count"])
        if result.get("latest_date"):
            latest_dates.append(result["latest_date"])
        detail_rows.append({
            "period": period,
            "table": table_name,
            **result,
            "expected_code_count": expected_code_count,
            "expected_per_code": expected_per_code,
            "expected_row_count": expected_row_count,
            "missing_rows": missing_rows,
            "extra_rows": 0,
            "extended_rows": extended_rows,
            "coverage_rate": round(coverage_rate, 6),
            "ok": result["row_count"] > 0 and result["code_count"] > 0,
            "complete": complete,
            "message": "complete" if complete else ("incomplete" if result["row_count"] > 0 else "missing for date"),
        })

    row.update({
        "row_count": total_rows,
        "code_count": max_codes,
        "expected_row_count": total_expected_rows,
        "missing_rows": total_missing_rows,
        "extra_rows": 0,
        "extended_rows": total_extended_rows,
        "coverage_rate": round(float(total_expected_rows - total_missing_rows) / total_expected_rows, 6) if total_expected_rows > 0 else 0.0,
        "latest_date": max(latest_dates) if latest_dates else None,
        "periods": detail_rows,
    })
    row["ok"] = total_rows > 0 and max_codes > 0
    # This legacy query covers only codes present in the daily table. It is a
    # sample diagnostic, not independent delivery verification (see /operations).
    row["coverage_rate"] = None
    row["complete"] = False
    row["verification_status"] = "unverified"
    row["message"] = "sample_only_use_data_health" if row["ok"] else "missing for date"
    for detail in detail_rows:
        detail["coverage_rate"] = None
        detail["complete"] = False
        detail["verification_status"] = "unverified"
    return row


def _build_data_source_counts_payload(trade_date: Optional[str]) -> Dict[str, Any]:
    from utils.market_warehouse import clickhouse_available, clickhouse_client, clickhouse_table_exists

    if not clickhouse_available():
        raise HTTPException(status_code=503, detail="ClickHouse unavailable")

    client = clickhouse_client()
    if not clickhouse_table_exists("stocks"):
        raise HTTPException(status_code=503, detail="stocks table missing")

    target_date = _parse_data_source_count_date(trade_date) if str(trade_date or "").strip() else _resolve_latest_data_source_count_date(client)

    rows = [
        _build_daily_data_source_row(client, "kline_daily", "stock_daily", "个股日线", "daily", target_date, True),
        _build_minute_data_source_row(client, "stock_minute", "个股分钟", "minute", target_date, True),
        _build_daily_data_source_row(client, "kline_daily", "index_daily", "指数日线", "daily", target_date, False),
        _build_minute_data_source_row(client, "index_minute", "指数分钟", "minute", target_date, False),
        _build_daily_data_source_row(client, "sector_kline_daily", "sector_daily", "板块日线", "daily", target_date, None),
    ]
    latest_available_date = max([item.get("latest_date") for item in rows if item.get("latest_date")] or [None])
    missing_count = len([item for item in rows if not item.get("ok")])
    incomplete_count = len([item for item in rows if not item.get("complete")])
    return {
        "trade_date": target_date,
        "latest_available_date": latest_available_date,
        "checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
        "summary": {
            "total": len(rows),
            "ok": len(rows) - missing_count,
            "missing": missing_count,
            "complete": len(rows) - incomplete_count,
            "incomplete": incomplete_count,
        },
        "rows": rows,
    }


def _cleanup_incomplete_stock_minute_rows_for_date(target_date: str) -> Dict[str, Any]:
    from utils.market_warehouse import clickhouse_client, clickhouse_table_exists

    client = clickhouse_client()
    periods = [
        ("5m", "kline_minute_5", 48),
        ("15m", "kline_minute_15", 16),
        ("30m", "kline_minute_30", 8),
        ("60m", "kline_minute_60", 4),
    ]
    summary: Dict[str, Any] = {}
    for period, table_name, expected_count in periods:
        if not clickhouse_table_exists(table_name):
            continue
        if not _is_month_partitioned_minute_table(client, table_name):
            summary[period] = {
                "table": table_name,
                "skipped": True,
                "reason": "unpartitioned_target_append_only",
            }
            continue
        rows = client.query(
            f"""
            SELECT assumeNotNull(k.code) AS code, count() AS row_count
            FROM {table_name} k
            LEFT JOIN stocks s ON assumeNotNull(k.code) = s.code
            WHERE toDate(k.datetime) = toDate('{target_date}')
              AND s.type = 'stock'
            GROUP BY code
            HAVING row_count != {expected_count}
            """
        ).result_rows
        codes = [str(row[0]) for row in rows if row and row[0]]
        if codes:
            code_sql = ",".join("'" + code.replace("\\", "\\\\").replace("'", "\\'") + "'" for code in codes)
            client.command(
                f"""
                ALTER TABLE {table_name}
                DELETE WHERE toDate(datetime) = toDate('{target_date}')
                  AND code IN ({code_sql})
                SETTINGS mutations_sync = 1
                """
            )
        summary[period] = {
            "table": table_name,
            "expected_per_code": expected_count,
            "removed_codes": codes[:200],
            "removed_code_count": len(codes),
            "removed_rows": sum(int(row[1] or 0) for row in rows),
        }
    return summary


def data_source_date_repair_task(trade_date: str, task_already_started: bool = False):
    task_name = "data_source_date_repair"
    normalized_date = _parse_data_source_count_date(trade_date)

    if not data_update_lock.acquire(blocking=False):
        msg = "已有其它数据更新任务正在执行，按日期修复跳过"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    try:
        if not task_already_started:
            if not task_manager.start_task(task_name, trigger_source="manual"):
                task_manager.set_error(task_name, "date repair task is already running")
                return

        try:
            from scheduler.trading_calendar import TradingCalendar

            target_dt = datetime.strptime(normalized_date, "%Y-%m-%d")
            if not TradingCalendar.is_trading_day(target_dt):
                msg = f"skip data source date repair for non-trading day: {normalized_date}"
                task_manager.update_progress(task_name, {"current": 0, "total": 0, "message": msg})
                task_manager.set_results(
                    task_name,
                    {
                        "message": msg,
                        "trade_date": normalized_date,
                        "skipped": True,
                        "skip_reason": "non_trading_day",
                        "validation_status": "skipped_non_trading_day",
                    },
                    mark_success=True,
                )
                return
        except Exception as exc:
            logger.warning(f"failed to validate repair trade date {normalized_date}: {exc}")

        task_manager.update_progress(
            task_name,
            {"current": 0, "total": 2, "message": f"弢始修?{normalized_date} 日线数据"},
        )

        preferred_source = str(app_config.get("data_sync.preferred_source", "qmt_xtquant") or "qmt_xtquant").strip().lower()
        if preferred_source in {"qmt", "qmtmini", "qmt_xtquant", "xtquant"}:
            if not _runtime_has_xtquant():
                _mark_qmt_host_collector_delegated(
                    task_name,
                    scenario="manual-date-repair",
                    target_date=normalized_date,
                    periods=["1d", "5m", "15m", "30m", "60m"],
                    extra={"preferred_source": preferred_source},
                )
                return

            qmt_script = REPO_ROOT / "scripts" / "qmt_xtquant_data_source_task.py"
            if not qmt_script.exists():
                raise RuntimeError(f"script_not_found:{qmt_script}")
            qmt_report = report_path("system_data_source_repair", f"qmt_xtquant_{normalized_date}_{datetime.now():%Y%m%d_%H%M%S}.json")
            qmt_report.parent.mkdir(parents=True, exist_ok=True)
            qmt_daily_batch_size = int(app_config.get("data_sync.qmt_xtquant.daily_batch_size", 80) or 80)
            qmt_minute_batch_size = int(app_config.get("data_sync.qmt_xtquant.minute_batch_size", 30) or 30)
            qmt_minute_periods_raw = app_config.get(
                "data_sync.qmt_xtquant.minute_periods",
                ["5m", "15m", "30m", "60m"],
            )
            if isinstance(qmt_minute_periods_raw, (list, tuple)):
                qmt_minute_periods = ",".join(str(item).strip() for item in qmt_minute_periods_raw if str(item).strip())
            else:
                qmt_minute_periods = str(qmt_minute_periods_raw or "5m,15m,30m,60m").strip()
            qmt_minute_phase = str(app_config.get("data_sync.qmt_xtquant.minute_phase", "fetch-validate") or "fetch-validate").strip()
            if qmt_minute_phase not in {"fetch", "validate-stage", "fetch-validate", "apply", "all"}:
                qmt_minute_phase = "fetch-validate"
            qmt_max_retries = int(app_config.get("data_sync.qmt_xtquant.max_retries", 2) or 2)
            qmt_retry_sleep = float(app_config.get("data_sync.qmt_xtquant.retry_sleep_sec", 1.0) or 1.0)
            qmt_minute_batch_timeout = int(app_config.get("data_sync.qmt_xtquant.minute_batch_timeout_sec", 180) or 180)
            qmt_timeout_sec = int(app_config.get("data_sync.qmt_xtquant.timeout_sec", 3 * 60 * 60) or (3 * 60 * 60))
            qmt_cmd = [
                sys.executable,
                str(qmt_script),
                "--mode",
                "date-repair",
                "--start-date",
                normalized_date,
                "--end-date",
                normalized_date,
                "--daily-phase",
                "all",
                "--daily-batch-size",
                str(qmt_daily_batch_size),
                "--minute-batch-size",
                str(qmt_minute_batch_size),
                "--minute-periods",
                qmt_minute_periods,
                "--minute-phase",
                qmt_minute_phase,
                "--max-retries",
                str(qmt_max_retries),
                "--retry-sleep",
                str(qmt_retry_sleep),
                "--minute-batch-timeout-sec",
                str(qmt_minute_batch_timeout),
                "--report",
                str(qmt_report),
            ]
            qmt_proc = subprocess.run(
                qmt_cmd,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=qmt_timeout_sec,
            )
            qmt_ok = qmt_proc.returncode == 0
            coverage = _build_data_source_counts_payload(normalized_date)
            coverage_summary = coverage.get("summary") or {}
            result = {
                "message": f"{normalized_date} QMT/xtquant data source repair completed" if qmt_ok else f"{normalized_date} QMT/xtquant data source repair did not fully pass",
                "trade_date": normalized_date,
                "preferred_source": preferred_source,
                "qmt_xtquant": {
                    "ok": qmt_ok,
                    "returncode": qmt_proc.returncode,
                    "report_path": str(qmt_report),
                    "stdout_tail": (qmt_proc.stdout or "")[-4000:],
                    "stderr_tail": (qmt_proc.stderr or "")[-4000:],
                },
                "coverage": coverage,
                "validation_status": "passed" if qmt_ok else "failed",
            }
            task_manager.update_progress(task_name, {"current": 2, "total": 2, "message": result["message"]})
            task_manager.set_results(task_name, result, mark_success=qmt_ok)
            if not qmt_ok:
                task_manager.set_error(
                    task_name,
                    result["qmt_xtquant"]["stderr_tail"]
                    or result["qmt_xtquant"]["stdout_tail"]
                    or f"{result['message']}; coverage_summary={coverage_summary}",
                )
            return

        raise RuntimeError("Legacy TDX data-source repair is disabled; use data_sync.preferred_source=qmt_xtquant")

        daily_script = REPO_ROOT / "scripts" / "backfill_tqcenter_daily_to_clickhouse.py"
        minute_script = REPO_ROOT / "scripts" / "build_tdx_minute_periods.py"
        if not daily_script.exists():
            raise RuntimeError(f"script_not_found:{daily_script}")
        if not minute_script.exists():
            raise RuntimeError(f"script_not_found:{minute_script}")

        daily_report = report_path("system_data_source_repair", f"daily_{normalized_date}_{datetime.now():%Y%m%d_%H%M%S}.json")
        daily_report.parent.mkdir(parents=True, exist_ok=True)
        daily_cmd = [
            sys.executable,
            str(daily_script),
            "--phase",
            "all",
            "--start-date",
            normalized_date,
            "--end-date",
            normalized_date,
            "--batch-size",
            "200",
            "--reset-stage",
            "--report",
            str(daily_report),
        ]
        daily_proc = subprocess.run(
            daily_cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45 * 60,
        )
        daily_ok = daily_proc.returncode == 0
        task_manager.update_progress(
            task_name,
            {"current": 1, "total": 2, "message": f"daily repair done; start minute repair for {normalized_date}"},
        )

        minute_cmd = [
            sys.executable,
            str(minute_script),
            "--tdx-root",
            os.getenv("AISTOCK_LOCAL_TDX_ROOT", r"D:\TDX\vipdoc"),
            "--start-date",
            normalized_date,
            "--end-date",
            normalized_date,
            "--periods",
            "5m,15m,30m,60m",
            "--delete-range",
        ]
        minute_proc = subprocess.run(
            minute_cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60 * 60,
        )
        minute_ok = minute_proc.returncode == 0
        minute_cleanup = _cleanup_incomplete_stock_minute_rows_for_date(normalized_date) if minute_ok else {}
        coverage = _build_data_source_counts_payload(normalized_date)
        coverage_summary = coverage.get("summary") or {}
        coverage_ok = (
            int(coverage_summary.get("missing") or 0) == 0
            and int(coverage_summary.get("incomplete") or 0) == 0
        )
        validation_status = "passed" if daily_ok and minute_ok and coverage_ok else "failed"
        result = {
            "message": f"{normalized_date} 数据源按日期修复完成" if validation_status == "passed" else f"{normalized_date} 数据源按日期修复未完全过",
            "trade_date": normalized_date,
            "daily": {
                "ok": daily_ok,
                "returncode": daily_proc.returncode,
                "report_path": str(daily_report),
                "stdout_tail": (daily_proc.stdout or "")[-4000:],
                "stderr_tail": (daily_proc.stderr or "")[-4000:],
            },
            "minute": {
                "ok": minute_ok,
                "returncode": minute_proc.returncode,
                "stdout_tail": (minute_proc.stdout or "")[-4000:],
                "stderr_tail": (minute_proc.stderr or "")[-4000:],
                "cleanup": minute_cleanup,
            },
            "coverage": coverage,
            "validation_status": validation_status,
        }
        task_manager.update_progress(task_name, {"current": 2, "total": 2, "message": result["message"]})
        task_manager.set_results(task_name, result, mark_success=validation_status == "passed")
        if validation_status != "passed":
            task_manager.set_error(
                task_name,
                result["daily"]["stderr_tail"]
                or result["minute"]["stderr_tail"]
                or f"{result['message']}; coverage_summary={coverage_summary}",
            )
    except subprocess.TimeoutExpired as exc:
        task_manager.set_error(task_name, f"按日期数据源修复超时: {exc}")
    except Exception as exc:
        logger.exception(f"按日期数据源修复失败: {exc}")
        task_manager.set_error(task_name, str(exc))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def _skip_intraday_auto_update_if_closed(task_name: str, force: bool = False) -> bool:
    from scheduler.trading_calendar import TradingCalendar

    now_dt = datetime.now()
    preflight = _ensure_trade_calendar_fresh_for_today(task_name)
    if not preflight.get("ok") or not preflight.get("trading_day"):
        return True
    if force:
        return False
    if _is_intraday_auto_update_window(now_dt):
        return False
    msg = f"skip intraday auto update outside trading window; now={now_dt.strftime('%Y-%m-%d %H:%M:%S')}"
    logger.info(msg)
    task_manager.update_progress(task_name, {"current": 0, "total": 0, "message": msg})
    task_manager.set_results(
        task_name,
        {
            "message": msg,
            "stats": {"total": 0, "success": 0, "failed": 0},
            "skipped": True,
            "skip_reason": "outside_intraday_auto_window",
        },
    )
    return True


def _refresh_gen2_v4_event_dataset_for_date(
    trade_date: str,
    task_name: str,
    reason: str = "daily_snapshot",
) -> Dict[str, Any]:
    """Rebuild and merge the V4 event rows for one trade date after a daily snapshot."""
    normalized_date = str(trade_date or "")[:10]
    if not normalized_date:
        return {"ok": False, "skipped": True, "reason": "empty_trade_date"}
    if not _gen2_v4_event_refresh_lock.acquire(blocking=False):
        return {"ok": False, "skipped": True, "reason": "refresh_already_running", "trade_date": normalized_date}
    try:
        script = REPO_ROOT / "scripts" / "gen2_refresh_v4_event_dataset.py"
        output_dir = report_path("gen2_event_study_full")
        if not script.exists():
            return {"ok": False, "skipped": True, "reason": f"script_missing: {script}", "trade_date": normalized_date}
        task_manager.update_progress(
            task_name,
            {"message": f"daily snapshot complete; refreshing G2 V4 event dataset for {normalized_date}"},
        )
        cmd = [
            sys.executable,
            str(script),
            "--start-date",
            normalized_date,
            "--end-date",
            normalized_date,
            "--output-dir",
            str(output_dir),
            "--no-backup",
        ]
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or f"returncode={proc.returncode}")[-1200:]
            logger.error(f"refresh G2 V4 event dataset failed: date={normalized_date}, error={err}")
            return {"ok": False, "trade_date": normalized_date, "reason": reason, "returncode": proc.returncode, "error": err}
        try:
            payload = json.loads(proc.stdout or "{}")
        except Exception:
            payload = {"stdout_tail": (proc.stdout or "")[-1200:]}
        logger.info(f"refresh G2 V4 event dataset done: date={normalized_date}, payload={payload}")
        return {"ok": True, "trade_date": normalized_date, "reason": reason, "payload": payload}
    except subprocess.TimeoutExpired as exc:
        logger.error(f"refresh G2 V4 event dataset timeout: date={normalized_date}, error={exc}")
        return {"ok": False, "trade_date": normalized_date, "reason": reason, "error": f"timeout: {exc}"}
    except Exception as exc:
        logger.exception(f"refresh G2 V4 event dataset failed: date={normalized_date}")
        return {"ok": False, "trade_date": normalized_date, "reason": reason, "error": str(exc)}
    finally:
        _gen2_v4_event_refresh_lock.release()


def _last_completed_intraday_bar_minutes(now_dt: Optional[datetime] = None, delay_seconds: int = 120) -> int:
    now_dt = now_dt or datetime.now()
    effective = now_dt.timestamp() - max(0, int(delay_seconds))
    effective_dt = datetime.fromtimestamp(effective)
    minute_of_day = effective_dt.hour * 60 + effective_dt.minute
    morning_open = 9 * 60 + 30
    morning_close = 11 * 60 + 30
    afternoon_open = 13 * 60
    afternoon_close = 15 * 60

    if minute_of_day <= morning_open:
        return 0
    if minute_of_day <= morning_close:
        return minute_of_day - morning_open
    if minute_of_day <= afternoon_open:
        return 120
    if minute_of_day <= afternoon_close:
        return 120 + (minute_of_day - afternoon_open)
    return 240


def _filter_ready_intraday_minute_periods(periods: List[str], task_name: str) -> List[str]:
    minute_periods = {"1m", "5m", "15m", "30m", "60m"}
    if not periods:
        return periods
    requested_minute_periods = [period for period in periods if period in minute_periods]
    if not requested_minute_periods or len(requested_minute_periods) != len(periods):
        return periods

    now_dt = datetime.now()
    completed_minutes = _last_completed_intraday_bar_minutes(now_dt)
    period_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
    ready_periods = [period for period in periods if completed_minutes >= period_minutes.get(period, 999)]
    if ready_periods:
        skipped_periods = [period for period in periods if period not in ready_periods]
        if skipped_periods:
            logger.info(
                f"skip not-ready intraday minute periods task={task_name}, "
                f"completed_minutes={completed_minutes}, skipped={skipped_periods}, ready={ready_periods}"
            )
        return ready_periods

    now_text = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    msg = (
        f"skip intraday minute sync before first confirmed bar; "
        f"now={now_text}, completed_minutes={completed_minutes}, periods={periods}"
    )
    logger.info(msg)
    task_manager.update_progress(task_name, {"current": 0, "total": 0, "message": msg})
    task_manager.set_results(
        task_name,
        {
            "message": msg,
            "stats": {"total": 0, "success": 0, "failed": 0},
            "skipped": True,
            "skip_reason": "before_first_confirmed_bar",
        },
    )
    return []


def _get_recent_complete_kline_dates(session, limit: int = 30) -> List[Any]:
    """
    Return recent trade dates whose stock-daily coverage is close to the active
    stock universe.
    Intraday sync can create a partial latest date; those rows must not drive
    emotion-cycle repair or the home chart's last points will jump to bad dates.
    """
    from utils.market_warehouse import clickhouse_available, clickhouse_query_df, clickhouse_scalar

    # ClickHouse is the canonical daily-bar warehouse.  The legacy SQL mirror
    # can lag it by a session, which previously made an already-complete day
    # invisible to the emotion-cycle repair job.
    if clickhouse_available():
        try:
            scan_limit = max(int(limit) * 3, 60)
            rows_df = clickhouse_query_df(
                """
                SELECT k.trade_date, count() AS row_count
                FROM kline_daily AS k FINAL
                INNER JOIN stocks s ON s.code = k.code
                WHERE s.type = 'stock'
                GROUP BY k.trade_date
                ORDER BY k.trade_date DESC
                LIMIT ?
                """,
                [scan_limit],
            )
            universe_count = int(clickhouse_scalar("SELECT count() FROM stocks WHERE type = 'stock'") or 0)
            min_count = max(MIN_EMOTION_KLINE_ROWS, int(universe_count * EMOTION_KLINE_COMPLETE_RATIO))
            if rows_df is not None and not rows_df.empty:
                dates: List[Any] = []
                for row in rows_df.itertuples(index=False):
                    if not row.trade_date or int(row.row_count or 0) < min_count:
                        continue
                    value = row.trade_date
                    # pandas returns a timezone-naive Timestamp for ClickHouse
                    # Date columns.  EmotionCycle requires a pure calendar day.
                    if hasattr(value, "date"):
                        value = value.date()
                    dates.append(value)
                return dates[: int(limit)]
        except Exception as exc:
            logger.warning("ClickHouse emotion-cycle coverage audit fallback to SQL mirror: %s", exc)

    from models.stock_models import KlineDaily, Stock

    scan_limit = max(int(limit) * 3, 60)
    rows = (
        session.query(KlineDaily.trade_date, func.count(KlineDaily.id).label("row_count"))
        .join(Stock, Stock.code == KlineDaily.code)
        .filter(Stock.type == "stock")
        .group_by(KlineDaily.trade_date)
        .order_by(desc(KlineDaily.trade_date))
        .limit(scan_limit)
        .all()
    )
    if not rows:
        return []

    universe_count = (
        session.query(func.count(Stock.id))
        .filter(Stock.type == "stock")
        .scalar()
        or 0
    )
    min_count = max(MIN_EMOTION_KLINE_ROWS, int(int(universe_count) * EMOTION_KLINE_COMPLETE_RATIO))
    dates = [trade_date for trade_date, row_count in rows if trade_date and int(row_count or 0) >= min_count]
    return dates[: int(limit)]


def _repair_emotion_for_dates(dates: List[Any]):
    """Rebuild emotion_cycle rows for selected complete daily-kline dates."""
    from sqlalchemy import func
    from utils.database import db
    from models.stock_models import EmotionCycle
    from scripts.generate_emotion_cycle import EmotionCycleGenerator, publish_emotion_cycle_to_clickhouse

    session = next(db.get_session())
    try:
        gen = EmotionCycleGenerator()
        gen.session = session
        saved = 0
        skipped = 0

        for d in dates:
            if not d:
                continue
            existing = session.query(EmotionCycle).filter(EmotionCycle.date == d).first()
            if existing:
                session.delete(existing)
                session.commit()

            ec = gen.generate_emotion_cycle(d)
            if ec:
                session.add(ec)
                session.commit()
                publish_emotion_cycle_to_clickhouse(ec)
                saved += 1
            else:
                skipped += 1

        latest = session.query(func.max(EmotionCycle.date)).scalar()
        return {"saved": saved, "skipped": skipped, "latest": latest.isoformat() if latest else None}
    finally:
        try:
            session.close()
        except Exception:
            pass


class RepairKlinesRequest(BaseModel):
    """System configuration task helper."""
    periods: Optional[List[str]] = None
    type: Optional[Union[str, List[str]]] = None
    force: bool = False


class StartupReferenceSyncToggleRequest(BaseModel):
    enabled: bool


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * max(4, len(text) - 4)}{text[-2:]}"


def _tdx_gateway_url() -> str:
    return str(os.environ.get("AISTOCK_TDX_GATEWAY_URL") or "http://host.docker.internal:8765").rstrip("/")


def _tdx_gateway_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 15) -> Dict[str, Any]:
    url = f"{_tdx_gateway_url()}{path}"
    try:
        response = requests.request(method, url, json=payload, timeout=timeout)
        body: Any
        try:
            body = response.json()
        except Exception:
            body = {"text": response.text[-2000:]}
        return {
            "ok": response.ok,
            "status_code": response.status_code,
            "url": url,
            "body": body,
        }
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def _tdx_gateway_market_probe() -> Dict[str, Any]:
    result = _tdx_gateway_request(
        "POST",
        "/market-data",
        {
            "field_list": [],
            "stock_list": ["999999.SH"],
            "period": "1d",
            "count": 1,
            "dividend_type": "none",
            "fill_data": False,
        },
        timeout=30,
    )
    body = result.get("body") if isinstance(result, dict) else None
    data = (body or {}).get("data") if isinstance(body, dict) else None
    result["fields"] = list(data.keys())[:12] if isinstance(data, dict) else []
    result["probe_ok"] = bool(result.get("ok") and result["fields"])
    return result


def _tdx_gateway_body(section: Any) -> Dict[str, Any]:
    if not isinstance(section, dict):
        return {}
    body = section.get("body")
    if isinstance(body, dict):
        return body
    data = section.get("data")
    if isinstance(data, dict):
        return data
    return section


def _tdx_gateway_host_data(section: Any) -> Dict[str, Any]:
    body = _tdx_gateway_body(section)
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else body


def _tdx_gateway_watchdog_snapshot() -> Dict[str, Any]:
    log_path = REPO_ROOT / "runtime" / "logs" / "tdx_gateway_memory_watchdog.csv"
    if not log_path.exists():
        return {"ok": False, "reason": "log_missing", "path": str(log_path)}
    try:
        lines = [line.strip() for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except Exception as exc:
        return {"ok": False, "reason": "read_failed", "path": str(log_path), "error": str(exc)}
    if len(lines) <= 1:
        return {"ok": False, "reason": "empty", "path": str(log_path)}
    header = [item.strip() for item in lines[0].split(",")]
    latest = [item.strip() for item in lines[-1].split(",")]
    row = {header[idx]: latest[idx] if idx < len(latest) else "" for idx in range(len(header))}
    recent_actions: List[Dict[str, Any]] = []
    for line in lines[-20:]:
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < len(header):
            continue
        item = {header[idx]: parts[idx] if idx < len(parts) else "" for idx in range(len(header))}
        action = str(item.get("action") or "").strip()
        if action and action not in {"sample", "no_gateway"}:
            recent_actions.append(item)
    return {
        "ok": True,
        "path": str(log_path),
        "latest": row,
        "recent_actions": recent_actions[-5:],
    }


def _tdx_gateway_is_ready(diagnostics: Dict[str, Any]) -> bool:
    probe = diagnostics.get("market_data_probe") or {}
    return bool(probe.get("probe_ok"))


def _tdx_gateway_verdict(diagnostics: Dict[str, Any]) -> Dict[str, Any]:
    health_result = diagnostics.get("health") or {}
    health = _tdx_gateway_body(health_result)
    host = _tdx_gateway_host_data(diagnostics.get("host_diagnostics"))
    process_section = host.get("process") if isinstance(host, dict) else {}
    process = _tdx_gateway_host_data(process_section)
    probe = diagnostics.get("market_data_probe") or {}
    watchdog = diagnostics.get("watchdog") or {}
    watchdog_latest = watchdog.get("latest") if isinstance(watchdog, dict) else {}
    watchdog_actions = watchdog.get("recent_actions") if isinstance(watchdog, dict) else []
    latest_watchdog_action = str((watchdog_latest or {}).get("action") or "").strip()
    last_stop_action = next(
        (
            item
            for item in reversed(watchdog_actions or [])
            if str(item.get("action") or "").startswith("stop_threshold_exceeded")
        ),
        None,
    )

    gateway_reachable = bool(health_result.get("ok"))
    health_ready = bool(health.get("ready") or str(health.get("status") or "").lower() == "available")
    probe_ok = bool(probe.get("probe_ok") or probe.get("ok"))
    process_ok = bool(process.get("pid") or process.get("data", {}).get("pid"))
    task_result = process.get("lastTaskResult")
    task_state = str(process.get("taskState") or "").strip()
    backend_gateway_url = str((diagnostics.get("backend") or {}).get("gateway_url") or "").strip()

    checks = [
        {
            "key": "backend_gateway_url",
            "label": "后端 Gateway 地址",
            "ok": bool(diagnostics.get("gateway_url")),
            "status": diagnostics.get("gateway_url") or backend_gateway_url or "-",
        },
        {
            "key": "gateway_reachable",
            "label": "Gateway HTTP 可达",
            "ok": gateway_reachable,
            "status": "reachable" if gateway_reachable else (health_result.get("error") or "unreachable"),
        },
        {
            "key": "health_ready",
            "label": "Health ready",
            "ok": health_ready,
            "status": health.get("status") or ("ready" if health_ready else "not_ready"),
        },
        {
            "key": "market_data_probe",
            "label": "真实取数探针",
            "ok": probe_ok,
            "status": "passed" if probe_ok else (probe.get("error") or "failed"),
        },
        {
            "key": "host_process",
            "label": "瀹夸富鏈?Gateway 杩涚▼",
            "ok": process_ok,
            "status": f"pid={process.get('pid')}" if process_ok else "not_found_or_unavailable",
        },
        {
            "key": "scheduled_task",
            "label": "Windows 璁″垝浠诲姟",
            "ok": bool(task_state),
            "status": f"state={task_state or '-'} result={task_result if task_result is not None else '-'}",
        },
        {
            "key": "backend_startup_safe",
            "label": "后端启动降级",
            "ok": str((diagnostics.get("backend") or {}).get("strict_startup") or "0") in {"", "0", "false", "False"},
            "status": f"strict={(diagnostics.get('backend') or {}).get('strict_startup') or '0'}",
        },
        {
            "key": "memory_watchdog",
            "label": "鍐呭瓨瀹堟姢",
            "ok": bool(watchdog.get("ok")) and latest_watchdog_action not in {"stop_threshold_exceeded", "restart_failed"},
            "status": latest_watchdog_action or (watchdog.get("reason") if isinstance(watchdog, dict) else "-") or "-",
        },
    ]

    blockers: List[Dict[str, Any]] = []
    actions: List[Dict[str, Any]] = []
    if last_stop_action:
        blockers.append(
            {
                "key": "watchdog_stopped_gateway",
                "message": (
                    "TDX Gateway was recently stopped by the memory guard. "
                    f"{last_stop_action.get('timestamp')} private={last_stop_action.get('private_gb')}GB "
                    f"threshold={last_stop_action.get('threshold_gb')}GB."
                ),
            }
        )
    if not gateway_reachable:
        message = "Backend cannot reach the TDX Gateway HTTP service."
        if last_stop_action:
            message = "Backend cannot reach TDX Gateway; the latest blocker came from the memory guard."
        blockers.append({"key": "gateway_unreachable", "message": message})
        actions.append({"key": "restart", "label": "Restart Gateway", "endpoint": "/api/system/tdx-gateway/restart", "method": "POST"})
    elif not health_ready:
        blockers.append({"key": "gateway_not_ready", "message": "Gateway HTTP is reachable, but TdxQuant is not ready."})
        actions.append({"key": "initialize", "label": "Initialize", "endpoint": "/api/system/tdx-gateway/initialize", "method": "POST"})
    if health_ready and not probe_ok:
        blockers.append({"key": "probe_failed", "message": "Health is ready, but the market-data probe failed."})
        actions.append({"key": "initialize", "label": "Initialize", "endpoint": "/api/system/tdx-gateway/initialize", "method": "POST"})
        actions.append({"key": "recover", "label": "Recover", "endpoint": "/api/system/tdx-gateway/recover", "method": "POST"})
    if not process_ok:
        actions.append({"key": "restart", "label": "Restart Gateway", "endpoint": "/api/system/tdx-gateway/restart", "method": "POST"})

    ready = probe_ok
    level = "ok" if ready else ("warning" if gateway_reachable else "error")
    if ready:
        summary = "TDX Gateway passed the market-data probe."
    elif health_ready:
        summary = "TDX Gateway health is ready, but the market-data probe did not pass."
    elif gateway_reachable:
        summary = "TDX Gateway is reachable but not ready."
    elif last_stop_action:
        summary = "TDX Gateway was recently stopped by the memory guard."
    else:
        summary = "TDX Gateway is unreachable."

    if not any(item.get("key") == "recover" for item in actions):
        actions.append({"key": "recover", "label": "Recover", "endpoint": "/api/system/tdx-gateway/recover", "method": "POST"})

    host_repo_root = str(os.environ.get("AISTOCK_HOST_REPO_ROOT") or "").strip()
    host_gateway_script = (
        str(PureWindowsPath(host_repo_root) / "scripts" / "start_tdx_gateway.bat")
        if host_repo_root
        else str(REPO_ROOT / "scripts" / "start_tdx_gateway.bat")
    )
    return {
        "ready": ready,
        "level": level,
        "summary": summary,
        "checks": checks,
        "blockers": blockers,
        "actions": actions,
        "manual_commands": [
            {
                "label": "通过 Windows 计划任务启动",
                "command": 'schtasks /Run /TN "AiStock TDX Gateway"',
            },
            {
                "label": "直接启动 Gateway 脚本",
                "command": host_gateway_script,
            },
        ],
        "recovery_order": [
            "刷新诊断",
            "真实取数探针",
            "重新初始?TdxQuant 句柄",
            "浠嶅け璐ユ椂閲嶅惎 Gateway",
                "Run the market-data probe again after restart",
        ],
    }


def _build_tdx_gateway_diagnostics(run_probe: bool = True) -> Dict[str, Any]:
    admin = _tdx_gateway_request("GET", "/admin/diagnostics?run_probe=false", timeout=12)
    market_probe = _tdx_gateway_market_probe() if run_probe else None
    health = _tdx_gateway_request("GET", "/health", timeout=8)
    diagnostics = {
        "gateway_url": _tdx_gateway_url(),
        "backend": {
            "strict_startup": str(os.environ.get("AISTOCK_STRICT_TDXQ_STARTUP") or ""),
            "gateway_url": str(os.environ.get("AISTOCK_TDX_GATEWAY_URL") or ""),
            "checked_from": "aistock-backend-dev",
        },
        "health": health,
        "host_diagnostics": admin,
        "market_data_probe": market_probe,
        "watchdog": _tdx_gateway_watchdog_snapshot(),
        "checked_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    diagnostics["verdict"] = _tdx_gateway_verdict(diagnostics)
    return diagnostics


def _run_tdx_gateway_recovery(allow_restart: bool = True) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = []
    before = _build_tdx_gateway_diagnostics(run_probe=True)
    steps.append({"name": "before_diagnostics", "ok": _tdx_gateway_is_ready(before), "diagnostics": before})
    if _tdx_gateway_is_ready(before):
        return {"ok": True, "recovered": False, "steps": steps, "diagnostics": before}

    init_result = _tdx_gateway_request("POST", "/initialize", timeout=45)
    steps.append({"name": "initialize", "ok": bool(init_result.get("ok")), "result": init_result})
    after_init = _build_tdx_gateway_diagnostics(run_probe=True)
    steps.append({"name": "after_initialize_probe", "ok": _tdx_gateway_is_ready(after_init), "diagnostics": after_init})
    if _tdx_gateway_is_ready(after_init):
        return {"ok": True, "recovered": True, "steps": steps, "diagnostics": after_init}

    if allow_restart:
        restart_result = _tdx_gateway_request("POST", "/admin/restart", timeout=10)
        steps.append({"name": "restart_requested", "ok": bool(restart_result.get("ok")), "result": restart_result})
        return {
            "ok": bool(restart_result.get("ok")),
            "recovered": False,
            "restart_requested": bool(restart_result.get("ok")),
            "steps": steps,
            "diagnostics": _build_tdx_gateway_diagnostics(run_probe=False),
        }

    return {"ok": False, "recovered": False, "steps": steps, "diagnostics": after_init}


def _ensure_strategy_daily_scheduler():
    global _strategy_daily_scheduler
    if _strategy_daily_scheduler is not None:
        return _strategy_daily_scheduler
    with _strategy_daily_scheduler_lock:
        if _strategy_daily_scheduler is not None:
            return _strategy_daily_scheduler
        from services.operations.schedulers import ObservedScheduler as BackgroundScheduler

        _strategy_daily_scheduler = BackgroundScheduler(timezone="Asia/Shanghai", owner="数据维护")
        _strategy_daily_scheduler.start()
    return _strategy_daily_scheduler


def _default_strategy_daily_runner_setting() -> Dict[str, Any]:
    return {
        "enabled": False,
        "hour": 17,
        "minute": 0,
        "weekdays_only": True,
    }


def get_strategy_daily_runner_setting() -> Dict[str, Any]:
    cfg = app_config.get(_strategy_daily_setting_key, None) or {}
    defaults = _default_strategy_daily_runner_setting()
    enabled = bool(cfg.get("enabled", defaults["enabled"]))
    hour = int(cfg.get("hour", defaults["hour"]))
    minute = int(cfg.get("minute", defaults["minute"]))
    weekdays_only = bool(cfg.get("weekdays_only", defaults["weekdays_only"]))
    hour = max(0, min(hour, 23))
    minute = max(0, min(minute, 59))
    return {
        "enabled": enabled,
        "hour": hour,
        "minute": minute,
        "weekdays_only": weekdays_only,
    }


def init_strategy_daily_runner_scheduler_from_config() -> Dict[str, Any]:
    cfg = get_strategy_daily_runner_setting()
    if _strategy_daily_scheduler is not None:
        try:
            _strategy_daily_scheduler.remove_job(_strategy_daily_job_id)
        except Exception:
            pass
    return {
        "enabled": False,
        "hour": int(cfg["hour"]),
        "minute": int(cfg["minute"]),
        "weekdays_only": bool(cfg["weekdays_only"]),
        "implemented": False,
        "removed": True,
        "job_registered": False,
        "next_run_time": None,
    }


def run_strategy_daily_runner_task(task_already_started: bool = False, trigger_source: str = "auto"):
    task_name = "strategy_daily_runner"
    message = "legacy single-strategy daily runner has been removed; use the system strategy workflow"
    if not task_already_started:
        task_manager.start_task(task_name, trigger_source=trigger_source)
    task_manager.set_error(task_name, message)
    raise HTTPException(status_code=410, detail=message)


def _build_strategy_daily_runner_payload() -> Dict[str, Any]:
    cfg = get_strategy_daily_runner_setting()
    scheduler = _strategy_daily_scheduler
    next_run_time = None
    job_registered = False
    if scheduler is not None:
        job = scheduler.get_job(_strategy_daily_job_id)
        job_registered = job is not None
        if job and job.next_run_time:
            next_run_time = job.next_run_time.isoformat()
    return {
        "enabled": bool(cfg["enabled"]),
        "hour": int(cfg["hour"]),
        "minute": int(cfg["minute"]),
        "time": f"{int(cfg['hour']):02d}:{int(cfg['minute']):02d}",
        "weekdays_only": bool(cfg["weekdays_only"]),
        "implemented": True,
        "job_registered": job_registered,
        "next_run_time": next_run_time,
        "task": task_manager.get_task_status("strategy_daily_runner") if "task_manager" in globals() else {},
    }


class SystemTaskManager:
    """Track background system task state."""
    
    def __init__(self):
        self.timeline_limit = 200
        self.timeline: List[Dict[str, Any]] = []
        self.tasks = {
            "sync_sectors": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "repair_history_klines": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "repair_daily_klines": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "repair_all_history_klines": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "update_trade_calendar": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "update_stock_list": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "update_index_list": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "update_today_data": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "update_stock_today_data": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "update_market_today_minute_data": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "sync_today_intraday_kline": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "update_sector_intraday_stats": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "sync_sector_history": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "repair_emotion_cycle_30d": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "repair_emotion_cycle_latest": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "emotion_cycle_auto_5m": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "strategy_daily_runner": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "official_daily_close_sync": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "repair_previous_daily_kline": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "daily_kline_coverage_maintenance": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "minute_kline_daily_repair_validate": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "market_minute_history_repair": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "data_source_date_repair": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "core_data_maintenance": {
                "is_running": False,
                "paused": True,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": {
                    "enabled": False,
                    "jobs": {}
                },
                "error": None
            },
            "core_data_manual_sync": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            },
            "today_full_market_refresh": {
                "is_running": False,
                "started_at": None,
                "progress": {"current": 0, "total": 0, "message": ""},
                "results": None,
                "error": None
            }
        }

        for task_name in self.tasks.keys():
            self._ensure_task_meta(task_name)

    def _ensure_task_meta(self, task_name: str):
        task = self.tasks.get(task_name)
        if not task:
            return
        task.setdefault("last_run_at", None)
        task.setdefault("last_success_at", None)
        task.setdefault("last_error_at", None)
        task.setdefault("last_error", None)
        task.setdefault("last_duration_sec", None)
        task.setdefault("trigger_source", None)

    def _append_timeline(self, task_name: str, status: str, message: str = ""):
        task = self.tasks.get(task_name, {})
        duration_sec = _safe_float(task.get("last_duration_sec"))
        timeline_item = {
            "task_name": task_name,
            "status": status,
            "message": message or task.get("progress", {}).get("message", ""),
            "trigger_source": task.get("trigger_source") or "auto",
            "started_at": task.get("started_at"),
            "last_run_at": task.get("last_run_at"),
            "last_success_at": task.get("last_success_at"),
            "last_error_at": task.get("last_error_at"),
            "duration_sec": round(duration_sec, 3) if duration_sec is not None else None,
            "duration_ms": int(round(duration_sec * 1000)) if duration_sec is not None else None,
            "created_at": _now_iso(),
        }
        self.timeline.append(timeline_item)
        if len(self.timeline) > self.timeline_limit:
            self.timeline = self.timeline[-self.timeline_limit:]
        _persist_system_task_run(task_name, status, timeline_item["message"], task)
    
    def get_task_status(self, task_name: str) -> Dict[str, Any]:
        """System configuration task helper."""
        task = self.tasks.get(task_name)
        if not task:
            return {"error": "Task not found"}
        self._ensure_task_meta(task_name)
        return task
    
    def start_task(self, task_name: str, trigger_source: str = "auto") -> bool:
        """System configuration task helper."""
        if task_name not in self.tasks:
            logger.error(f"Task not registered: {task_name}")
            return False
        if self.tasks[task_name]["is_running"]:
            return False
        self._ensure_task_meta(task_name)
        self.tasks[task_name]["paused"] = False
        # 重置任务状?
        self.tasks[task_name]["is_running"] = True
        self.tasks[task_name]["started_at"] = _now_iso()
        self.tasks[task_name]["last_run_at"] = self.tasks[task_name]["started_at"]
        self.tasks[task_name]["progress"] = {"current": 0, "total": 0, "message": ""}
        self.tasks[task_name]["results"] = None
        self.tasks[task_name]["error"] = None
        self.tasks[task_name]["trigger_source"] = trigger_source
        self.tasks[task_name]["last_duration_sec"] = None
        self._append_timeline(task_name, "running")
        return True
    
    def update_progress(self, task_name: str, progress: Dict[str, Any]):
        """System configuration task helper."""
        if task_name in self.tasks:
            self.tasks[task_name]["progress"].update(progress)
    
    def set_error(self, task_name: str, error: str):
        """System configuration task helper."""
        if task_name in self.tasks:
            self._ensure_task_meta(task_name)
            task = self.tasks[task_name]
            started_at = _to_datetime(task.get("started_at"))
            finished_at = datetime.now()
            task["error"] = error
            task["last_error"] = error
            task["last_error_at"] = _now_iso()
            task["is_running"] = False
            task["last_duration_sec"] = (finished_at - started_at).total_seconds() if started_at else None
            self._append_timeline(task_name, "failed", message=error)
    
    def set_results(self, task_name: str, results: Dict[str, Any], mark_success: Optional[bool] = None):
        """System configuration task helper."""
        if task_name in self.tasks:
            self._ensure_task_meta(task_name)
            task = self.tasks[task_name]
            started_at = _to_datetime(task.get("started_at"))
            finished_at = datetime.now()
            if mark_success is None:
                mark_success = not _result_payload_indicates_failure(results)
            task["results"] = results
            task["is_running"] = False
            task["last_duration_sec"] = (finished_at - started_at).total_seconds() if started_at else None
            if mark_success:
                task["error"] = None
                task["last_error"] = None
                task["last_error_at"] = None
                task["last_success_at"] = _now_iso()
                self._append_timeline(task_name, "success", message=results.get("message", ""))

    def latest_timeline(self, limit: int = 20, task_names: Optional[List[str]] = None):
        records = self.timeline
        if task_names:
            allow = set(task_names)
            records = [item for item in records if item.get("task_name") in allow]
        return list(reversed(records[-limit:]))


# 初始化任务管理器
task_manager = SystemTaskManager()

# 重量级数据更新任务互斥锁，避免并发触发导致内存暴?
data_update_lock = threading.Lock()

_QMT_HOST_DELEGATED_TASK_KEYS = {
    "stock_list_sync",
    "index_list_sync",
    "official_daily",
    "repair_daily",
    "market_intraday_kline_refresh",
    "minute_kline_daily_repair_validate",
    "stock_intraday",
    "market_intraday_minutes",
    "sector_intraday_stats_refresh",
    "sector_list_sync",
    "today_full_market_refresh",
}
_QMT_TRANSITION_ERROR_MARKERS = (
    "xtquant is not installed",
    "/app/scripts/qmt_xtquant",
    "qmt minute fetch has failed batches",
    "collect_baostock_all_minutes.py",
    "股票列表为空",
    "所有数据源都失败",
)


def _runtime_has_xtquant() -> bool:
    try:
        return importlib.util.find_spec("xtquant") is not None
    except Exception:
        return False


def _probe_qmt_host_intraday_coverage(target_date: Optional[str], periods: Optional[List[str]]) -> Dict[str, Any]:
    normalized_date = (target_date or datetime.now().strftime("%Y-%m-%d"))[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized_date):
        normalized_date = datetime.now().strftime("%Y-%m-%d")
    normalized_periods = [str(item).strip().lower() for item in (periods or []) if str(item).strip()]
    min_codes = int(app_config.get("data_sync.qmt_xtquant.intraday_min_covered_codes", 100) or 100)
    result: Dict[str, Any] = {
        "target_date": normalized_date,
        "min_covered_codes": min_codes,
        "checks": {},
        "ok": False,
    }
    try:
        from utils.market_warehouse import clickhouse_client, clickhouse_table_exists

        client = clickhouse_client()
        universe_rows = client.query("SELECT count() FROM stocks WHERE type = 'stock'").result_rows
        result["stock_universe_codes"] = int(universe_rows[0][0] or 0) if universe_rows else 0
        if any(period in {"1d", "day", "daily"} for period in normalized_periods):
            if clickhouse_table_exists("kline_daily"):
                daily_rows = client.query(
                    f"""
                    SELECT uniqExact(code), max(trade_date)
                    FROM kline_daily
                    WHERE trade_date = toDate('{normalized_date}')
                    """
                ).result_rows
                result["checks"]["daily_1d"] = {
                    "covered_codes": int(daily_rows[0][0] or 0) if daily_rows else 0,
                    "max_trade_date": str(daily_rows[0][1]) if daily_rows and daily_rows[0][1] is not None else None,
                }
            else:
                result["checks"]["daily_1d"] = {"covered_codes": 0, "max_trade_date": None, "missing_table": "kline_daily"}
        if clickhouse_table_exists("qmt_intraday_latest_5m"):
            latest_rows = client.query(
                f"""
                SELECT uniqExact(code), max(datetime)
                FROM qmt_intraday_latest_5m
                WHERE trade_date = toDate('{normalized_date}')
                """
            ).result_rows
            if latest_rows:
                result["checks"]["latest_5m"] = {
                    "covered_codes": int(latest_rows[0][0] or 0),
                    "max_datetime": str(latest_rows[0][1]) if latest_rows[0][1] is not None else None,
                }
        for period in normalized_periods:
            if period not in {"5m", "15m", "30m", "60m"}:
                continue
            table_name = f"kline_minute_{period[:-1]}"
            if not clickhouse_table_exists(table_name):
                result["checks"][period] = {"covered_codes": 0, "max_datetime": None, "missing_table": table_name}
                continue
            rows = client.query(
                f"""
                SELECT uniqExact(code), max(datetime)
                FROM {table_name}
                WHERE toDate(datetime) = toDate('{normalized_date}')
                """
            ).result_rows
            result["checks"][period] = {
                "covered_codes": int(rows[0][0] or 0) if rows else 0,
                "max_datetime": str(rows[0][1]) if rows and rows[0][1] is not None else None,
            }
        covered_values = [
            int(item.get("covered_codes") or 0)
            for item in result["checks"].values()
            if isinstance(item, dict) and "covered_codes" in item
        ]
        result["ok"] = bool(covered_values) and max(covered_values) >= min_codes
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _compact_recent_daily_kline_duplicates(days: int = 14) -> Dict[str, Any]:
    """Validate and compact ReplacingMergeTree daily rows before night repair ends."""
    result: Dict[str, Any] = {"ok": False, "days": int(days), "raw_rows": 0, "unique_keys": 0, "duplicate_rows": 0}
    try:
        from utils.market_warehouse import clickhouse_client, clickhouse_table_exists

        if not clickhouse_table_exists("kline_daily"):
            result["error"] = "kline_daily_missing"
            return result
        client = clickhouse_client()
        audit_sql = f"""
            SELECT
                count() AS raw_rows,
                uniqExact(concat(code, '|', toString(trade_date))) AS unique_keys
            FROM kline_daily
            WHERE trade_date >= today() - INTERVAL {max(1, int(days))} DAY
        """
        before = client.query(audit_sql).result_rows[0]
        result["raw_rows"] = int(before[0] or 0)
        result["unique_keys"] = int(before[1] or 0)
        result["duplicate_rows"] = max(0, result["raw_rows"] - result["unique_keys"])
        if result["duplicate_rows"]:
            client.command("OPTIMIZE TABLE kline_daily FINAL")
            after = client.query(audit_sql).result_rows[0]
            result["raw_rows_after"] = int(after[0] or 0)
            result["unique_keys_after"] = int(after[1] or 0)
            result["duplicate_rows_after"] = max(0, result["raw_rows_after"] - result["unique_keys_after"])
            result["compacted"] = True
            result["ok"] = result["duplicate_rows_after"] == 0
        else:
            result["compacted"] = False
            result["duplicate_rows_after"] = 0
            result["ok"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _mark_qmt_host_collector_delegated(
    task_name: str,
    *,
    scenario: str,
    target_date: Optional[str] = None,
    periods: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    message = "QMT/xtquant 采集已委派给 Windows 主机统一采集器；后端容器不再直接调用 xtquant"
    result: Dict[str, Any] = {
        "message": message,
        "active_source": "qmt_xtquant_host_collector",
        "provider": "qmt_xtquant_host_collector",
        "scenario": scenario,
        "target_date": target_date,
        "periods": periods or [],
        "delegated": True,
        "validation_status": "delegated_to_host_collector",
        "validation_reason": "xtquant_not_available_in_backend_runtime",
        "host_entry": "scripts/qmt_xtquant_data_source_task.py",
        "host_runner": "scripts/run_qmt_xtquant_collector.ps1",
    }
    if extra:
        result.update(extra)
    if scenario.startswith("intraday"):
        coverage = _probe_qmt_host_intraday_coverage(target_date, periods)
        result["host_coverage"] = coverage
        if coverage.get("ok"):
            result["validation_status"] = "host_coverage_passed"
        else:
            result["validation_status"] = "host_coverage_failed"
            result["validation_reason"] = "delegated_host_collector_has_insufficient_clickhouse_coverage"
            message = "QMT/xtquant 已委派给 Windows 主机采集器，但 ClickHouse 覆盖不足"
            result["message"] = message
    task_manager.update_progress(task_name, {"current": 0, "total": 0, "message": message})
    ok = result.get("validation_status") != "host_coverage_failed"
    task_manager.set_results(task_name, result, mark_success=ok)
    if not ok:
        task_manager.set_error(task_name, message)
    return result


def _table_row_count(table_name: str, where_sql: str = "1=1") -> int:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
        return 0
    try:
        from utils.database import db

        with db.engine.connect() as conn:
            value = conn.execute(text(f"SELECT COUNT(*) FROM {table_name} WHERE {where_sql}")).scalar()
        return int(value or 0)
    except Exception as exc:
        logger.warning(f"failed to count {table_name}: {exc}")
        return 0


def _mark_reference_cache_success(
    task_name: str,
    *,
    source_error: Exception,
    counts: Dict[str, int],
    minimums: Dict[str, int],
) -> bool:
    failed = [key for key, minimum in minimums.items() if int(counts.get(key) or 0) < int(minimum)]
    if failed:
        return False
    message = "外部基础资料源本次不可用，已沿用本地缓存；QMT 主机采集器将在后续窗口刷新"
    task_manager.set_results(
        task_name,
        {
            "message": message,
            "active_source": "local_reference_cache",
            "validation_status": "degraded_using_existing_cache",
            "validation_reason": str(source_error),
            "counts": counts,
            "minimums": minimums,
        },
        mark_success=False,
    )
    return True


def _is_qmt_transition_failure(task_key: str, task: Dict[str, Any]) -> bool:
    if task_key not in _QMT_HOST_DELEGATED_TASK_KEYS:
        return False
    status = str(task.get("status") or "").strip().lower()
    if status in {"failed", "error"} and not _runtime_has_xtquant():
        return True
    message = " ".join(
        str(value or "")
        for value in (
            task.get("last_error"),
            task.get("error"),
            (task.get("progress") or {}).get("message"),
        )
    )
    return any(marker in message for marker in _QMT_TRANSITION_ERROR_MARKERS)


def update_stock_list_task():
    task_name = "update_stock_list"

    try:
        from api.stocks import update_stock_list as run_update_stock_list

        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始更新股票列?.."})

        result = run_update_stock_list()
        if not result or not result.get("success"):
            raise RuntimeError((result or {}).get("message") or "更新股票列表失败")

        task_manager.set_results(
            task_name,
            {
                "message": result.get("message") or "股票列表更新成功",
                "stats": {
                    "new_count": result.get("new_count", 0),
                    "update_count": result.get("update_count", 0),
                    "deleted_count": result.get("deleted_count", 0),
                    "skip_count": result.get("skip_count", 0),
                    "total_count": result.get("total_count", 0),
                },
            },
        )
        logger.info("股票列表更新完成")
    except Exception as e:
        logger.error(f"更新股票列表失败: {e}")
        if _mark_reference_cache_success(
            task_name,
            source_error=e,
            counts={"stocks": _table_row_count("stocks", "type = 'stock'")},
            minimums={"stocks": 1000},
        ):
            return
        task_manager.set_error(task_name, str(e))


def update_index_list_task():
    task_name = "update_index_list"

    try:
        from api.stocks import update_indices as run_update_indices

        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始更新指数列?.."})

        result = run_update_indices()
        if not result or int(result.get("success") or 0) <= 0:
            raise RuntimeError((result or {}).get("error") or "更新指数列表失败")

        task_manager.set_results(
            task_name,
            {
                "message": "指数列表更新成功",
                "stats": {
                    "added": int(result.get("added") or 0),
                    "updated": int(result.get("updated") or 0),
                    "skipped": int(result.get("skipped") or 0),
                    "success": int(result.get("success") or 0),
                },
            },
        )
        logger.info("指数列表更新完成")
    except Exception as e:
        logger.error(f"更新指数列表失败: {e}")
        if _mark_reference_cache_success(
            task_name,
            source_error=e,
            counts={"indices": _table_row_count("stocks", "type = 'index'")},
            minimums={"indices": 3},
        ):
            return
        task_manager.set_error(task_name, str(e))


def update_sector_intraday_stats_task(force: bool = False):
    task_name = "update_sector_intraday_stats"

    if _skip_intraday_auto_update_if_closed(task_name, force=force):
        return

    if not data_update_lock.acquire(blocking=False):
        msg = "已有其他数据更新任务正在执行，请稍后重试"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    session = None
    try:
        from sqlalchemy import func
        from utils.database import db
        from utils.market_warehouse import clickhouse_client
        from models.stock_models import Sector, SectorStock, Stock, KlineDaily

        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始刷新板块当日成分涨跌统?.."})

        session = next(db.get_session())
        latest_trade_date = session.query(func.max(KlineDaily.trade_date)).join(
            Stock, Stock.code == KlineDaily.code
        ).filter(
            Stock.type == "stock"
        ).scalar()

        if not latest_trade_date:
            task_manager.set_results(task_name, {"message": "no stock daily data; skipped"})
            return

        sector_codes = [row[0] for row in session.query(Sector.code).all()]
        total_sectors = len(sector_codes)
        task_manager.update_progress(task_name, {"message": "task progress"})

        sector_members = defaultdict(list)
        for sector_code, stock_code in session.query(SectorStock.sector_code, SectorStock.stock_code).all():
            sector_members[sector_code].append(stock_code)

        latest_stock_klines = session.query(
            KlineDaily.code,
            KlineDaily.change_pct,
            KlineDaily.amount,
            KlineDaily.volume,
        ).join(
            Stock, Stock.code == KlineDaily.code
        ).filter(
            KlineDaily.trade_date == latest_trade_date,
            Stock.type == "stock",
        ).all()

        stock_snapshot = {
            row.code: {
                "change_pct": float(row.change_pct) if row.change_pct is not None else None,
                "amount": float(row.amount) if row.amount is not None else 0.0,
                "volume": int(row.volume) if row.volume is not None else 0,
            }
            for row in latest_stock_klines
        }

        upsert_rows = []
        now_dt = datetime.now().replace(tzinfo=timezone.utc)
        refreshed = 0
        skipped = 0
        for idx, sector_code in enumerate(sector_codes, start=1):
            member_codes = sector_members.get(sector_code, [])
            total_members = len(member_codes)
            rise_count = 0
            fall_count = 0
            flat_count = 0
            limit_up_count = 0
            limit_down_count = 0
            total_amount = 0.0
            total_volume = 0
            valid_changes: List[float] = []

            for stock_code in member_codes:
                snapshot = stock_snapshot.get(stock_code)
                if not snapshot:
                    continue
                change_pct = snapshot.get("change_pct")
                if change_pct is not None:
                    valid_changes.append(change_pct)
                    if change_pct > 0:
                        rise_count += 1
                    elif change_pct < 0:
                        fall_count += 1

            if not member_codes:
                skipped += 1
                task_manager.update_progress(task_name, {"current": idx, "message": f"板块 {sector_code} 没有成分股，跳过 ({idx}/{total_sectors})"})

            avg_change_pct = round(sum(valid_changes) / len(valid_changes), 2) if valid_changes else 0.0
            upsert_rows.append(
                (
                    sector_code,
                    latest_trade_date,
                    avg_change_pct,
                    total_members,
                    rise_count,
                    fall_count,
                    flat_count,
                    limit_up_count,
                    limit_down_count,
                    total_amount,
                    total_volume,
                    now_dt,
                )
            )

            refreshed += 1
            task_manager.update_progress(task_name, {"current": idx, "message": f"正在刷新板块 {sector_code} ({idx}/{total_sectors})"})

        if upsert_rows:
            clickhouse_client().insert(
                "sector_kline_daily",
                upsert_rows,
                column_names=[
                    "code",
                    "trade_date",
                    "change_pct",
                    "stock_count",
                    "rise_count",
                    "fall_count",
                    "flat_count",
                    "limit_up_count",
                    "limit_down_count",
                    "total_amount",
                    "total_volume",
                    "created_at",
                ],
            )
        task_manager.set_results(
            task_name,
            {
                "message": "板块当日成分涨跌统计刷新完成",
                "trade_date": latest_trade_date.isoformat() if hasattr(latest_trade_date, "isoformat") else str(latest_trade_date),
                "stats": {
                    "total_sectors": total_sectors,
                    "refreshed_sectors": refreshed,
                    "skipped_sectors": skipped,
                    "snapshot_stocks": len(stock_snapshot),
                },
            },
        )
        logger.info(f"板块当日成分涨跌统计刷新完成: trade_date={latest_trade_date}, refreshed={refreshed}")
    except Exception as e:
        if session is not None:
            try:
                session.rollback()
            except Exception:
                pass
        logger.error(f"刷新板块当日成分涨跌统计失败: {e}")
        task_manager.set_error(task_name, str(e))
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        if data_update_lock.locked():
            data_update_lock.release()


def sync_sectors_task():
    """System configuration task helper."""
    task_name = "sync_sectors"

    try:
        from scripts.sync_sectors_and_mapping import SectorSyncer

        logger.info("弢始同步一二三行业板块")
        task_manager.update_progress(task_name, {"message": "弢始同步行业板?.."})

        syncer = SectorSyncer(registered_only=True)
        syncer.sync_all_sectors()
        if syncer.stats.get('failed') or not syncer.stats.get('source_fresh'):
            raise RuntimeError('QMT sector membership sync has failed items; cached data is not a fresh success')

        task_manager.set_results(
            task_name,
            {
                "message": "行业板块同步完成",
                "stats": syncer.stats,
            },
        )
        logger.info("行业板块同步完成")

    except Exception as e:
        logger.error(f"同步行业板块失败: {e}")
        if _mark_reference_cache_success(
            task_name,
            source_error=e,
            counts={
                "sectors": _table_row_count("sectors"),
                "sector_stocks": _table_row_count("sector_stocks"),
            },
            minimums={"sectors": 100, "sector_stocks": 1000},
        ):
            return
        task_manager.set_error(task_name, str(e))


def sync_sector_history_task(days=90):
    """
    同步板块历史涨跌数据的后台任?
    
    同步扢有板块的历史涨跌幅成交量、成交额，并计算上涨家数、下跌家数等统计信息
    
    Args:
        days: 同步多少天的历史数据，默?0?
    """
    task_name = "sync_sector_history"
    if os.getenv('AISTOCK_SECTOR_DAILY_OWNER', 'host') == 'host':
        task_manager.set_error(task_name, '板块日线已由宿主固定任务接管；旧历史同步入口已禁用')
        return
    
    try:
        from datetime import datetime, timedelta
        from decimal import Decimal
        from sqlalchemy import and_, func
        from sqlalchemy.exc import IntegrityError
        import pandas as pd
        
        from data_fetcher.manager import DataSourceManager
        from models.stock_models import SectorKlineDaily, Stock, KlineDaily
        from utils.database import db
        
        logger.info(f"弢始同步板块历史涨跌数据，天数: {days}")
        task_manager.update_progress(task_name, {"message": f"弢始同步板块历史数据（{days}天）..."})
        
        # 确保TdxQuant已初始化
        if False:
            raise Exception("TdxQuant is not initialized")
        
        # 1. 从数据库获取扢有一二三级行业板?
        task_manager.update_progress(task_name, {"message": "从数据库获取行业板块列表..."})
        from models.stock_models import Sector
        data_sources = DataSourceManager()
        
        # 获取数据库会?
        session_temp = next(db.get_session())
        try:
            # 查询扢有行业板块（type='industry'?
            sectors = session_temp.query(Sector).filter(
                Sector.type == 'industry'
            ).all()
            
            sector_list = []
            for sector in sectors:
                sector_list.append({
                    'code': sector.code,
                    'name': sector.name,
                    'level': sector.level
                })
            
            logger.info("task message")
            task_manager.update_progress(task_name, {
                "total": len(sector_list),
                "message": "task message"
            })
        finally:
            session_temp.close()
        
        # 2. 计算日期范围
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        start_date_str = start_date.strftime('%Y%m%d')
        end_date_str = end_date.strftime('%Y%m%d')
        
        # 3. 获取数据库会?
        session = next(db.get_session())
        
        total_saved = 0
        success_count = 0
        
        try:
            # 4. 遍历每个板块，获取K线数据并保存
            for i, sector in enumerate(sector_list):
                sector_code = sector['code']
                sector_name = sector['name']
                
                task_manager.update_progress(task_name, {
                    "current": i + 1,
                    "message": f"正在处理 {sector_code} ({sector_name}) ({i+1}/{len(sector_list)})"
                })
                
                try:
                    # 获取板块K线数?
                    kline_data = data_sources.get_stock_history(
                        stock_code=sector_code,
                        start_date=start_date.strftime('%Y-%m-%d'),
                        end_date=end_date.strftime('%Y-%m-%d'),
                        period='1d'
                    )
                    
                    if kline_data is None or kline_data.empty:
                        logger.warning("task warning")
                        continue
                    
                    # 获取该板块的成分股（从SectorStock表中查询?
                    from models.stock_models import SectorStock
                    sector_stocks = session.query(SectorStock.stock_code).filter(
                        SectorStock.sector_code == sector_code
                    ).all()
                    sector_stock_codes = [s.stock_code for s in sector_stocks]
                    stock_count = len(sector_stock_codes)
                    
                    # 先按日期升序排序，确保数据按时间顺序排列
                    kline_data_sorted = kline_data.sort_values('date').reset_index(drop=True)
                    
                    # 遍历每个交易?
                    for idx, row in kline_data_sorted.iterrows():
                        try:
                            trade_date = row['date'].date() if hasattr(row['date'], 'date') else pd.to_datetime(row['date']).date()
                            
                            close_price = float(row['close'])
                            open_price = float(row['open'])
                            high_price = float(row['high'])
                            low_price = float(row['low'])
                            volume = int(row['volume'])
                            amount = float(row['amount'])
                            
                            # 璁＄畻娑ㄨ穼骞?
                            change_pct = Decimal('0.00')
                            if idx > 0:  # skip the first row
                                prev_close = float(kline_data_sorted.iloc[idx - 1]['close'])
                                if prev_close > 0:
                                    change_pct = Decimal(str(round((close_price - prev_close) / prev_close * 100, 2)))
                            
                            # 计算成分股涨跌统?
                            rise_count = 0
                            fall_count = 0
                            flat_count = 0
                            limit_up_count = 0
                            limit_down_count = 0
                            
                            if sector_stock_codes:
                                # 查询该交易日成分股的涨跌情况
                                stock_klines = session.query(KlineDaily).filter(
                                    and_(
                                        KlineDaily.code.in_(sector_stock_codes),
                                        KlineDaily.trade_date == trade_date
                                    )
                                ).all()
                                
                                for sk in stock_klines:
                                    if sk.change_pct > 0:
                                        rise_count += 1
                                    elif sk.change_pct < 0:
                                        fall_count += 1
                                    else:
                                        flat_count += 1
                                    
                                    # 判断涨跌停（箢化判断，实际应根据股票类型判断涨跌停幅度?
                                    if sk.change_pct >= 9.5:
                                        limit_up_count += 1
                                    elif sk.change_pct <= -9.5:
                                        limit_down_count += 1
                            
                            # 棢查是否已存在
                            existing = session.query(SectorKlineDaily).filter(
                                and_(
                                    SectorKlineDaily.code == sector_code,
                                    SectorKlineDaily.trade_date == trade_date
                                )
                            ).first()
                            
                            if existing:
                                # 更新现有记录
                                existing.change_pct = change_pct
                                existing.total_volume = volume
                                existing.total_amount = Decimal(str(amount))
                                existing.stock_count = stock_count
                                existing.rise_count = rise_count
                                existing.fall_count = fall_count
                                existing.flat_count = flat_count
                                existing.limit_up_count = limit_up_count
                                existing.limit_down_count = limit_down_count
                                existing.open = Decimal(str(open_price))
                                existing.high = Decimal(str(high_price))
                                existing.low = Decimal(str(low_price))
                                existing.close = Decimal(str(close_price))
                                existing.created_at = datetime.now()
                            else:
                                # 创建新记?
                                new_record = SectorKlineDaily(
                                    code=sector_code,
                                    trade_date=trade_date,
                                    change_pct=change_pct,
                                    total_volume=volume,
                                    total_amount=Decimal(str(amount)),
                                    stock_count=stock_count,
                                    rise_count=rise_count,
                                    fall_count=fall_count,
                                    flat_count=flat_count,
                                    limit_up_count=limit_up_count,
                                    limit_down_count=limit_down_count,
                                    open=Decimal(str(open_price)),
                                    high=Decimal(str(high_price)),
                                    low=Decimal(str(low_price)),
                                    close=Decimal(str(close_price)),
                                    created_at=datetime.now()
                                )
                                session.add(new_record)
                            
                            total_saved += 1
                            
                        except Exception as e:
                            logger.warning(f"处理 {sector_code} {trade_date} 数据失败: {e}")
                            continue
                    
                    # 提交该板块的数据
                    session.commit()
                    success_count += 1
                    
                except Exception as e:
                    logger.error(f"处理板块 {sector_code} 失败: {e}")
                    session.rollback()
                    continue
            
            # 设置结果
            results = {
                "message": "板块历史涨跌数据同步完成",
                "stats": {
                    "total_sectors": len(sector_list),
                    "success_sectors": success_count,
                    "total_records": total_saved,
                    "days": days,
                    "date_range": f"{start_date_str} 鑷?{end_date_str}"
                }
            }
            task_manager.set_results(task_name, results)
            logger.info("task message")
            
        finally:
            session.close()
        
    except Exception as e:
        logger.error(f"同步板块历史涨跌数据失败: {e}")
        import traceback
        traceback.print_exc()
        task_manager.set_error(task_name, str(e))


def repair_history_klines_task(periods=None, type=None):
    """System configuration task helper."""
    task_name = "repair_history_klines"
    
    try:
        from scripts.sync_all_klines import KlineSyncer
        
        logger.info(f"弢始修复历史K线数据，类型: {type}")
        task_manager.update_progress(task_name, {"message": "弢始同步K线数?.."})
        
        # 创建同步器并执行
        syncer = KlineSyncer()
        syncer.sync_all_klines(max_workers=3, periods=periods, type=type)
        
        # 设置结果
        results = {
            "message": "task message",
            "stats": syncer.stats
        }
        task_manager.set_results(task_name, results)
        logger.info("task message")
        
    except Exception as e:
        logger.error(f"修复历史K线数据失? {e}")
        task_manager.set_error(task_name, str(e))


def repair_daily_klines_task():
    """System configuration task helper."""
    task_name = "repair_daily_klines"
    
    try:
        from utils.database import db
        from models.stock_models import EmotionCycle
        import pandas as pd
        
        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始修复日K线数?.."})
        
        # 获取扢有股票代?
        session = next(db.get_session())
        try:
            # 获取扢有股票代?
            from models.stock_models import Stock
            stocks = session.query(Stock.code).filter(
                Stock.type.in_(['stock', 'index'])
            ).all()
            stock_codes = [stock.code for stock in stocks]
            total_stocks = len(stock_codes)
            
            task_manager.update_progress(task_name, {"message": "task progress"})
            
            repaired_stocks = 0
            repaired_records = 0
            
            # 逐个股票修复
            for i, code in enumerate(stock_codes):
                task_manager.update_progress(task_name, {
                    "current": i + 1,
                    "message": f"正在修复 {code} ({i+1}/{total_stocks})"
                })
                
                # 获取该股票的扢有日线数?
                klines = session.query(KlineDaily).filter(
                    KlineDaily.code == code
                ).order_by(KlineDaily.trade_date).all()
                
                if len(klines) > 1:
                    # Convert the result to a DataFrame before calculating metrics.
                    data = []
                    for kline in klines:
                        data.append({
                            'id': kline.id,
                            'trade_date': kline.trade_date,
                            'close': kline.close
                        })
                    
                    df = pd.DataFrame(data)
                    
                    # 璁＄畻娑ㄨ穼棰濆拰娑ㄨ穼骞?
                    df['change_amount'] = df['close'].diff()
                    df['change_pct'] = (df['change_amount'] / df['close'].shift(1)) * 100
                    
                    # 计算振幅（需要high和low数据?
                    for j, kline in enumerate(klines):
                        if j > 0:
                            prev_close = klines[j-1].close
                            if prev_close > 0:
                                amplitude = ((kline.high - kline.low) / prev_close) * 100
                                kline.amplitude = amplitude
                                kline.change_amount = float(df.loc[j, 'change_amount'])
                                kline.change_pct = float(df.loc[j, 'change_pct'])
                                repaired_records += 1
                    
                    # 每处?0只股票提交一?
                    if (i + 1) % 10 == 0:
                        session.commit()
                    
                    repaired_stocks += 1
            
            # 提交剩余的修?
            session.commit()
            
            # 设置结果
            results = {
                "message": "task message",
                "stats": {
                    "repaired_stocks": repaired_stocks,
                    "repaired_records": repaired_records,
                    "total_stocks": total_stocks
                }
            }
            task_manager.set_results(task_name, results)
            logger.info("task message")
            
        finally:
            session.close()
            
    except Exception as e:
        logger.error(f"修复日K线数据失? {e}")
        task_manager.set_error(task_name, str(e))


INDEX_VALIDATION_DEFAULT_THRESHOLD_PCT = 20.0
INDEX_VALIDATION_CORE_THRESHOLD_PCT = 15.0
CORE_INDEX_CODES = {"999999.SH", "399001.SZ"}


def _get_previous_close_for_code(code: str, trade_date: str) -> Optional[float]:
    """System configuration task helper."""
    try:
        from utils.database import db

        with db.engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT close
                    FROM kline_daily
                    WHERE code = :code AND trade_date < :trade_date
                    ORDER BY trade_date DESC
                    LIMIT 1
                    """
                ),
                {"code": code, "trade_date": trade_date},
            ).fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])
    except Exception:
        return None


def _validate_index_daily_df(code: str, df, trade_date: str) -> Dict[str, Any]:
    """
    对单指数日线数据做写库前校验?
    杩斿洖: {"passed": bool, "reason": str, "change_pct": float|None}
    """
    try:
        if df is None or df.empty:
            return {"passed": False, "reason": "empty_df", "change_pct": None}

        row = df.iloc[-1]
        close_price = float(row.get("close", 0) or 0)
        open_price = float(row.get("open", 0) or 0)
        high_price = float(row.get("high", 0) or 0)
        low_price = float(row.get("low", 0) or 0)
        volume = float(row.get("volume", 0) or 0)
        amount = float(row.get("amount", 0) or 0)

        # 关键字段不能无效
        if close_price <= 0 or high_price <= 0 or low_price <= 0:
            return {"passed": False, "reason": "invalid_ohlc", "change_pct": None}
        if open_price <= 0:
            return {"passed": False, "reason": "invalid_open", "change_pct": None}
        if volume <= 0 or amount <= 0:
            return {"passed": False, "reason": "invalid_volume_or_amount", "change_pct": None}

        prev_close = _get_previous_close_for_code(code, trade_date)
        if not prev_close or prev_close <= 0:
            return {"passed": True, "reason": "no_prev_close", "change_pct": None}

        change_pct = ((close_price - prev_close) / prev_close) * 100
        threshold = INDEX_VALIDATION_CORE_THRESHOLD_PCT if code in CORE_INDEX_CODES else INDEX_VALIDATION_DEFAULT_THRESHOLD_PCT
        if abs(change_pct) > threshold:
            return {
                "passed": False,
                "reason": f"change_pct_outlier({change_pct:.2f}%>{threshold:.2f}%)",
                "change_pct": round(change_pct, 4),
            }
        return {"passed": True, "reason": "ok", "change_pct": round(change_pct, 4)}
    except Exception as e:
        return {"passed": False, "reason": f"validation_error:{e}", "change_pct": None}


def _tushare_index_cross_check(code: str, trade_date: str, target_close: float) -> Dict[str, Any]:
    """
    使用 Tushare 对异常指数做交叉校验（仅判定，不直接落库）?
    """
    try:
        from scheduler.tasks.tushare_daily_kline_task import TushareDailyKlineTask

        task = TushareDailyKlineTask()
        ts_date = trade_date.replace("-", "")
        df = task.tushare.pro.daily(ts_code=code, trade_date=ts_date)
        if df is None or df.empty:
            return {"available": True, "passed": False, "reason": "tushare_empty"}

        ts_close = float(df.iloc[0].get("close", 0) or 0)
        if ts_close <= 0 or target_close <= 0:
            return {"available": True, "passed": False, "reason": "invalid_close"}

        diff_pct = abs(target_close - ts_close) / ts_close * 100
        # 交叉校验阈度放宽，避免口径细差导致误?
        if diff_pct > 1.5:
            return {
                "available": True,
                "passed": False,
                "reason": f"tushare_diff_too_large({diff_pct:.2f}%)",
                "ts_close": ts_close,
            }
        return {"available": True, "passed": True, "reason": "tushare_confirmed", "ts_close": ts_close}
    except Exception as e:
        return {"available": False, "passed": False, "reason": f"tushare_unavailable:{e}"}


def _legacy_update_today_data_task(periods=None):
    """System configuration task helper."""
    task_name = "update_today_data"

    if not data_update_lock.acquire(blocking=False):
        msg = "task is already running"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return
    
    try:
        from scripts.sync_all_klines import KlineSyncer
        from datetime import datetime
        
        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始更新当天指数数?.."})
        
        # 创建同步?
        syncer = KlineSyncer()

        # 预取丢次指数列表，避免每个周期重复加载全量列表导致额外内存占用
        all_indices = syncer.get_all_indices()
        
        # 确定要同步的周期
        sync_periods = periods or list(syncer.periods.keys())
        
        total_tasks = 0
        completed_tasks = 0
        success_tasks = 0
        failed_tasks = 0
        blocked_codes: List[Dict[str, Any]] = []
        validation_status = "passed"
        validation_reason = "ok"
        active_source = "qmt_xtquant"
        
        # 计算总任务数
        for period in sync_periods:
            # 只同步指?
            total_tasks += len(all_indices)
        
        task_manager.update_progress(task_name, {"message": "task progress"})
        
        # 逐个周期同步
        for period in sync_periods:
            if period not in syncer.periods:
                logger.warning("task warning")
                continue
            
            period_name = syncer.periods[period]
            logger.info(f"弢始更?{period_name} ({period}) 当天指数数据")
            
            # 只同步指?
            stocks = all_indices
            
            if not stocks:
                logger.warning(f"没有获取到指数列表，跳过 {period_name} 同步")
                continue
            
            # 逐个指数同步当天数据
            for stock in stocks:
                try:
                    code = stock['code']
                    name = stock['name']
                    
                    # 只获取当天的数据
                    today = datetime.now().strftime('%Y-%m-%d')
                    
                    # 鑾峰彇K绾挎暟鎹?
                    df = syncer.market_data_source.get_stock_history(
                        stock_code=code,
                        start_date=today,
                        end_date=today,
                        period=period,
                        dividend_type='front'  # 前复?
                    )
                    
                    if df is not None and not df.empty:
                        # 保存到数据库
                        syncer._save_kline_to_db(code, period, df)
                        logger.info("task message")
                    else:
                        logger.warning("task warning")
                    
                    # 閲婃斁DataFrame鍐呭瓨
                    if df is not None:
                        del df
                        import gc
                        gc.collect()
                    
                    completed_tasks += 1
                    task_manager.update_progress(task_name, {
                        "current": completed_tasks,
                        "message": f"正在更新 {code} {period_name} ({completed_tasks}/{total_tasks})"
                    })
                    
                except Exception as e:
                    logger.error(f"?{stock['code']} {stock['name']} {period} 更新失败: {e}")
                    completed_tasks += 1
                    task_manager.update_progress(task_name, {
                        "current": completed_tasks,
                        "message": f"更新 {stock['code']} {period_name} 失败 ({completed_tasks}/{total_tasks})"
                    })
        
        # 设置结果
        results = {
            "message": "当天指数数据更新完成",
            "stats": {
                "total": total_tasks,
                "success": completed_tasks,
                "failed": 0  # 失败的已在循环中处理
            }
        }
        task_manager.set_results(task_name, results)
        logger.info("当天指数数据更新完成")
        
    except Exception as e:
        logger.error(f"更新当天指数数据失败: {e}")
        task_manager.set_error(task_name, str(e))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def update_today_data_task(periods=None, force: bool = False):
    """System configuration task helper."""
    task_name = "update_today_data"

    if _skip_intraday_auto_update_if_closed(task_name, force=force):
        return

    if not data_update_lock.acquire(blocking=False):
        msg = "已有其他数据更新任务正在执行，请稍后重试"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    try:
        requested_periods = list(periods) if periods else []
        filtered_periods = _filter_ready_intraday_minute_periods(requested_periods, task_name)
        if requested_periods and not filtered_periods:
            return
        periods = filtered_periods if requested_periods else periods
        normalized_periods_for_delegate = [str(item).strip().lower() for item in (periods or []) if str(item).strip()]
        if normalized_periods_for_delegate and all(item in {"1d", "day", "daily"} for item in normalized_periods_for_delegate) and not _runtime_has_xtquant():
            _mark_qmt_host_collector_delegated(
                task_name,
                scenario="intraday-daily",
                target_date=datetime.now().strftime("%Y-%m-%d"),
                periods=normalized_periods_for_delegate,
                extra={"universe": "stock,index"},
            )
            return

        import gc
        import math
        import time
        import pandas as pd
        from scripts.sync_all_klines import KlineSyncer

        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始更新当天指数数?.."})

        syncer = KlineSyncer()
        all_indices = syncer.get_all_indices()
        sync_periods = periods or list(syncer.periods.keys())
        minute_periods = {"1m", "5m", "15m", "30m", "60m"}
        minute_indices = _build_intraday_minute_index_pool(all_indices) if any(p in minute_periods for p in sync_periods) else []

        has_minute_periods = any(period in minute_periods for period in sync_periods)
        total_tasks = (len(minute_indices) if has_minute_periods else 0) + sum(
            len(all_indices) for period in sync_periods if period not in minute_periods
        )
        completed_tasks = 0
        success_tasks = 0
        failed_tasks = 0
        blocked_codes: List[Dict[str, Any]] = []
        validation_status = "passed"
        validation_reason = "ok"
        active_source = "qmt_xtquant"

        def _try_batch_sync_index_daily(indices: List[Dict[str, Any]], completed: int) -> Dict[str, Any]:
            from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient
            from utils.market_warehouse import clickhouse_client

            query_date = _resolve_intraday_daily_trade_date()
            code_list = [str(item.get("code")) for item in indices if item.get("code")]
            if not code_list:
                return {"used": True, "completed": completed, "success": 0, "failed": 0, "blocked": []}
            if False:
                reason = "qmt_xtquant_unavailable"
                task_manager.update_progress(task_name, {"message": f"指数日线批量路径不可用，已停? {reason}"})
                return {"used": False, "completed": completed, "success": 0, "failed": len(code_list), "blocked": [], "reason": reason}

            qmt_market = QmtMiniMarketClient()
            qmt_market.connect()
            market_data = qmt_market.get_market_data_tdx_shape(
                field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
                stock_list=code_list,
                period="1d",
                start_time=query_date.replace("-", ""),
                end_time=query_date.replace("-", ""),
                count=1,
                dividend_type="front",
                fill_data=False,
            )
            close_df = market_data.get("Close") if isinstance(market_data, dict) else None
            exact_coverage = 0.0
            if close_df is not None and not close_df.empty:
                exact_coverage = min(1.0, len(close_df.columns) / len(code_list))
            if close_df is None or close_df.empty or exact_coverage < 0.70:
                latest_market_data = qmt_market.get_market_data_tdx_shape(
                    field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
                    stock_list=code_list,
                    period="1d",
                    count=1,
                    dividend_type="front",
                    fill_data=False,
                )
                latest_close_df = latest_market_data.get("Close") if isinstance(latest_market_data, dict) else None
                if latest_close_df is not None and not latest_close_df.empty:
                    market_data = latest_market_data
                    close_df = latest_close_df
                    latest_date = str(close_df.index[0])[:10] if len(close_df.index) else query_date
                    latest_coverage = min(1.0, len(close_df.columns) / len(code_list))
                    logger.info(
                        "index daily batch exact date incomplete; using latest 1d snapshot "
                        f"latest_date={latest_date}, target_date={query_date}, "
                        f"exact_coverage={exact_coverage:.2%}, latest_coverage={latest_coverage:.2%}"
                    )
                else:
                    reason = "empty Close from batch get_market_data"
                    task_manager.update_progress(task_name, {"message": f"指数日线批量失败，已停止: {reason}"})
                    logger.error(f"index daily batch rejected: {reason}")
                    return {"used": False, "completed": completed, "success": 0, "failed": len(code_list), "blocked": [], "reason": reason}

            final_coverage = 0.0
            if close_df is not None and not close_df.empty:
                final_coverage = min(1.0, len(close_df.columns) / len(code_list))
            if final_coverage < 0.70:
                reason = f"coverage={final_coverage:.2%}<70%"
                task_manager.update_progress(task_name, {"message": f"指数日线批量覆盖率不足，已停? {reason}"})
                logger.error(f"index daily batch rejected: {reason}")
                return {"used": False, "completed": completed, "success": 0, "failed": len(code_list), "blocked": [], "reason": reason}

            def _col_idx(df: Optional[pd.DataFrame], code: str, fallback: int) -> int:
                if df is None or df.empty:
                    return fallback
                target = str(code).upper()
                raw = target.split(".", 1)[0]
                for idx, col in enumerate(df.columns):
                    text = str(col).upper()
                    if text == target or text.split(".", 1)[0] == raw:
                        return idx
                return fallback

            def _num(df: Optional[pd.DataFrame], row_idx: int, col_idx: int) -> float:
                try:
                    if df is None or df.empty or col_idx >= len(df.columns):
                        return 0.0
                    value = df.iloc[row_idx, col_idx]
                    return 0.0 if value is None or pd.isna(value) else float(value)
                except Exception:
                    return 0.0

            open_df = market_data.get("Open")
            high_df = market_data.get("High")
            low_df = market_data.get("Low")
            volume_df = market_data.get("Volume")
            amount_df = market_data.get("Amount")
            trade_date = query_date

            prev_close_map: Dict[str, float] = {}
            from utils.database import db

            with db._engine.connect() as conn:
                placeholders = ", ".join([f":code_{idx}" for idx in range(len(code_list))])
                params = {f"code_{idx}": code for idx, code in enumerate(code_list)}
                params["trade_date"] = trade_date
                query = text(
                    f"""
                    SELECT kd.code, kd.close
                    FROM kline_daily kd
                    JOIN (
                        SELECT code, MAX(trade_date) AS prev_trade_date
                        FROM kline_daily
                        WHERE trade_date < :trade_date AND code IN ({placeholders})
                        GROUP BY code
                    ) prev ON prev.code = kd.code AND prev.prev_trade_date = kd.trade_date
                    """
                )
                for row in conn.execute(query, params).mappings():
                    try:
                        prev_close_map[row["code"]] = float(row["close"] or 0)
                    except Exception:
                        pass

            rows: List[Dict[str, Any]] = []
            blocked: List[Dict[str, Any]] = []
            failed = 0
            for fallback_idx, item in enumerate(indices):
                code = str(item.get("code"))
                name = item.get("name")
                idx = _col_idx(close_df, code, fallback_idx)
                close_price = _num(close_df, 0, idx)
                if close_price <= 0:
                    failed += 1
                    continue
                open_price = _num(open_df, 0, idx) or close_price
                high_price = _num(high_df, 0, idx) or close_price
                low_price = _num(low_df, 0, idx) or close_price
                one_df = pd.DataFrame([{
                    "date": trade_date,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": _num(volume_df, 0, idx),
                    "amount": _num(amount_df, 0, idx),
                }])
                check_result = _validate_index_daily_df(code, one_df, trade_date)
                if not check_result.get("passed"):
                    failed += 1
                    blocked.append({"code": code, "name": name, "reason": check_result.get("reason")})
                    continue
                prev_close = float(prev_close_map.get(code) or 0)
                if prev_close > 0:
                    change_amount = close_price - prev_close
                    change_pct = change_amount / prev_close * 100
                    amplitude = (high_price - low_price) / prev_close * 100
                else:
                    change_amount = 0.0
                    change_pct = 0.0
                    amplitude = 0.0
                rows.append({
                    "code": code,
                    "trade_date": trade_date,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close_price,
                    "volume": float(one_df.iloc[-1]["volume"]),
                    "amount": float(one_df.iloc[-1]["amount"]),
                    "amplitude": amplitude,
                    "change_pct": change_pct,
                    "change_amount": change_amount,
                    "turnover_rate": 0.0,
                    "created_at": datetime.now().replace(tzinfo=timezone.utc),
                })

            if not rows:
                reason = "no valid rows after batch validation"
                task_manager.update_progress(task_name, {"message": f"指数日线批量失败，已跳过逐条回: {reason}"})
                return {"used": False, "completed": completed, "success": 0, "failed": failed, "blocked": blocked, "reason": reason}

            def _quote(value: Any) -> str:
                return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

            client = clickhouse_client()
            code_sql = ", ".join(_quote(row["code"]) for row in rows)
            client.command(
                f"""
                ALTER TABLE kline_daily
                DELETE WHERE trade_date = toDate({_quote(trade_date)})
                  AND code IN ({code_sql})
                SETTINGS mutations_sync = 1
                """
            )
            columns = [
                "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
                "amplitude", "change_pct", "change_amount", "turnover_rate", "created_at",
            ]
            from utils.kline_store import filter_trading_day_rows

            rows = filter_trading_day_rows("1d", pd.DataFrame(rows)).to_dict("records")
            if not rows:
                logger.warning("index daily batch write blocked by trade_calendar guard: no trading-day rows")
                return {"used": True, "completed": completed, "success": 0, "failed": failed, "blocked": blocked}

            numeric_columns = {
                "open", "high", "low", "close", "volume", "amount",
                "amplitude", "change_pct", "change_amount", "turnover_rate",
            }

            def _insert_value(row: Dict[str, Any], col: str) -> Any:
                value = row.get(col)
                if col == "trade_date" and isinstance(value, str):
                    return datetime.strptime(value[:10], "%Y-%m-%d").date()
                if col == "created_at":
                    if isinstance(value, str):
                        return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if isinstance(value, datetime) and value.tzinfo is None:
                        return value.replace(tzinfo=timezone.utc)
                if col in numeric_columns:
                    try:
                        if value is None or pd.isna(value):
                            return 0.0
                        return float(value)
                    except Exception:
                        return 0.0
                return value

            client.insert(
                "kline_daily",
                [tuple(_insert_value(row, col) for col in columns) for row in rows],
                column_names=columns,
            )
            completed += len(indices)
            task_manager.update_progress(task_name, {"current": completed, "message": f"指数日线批量写入完成: {len(rows)}/{len(indices)}"})
            logger.info(f"index daily batch upsert done: total={len(indices)}, written={len(rows)}, failed={failed}, blocked={len(blocked)}")
            return {"used": True, "completed": completed, "success": len(rows), "failed": failed, "blocked": blocked}

        index_snapshot_result: Optional[Dict[str, Any]] = None
        index_snapshot_error: Optional[str] = None

        if any(period in minute_periods for period in sync_periods):
            try:
                from scripts.collect_intraday_snapshots import collect_global_index_snapshot

                target_codes = [str(item.get("code")) for item in minute_indices if item.get("code")]
                task_manager.update_progress(
                    task_name,
                    {
                        "current": completed_tasks,
                        "total": total_tasks,
                        "message": "collecting intraday global index quote snapshot",
                    },
                )
                index_snapshot_result = collect_global_index_snapshot(target_codes=target_codes)
                rows = int((index_snapshot_result or {}).get("rows") or 0)
                if rows <= 0:
                    index_snapshot_error = "global index snapshot returned zero rows"
                    failed_tasks += max(1, len(minute_indices))
                    validation_status = "failed"
                    validation_reason = index_snapshot_error
                    logger.error(f"intraday global index snapshot failed: {index_snapshot_error}")
                else:
                    completed_tasks += len(minute_indices)
                    success_tasks += min(rows, len(minute_indices))
                    active_source = str((index_snapshot_result or {}).get("source") or "intraday_quote_snapshot")
                    logger.info(f"intraday global index snapshot collected before minute sync: {index_snapshot_result}")
            except Exception as exc:
                index_snapshot_error = str(exc)
                failed_tasks += max(1, len(minute_indices))
                validation_status = "failed"
                validation_reason = index_snapshot_error
                logger.exception(f"intraday global index snapshot collection failed: {exc}")

        task_manager.update_progress(task_name, {"message": "task progress"})

        for period in sync_periods:
            if period not in syncer.periods:
                logger.warning("task warning")
                continue

            period_name = syncer.periods[period]
            logger.info(f"弢始更?{period_name} ({period}) 当天指数数据")

            current_indices = minute_indices if period in minute_periods else all_indices
            if not current_indices:
                logger.info(f"skip index sync period={period}: no target indices")
                continue

            if period == "1d":
                try:
                    batch_result = _try_batch_sync_index_daily(all_indices, completed_tasks)
                    completed_tasks = int(batch_result.get("completed", completed_tasks))
                    if batch_result.get("used"):
                        success_tasks += int(batch_result.get("success", 0) or 0)
                        failed_tasks += int(batch_result.get("failed", 0) or 0)
                        batch_blocked = batch_result.get("blocked") or []
                        if batch_blocked or int(batch_result.get("failed", 0) or 0) > 0:
                            blocked_codes.extend(batch_blocked)
                            validation_status = "failed"
                            validation_reason = "index_daily_batch_incomplete"
                        continue
                    failed_tasks += int(batch_result.get("failed", 0) or 0)
                    validation_status = "failed"
                    validation_reason = str(batch_result.get("reason") or "index_daily_batch_not_used")
                    continue
                except Exception as e:
                    logger.exception(f"index daily batch path failed: {e}")
                    task_manager.update_progress(
                        task_name,
                        {"message": f"指数日线批量路径异常，已跳过逐条回: {e}"},
                    )
                    failed_tasks += len(all_indices)
                    validation_status = "failed"
                    validation_reason = str(e)
                    continue

            if period in minute_periods:
                reason = "snapshot_collector_only"
                logger.info(
                    f"index intraday minute period uses quote snapshot collector only: "
                    f"period={period}, target_count={len(current_indices)}, reason={reason}"
                )
                task_manager.update_progress(
                    task_name,
                    {
                        "current": completed_tasks,
                        "total": total_tasks,
                        "message": f"index minute snapshot collector already sampled; period={period}",
                    },
                )
                continue

            for stock in current_indices:
                df = None
                try:
                    code = stock["code"]
                    name = stock["name"]
                    today = datetime.now().strftime("%Y-%m-%d")

                    df = syncer.market_data_source.get_stock_history(
                        stock_code=code,
                        start_date=today,
                        end_date=today,
                        period=period,
                        dividend_type="front",
                    )

                    if df is None or df.empty:
                        failed_tasks += 1
                        logger.warning("task warning")
                    else:
                        should_save = True
                        if period == "1d":
                            check_result = _validate_index_daily_df(code, df, today)
                            if not check_result.get("passed"):
                                retry_df = syncer.market_data_source.get_stock_history(
                                    stock_code=code,
                                    start_date=today,
                                    end_date=today,
                                    period=period,
                                    dividend_type="front",
                                )
                                if retry_df is not None and not retry_df.empty:
                                    retry_check = _validate_index_daily_df(code, retry_df, today)
                                    if retry_check.get("passed"):
                                        df = retry_df
                                    else:
                                        target_close = float(retry_df.iloc[-1].get("close", 0) or 0)
                                        ts_check = _tushare_index_cross_check(code, today, target_close)
                                        should_save = False
                                        validation_status = "failed"
                                        validation_reason = (
                                            f"{code}:{retry_check.get('reason')};cross={ts_check.get('reason')}"
                                        )
                                        blocked_codes.append(
                                            {
                                                "code": code,
                                                "name": name,
                                                "reason": retry_check.get("reason"),
                                                "cross_check": ts_check.get("reason"),
                                            }
                                        )
                                else:
                                    target_close = float(df.iloc[-1].get("close", 0) or 0)
                                    ts_check = _tushare_index_cross_check(code, today, target_close)
                                    should_save = False
                                    validation_status = "failed"
                                    validation_reason = (
                                        f"{code}:{check_result.get('reason')};cross={ts_check.get('reason')}"
                                    )
                                    blocked_codes.append(
                                        {
                                            "code": code,
                                            "name": name,
                                            "reason": check_result.get("reason"),
                                            "cross_check": ts_check.get("reason"),
                                        }
                                    )

                        if should_save:
                            syncer._save_kline_to_db(code, period, df)
                            success_tasks += 1
                            logger.info("task message")
                        else:
                            failed_tasks += 1
                            logger.error(f"?拦截指数异常写入: {code} {name}, reason={validation_reason}")

                    completed_tasks += 1
                    task_manager.update_progress(
                        task_name,
                        {"current": completed_tasks, "message": f"正在更新 {code} {period_name} ({completed_tasks}/{total_tasks})"},
                    )
                except Exception as e:
                    failed_tasks += 1
                    completed_tasks += 1
                    logger.error(f"?{stock['code']} {stock['name']} {period} 更新失败: {e}")
                    task_manager.update_progress(
                        task_name,
                        {"current": completed_tasks, "message": f"更新 {stock['code']} {period_name} 失败 ({completed_tasks}/{total_tasks})"},
                    )
                finally:
                    if df is not None:
                        del df
                        gc.collect()

        if failed_tasks > 0:
            validation_status = "failed"
            if validation_reason == "ok":
                validation_reason = f"failed_tasks={failed_tasks}"

        results = {
            "message": "当天指数数据更新完成",
            "stats": {"total": total_tasks, "success": success_tasks, "failed": failed_tasks},
            "intraday_snapshot": index_snapshot_result,
            "intraday_snapshot_error": index_snapshot_error,
            "active_source": active_source,
            "validation_status": validation_status,
            "validation_reason": validation_reason,
            "blocked_codes": blocked_codes,
        }
        task_manager.set_results(task_name, results)
        logger.info("当天指数数据更新完成")

        if validation_status == "failed":
            task_manager.set_error(task_name, f"指数当天数据更新未完全过: {validation_reason}")
    except Exception as e:
        logger.error(f"更新当天指数数据失败: {e}")
        task_manager.set_error(task_name, str(e))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def _legacy_update_stock_today_data_task(periods=None):
    """System configuration task helper."""
    task_name = "update_stock_today_data"

    if not data_update_lock.acquire(blocking=False):
        msg = "task is already running"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return
    
    try:
        from scripts.sync_all_klines import KlineSyncer
        from datetime import datetime
        
        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始更新当天个股数?.."})
        
        # 创建同步?
        syncer = KlineSyncer()

        # 预取丢次股票列表，避免每个周期重复查询导致内存抖动
        all_stocks = syncer.get_all_stocks()
        watchlist_stocks = syncer.get_watchlist_and_holding_stocks()
        all_indices = syncer.get_all_indices()
        critical_indices = [i for i in all_indices if i.get("code") in ("999999.SH", "399001.SZ")]
        
        # 确定要同步的周期
        sync_periods = periods or list(syncer.periods.keys())
        
        total_tasks = 0
        completed_tasks = 0
        
        # 计算总任务数
        for period in sync_periods:
            if period == '1m':
                # 1分钟数据只同步自选股和持仓股
                total_tasks += len(watchlist_stocks)
            else:
                # 其他周期同步扢有股?
                total_tasks += len(all_stocks)
            # 日线额外同步关键指数（首页涨跌统计依?999999.SH/399001.SZ?
            if period == "1d":
                total_tasks += len(critical_indices)
        
        task_manager.update_progress(task_name, {"message": "task progress"})
        
        # 逐个周期同步
        for period in sync_periods:
            if period not in syncer.periods:
                logger.warning("task warning")
                continue
            
            period_name = syncer.periods[period]
            logger.info(f"弢始更?{period_name} ({period}) 当天股票数据")
            
            # 根据周期选择股票列表
            if period == '1m':
                # 1分钟数据只同步自选股和持仓股
                stocks = watchlist_stocks
            else:
                # 其他周期同步扢有股?
                stocks = all_stocks
            
            if not stocks:
                logger.warning(f"没有获取到股票列表，跳过 {period_name} 同步")
                continue
            
            # 逐个股票同步当天数据
            for stock in stocks:
                try:
                    code = stock['code']
                    name = stock['name']
                    
                    # 只获取当天的数据（若当天无数据，日线自动回上一交易日）
                    today = datetime.now().strftime('%Y-%m-%d')
                    query_date = today
                    
                    # 鑾峰彇K绾挎暟鎹?
                    df = syncer.market_data_source.get_stock_history(
                        stock_code=code,
                        start_date=query_date,
                        end_date=query_date,
                        period=period,
                        dividend_type='front'  # 前复?
                    )

                    # 日线在凌?休市时可能当日无数据，回逢上一交易日重?
                    if (df is None or df.empty) and period == '1d':
                        from scheduler.trading_calendar import TradingCalendar

                        now_dt = datetime.now()
                        previous_trading_day = TradingCalendar.get_previous_trading_day(now_dt).strftime('%Y-%m-%d')
                        if previous_trading_day != query_date:
                            logger.warning(
                                f"⚠️  {code} {name} 1d 当天无数据，回上一交易日重? {previous_trading_day} "
                                f"(当前时间: {now_dt.strftime('%Y-%m-%d %H:%M:%S')})"
                            )
                            query_date = previous_trading_day
                            df = syncer.market_data_source.get_stock_history(
                                stock_code=code,
                                start_date=query_date,
                                end_date=query_date,
                                period=period,
                                dividend_type='front'
                            )
                    
                    if df is not None and not df.empty:
                        # 保存到数据库
                        syncer._save_kline_to_db(code, period, df)
                        logger.info(f"?{code} {name} {period} 数据更新成功: {len(df)} ?(date={query_date})")
                    else:
                        logger.warning(
                            f"⚠️  {code} {name} {period} 无数?"
                            f"(query_date={query_date}, now={datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
                        )
                    
                    # 閲婃斁DataFrame鍐呭瓨
                    if df is not None:
                        del df
                        import gc
                        gc.collect()
                    
                    completed_tasks += 1
                    task_manager.update_progress(task_name, {
                        "current": completed_tasks,
                        "message": f"正在更新 {code} {period_name} ({completed_tasks}/{total_tasks})"
                    })
                    
                except Exception as e:
                    logger.error(f"?{stock['code']} {stock['name']} {period} 更新失败: {e}")
                    completed_tasks += 1
                    task_manager.update_progress(task_name, {
                        "current": completed_tasks,
                        "message": f"更新 {stock['code']} {period_name} 失败 ({completed_tasks}/{total_tasks})"
                    })

            # 补充：日线同步关键指数，避免首页涨跌统计停留在旧交易?
            if period == "1d" and critical_indices:
                for stock in critical_indices:
                    try:
                        code = stock.get("code")
                        name = stock.get("name") or code
                        today = datetime.now().strftime('%Y-%m-%d')
                        query_date = today

                        df = syncer.market_data_source.get_stock_history(
                            stock_code=code,
                            start_date=query_date,
                            end_date=query_date,
                            period=period,
                            dividend_type='front'
                        )

                        if df is None or df.empty:
                            from scheduler.trading_calendar import TradingCalendar

                            now_dt = datetime.now()
                            previous_trading_day = TradingCalendar.get_previous_trading_day(now_dt).strftime('%Y-%m-%d')
                            if previous_trading_day != query_date:
                                query_date = previous_trading_day
                                df = syncer.market_data_source.get_stock_history(
                                    stock_code=code,
                                    start_date=query_date,
                                    end_date=query_date,
                                    period=period,
                                    dividend_type='front'
                                )

                        if df is not None and not df.empty:
                            syncer._save_kline_to_db(code, period, df)
                            logger.info(f"?{code} {name} {period} 关键指数更新成功: {len(df)} ?(date={query_date})")
                        else:
                            logger.warning(f"⚠️  {code} {name} {period} 关键指数无数?(query_date={query_date})")

                        if df is not None:
                            del df
                            import gc
                            gc.collect()

                        completed_tasks += 1
                        task_manager.update_progress(task_name, {
                            "current": completed_tasks,
                            "message": f"正在更新 {code} 关键指数日线 ({completed_tasks}/{total_tasks})"
                        })
                    except Exception as e:
                        logger.error(f"?{stock.get('code')} {stock.get('name')} {period} 关键指数更新失败: {e}")
                        completed_tasks += 1
                        task_manager.update_progress(task_name, {
                            "current": completed_tasks,
                            "message": f"更新关键指数 {stock.get('code')} 失败 ({completed_tasks}/{total_tasks})"
                        })
        
        # 自动补算情绪周期：避免首页缺失某个交易日（例?4/8?
        if "1d" in sync_periods:
            try:
                from models.stock_models import EmotionCycle, KlineDaily
                from scripts.generate_emotion_cycle import EmotionCycleGenerator
                from sqlalchemy import func
                from utils.database import db

                session = next(db.get_session())
                try:
                    latest_date = session.query(func.max(KlineDaily.trade_date)).scalar()
                    if latest_date:
                        existing = session.query(EmotionCycle).filter(EmotionCycle.date == latest_date).first()
                        if existing:
                            session.delete(existing)
                            session.commit()

                        generator = EmotionCycleGenerator()
                        generator.session = session
                        emotion_cycle = generator.generate_emotion_cycle(latest_date)
                        if emotion_cycle:
                            session.add(emotion_cycle)
                            session.commit()
                            logger.info(f"?情绪周期已生? {latest_date}")
                        else:
                            logger.warning(f"鈿狅笍 鎯呯华鍛ㄦ湡鐢熸垚澶辫触: {latest_date}")
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"鐢熸垚鎯呯华鍛ㄦ湡澶辫触: {e}")
        
        # 设置结果
        results = {
            "message": "当天个股数据更新完成",
            "stats": {
                "total": total_tasks,
                "success": completed_tasks,
                "failed": 0  # 失败的已在循环中处理
            }
        }
        task_manager.set_results(task_name, results)
        logger.info("当天个股数据更新完成")
        
    except Exception as e:
        logger.error(f"更新当天个股数据失败: {e}")
        task_manager.set_error(task_name, str(e))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def _normalize_intraday_stock_code(raw_code: Any) -> Optional[str]:
    raw = str(raw_code or "").strip().upper()
    if not raw:
        return None
    if "." in raw:
        code, market = raw.split(".", 1)
        code = "".join(ch for ch in code if ch.isdigit())
        market = market[:2]
        if len(code) == 6 and market in {"SH", "SZ", "BJ"}:
            return f"{code}.{market}"
        return None
    code = "".join(ch for ch in raw if ch.isdigit())
    if len(code) != 6:
        return None
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    return None


def _build_intraday_minute_stock_pool(syncer) -> List[Dict[str, Any]]:
    """Build the narrow intraday minute pool: watchlist, holdings and candidates."""
    repo_root = Path(__file__).resolve().parents[1]
    rows_by_code: Dict[str, Dict[str, Any]] = {}
    source_counts: Dict[str, int] = defaultdict(int)

    def _add_item(item: Any, source: str) -> None:
        if not isinstance(item, dict):
            return
        code = _normalize_intraday_stock_code(item.get("code") or item.get("stock_code") or item.get("symbol"))
        if not code:
            return
        name = str(item.get("name") or item.get("stock_name") or item.get("security_name") or "").strip()
        market = code.split(".", 1)[1]
        current = rows_by_code.setdefault(
            code,
            {
                "code": code,
                "name": name or code,
                "type": "stock",
                "market": market,
                "sources": set(),
            },
        )
        if name and (not current.get("name") or current.get("name") == code):
            current["name"] = name
        current["sources"].add(source)
        source_counts[source] += 1

    try:
        for item in syncer.get_watchlist_and_holding_stocks() or []:
            _add_item(item, "user_stocks")
    except Exception as exc:
        logger.warning(f"intraday minute pool load user_stocks failed: {exc}")

    runtime_paths = [
        (repo_root / "data" / "runtime" / "v4_live_monitor" / "monitor_state.json", "v4_manual_holdings"),
        (repo_root / "data" / "runtime" / "v4_live_monitor" / "ths_capital_holdings_cache.json", "ths_holdings_cache"),
    ]
    for path, source in runtime_paths:
        try:
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            for item in (payload.get("holdings") if isinstance(payload, dict) else []) or []:
                _add_item(item, source)
        except Exception as exc:
            logger.warning(f"intraday minute pool load {source} failed: {exc}")

    try:
        from api.gen2_strategy import build_gen2_open_signals

        open_signals = build_gen2_open_signals(None, 80)
        signal_rows = []
        for key in ("exact_rows", "recent_rows"):
            rows = open_signals.get(key) if isinstance(open_signals, dict) else []
            if isinstance(rows, list):
                signal_rows.extend(rows)
        for item in signal_rows:
            _add_item(item, "gen2_open_signals")
    except Exception as exc:
        logger.warning(f"intraday minute pool load gen2 open signals failed: {exc}")

    try:
        from scripts.gen2_update_live_shadow import build_g2_promotion_snapshot_codes
        from utils.market_warehouse import clickhouse_query_df

        latest_df = clickhouse_query_df(
            """
            SELECT max(trade_date) AS trade_date
            FROM kline_daily k
            JOIN stocks s ON s.code = k.code
            WHERE s.type = 'stock'
            """
        )
        latest_date = "" if latest_df is None or latest_df.empty else str(latest_df.iloc[0].get("trade_date") or "")[:10]
        signal_date = datetime.now().strftime("%Y-%m-%d")
        if latest_date and latest_date <= signal_date:
            signal_date = latest_date if latest_date == signal_date else signal_date
        for item in build_g2_promotion_snapshot_codes(str(signal_date), pool_rank=200, limit=300):
            _add_item(item, "gen2_promotion_zone")
    except Exception as exc:
        logger.warning(f"intraday minute pool load gen2 promotion zone failed: {exc}")

    codes = list(rows_by_code.keys())
    if codes:
        try:
            from utils.market_warehouse import clickhouse_query_df

            def _quote(value: str) -> str:
                return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"

            code_list_sql = ", ".join(_quote(code) for code in codes)
            stock_df = clickhouse_query_df(
                f"""
                SELECT code, name, type, market
                FROM stocks
                WHERE code IN ({code_list_sql})
                """
            )
            if stock_df is not None and not stock_df.empty:
                for row in stock_df.itertuples(index=False):
                    code = str(row.code)
                    current = rows_by_code.get(code)
                    if not current:
                        continue
                    current["name"] = str(row.name or current.get("name") or code)
                    current["type"] = str(row.type or current.get("type") or "stock")
                    current["market"] = str(row.market or current.get("market") or code.split(".", 1)[1])
        except Exception as exc:
            logger.warning(f"intraday minute pool enrich from stocks failed: {exc}")

    result: List[Dict[str, Any]] = []
    for code in sorted(rows_by_code.keys()):
        item = rows_by_code[code]
        sources = sorted(item.pop("sources", set()))
        item["source"] = ",".join(sources)
        result.append(item)

    logger.info(
        f"intraday minute stock pool built: target_count={len(result)}, "
        f"source_counts={dict(source_counts)}"
    )
    return result


def _build_intraday_minute_index_pool(indices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    core_codes = {
        "999999.SH",  # Shanghai Composite in this project
        "399001.SZ",  # Shenzhen Component
        "399006.SZ",  # ChiNext
        "000680.SH",  # SSE STAR composite shown on home page
        "000300.SH",  # CSI 300
        "000905.SH",  # CSI 500
        "000852.SH",  # CSI 1000
    }
    by_code = {str(item.get("code")): item for item in indices if item.get("code")}
    selected = [by_code[code] for code in sorted(core_codes) if code in by_code]
    logger.info(f"intraday minute index pool built: target_count={len(selected)}, codes={[x.get('code') for x in selected]}")
    return selected


class _IntradayMinutePoolSource:
    def get_watchlist_and_holding_stocks(self) -> List[Dict[str, Any]]:
        try:
            from utils.market_warehouse import clickhouse_query_df

            df = clickhouse_query_df(
                """
                SELECT DISTINCT s.code, s.name, s.type, s.market
                FROM user_stocks u
                JOIN stocks s ON s.code = u.code
                ORDER BY s.code
                """
            )
            if df is None or df.empty:
                return []
            return [
                {
                    "code": str(row.code),
                    "name": str(row.name or row.code),
                    "type": str(row.type or ""),
                    "market": str(row.market or ""),
                }
                for row in df.itertuples(index=False)
            ]
        except Exception as exc:
            logger.info(f"intraday minute watchlist pool empty or unavailable: {exc}")
            return []


def _get_intraday_minute_indices() -> List[Dict[str, Any]]:
    try:
        from utils.market_warehouse import clickhouse_query_df

        df = clickhouse_query_df(
            """
            SELECT code, name, type, market
            FROM stocks
            WHERE type = 'index' AND (quit = 0 OR quit IS NULL)
            ORDER BY code
            """
        )
        if df is None or df.empty:
            return []
        return [
            {
                "code": str(row.code),
                "name": str(row.name or row.code),
                "type": str(row.type or ""),
                "market": str(row.market or ""),
            }
            for row in df.itertuples(index=False)
        ]
    except Exception as exc:
        logger.warning(f"intraday minute index list unavailable: {exc}")
        return []


def update_market_today_minute_data_task(periods=None, force: bool = False):
    """Collect one intraday quote snapshot for the stock pool and core indices."""
    task_name = "update_market_today_minute_data"

    if _skip_intraday_auto_update_if_closed(task_name, force=force):
        return

    if not data_update_lock.acquire(blocking=False):
        msg = "已有其他数据更新任务正在执行，请稍后重试"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    try:
        requested_periods = list(periods) if periods else ["5m", "15m", "30m", "60m"]
        filtered_periods = _filter_ready_intraday_minute_periods(requested_periods, task_name)
        if requested_periods and not filtered_periods:
            return
        sync_periods = filtered_periods if requested_periods else requested_periods

        from scripts.collect_intraday_snapshots import collect_global_index_snapshot, collect_global_stock_snapshot

        stock_pool = _build_intraday_minute_stock_pool(_IntradayMinutePoolSource())
        index_pool = _build_intraday_minute_index_pool(_get_intraday_minute_indices())
        stock_codes = [str(item.get("code")) for item in stock_pool if item.get("code")]
        index_codes = [str(item.get("code")) for item in index_pool if item.get("code")]
        total = len(stock_codes) + len(index_codes)
        completed = 0
        success = 0
        failed = 0
        errors: List[str] = []
        active_sources: List[str] = []

        task_manager.update_progress(
            task_name,
            {
                "current": completed,
                "total": total,
                "message": f"collecting intraday market quote snapshot: stocks={len(stock_codes)}, indices={len(index_codes)}",
            },
        )

        stock_result: Dict[str, Any] = {}
        if stock_codes:
            try:
                stock_result = collect_global_stock_snapshot(target_codes=stock_codes)
                rows = int((stock_result or {}).get("rows") or 0)
                if rows <= 0:
                    failed += len(stock_codes)
                    errors.append("stock snapshot returned zero rows")
                    logger.error("intraday market stock snapshot failed: zero rows")
                else:
                    completed += len(stock_codes)
                    success += min(rows, len(stock_codes))
                    active_sources.append(str((stock_result or {}).get("source") or "stock_snapshot"))
            except Exception as exc:
                failed += len(stock_codes)
                errors.append(f"stock snapshot failed: {exc}")
                logger.exception(f"intraday market stock snapshot collection failed: {exc}")

        task_manager.update_progress(
            task_name,
            {
                "current": completed,
                "total": total,
                "message": f"stock snapshot done; collecting index snapshot: indices={len(index_codes)}",
            },
        )

        index_result: Dict[str, Any] = {}
        if index_codes:
            try:
                index_result = collect_global_index_snapshot(target_codes=index_codes)
                rows = int((index_result or {}).get("rows") or 0)
                if rows <= 0:
                    failed += len(index_codes)
                    errors.append("index snapshot returned zero rows")
                    logger.error("intraday market index snapshot failed: zero rows")
                else:
                    completed += len(index_codes)
                    success += min(rows, len(index_codes))
                    active_sources.append(str((index_result or {}).get("source") or "index_snapshot"))
            except Exception as exc:
                failed += len(index_codes)
                errors.append(f"index snapshot failed: {exc}")
                logger.exception(f"intraday market index snapshot collection failed: {exc}")

        if failed == 0 and success < total:
            failed = max(0, total - success)

        status = "passed" if failed == 0 and not errors else "failed"
        result = {
            "message": f"盘中股票/指数分钟级快照完? success={success}, failed={failed}",
            "periods": sync_periods,
            "stats": {"total": total, "success": success, "failed": failed},
            "stock_pool_count": len(stock_codes),
            "index_pool_count": len(index_codes),
            "stock_snapshot": stock_result,
            "index_snapshot": index_result,
            "active_source": ",".join(active_sources),
            "validation_status": status,
            "errors": errors,
        }
        task_manager.update_progress(
            task_name,
            {
                "current": completed,
                "total": total,
                "message": result["message"],
            },
        )
        task_manager.set_results(task_name, result)
        if status != "passed":
            task_manager.set_error(task_name, "; ".join(errors) or "intraday market minute snapshot failed")
    except Exception as exc:
        logger.exception(f"盘中股票/指数分钟级快照失? {exc}")
        task_manager.set_error(task_name, str(exc))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def sync_today_intraday_kline_task(
    task_already_started: bool = False,
    periods: Optional[List[str]] = None,
    target_date: Optional[str] = None,
    timeout_seconds: int = 1800,
):
    """Refresh strategy-critical same-day 15m/30m K-lines into ClickHouse."""
    task_name = "sync_today_intraday_kline"

    if target_date is None:
        preflight = _ensure_trade_calendar_fresh_for_today(task_name)
        if not preflight.get("ok") or not preflight.get("trading_day"):
            return

    if not data_update_lock.acquire(blocking=False):
        msg = "已有其他数据更新任务正在执行，请稍后重试"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    try:
        if not task_already_started:
            if not task_manager.start_task(task_name):
                task_manager.set_error(task_name, "当天分钟K线落库任务正在运行中，请稍后再试")
                return

        normalized_periods = [str(item).strip().lower() for item in (periods or ["15m", "30m"]) if str(item).strip()]
        normalized_periods = [item for item in normalized_periods if item in {"15m", "30m"}]
        if not normalized_periods:
            normalized_periods = ["15m", "30m"]

        normalized_date = (target_date or datetime.now().strftime("%Y-%m-%d"))[:10]
        try:
            from scheduler.trading_calendar import TradingCalendar

            target_dt = datetime.strptime(normalized_date, "%Y-%m-%d")
            if not TradingCalendar.is_trading_day(target_dt):
                msg = f"skip intraday minute K-line sync for non-trading day: {normalized_date}"
                logger.info(msg)
                task_manager.update_progress(task_name, {"current": 0, "total": 0, "message": msg})
                task_manager.set_results(
                    task_name,
                    {
                        "message": msg,
                        "target_date": normalized_date,
                        "periods": normalized_periods,
                        "validation_status": "skipped_non_trading_day",
                    },
                    mark_success=True,
                )
                return
        except Exception as exc:
            logger.warning(f"failed to validate intraday minute K-line trade date {normalized_date}: {exc}")

        if not _runtime_has_xtquant():
            _mark_qmt_host_collector_delegated(
                task_name,
                scenario="intraday",
                target_date=normalized_date,
                periods=normalized_periods,
                extra={"universe": "stock,index"},
            )
            return

        script_path = REPO_ROOT / "scripts" / "qmt_xtquant_data_source_task.py"
        if not script_path.exists():
            raise RuntimeError(f"script_not_found:{script_path}")

        intraday_stock_pool = _build_intraday_minute_stock_pool(_IntradayMinutePoolSource())
        intraday_index_pool = _build_intraday_minute_index_pool(_get_intraday_minute_indices())
        target_codes = [
            str(item.get("code")).strip().upper()
            for item in [*intraday_stock_pool, *intraday_index_pool]
            if str(item.get("code") or "").strip()
        ]
        target_codes = list(dict.fromkeys(target_codes))

        report_dir = report_path(
            "system_intraday_qmt_minute_gap_repair",
            f"{normalized_date}_{datetime.now():%Y%m%d_%H%M%S}",
        )
        cmd = [
            sys.executable,
            str(script_path),
            "--mode",
            "minute-gap-repair",
            "--scenario",
            "intraday",
            "--start-date",
            normalized_date,
            "--end-date",
            normalized_date,
            "--minute-periods",
            ",".join(normalized_periods),
            "--universe",
            "stock,index",
            "--repair-code-chunk-size",
            "1",
            "--minute-batch-size",
            "1",
            "--minute-batch-timeout-sec",
            "240",
            "--max-retries",
            "1",
            "--retry-sleep",
            "1",
            "--minute-report-dir",
            str(report_dir),
        ]
        if target_codes:
            cmd.extend(["--codes", ",".join(target_codes)])
        else:
            cmd.extend(["--universe", "index"])
        task_manager.update_progress(
            task_name,
            {
                "current": 0,
                "total": len(normalized_periods),
                "message": f"弢始同步当天全市场分钟K? {normalized_date} {','.join(normalized_periods)}",
            },
        )

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(300, int(timeout_seconds or 1800)),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"当天分钟K线落库超? timeout={timeout_seconds}s")

        stdout_tail = (proc.stdout or "")[-8000:]
        stderr_tail = (proc.stderr or "")[-8000:]
        tail_lines = [line.strip() for line in stdout_tail.splitlines() if line.strip()]
        if tail_lines:
            task_manager.update_progress(task_name, {"message": tail_lines[-1]})
        parsed_summary: Dict[str, Any] = {}
        summary_file = report_dir / "collector_summary.json"
        if summary_file.exists():
            try:
                parsed_summary = json.loads(summary_file.read_text(encoding="utf-8"))
            except Exception:
                parsed_summary = {}
        json_start = stdout_tail.rfind("\n{")
        if json_start >= 0:
            json_text = stdout_tail[json_start + 1 :]
        else:
            json_text = stdout_tail[stdout_tail.find("{") :] if "{" in stdout_tail else ""
        if json_text and not parsed_summary:
            try:
                parsed_summary = json.loads(json_text)
            except Exception:
                parsed_summary = {}

        ok = proc.returncode == 0 and bool(parsed_summary.get("ok", proc.returncode == 0))
        result = {
            "message": "current market minute sync completed" if ok else "current market minute sync failed",
            "target_date": normalized_date,
            "periods": normalized_periods,
            "returncode": int(proc.returncode or 0),
            "cmd": cmd,
            "report_dir": str(report_dir),
            "report_path": str(summary_file),
            "summary": parsed_summary,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
            "validation_status": "passed" if ok else "failed",
        }
        task_manager.set_results(task_name, result, mark_success=ok)
        if not ok:
            task_manager.set_error(task_name, result["stderr_tail"] or result["stdout_tail"] or result["message"])
    except Exception as exc:
        logger.exception(f"当天全市场分钟K线落库失? {exc}")
        task_manager.set_error(task_name, str(exc))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def update_stock_today_data_task(periods=None, force: bool = False):
    """System configuration task helper."""
    task_name = "update_stock_today_data"

    if _skip_intraday_auto_update_if_closed(task_name, force=force):
        return

    if not data_update_lock.acquire(blocking=False):
        msg = "已有其他数据更新任务正在执行，请稍后重试"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    try:
        requested_periods = list(periods) if periods else []
        filtered_periods = _filter_ready_intraday_minute_periods(requested_periods, task_name)
        if requested_periods and not filtered_periods:
            return
        periods = filtered_periods if requested_periods else periods
        normalized_periods_for_delegate = [str(item).strip().lower() for item in (periods or []) if str(item).strip()]
        if normalized_periods_for_delegate and all(item in {"1d", "day", "daily"} for item in normalized_periods_for_delegate) and not _runtime_has_xtquant():
            _mark_qmt_host_collector_delegated(
                task_name,
                scenario="intraday-daily",
                target_date=datetime.now().strftime("%Y-%m-%d"),
                periods=normalized_periods_for_delegate,
                extra={"universe": "stock,index"},
            )
            return

        import gc
        import math
        import time
        import pandas as pd
        from scripts.sync_all_klines import KlineSyncer
        from scheduler.trading_calendar import TradingCalendar

        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始更新当天个股数?.."})

        syncer = KlineSyncer()
        all_stocks = syncer.get_all_stocks()
        intraday_minute_stocks = _build_intraday_minute_stock_pool(syncer)
        all_indices = syncer.get_all_indices()
        daily_assets_by_code: Dict[str, Dict[str, Any]] = {}
        for item in list(all_stocks or []) + list(all_indices or []):
            code = str(item.get("code") or "")
            if code:
                daily_assets_by_code[code] = item
        daily_assets = list(daily_assets_by_code.values())
        critical_indices: List[Dict[str, Any]] = []
        sync_periods = periods or list(syncer.periods.keys())
        total_tasks = 0
        completed_tasks = 0
        success_tasks = 0
        failed_tasks = 0
        minute_periods = {"1m", "5m", "15m", "30m", "60m"}
        has_minute_periods = any(period in minute_periods for period in sync_periods)
        if has_minute_periods:
            total_tasks += len(intraday_minute_stocks)
        for period in sync_periods:
            if period not in minute_periods:
                total_tasks += len(daily_assets if period == "1d" else all_stocks)
        task_manager.update_progress(task_name, {"message": "task progress"})

        minute_snapshot_result: Optional[Dict[str, Any]] = None
        minute_snapshot_error: Optional[str] = None
        v4_event_refresh_result: Optional[Dict[str, Any]] = None
        daily_retry_failures: List[Dict[str, str]] = []
        daily_batch_result: Optional[Dict[str, Any]] = None

        if any(period in minute_periods for period in sync_periods):
            try:
                from scripts.collect_intraday_snapshots import collect_global_stock_snapshot

                task_manager.update_progress(
                    task_name,
                    {
                        "current": completed_tasks,
                        "total": total_tasks,
                        "message": "collecting intraday global stock quote snapshot",
                    },
                )
                target_codes = [str(item.get("code")) for item in intraday_minute_stocks if item.get("code")]
                # G2/G3 intraday logic needs broad market coverage. Passing
                # None lets the collector fetch the full A-share spot table;
                # target_codes is kept only for status/progress accounting.
                minute_snapshot_result = collect_global_stock_snapshot(target_codes=None)
                rows = int((minute_snapshot_result or {}).get("rows") or 0)
                if rows <= 0:
                    minute_snapshot_error = "global snapshot returned zero rows"
                    failed_tasks += max(1, len(intraday_minute_stocks))
                    logger.error(f"intraday global stock snapshot failed: {minute_snapshot_error}")
                else:
                    completed_tasks += len(intraday_minute_stocks)
                    success_tasks += min(rows, len(intraday_minute_stocks))
                    logger.info(f"intraday global stock snapshot collected before minute sync: {minute_snapshot_result}")
            except Exception as exc:
                minute_snapshot_error = str(exc)
                failed_tasks += max(1, len(intraday_minute_stocks))
                logger.exception(f"intraday global stock snapshot collection failed: {exc}")

        def _resolve_daily_trade_date() -> str:
            return _resolve_intraday_daily_trade_date()

        def _build_daily_df_from_quote(quote: Dict[str, Any], trade_date: str) -> Optional[pd.DataFrame]:
            try:
                open_price = float(quote.get("open", 0) or 0)
                high_price = float(quote.get("high", 0) or 0)
                low_price = float(quote.get("low", 0) or 0)
                close_price = float(quote.get("price", 0) or 0)
                volume = float(quote.get("volume", 0) or 0)
                amount = float(quote.get("amount", 0) or 0)
                if close_price <= 0:
                    return None
                row = {
                    "date": trade_date,
                    "open": open_price if open_price > 0 else close_price,
                    "high": high_price if high_price > 0 else close_price,
                    "low": low_price if low_price > 0 else close_price,
                    "close": close_price,
                    "volume": volume,
                    "amount": amount,
                }
                return pd.DataFrame([row])
            except Exception:
                return None

        def _try_batch_sync_daily(
            stocks: List[Dict[str, Any]],
            period_name: str,
            completed: int,
            total: int,
        ) -> Dict[str, Any]:
            """
            1次股票列?+ N?get_market_data 批量行情的日线快照更新?
            启用前做样本校验，不通过则返?used=False 让主流程自动回旧辑?
            """
            from data_fetcher.sources.qmtmini_client import QmtMiniMarketClient

            query_date = _resolve_daily_trade_date()
            batch_size = 200
            sleep_between_batches = 0.05
            total_codes = len(stocks)

            if total_codes == 0:
                return {"used": True, "completed": completed}
            if False:
                reason = "qmt_xtquant_unavailable"
                task_manager.update_progress(task_name, {"message": f"日线批量路径不可用，已停? {reason}"})
                return {"used": False, "completed": completed, "failed": total_codes, "reason": reason}

            code_list = [s.get("code") for s in stocks if s.get("code")]
            qmt_market = QmtMiniMarketClient()
            qmt_market.connect()
            total_batches = math.ceil(len(code_list) / batch_size)
            rows: List[Dict[str, Any]] = []
            written_codes: set[str] = set()
            fetch_errors = 0

            def _extract_float(df: Optional[pd.DataFrame], row_idx: int, col_idx: int) -> float:
                try:
                    if df is None or df.empty or col_idx >= len(df.columns):
                        return 0.0
                    value = df.iloc[row_idx, col_idx]
                    if value is None or pd.isna(value):
                        return 0.0
                    return float(value)
                except Exception:
                    return 0.0

            def _resolve_col_idx(df: Optional[pd.DataFrame], code: str, fallback_idx: int) -> int:
                if df is None or df.empty:
                    return fallback_idx
                target = str(code or "").strip().upper()
                target_raw = target.split(".", 1)[0]
                for idx, col in enumerate(df.columns):
                    col_text = str(col or "").strip().upper()
                    if col_text == target or col_text.split(".", 1)[0] == target_raw:
                        return idx
                return fallback_idx

            for i in range(total_batches):
                batch_codes = code_list[i * batch_size:(i + 1) * batch_size]
                try:
                    market_data = qmt_market.get_market_data_tdx_shape(
                        field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
                        stock_list=batch_codes,
                        period="1d",
                        count=1,
                        dividend_type="front",
                        fill_data=False,
                    )
                    close_df = market_data.get("Close") if isinstance(market_data, dict) else None
                    if close_df is None or close_df.empty:
                        fetch_errors += 1
                        continue
                    open_df = market_data.get("Open")
                    high_df = market_data.get("High")
                    low_df = market_data.get("Low")
                    volume_df = market_data.get("Volume")
                    amount_df = market_data.get("Amount")
                    row_idx = len(close_df.index) - 1
                    for fallback_idx, code in enumerate(batch_codes):
                        col_idx = _resolve_col_idx(close_df, str(code), fallback_idx)
                        close_price = _extract_float(close_df, row_idx, col_idx)
                        if close_price <= 0:
                            continue
                        open_price = _extract_float(open_df, row_idx, col_idx) or close_price
                        high_price = _extract_float(high_df, row_idx, col_idx) or close_price
                        low_price = _extract_float(low_df, row_idx, col_idx) or close_price
                        rows.append(
                            {
                                "code": str(code),
                                "trade_date": query_date,
                                "open": open_price,
                                "high": high_price,
                                "low": low_price,
                                "close": close_price,
                                "volume": _extract_float(volume_df, row_idx, col_idx),
                                "amount": _extract_float(amount_df, row_idx, col_idx),
                                "amplitude": 0.0,
                                "change_pct": 0.0,
                                "change_amount": 0.0,
                                "turnover_rate": 0.0,
                                "created_at": datetime.now().replace(tzinfo=timezone.utc),
                            }
                        )
                        written_codes.add(str(code))
                except Exception as exc:
                    fetch_errors += 1
                    logger.warning(f"daily batch get_market_data failed batch={i + 1}/{total_batches}: {exc}")
                time.sleep(sleep_between_batches)

            if not rows:
                reason = f"empty_daily_batch_rows fetch_errors={fetch_errors}"
                return {
                    "used": False,
                    "completed": completed,
                    "failed": total_codes,
                    "failed_codes": [str(code) for code in code_list],
                    "reason": reason,
                }

            from utils.market_warehouse import clickhouse_client

            client = clickhouse_client()

            def _quote(value: Any) -> str:
                return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

            prev_close_map: Dict[str, float] = {}
            for idx in range(0, len(rows), 500):
                chunk = rows[idx:idx + 500]
                code_sql = ", ".join(_quote(row["code"]) for row in chunk)
                prev_rows = client.query(
                    f"""
                    SELECT code, argMax(close, trade_date) AS prev_close
                    FROM kline_daily
                    WHERE trade_date < toDate({_quote(query_date)})
                      AND code IN ({code_sql})
                    GROUP BY code
                    """
                ).result_rows
                for code, prev_close in prev_rows:
                    try:
                        prev_close_map[str(code)] = float(prev_close or 0)
                    except Exception:
                        prev_close_map[str(code)] = 0.0

            for row in rows:
                prev_close = prev_close_map.get(str(row["code"]), 0.0)
                row["previous_close"] = prev_close if prev_close > 0 else None
                if prev_close > 0:
                    row["change_amount"] = float(row["close"] - prev_close)
                    row["change_pct"] = float((row["close"] - prev_close) / prev_close * 100)
                    row["amplitude"] = float((row["high"] - row["low"]) / prev_close * 100)

            columns = [
                "code", "trade_date", "open", "high", "low", "close", "volume", "amount",
                "previous_close", "amplitude", "change_pct", "change_amount", "turnover_rate",
                "snapshot_at", "source", "is_provisional",
            ]
            from utils.kline_store import filter_trading_day_rows

            rows = filter_trading_day_rows("1d", pd.DataFrame(rows)).to_dict("records")
            if not rows:
                logger.warning(f"daily batch write blocked by trade_calendar guard: date={query_date}")
                return {
                    "used": True,
                    "completed": completed + total_codes,
                    "success": 0,
                    "failed": total_codes,
                    "failed_codes": [str(code) for code in code_list],
                    "skip_reason": "non_trading_day",
                }

            snapshot_at = datetime.now(timezone(timedelta(hours=8)))
            client.insert(
                "kline_daily_intraday",
                [
                    (
                        row["code"],
                        datetime.strptime(str(row["trade_date"])[:10], "%Y-%m-%d").date(),
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["volume"],
                        row["amount"],
                        row.get("previous_close"),
                        row["amplitude"],
                        row["change_pct"],
                        row["change_amount"],
                        row["turnover_rate"],
                        snapshot_at,
                        "qmtmini:intraday_daily_snapshot",
                        1,
                    )
                    for row in rows
                ],
                column_names=columns,
            )
            saved = len(rows)
            failed_codes = [str(code) for code in code_list if str(code) not in written_codes]
            failed_count = len(failed_codes)
            logger.info(
                f"provisional daily snapshot write done: total={total_codes}, written={saved}, "
                f"failed={failed_count}, fetch_errors={fetch_errors}, date={query_date}"
            )
            return {
                "used": True,
                "completed": completed + total_codes,
                "success": saved,
                "failed": failed_count,
                "failed_codes": failed_codes,
            }

        def _try_batch_sync_minute(
            stocks: List[Dict[str, Any]],
            period: str,
            period_name: str,
            completed: int,
            total: int,
        ) -> Dict[str, Any]:
            code_list = [str(s.get("code")) for s in stocks if s.get("code")]
            if not code_list:
                return {"used": True, "completed": completed}

            skipped = len(code_list)
            reason = "snapshot_collector_only"
            logger.info(
                f"stock intraday minute period uses quote snapshot collector only: "
                f"period={period}, target_count={skipped}, reason={reason}"
            )
            task_manager.update_progress(
                task_name,
                {
                    "current": completed,
                    "total": total,
                    "message": f"stock minute snapshot collector already sampled; period={period}",
                },
            )
            return {
                "used": True,
                "completed": completed,
                "success": 0,
                "failed": 0,
                "skipped": skipped,
                "reason": reason,
            }

        def _classify_missing_daily_codes(codes: List[str], trade_date: str) -> Dict[str, Any]:
            normalized_trade_date = str(trade_date or "")[:10]
            clean_codes = [str(code or "").strip() for code in codes if str(code or "").strip()]
            if not clean_codes or not normalized_trade_date:
                return {
                    "skip_new_ipo": [],
                    "skip_not_listed": [],
                    "dirty_history": [],
                    "retry_codes": clean_codes,
                }

            try:
                from utils.market_warehouse import clickhouse_client

                client = clickhouse_client()
                quoted_codes = ", ".join("'" + code.replace("\\", "\\\\").replace("'", "\\'") + "'" for code in clean_codes)
                rows = client.query(
                    f"""
                    SELECT
                        s.code,
                        s.name,
                        s.list_date,
                        min(k.trade_date) AS first_kline_date,
                        max(k.trade_date) AS last_kline_date,
                        countIf(k.trade_date = toDate('{normalized_trade_date}')) AS has_trade_date_row
                    FROM stocks s
                    LEFT JOIN kline_daily k ON s.code = k.code
                    WHERE s.code IN ({quoted_codes})
                    GROUP BY s.code, s.name, s.list_date
                    """
                ).result_rows
            except Exception as exc:
                logger.warning(f"classify missing daily codes failed, fallback to retry all: {exc}")
                return {
                    "skip_new_ipo": [],
                    "skip_not_listed": [],
                    "dirty_history": [],
                    "retry_codes": clean_codes,
                }

            by_code: Dict[str, Dict[str, Any]] = {}
            for code, name, list_date, first_kline_date, last_kline_date, has_trade_date_row in rows:
                by_code[str(code)] = {
                    "code": str(code),
                    "name": str(name or code),
                    "list_date": list_date.isoformat() if list_date else None,
                    "first_kline_date": first_kline_date.isoformat() if first_kline_date else None,
                    "last_kline_date": last_kline_date.isoformat() if last_kline_date else None,
                    "has_trade_date_row": int(has_trade_date_row or 0),
                }

            skip_new_ipo: List[Dict[str, Any]] = []
            skip_not_listed: List[Dict[str, Any]] = []
            dirty_history: List[Dict[str, Any]] = []
            retry_codes: List[str] = []

            for code in clean_codes:
                meta = by_code.get(code) or {"code": code, "name": code}
                list_date = str(meta.get("list_date") or "")[:10]
                first_kline_date = str(meta.get("first_kline_date") or "")[:10]
                has_trade_date_row = int(meta.get("has_trade_date_row") or 0)
                has_placeholder_first_kline = (not first_kline_date) or first_kline_date <= "1970-01-02"
                if list_date and list_date > normalized_trade_date:
                    meta["reason"] = "not_listed_yet"
                    skip_not_listed.append(meta)
                    continue
                if list_date == normalized_trade_date:
                    if has_trade_date_row <= 0 and has_placeholder_first_kline:
                        meta["reason"] = "ipo_today_no_daily_bar"
                        skip_new_ipo.append(meta)
                        continue
                    if first_kline_date and first_kline_date < list_date:
                        meta["reason"] = "history_before_listing"
                        dirty_history.append(meta)
                        continue
                    if has_trade_date_row <= 0:
                        meta["reason"] = "ipo_today_no_daily_bar"
                        skip_new_ipo.append(meta)
                        continue
                retry_codes.append(code)

            return {
                "skip_new_ipo": skip_new_ipo,
                "skip_not_listed": skip_not_listed,
                "dirty_history": dirty_history,
                "retry_codes": retry_codes,
            }

        for period in sync_periods:
            if period not in syncer.periods:
                logger.warning("task warning")
                continue

            period_name = syncer.periods[period]
            logger.info(f"弢始更?{period_name} ({period}) 当天股票数据")
            if period in minute_periods:
                stocks = intraday_minute_stocks
                logger.info(
                    f"盘中分钟线仅同步候?持仓/观察? period={period}, target_count={len(stocks)}"
                )
            elif period == "1d":
                stocks = daily_assets
                logger.info(
                    f"daily snapshot uses combined stock/index universe: "
                    f"stocks={len(all_stocks)}, indices={len(all_indices)}, total={len(stocks)}"
                )
            else:
                stocks = all_stocks
            if not stocks:
                logger.warning(f"没有获取到股票列表，跳过 {period_name} 同步")
                continue

            daily_batch_used = False
            if period == "1d":
                try:
                    batch_result = _try_batch_sync_daily(stocks, period_name, completed_tasks, total_tasks)
                    daily_batch_result = dict(batch_result) if isinstance(batch_result, dict) else None
                    daily_batch_used = bool(batch_result.get("used"))
                    completed_tasks = int(batch_result.get("completed", completed_tasks))
                    if daily_batch_used:
                        success_tasks += int(batch_result.get("success", 0) or 0)
                        failed_tasks += int(batch_result.get("failed", 0) or 0)
                    if daily_batch_used and int(batch_result.get("failed", 0) or 0) > 0:
                        logger.warning(
                            f"daily batch snapshot incomplete: success={batch_result.get('success')}, "
                            f"failed={batch_result.get('failed')}"
                        )
                        missing_code_classification = _classify_missing_daily_codes(
                            [str(code) for code in (batch_result.get("failed_codes") or [])],
                            _resolve_daily_trade_date(),
                        )
                        skipped_new_ipo = missing_code_classification.get("skip_new_ipo") or []
                        skipped_not_listed = missing_code_classification.get("skip_not_listed") or []
                        dirty_history_codes = missing_code_classification.get("dirty_history") or []
                        retry_codes = [str(code) for code in (missing_code_classification.get("retry_codes") or [])]

                        if skipped_new_ipo or skipped_not_listed or dirty_history_codes:
                            exempt_count = len(skipped_new_ipo) + len(skipped_not_listed) + len(dirty_history_codes)
                            failed_tasks = max(0, failed_tasks - exempt_count)
                            logger.info(
                                f"daily snapshot exempted non-actionable listing gaps: "
                                f"new_ipo={len(skipped_new_ipo)}, not_listed={len(skipped_not_listed)}, "
                                f"dirty_history={len(dirty_history_codes)}"
                            )
                        if dirty_history_codes:
                            logger.warning(
                                "daily snapshot found dirty history before listing: "
                                f"{[item.get('code') for item in dirty_history_codes]}"
                            )

                        if retry_codes:
                            retry_stock_map = {str(item.get('code') or ''): item for item in stocks if item.get('code')}
                            retry_stocks = [retry_stock_map[code] for code in retry_codes if code in retry_stock_map]
                            retry_success = 0
                            retry_failed = 0
                            for idx, stock in enumerate(retry_stocks, start=1):
                                df = None
                                code = str(stock.get("code") or "")
                                name = stock.get("name") or code
                                try:
                                    today = datetime.now().strftime("%Y-%m-%d")
                                    query_date = today
                                    task_manager.update_progress(
                                        task_name,
                                        {
                                            "current": completed_tasks,
                                            "total": total_tasks,
                                            "message": f"retry daily snapshot {code} ({idx}/{len(retry_stocks)})",
                                        },
                                    )
                                    df = syncer.market_data_source.get_stock_history(
                                        stock_code=code,
                                        start_date=query_date,
                                        end_date=query_date,
                                        period=period,
                                        dividend_type="front",
                                    )
                                    if df is None or df.empty:
                                        now_dt = datetime.now()
                                        previous_trading_day = TradingCalendar.get_previous_trading_day(now_dt).strftime("%Y-%m-%d")
                                        if previous_trading_day != query_date:
                                            query_date = previous_trading_day
                                            df = syncer.market_data_source.get_stock_history(
                                                stock_code=code,
                                                start_date=query_date,
                                                end_date=query_date,
                                                period=period,
                                                dividend_type="front",
                                            )
                                    if df is not None and not df.empty and int(syncer._save_kline_to_db(code, period, df) or 0) > 0:
                                        retry_success += 1
                                        success_tasks += 1
                                        failed_tasks = max(0, failed_tasks - 1)
                                        logger.info(f"daily snapshot retry success: code={code}, name={name}, date={query_date}")
                                    else:
                                        retry_failed += 1
                                        daily_retry_failures.append({"code": code, "name": str(name), "reason": "empty_retry_result"})
                                        logger.warning(f"daily snapshot retry empty: code={code}, name={name}")
                                except Exception as exc:
                                    retry_failed += 1
                                    daily_retry_failures.append({"code": code, "name": str(name), "reason": str(exc)})
                                    logger.warning(f"daily snapshot retry failed: code={code}, name={name}, error={exc}")
                                finally:
                                    if df is not None:
                                        del df
                                        gc.collect()
                            logger.info(
                                f"daily snapshot retry finished: total={len(retry_stocks)}, "
                                f"success={retry_success}, failed={retry_failed}"
                            )

                        batch_result["skipped_new_ipo"] = skipped_new_ipo
                        batch_result["skipped_not_listed"] = skipped_not_listed
                        batch_result["dirty_history"] = dirty_history_codes
                        batch_result["retry_codes"] = retry_codes
                        daily_batch_result = dict(batch_result)
                except Exception as e:
                    logger.warning(f"日线批量路径异常，回逢逐股路径: {e}")
                    daily_batch_used = False

            minute_batch_used = False
            if period in minute_periods:
                try:
                    batch_result = _try_batch_sync_minute(stocks, period, period_name, completed_tasks, total_tasks)
                    minute_batch_used = bool(batch_result.get("used"))
                    completed_tasks = int(batch_result.get("completed", completed_tasks))
                    if minute_batch_used:
                        success_tasks += int(batch_result.get("success", 0) or 0)
                        failed_tasks += int(batch_result.get("failed", 0) or 0)
                except Exception as e:
                    logger.warning(f"minute batch path failed, fallback to per-stock path: period={period}, err={e}")
                    minute_batch_used = False

            if period == "1d" and not daily_batch_used:
                logger.error("daily batch path was not used; skip per-stock fallback for 1d")
                reason = str((batch_result or {}).get("reason") or "daily_batch_not_used")
                task_manager.set_error(task_name, f"日线快照失败: {reason}")
                return
                task_manager.update_progress(
                    task_name,
                    {"message": "日线批量路径未启用，已跳过股回；请查看 daily batch precheck/exception 日志"},
                )
                continue

            if not daily_batch_used and not minute_batch_used:
                for stock in stocks:
                    df = None
                    try:
                        code = stock["code"]
                        name = stock["name"]
                        today = datetime.now().strftime("%Y-%m-%d")
                        query_date = today

                        df = syncer.market_data_source.get_stock_history(
                            stock_code=code,
                            start_date=query_date,
                            end_date=query_date,
                            period=period,
                            dividend_type="front",
                        )

                        if (df is None or df.empty) and period == "1d":
                            now_dt = datetime.now()
                            previous_trading_day = TradingCalendar.get_previous_trading_day(now_dt).strftime("%Y-%m-%d")
                            if previous_trading_day != query_date:
                                logger.warning(
                                    f"⚠️  {code} {name} 1d 当天无数据，回上一交易日重? {previous_trading_day} "
                                    f"(当前时间: {now_dt.strftime('%Y-%m-%d %H:%M:%S')})"
                                )
                                query_date = previous_trading_day
                                df = syncer.market_data_source.get_stock_history(
                                    stock_code=code,
                                    start_date=query_date,
                                    end_date=query_date,
                                    period=period,
                                    dividend_type="front",
                                )

                        if df is not None and not df.empty:
                            saved = int(syncer._save_kline_to_db(code, period, df) or 0)
                            if saved > 0:
                                success_tasks += 1
                                logger.info(f"?{code} {name} {period} 数据更新成功: {len(df)} ?(date={query_date})")
                            else:
                                failed_tasks += 1
                                logger.warning(f"鈿狅笍  {code} {name} {period} 鍐欏叆0琛?(query_date={query_date})")
                        else:
                            failed_tasks += 1
                            logger.warning(
                                f"⚠️  {code} {name} {period} 无数?"
                                f"(query_date={query_date}, now={datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
                            )
                    except Exception as e:
                        failed_tasks += 1
                        logger.error(f"?{stock['code']} {stock['name']} {period} 更新失败: {e}")
                    finally:
                        if df is not None:
                            del df
                            gc.collect()
                        completed_tasks += 1
                        task_manager.update_progress(
                            task_name,
                            {"current": completed_tasks, "message": f"正在更新 {stock['code']} {period_name} ({completed_tasks}/{total_tasks})"},
                        )

            if period == "1d" and critical_indices:
                for stock in critical_indices:
                    df = None
                    try:
                        code = stock.get("code")
                        name = stock.get("name") or code
                        today = datetime.now().strftime("%Y-%m-%d")
                        query_date = today

                        df = syncer.market_data_source.get_stock_history(
                            stock_code=code,
                            start_date=query_date,
                            end_date=query_date,
                            period=period,
                            dividend_type="front",
                        )

                        if df is None or df.empty:
                            now_dt = datetime.now()
                            previous_trading_day = TradingCalendar.get_previous_trading_day(now_dt).strftime("%Y-%m-%d")
                            if previous_trading_day != query_date:
                                query_date = previous_trading_day
                                df = syncer.market_data_source.get_stock_history(
                                    stock_code=code,
                                    start_date=query_date,
                                    end_date=query_date,
                                    period=period,
                                    dividend_type="front",
                                )

                        if df is not None and not df.empty:
                            syncer._save_kline_to_db(code, period, df)
                            success_tasks += 1
                            logger.info(f"?{code} {name} {period} 关键指数更新成功: {len(df)} ?(date={query_date})")
                        else:
                            failed_tasks += 1
                            logger.warning(f"⚠️  {code} {name} {period} 关键指数无数?(query_date={query_date})")
                    except Exception as e:
                        failed_tasks += 1
                        logger.error(f"?{stock.get('code')} {stock.get('name')} {period} 关键指数更新失败: {e}")
                    finally:
                        if df is not None:
                            del df
                            gc.collect()
                        completed_tasks += 1
                        task_manager.update_progress(
                            task_name,
                            {"current": completed_tasks, "message": f"正在更新 {code} 关键指数日线 ({completed_tasks}/{total_tasks})"},
                        )

        if "1d" in sync_periods:
            try:
                from models.stock_models import EmotionCycle, KlineDaily
                from scripts.generate_emotion_cycle import EmotionCycleGenerator
                from sqlalchemy import func
                from utils.database import db

                session = next(db.get_session())
                try:
                    latest_date = session.query(func.max(KlineDaily.trade_date)).scalar()
                    if latest_date:
                        existing = session.query(EmotionCycle).filter(EmotionCycle.date == latest_date).first()
                        if existing:
                            session.delete(existing)
                            session.commit()

                        generator = EmotionCycleGenerator()
                        generator.session = session
                        emotion_cycle = generator.generate_emotion_cycle(latest_date)
                        if emotion_cycle:
                            session.add(emotion_cycle)
                            session.commit()
                            logger.info(f"?情绪周期已生? {latest_date}")
                        else:
                            logger.warning(f"鈿狅笍 鎯呯华鍛ㄦ湡鐢熸垚澶辫触: {latest_date}")
                finally:
                    try:
                        session.close()
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"鐢熸垚鎯呯华鍛ㄦ湡澶辫触: {e}")

            try:
                daily_trade_date = _resolve_daily_trade_date()
                if success_tasks > 0:
                    v4_event_refresh_result = _refresh_gen2_v4_event_dataset_for_date(
                        daily_trade_date,
                        task_name,
                        reason="stock_intraday_daily_snapshot",
                    )
                    if not v4_event_refresh_result.get("ok"):
                        logger.warning(f"G2 V4 event dataset refresh not ok: {v4_event_refresh_result}")
                else:
                    v4_event_refresh_result = {
                        "ok": False,
                        "skipped": True,
                        "trade_date": daily_trade_date,
                        "reason": "daily_snapshot_no_success_rows",
                    }
            except Exception as e:
                logger.error(f"G2 V4 event dataset refresh failed after daily snapshot: {e}")
                v4_event_refresh_result = {"ok": False, "error": str(e)}

        daily_coverage_rate = round(float(success_tasks) / float(total_tasks), 6) if total_tasks > 0 else 0.0
        tolerated_daily_gap_threshold = 0.995
        daily_batch_failed = int((daily_batch_result or {}).get("failed", 0) or 0)
        has_only_small_daily_gaps = (
            daily_batch_result is not None
            and failed_tasks > 0
            and failed_tasks == daily_batch_failed
            and success_tasks > 0
            and daily_coverage_rate >= tolerated_daily_gap_threshold
        )
        validation_status = "passed" if failed_tasks == 0 or has_only_small_daily_gaps else "failed"
        validation_reason = "ok" if failed_tasks == 0 else (
            f"daily_snapshot_partial_coverage:{success_tasks}/{total_tasks}; failed_tasks={failed_tasks}"
            if has_only_small_daily_gaps
            else f"failed_tasks={failed_tasks}"
        )
        warnings: List[Dict[str, Any]] = []
        if has_only_small_daily_gaps:
            warnings.append(
                {
                    "type": "daily_snapshot_partial_coverage",
                    "message": "daily snapshot coverage is above tolerance; unresolved codes kept for review",
                    "coverage_rate": daily_coverage_rate,
                    "threshold": tolerated_daily_gap_threshold,
                    "failed_tasks": failed_tasks,
                    "failed_codes": (daily_batch_result or {}).get("failed_codes") or [],
                }
            )

        results = {
            "message": "当天个股数据更新完成",
            "stats": {"total": total_tasks, "success": success_tasks, "failed": failed_tasks},
            "coverage_rate": daily_coverage_rate,
            "intraday_snapshot": minute_snapshot_result,
            "intraday_snapshot_error": minute_snapshot_error,
            "daily_retry_failures": daily_retry_failures,
            "daily_retry_failure_count": len(daily_retry_failures),
            "daily_batch_result": daily_batch_result,
            "gen2_v4_event_refresh": v4_event_refresh_result,
            "validation_status": validation_status,
            "validation_reason": validation_reason,
            "warnings": warnings,
        }
        task_manager.update_progress(
            task_name,
            {
                "current": completed_tasks,
                "total": total_tasks,
                "message": (
                    f"daily snapshot finished: success={success_tasks}, failed={failed_tasks}, "
                    f"retry_failures={len(daily_retry_failures)}"
                ),
            },
        )
        task_manager.set_results(task_name, results, mark_success=validation_status == "passed")
        logger.info("当天个股数据更新完成")
        if validation_status != "passed":
            task_manager.set_error(task_name, f"个股当天数据更新未完全过: failed_tasks={failed_tasks}")
    except Exception as e:
        logger.error(f"更新当天个股数据失败: {e}")
        task_manager.set_error(task_name, str(e))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def _legacy_official_daily_close_sync_task(task_already_started: bool = False):
    task_name = "official_daily_close_sync"

    if not data_update_lock.acquire(blocking=False):
        msg = "task is already running"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    try:
        from scripts.sync_all_klines import KlineSyncer
        from utils.database import db

        if not task_already_started:
            if not task_manager.start_task(task_name):
                task_manager.set_error(task_name, "task is already running")
                return
        task_manager.update_progress(task_name, {"message": "弢始盘后正式日线落?.."})

        syncer = KlineSyncer()
        try:
            from scheduler.tasks.tushare_daily_kline_task import TushareDailyKlineTask

            task_manager.update_progress(task_name, {"message": "使用 Tushare 执行盘后日线主写?.."})
            tushare_task = TushareDailyKlineTask()
            tushare_task.execute()
        except Exception as source_exc:
            logger.warning(f"Tushare 盘后日线任务不可用，回?KlineSyncer: {source_exc}")
            task_manager.update_progress(task_name, {"message": "Tushare 不可用，回?KlineSyncer 同步 1d..."})
            syncer.sync_all_klines(max_workers=3, periods=["1d"])

        task_manager.update_progress(task_name, {"message": "执行交易日完整校验与自动补修..."})
        repair_result = syncer.ensure_trade_date_complete(max_workers=3)

        task_manager.set_results(
            task_name,
            {
                "message": "盘后正式日线落库完成",
                "repair_result": repair_result,
            },
        )
    except Exception as exc:
        logger.error(f"盘后正式日线落库失败: {exc}")
        task_manager.set_error(task_name, str(exc))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def official_daily_close_sync_task(task_already_started: bool = False):
    task_name = "official_daily_close_sync"

    preflight = _ensure_trade_calendar_fresh_for_today(task_name)
    if not preflight.get("ok") or not preflight.get("trading_day"):
        return

    if not data_update_lock.acquire(blocking=False):
        msg = "task is already running"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    try:
        from scripts.sync_all_klines import KlineSyncer
        from scheduler.trading_calendar import TradingCalendar
        from utils.database import db

        if not task_already_started:
            if not task_manager.start_task(task_name):
                task_manager.set_error(task_name, "task is already running")
                return

        now_dt = datetime.now()
        if not TradingCalendar.is_trading_day(now_dt):
            msg = f"skip official daily close sync for non-trading day: {now_dt.strftime('%Y-%m-%d')}"
            logger.info(msg)
            task_manager.update_progress(task_name, {"current": 0, "total": 0, "message": msg})
            task_manager.set_results(
                task_name,
                {
                    "message": msg,
                    "skipped": True,
                    "skip_reason": "non_trading_day",
                    "validation_status": "skipped_non_trading_day",
                },
                mark_success=True,
            )
            return
        if not _runtime_has_xtquant():
            _mark_qmt_host_collector_delegated(
                task_name,
                scenario="after-close-daily",
                target_date=now_dt.strftime("%Y-%m-%d"),
                periods=["1d"],
                extra={"universe": "stock,index"},
            )
            return
        task_manager.update_progress(task_name, {"message": "弢始执行盘后正式日线落?.."})

        syncer = KlineSyncer()
        active_source = "qmt_xtquant"
        validation_status = "passed"
        validation_reason = "ok"
        blocked_codes: List[Dict[str, Any]] = []
        sync_results: Dict[str, Any] = {}
        repair_results: Dict[str, Any] = {}

        for sync_type, label in (("stock", "股票"), ("index", "指数")):
            task_manager.update_progress(task_name, {"message": f"using QMT xtquant for {label} 1d official sync..."})
            try:
                before_stats = dict(getattr(syncer, "stats", {}) or {})
                syncer.sync_all_klines(max_workers=3, periods=["1d"], type=sync_type)
                after_stats = dict(getattr(syncer, "stats", {}) or {})
                sync_results[sync_type] = {
                    "source": "qmt_xtquant",
                    "total_delta": int(after_stats.get("total", 0) or 0) - int(before_stats.get("total", 0) or 0),
                    "success_delta": int(after_stats.get("success", 0) or 0) - int(before_stats.get("success", 0) or 0),
                    "failed_delta": int(after_stats.get("failed", 0) or 0) - int(before_stats.get("failed", 0) or 0),
                }
            except Exception as source_exc:
                active_source = "qmt_xtquant_partial_with_tushare_fallback"
                logger.warning(f"QMT after-hours {label} sync failed; trying Tushare fallback: {source_exc}")
                task_manager.update_progress(task_name, {"message": f"QMT unavailable; using Tushare fallback for {label} 1d..."})
                try:
                    from scheduler.tasks.tushare_daily_kline_task import TushareDailyKlineTask

                    tushare_task = TushareDailyKlineTask()
                    tushare_task.execute()
                    sync_results[sync_type] = {"source": "tushare_fallback", "error": str(source_exc)}
                except Exception as fallback_exc:
                    raise RuntimeError(f"{label} daily QMT and Tushare are both unavailable: {fallback_exc}") from fallback_exc

            task_manager.update_progress(task_name, {"message": f"执行{label}交易日完整校验与自动修复..."})
            repair_result = syncer.ensure_trade_date_complete(sync_type=sync_type, max_workers=3)
            repair_results[sync_type] = repair_result
            if repair_result.get("status") != "completed":
                validation_status = "failed"
                validation_reason = f"{sync_type}_daily_completeness_failed"
                blocked_codes.extend(
                    {"code": code, "type": sync_type, "reason": "still_missing_after_repair"}
                    for code in (repair_result.get("remaining_codes") or [])[:200]
                )

        task_manager.set_results(
            task_name,
            {
                "message": "盘后正式日线落库完成",
                "active_source": active_source,
                "validation_status": validation_status,
                "validation_reason": validation_reason,
                "blocked_codes": blocked_codes,
                "sync_results": sync_results,
                "repair_results": repair_results,
            },
        )
        if validation_status == "failed":
            task_manager.set_error(task_name, f"日线校验未过: {validation_reason}")
    except Exception as exc:
        logger.error(f"盘后正式日线落库失败: {exc}")
        task_manager.set_error(task_name, str(exc))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def minute_kline_daily_repair_validate_task(
    task_already_started: bool = False,
    days: int = 2,
    task_name: str = "minute_kline_daily_repair_validate",
    periods: Optional[List[str]] = None,
):

    if not data_update_lock.acquire(blocking=False):
        msg = "task is already running"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    try:
        from datetime import timedelta
        from scheduler.trading_calendar import TradingCalendar, get_trading_dates_from_db
        from scripts.sync_all_klines import KlineSyncer

        from utils.market_warehouse import clickhouse_client, clickhouse_query_df

        if not task_already_started:
            if not task_manager.start_task(task_name):
                task_manager.set_error(task_name, "分钟K线补全验证任务正在运行中，请稍后再试")
                return

        now_dt = datetime.now()
        if TradingCalendar.is_trading_day(now_dt) and now_dt.time() >= dt_time(15, 5):
            end_date = now_dt.strftime("%Y-%m-%d")
        else:
            end_date = TradingCalendar.get_previous_trading_day(now_dt).strftime("%Y-%m-%d")

        scan_start_dt = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=max(14, int(days) * 3))
        raw_trade_dates = get_trading_dates_from_db(scan_start_dt.strftime("%Y-%m-%d"), end_date)
        unique_trade_dates = sorted({str(item)[:10] for item in raw_trade_dates if item})
        trade_dates = unique_trade_dates[-max(1, int(days)):]
        if not trade_dates:
            raise RuntimeError("no recent trade dates resolved")
        start_date = trade_dates[0]
        end_date = trade_dates[-1]

        expected_per_day = {"5m": 48, "15m": 16, "30m": 8, "60m": 4}
        table_map = {
            "5m": "kline_minute_5",
            "15m": "kline_minute_15",
            "30m": "kline_minute_30",
            "60m": "kline_minute_60",
        }
        requested_periods = [str(item).strip().lower() for item in (periods or ["5m", "15m", "30m", "60m"]) if str(item).strip()]
        periods = [item for item in requested_periods if item in table_map]
        if not periods:
            periods = ["5m", "15m", "30m", "60m"]

        if not _runtime_has_xtquant():
            _mark_qmt_host_collector_delegated(
                task_name,
                scenario="after-close",
                target_date=f"{start_date}~{end_date}",
                periods=periods,
                extra={"trade_dates": trade_dates},
            )
            return

        qmt_collector_script = Path(__file__).resolve().parents[1] / "scripts" / "qmt_xtquant_data_source_task.py"
        if not qmt_collector_script.exists():
            raise RuntimeError(f"script_not_found:{qmt_collector_script}")

        qmt_report_dir = report_path(
            "system_minute_kline_repair",
            f"qmt_gap_repair_{start_date}_{end_date}_{datetime.now():%Y%m%d_%H%M%S}",
        )
        qmt_cmd = [
            sys.executable,
            str(qmt_collector_script),
            "--mode",
            "minute-gap-repair",
            "--scenario",
            "after-close",
            "--start-date",
            str(start_date)[:10],
            "--end-date",
            str(end_date)[:10],
            "--minute-periods",
            ",".join(periods),
            "--universe",
            "stock,index",
            "--audit-code-chunk-size",
            "400",
            "--repair-code-chunk-size",
            "1",
            "--minute-batch-size",
            "1",
            "--minute-batch-timeout-sec",
            "240",
            "--minute-chunk-timeout-sec",
            "900",
            "--max-retries",
            "1",
            "--retry-sleep",
            "1",
            "--minute-report-dir",
            str(qmt_report_dir),
        ]
        task_manager.update_progress(
            task_name,
            {
                "current": 0,
                "total": max(1, len(trade_dates) * len(periods)),
                "message": f"repairing minute K-lines from QMT gap audit: {start_date}~{end_date}",
            },
        )
        proc = subprocess.run(
            qmt_cmd,
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45 * 60,
        )
        summary_file = qmt_report_dir / "collector_summary.json"
        qmt_summary: Dict[str, Any] = {}
        if summary_file.exists():
            try:
                qmt_summary = json.loads(summary_file.read_text(encoding="utf-8"))
            except Exception:
                qmt_summary = {}
        ok = proc.returncode == 0 and bool(qmt_summary.get("ok", proc.returncode == 0))
        result = {
            "message": f"QMT minute gap repair validation completed for last {len(trade_dates)} trading days",
            "trade_dates": trade_dates,
            "periods": periods,
            "provider": "qmt_xtquant_gap_repair",
            "returncode": proc.returncode,
            "cmd": qmt_cmd,
            "report_dir": str(qmt_report_dir),
            "report_path": str(summary_file),
            "summary": qmt_summary,
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-4000:],
            "validation_status": "passed" if ok else "failed",
        }
        result["coverage_alert"] = _maybe_send_minute_kline_coverage_alert(result, threshold=0.95)
        task_manager.set_results(task_name, result, mark_success=ok)
        if not ok:
            task_manager.set_error(task_name, result["stderr_tail"] or result["stdout_tail"] or result["message"])
        return

        syncer = KlineSyncer()
        assets = []
        for item in syncer.get_all_stocks():
            if item.get("code"):
                assets.append({**item, "type": item.get("type") or "stock"})
        for item in syncer.get_all_indices():
            if item.get("code"):
                assets.append({**item, "type": "index"})
        asset_by_code = {str(item.get("code")): item for item in assets if item.get("code")}

        total_checks = len(asset_by_code) * len(periods)
        task_manager.update_progress(
            task_name,
            {
                "current": 0,
                "total": total_checks,
                "message": f"checking last {len(trade_dates)} trading days minute K-lines: {start_date}~{end_date}",
            },
        )

        before_summary: Dict[str, Any] = {}
        repair_plan: Dict[str, List[str]] = {period: [] for period in periods}

        for period in periods:
            table = table_map[period]
            df = clickhouse_query_df(
                f"""
                SELECT code, toString(toDate(datetime)) AS trade_date,
                       count() AS raw_count,
                       uniqExact(datetime) AS uniq_count
                FROM {table}
                WHERE toDate(datetime) >= toDate(?)
                  AND toDate(datetime) <= toDate(?)
                GROUP BY code, trade_date
                """,
                [start_date, end_date],
            )
            counts = {}
            if df is not None and not df.empty:
                for row in df.itertuples(index=False):
                    counts[(str(row.code), str(row.trade_date)[:10])] = {
                        "raw": int(row.raw_count or 0),
                        "uniq": int(row.uniq_count or 0),
                    }

            missing_codes = []
            duplicate_codes = []
            complete_codes = 0
            expected = int(expected_per_day[period])
            for code in asset_by_code:
                code_ok = True
                code_has_duplicate = False
                for trade_date in trade_dates:
                    stat = counts.get((code, str(trade_date)[:10]), {"raw": 0, "uniq": 0})
                    if int(stat["uniq"]) < expected:
                        code_ok = False
                    if int(stat["raw"]) > int(stat["uniq"]):
                        code_has_duplicate = True
                        code_ok = False
                if code_ok:
                    complete_codes += 1
                else:
                    missing_codes.append(code)
                    if code_has_duplicate:
                        duplicate_codes.append(code)

            repair_plan[period] = missing_codes
            before_summary[period] = {
                "total_codes": len(asset_by_code),
                "complete_codes": complete_codes,
                "repair_codes": len(missing_codes),
                "duplicate_codes": len(duplicate_codes),
                "expected_bars_per_day": expected,
            }

        total_repairs = sum(len(v) for v in repair_plan.values())
        repair_codes = sorted({code for codes in repair_plan.values() for code in codes})
        success = 0
        failed = 0
        failure_examples: List[Dict[str, Any]] = []
        qmt_result: Dict[str, Any] = {}
        fallback_result: Dict[str, Any] = {}

        qmt_script = Path(__file__).resolve().parents[1] / "scripts" / "qmt_xtquant_minute_backfill_validate.py"
        if not qmt_script.exists():
            failed = 1
            failure_examples.append({"provider": "qmt_xtquant", "reason": f"script_not_found:{qmt_script}"})
        else:
            qmt_report = report_path(
                "system_minute_kline_repair",
                f"qmt_minute_repair_{datetime.now():%Y%m%d_%H%M%S}.json",
            )
            qmt_report.parent.mkdir(parents=True, exist_ok=True)
            qmt_cmd = [
                sys.executable,
                str(qmt_script),
                "--phase",
                "all",
                "--start-date",
                str(start_date)[:10],
                "--end-date",
                str(end_date)[:10],
                "--periods",
                ",".join(periods),
                "--batch-size",
                "30",
                "--include-index",
                "--reset-stage",
                "--report",
                str(qmt_report),
            ]
            if repair_codes:
                qmt_cmd.extend(["--codes", ",".join(repair_codes)])
            task_manager.update_progress(
                task_name,
                {
                    "current": 0,
                    "total": max(1, len(repair_codes) or len(asset_by_code)),
                    "message": f"rebuilding minute K-lines from QMT: {start_date}~{end_date}",
                },
            )
            proc = subprocess.run(
                qmt_cmd,
                cwd=str(Path(__file__).resolve().parents[1]),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45 * 60,
            )
            qmt_result = {
                "provider": "qmt_xtquant",
                "returncode": proc.returncode,
                "report_path": str(qmt_report),
                "repair_code_count": len(repair_codes),
                "stdout_tail": (proc.stdout or "")[-4000:],
                "stderr_tail": (proc.stderr or "")[-4000:],
            }
            if proc.returncode == 0:
                success = total_repairs
            else:
                failed = 1
                failure_examples.append(
                    {
                        "provider": "qmt_xtquant",
                        "returncode": proc.returncode,
                        "report_path": str(qmt_report),
                    }
                )

        if failed > 0 or not qmt_result:
            fallback_script = Path(__file__).resolve().parents[1] / "scripts" / "collect_baostock_all_minutes.py"
            if fallback_script.exists():
                fallback_report = report_path(
                    "system_minute_kline_repair",
                    f"baostock_minute_fallback_{datetime.now():%Y%m%d_%H%M%S}.json",
                )
                fallback_timeout_sec = 45 * 60
                fallback_cmd = [
                    sys.executable,
                    str(fallback_script),
                    "--start-date",
                    str(start_date)[:10],
                    "--end-date",
                    str(end_date)[:10],
                    "--frequencies",
                    "5,15,30,60",
                    "--chunk",
                    "day",
                    "--replace",
                    "--no-resume",
                    "--derive-higher-from-5m",
                    "--timeout-sec",
                    "45",
                    "--retries",
                    "1",
                    "--sleep-seconds",
                    "0.02",
                    "--report",
                    str(fallback_report),
                ]
                task_manager.update_progress(
                    task_name,
                    {
                        "current": 0,
                        "total": max(1, total_repairs),
                        "message": f"QMT minute repair failed; using baostock fallback: {start_date}~{end_date}",
                    },
                )
                proc = subprocess.run(
                    fallback_cmd,
                    cwd=str(Path(__file__).resolve().parents[1]),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=fallback_timeout_sec,
                )
                fallback_result = {
                    "provider": "baostock",
                    "returncode": proc.returncode,
                    "timeout_sec": fallback_timeout_sec,
                    "timed_out": False,
                    "report_path": str(fallback_report),
                    "stdout_tail": (proc.stdout or "")[-4000:],
                    "stderr_tail": (proc.stderr or "")[-4000:],
                }
                if proc.returncode == 0:
                    failed = 0
                    success = total_repairs
                else:
                    failed = max(1, failed)
                    failure_examples.append(
                        {
                            "provider": "baostock",
                            "returncode": proc.returncode,
                            "report_path": str(fallback_report),
                        }
                    )
            else:
                failed = max(1, failed)
                failure_examples.append({"provider": "baostock", "reason": f"script_not_found:{fallback_script}"})

        client = clickhouse_client()

        repair_plan = {period: [] for period in periods}

        for period in periods:
            codes = repair_plan.get(period) or []
            if not codes:
                continue
            table = table_map[period]
            if _is_month_partitioned_minute_table(client, table):
                for idx in range(0, len(codes), 500):
                    chunk = codes[idx:idx + 500]
                    quoted = ", ".join("'" + code.replace("\\", "\\\\").replace("'", "\\'") + "'" for code in chunk)
                    client.command(
                        f"""
                        ALTER TABLE {table}
                        DELETE WHERE code IN ({quoted})
                          AND toDate(datetime) >= toDate('{start_date}')
                          AND toDate(datetime) <= toDate('{end_date}')
                        SETTINGS mutations_sync = 1
                        """
                    )
            else:
                logger.warning("skip destructive minute replacement on unpartitioned table: %s", table)

            for code in codes:
                asset = asset_by_code.get(code)
                if not asset:
                    continue
                processed += 1
                task_manager.update_progress(
                    task_name,
                    {
                        "current": processed,
                        "total": total_repairs,
                        "message": f"琛ユ媺 {code} {period} ({processed}/{total_repairs})",
                    },
                )
                try:
                    ok = bool(syncer.sync_kline_for_stock(asset, period, start_date=start_date, end_date=end_date))
                    if ok:
                        success += 1
                    else:
                        failed += 1
                        if len(failure_examples) < 50:
                            failure_examples.append({"code": code, "period": period, "reason": "sync_return_false"})
                except Exception as exc:
                    failed += 1
                    if len(failure_examples) < 50:
                        failure_examples.append({"code": code, "period": period, "reason": str(exc)})

        after_summary: Dict[str, Any] = {}
        still_bad: List[Dict[str, Any]] = []
        for period in periods:
            table = table_map[period]
            df = clickhouse_query_df(
                f"""
                SELECT code, toString(toDate(datetime)) AS trade_date,
                       count() AS raw_count,
                       uniqExact(datetime) AS uniq_count
                FROM {table}
                WHERE toDate(datetime) >= toDate(?)
                  AND toDate(datetime) <= toDate(?)
                GROUP BY code, trade_date
                """,
                [start_date, end_date],
            )
            counts = {}
            if df is not None and not df.empty:
                for row in df.itertuples(index=False):
                    counts[(str(row.code), str(row.trade_date)[:10])] = {
                        "raw": int(row.raw_count or 0),
                        "uniq": int(row.uniq_count or 0),
                    }

            complete_codes = 0
            duplicate_codes = 0
            expected = int(expected_per_day[period])
            for code in asset_by_code:
                code_ok = True
                code_dup = False
                for trade_date in trade_dates:
                    stat = counts.get((code, str(trade_date)[:10]), {"raw": 0, "uniq": 0})
                    if int(stat["uniq"]) < expected:
                        code_ok = False
                    if int(stat["raw"]) > int(stat["uniq"]):
                        code_dup = True
                        code_ok = False
                if code_ok:
                    complete_codes += 1
                else:
                    if code_dup:
                        duplicate_codes += 1
                    if len(still_bad) < 200:
                        still_bad.append({"code": code, "period": period, "reason": "incomplete_or_duplicate"})

            after_summary[period] = {
                "total_codes": len(asset_by_code),
                "complete_codes": complete_codes,
                "still_bad_codes": len(asset_by_code) - complete_codes,
                "duplicate_codes": duplicate_codes,
                "expected_bars_per_day": expected,
                "coverage_ratio": round((complete_codes / len(asset_by_code)) if asset_by_code else 0.0, 6),
            }

        coverage_threshold = 0.95
        low_coverage_periods = []
        for period, summary in after_summary.items():
            coverage_ratio = float(summary.get("coverage_ratio") or 0.0)
            if coverage_ratio < coverage_threshold:
                low_coverage_periods.append(
                    {
                        "period": period,
                        "coverage_ratio": coverage_ratio,
                        "complete_codes": int(summary.get("complete_codes") or 0),
                        "total_codes": int(summary.get("total_codes") or 0),
                        "still_bad_codes": int(summary.get("still_bad_codes") or 0),
                    }
                )

        validation_status = "passed" if failed == 0 and not low_coverage_periods else "failed"
        result = {
            "message": f"minute K-line repair validation completed for last {len(trade_dates)} trading days",
            "trade_dates": trade_dates,
            "asset_count": len(asset_by_code),
            "periods": periods,
            "before": before_summary,
            "after": after_summary,
            "repairs": {"planned": total_repairs, "success": success, "failed": failed},
            "coverage_threshold": coverage_threshold,
            "low_coverage_periods": low_coverage_periods,
            "qmt_xtquant_result": qmt_result,
            "fallback_result": fallback_result,
            "failure_examples": failure_examples,
            "still_bad": still_bad,
            "still_bad_sample_count": len(still_bad),
            "validation_status": validation_status,
        }
        result["coverage_alert"] = _maybe_send_minute_kline_coverage_alert(result, threshold=coverage_threshold)
        task_manager.set_results(task_name, result)
        if validation_status != "passed" and (failed > 0 or bool(low_coverage_periods)):
            task_manager.set_error(task_name, f"分钟K线补全验证未完全通过: still_bad={len(still_bad)}, failed={failed}")
    except Exception as exc:
        logger.exception(f"分钟K线补全验证失? {exc}")
        task_manager.set_error(task_name, str(exc))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def market_minute_history_repair_task(
    task_already_started: bool = False,
    days: int = 30,
    periods: Optional[List[str]] = None,
):
    return minute_kline_daily_repair_validate_task(
        task_already_started=task_already_started,
        days=days,
        task_name="market_minute_history_repair",
        periods=periods,
    )


def repair_previous_daily_kline_task(task_already_started: bool = False):
    task_name = "repair_previous_daily_kline"

    if not data_update_lock.acquire(blocking=False):
        msg = "已有其他数据更新任务正在执行，次日巡棢修复跳过"
        logger.warning(msg)
        task_manager.set_error(task_name, msg)
        return

    try:
        from scripts.sync_all_klines import KlineSyncer

        if not task_already_started:
            if not task_manager.start_task(task_name):
                task_manager.set_error(task_name, "task is already running")
                return
        deduplication = _compact_recent_daily_kline_duplicates(days=14)
        if not deduplication.get("ok"):
            task_manager.set_results(task_name, {"message": "daily_kline_duplicate_compaction_failed", "daily_deduplication": deduplication})
            task_manager.set_error(task_name, f"daily_kline_duplicate_compaction_failed: {deduplication.get('error') or deduplication}")
            return
        if not _runtime_has_xtquant():
            _mark_qmt_host_collector_delegated(
                task_name,
                scenario="next-day-daily-repair",
                target_date=datetime.now().strftime("%Y-%m-%d"),
                periods=["1d"],
                extra={"universe": "stock,index", "daily_deduplication": deduplication},
            )
            return
        task_manager.update_progress(task_name, {"message": "弢始次日巡棢并修复上丢交易日日?.."})

        syncer = KlineSyncer()
        result = syncer.ensure_trade_date_complete(max_workers=3)
        task_manager.set_results(task_name, {"message": "次日巡检修复完成", "result": result})
        if isinstance(result, dict) and result.get("status") == "failed":
            task_manager.set_error(
                task_name,
                f"次日巡检修复未过: {result.get('after', {}).get('actual_count')}/"
                f"{result.get('after', {}).get('baseline_count')}",
            )
    except Exception as exc:
        logger.error(f"次日巡检修复失败: {exc}")
        task_manager.set_error(task_name, str(exc))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def daily_kline_coverage_maintenance_task(task_already_started: bool = False):
    """Publish the host-side QMT daily-coverage repair result without importing xtquant in the container."""
    task_name = "daily_kline_coverage_maintenance"
    if not data_update_lock.acquire(blocking=False):
        task_manager.set_error(task_name, "another data update task is running")
        return
    try:
        if not task_already_started and not task_manager.start_task(task_name):
            task_manager.set_error(task_name, "task is already running")
            return
        task_manager.update_progress(task_name, {"message": "reading Windows QMT host daily-coverage repair report"})
        artifact = runtime_path("daily_kline_coverage", "latest.json")
        report: Dict[str, Any] = {}
        if artifact.exists():
            try:
                report = json.loads(artifact.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                report = {"read_error": str(exc)}
        checked_at = str(report.get("checked_at") or "")
        current_day = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        repair = report.get("repair") if isinstance(report.get("repair"), dict) else {}
        result = {"execution_source": "qmt_xtquant_host_collector", "coverage": report}
        task_manager.set_results(task_name, result)
        if not report or not checked_at.startswith(current_day):
            task_manager.set_error(task_name, "host daily coverage report is missing or stale")
        elif repair.get("status") != "completed":
            task_manager.set_error(task_name, f"host daily coverage repair failed: {repair.get('error') or 'missing repair result'}")
        else:
            unit_audit = report.get("unit_audit") if isinstance(report.get("unit_audit"), dict) else {}
            unresolved_units = int(unit_audit.get("unresolved_mismatch_rows") or 0)
            if unresolved_units > 0:
                task_manager.set_error(
                    task_name,
                    f"daily unit audit has {unresolved_units} unresolved volume/amount mismatches",
                )
    except Exception as exc:
        logger.exception("daily K-line coverage maintenance failed")
        task_manager.set_error(task_name, str(exc))
    finally:
        if data_update_lock.locked():
            data_update_lock.release()


def repair_all_history_klines_task():
    """System configuration task helper."""
    task_name = "repair_all_history_klines"
    
    try:
        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始修复全量历史K线数?.."})
        
        # 首先修复日K线数?
        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始修复日K线数?.."})
        
        # 调用修复日K线数据的函数
        from utils.database import db
        from models.stock_models import EmotionCycle
        import pandas as pd
        
        # 获取扢有股票代?
        session = next(db.get_session())
        try:
            # 获取扢有股票代?
            from models.stock_models import Stock
            stocks = session.query(Stock.code).filter(
                Stock.type.in_(['stock', 'index'])
            ).all()
            stock_codes = [stock.code for stock in stocks]
            total_stocks = len(stock_codes)
            
            task_manager.update_progress(task_name, {"message": "task progress"})
            
            repaired_stocks = 0
            repaired_records = 0
            
            # 逐个股票修复
            for i, code in enumerate(stock_codes):
                task_manager.update_progress(task_name, {
                    "current": i + 1,
                    "message": f"正在修复 {code} 的日K线数?({i+1}/{total_stocks})"
                })
                
                # 获取该股票的扢有日线数?
                klines = session.query(KlineDaily).filter(
                    KlineDaily.code == code
                ).order_by(KlineDaily.trade_date).all()
                
                if len(klines) > 1:
                    # Convert the result to a DataFrame before calculating metrics.
                    data = []
                    for kline in klines:
                        data.append({
                            'id': kline.id,
                            'trade_date': kline.trade_date,
                            'close': kline.close
                        })
                    
                    df = pd.DataFrame(data)
                    
                    # 璁＄畻娑ㄨ穼棰濆拰娑ㄨ穼骞?
                    df['change_amount'] = df['close'].diff()
                    df['change_pct'] = (df['change_amount'] / df['close'].shift(1)) * 100
                    
                    # 计算振幅（需要high和low数据?
                    for j, kline in enumerate(klines):
                        if j > 0:
                            prev_close = klines[j-1].close
                            if prev_close > 0:
                                amplitude = ((kline.high - kline.low) / prev_close) * 100
                                kline.amplitude = amplitude
                                kline.change_amount = float(df.loc[j, 'change_amount'])
                                kline.change_pct = float(df.loc[j, 'change_pct'])
                                repaired_records += 1
                    
                    # 每处?0只股票提交一?
                    if (i + 1) % 10 == 0:
                        session.commit()
                    
                    repaired_stocks += 1
            
            # 提交剩余的修?
            session.commit()
            
            logger.info("task message")
            
        finally:
            session.close()
        
        # 然后同步全量历史K线数?
        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始同步全量历史K线数?.."})
        
        from scripts.sync_all_klines import KlineSyncer
        
        # 创建同步器并执行
        syncer = KlineSyncer()
        syncer.sync_all_klines(max_workers=3)
        
        # 设置结果
        results = {
            "message": "task message",
            "stats": {
                "repaired_daily_klines": repaired_records,
                "synced_history_klines": syncer.stats.get("total", 0)
            }
        }
        task_manager.set_results(task_name, results)
        logger.info("task message")
        
    except Exception as e:
        logger.error(f"修复全量历史K线数据失? {e}")
        task_manager.set_error(task_name, str(e))

def update_trade_calendar_task():
    """System configuration task helper."""
    task_name = "update_trade_calendar"
    
    try:
        logger.info("task message")
        task_manager.update_progress(task_name, {"message": "弢始更新股市日?.."})
        
        # 调用同步交易日历的函?
        from scripts.sync_trade_calendar import sync_trade_calendar
        
        # 执行同步
        sync_trade_calendar()
        
        # 设置结果
        results = {
            "message": "股市日历更新完成"
        }
        task_manager.set_results(task_name, results)
        logger.info("股市日历更新完成")
        
    except Exception as e:
        logger.error(f"更新股市日历失败: {e}")
        task_manager.set_error(task_name, str(e))


def repair_emotion_cycle_30d_task(days: int = 30):
    """
    丢键修复近 N 个交易日的情绪周期数据（默认 30）?
    """
    task_name = "repair_emotion_cycle_30d"
    try:
        from utils.database import db
        from models.stock_models import EmotionCycle

        if data_update_lock.locked():
            # Avoid concurrent execution with other heavy data-maintenance tasks.
            raise Exception("数据更新任务繁忙，请稍后重试")

        data_update_lock.acquire()
        task_manager.update_progress(task_name, {"message": f"弢始修复近{days}个交易日的情绪周?.."})

        session = next(db.get_session())
        try:
            dates = _get_recent_complete_kline_dates(session, int(days))
        finally:
            session.close()

        metric_repair_result = None
        if dates:
            try:
                from scripts.repair_kline_daily_metrics import repair_metrics

                metric_repair_result = repair_metrics(str(min(dates)), str(max(dates)))
                logger.info(f"emotion repair preflight kline metric repair: {metric_repair_result}")
            except Exception as exc:
                logger.warning(f"emotion repair preflight kline metric repair failed: {exc}")

        stale_deleted = 0
        if dates:
            session = next(db.get_session())
            try:
                stale_rows = (
                    session.query(EmotionCycle)
                    .filter(
                        EmotionCycle.date >= min(dates),
                        EmotionCycle.date <= max(dates),
                        ~EmotionCycle.date.in_(dates),
                    )
                    .all()
                )
                stale_deleted = len(stale_rows)
                for row in stale_rows:
                    session.delete(row)
                if stale_rows:
                    session.commit()
            finally:
                session.close()

        task_manager.update_progress(task_name, {"total": len(dates), "current": 0})

        # 逐日重算
        saved_total = 0
        skipped_total = 0
        for i, d in enumerate(reversed(dates)):
            task_manager.update_progress(task_name, {"current": i + 1, "message": f"重算情绪周期: {d} ({i+1}/{len(dates)})"})
            r = _repair_emotion_for_dates([d])
            saved_total += int(r.get("saved", 0))
            skipped_total += int(r.get("skipped", 0))

        task_manager.set_results(
            task_name,
            {
                "message": f"emotion cycle repair completed for last {len(dates)} trading days",
                "stats": {
                    "days": len(dates),
                    "saved": saved_total,
                    "skipped": skipped_total,
                    "stale_deleted": stale_deleted,
                    "metric_repair": metric_repair_result,
                },
            },
        )
    except Exception as e:
        logger.error(f"修复?0天情绪周期失? {e}")
        task_manager.set_error(task_name, str(e))
    finally:
        if data_update_lock.locked():
            try:
                data_update_lock.release()
            except Exception:
                pass


def repair_emotion_cycle_latest_task():
    """
    Repair the latest emotion cycle and every recent complete-date gap.

    A one-day task is not sufficient: if the scheduler is unavailable at a
    single execution window, that date used to remain absent indefinitely and
    the home-page timeline silently connected the surrounding sessions.
    """
    task_name = "repair_emotion_cycle_latest"
    try:
        from utils.database import db
        from sqlalchemy import func
        from models.stock_models import EmotionCycle

        if data_update_lock.locked():
            raise Exception("数据更新任务繁忙，请稍后重试")

        data_update_lock.acquire()
        task_manager.update_progress(task_name, {"message": "审计最近完整交易日的情绪周期覆盖..."})

        session = next(db.get_session())
        try:
            # The limit is deliberately wider than the chart window.  This
            # makes the next successful run self-heal missed after-close jobs
            # instead of assuming that yesterday was the only possible gap.
            dates = _get_recent_complete_kline_dates(session, 60)
            latest_date = dates[0] if dates else None
            existing_dates = {
                row[0]
                for row in session.query(EmotionCycle.date)
                .filter(EmotionCycle.date.in_(dates))
                .all()
                if row[0]
            } if dates else set()
        finally:
            session.close()

        if not latest_date:
            raise Exception("required data not available")

        missing_dates = sorted(date_value for date_value in dates if date_value not in existing_dates)
        # Always rebuild the latest complete date in case its official daily
        # bars were corrected; additionally repair every discovered hole.
        repair_dates = sorted(set(missing_dates + [latest_date]))
        saved_total = 0
        skipped_total = 0
        for index, trade_date in enumerate(repair_dates, start=1):
            task_manager.update_progress(
                task_name,
                {
                    "total": len(repair_dates),
                    "current": index,
                    "message": f"重算情绪周期: {trade_date} ({index}/{len(repair_dates)})",
                },
            )
            result = _repair_emotion_for_dates([trade_date])
            saved_total += int(result.get("saved", 0))
            skipped_total += int(result.get("skipped", 0))

        task_manager.set_results(
            task_name,
            {
                "message": "情绪周期完整性修复完成",
                "stats": {
                    "latest_date": str(latest_date),
                    "missing_dates": [str(date_value) for date_value in missing_dates],
                    "repaired_dates": [str(date_value) for date_value in repair_dates],
                    "saved": saved_total,
                    "skipped": skipped_total,
                },
            },
        )
    except Exception as e:
        logger.error(f"重算朢新交易日情绪周期失败: {e}")
        task_manager.set_error(task_name, str(e))
    finally:
        if data_update_lock.locked():
            try:
                data_update_lock.release()
            except Exception:
                pass


def _run_scheduled_repair_emotion_cycle_latest():
    task_name = "repair_emotion_cycle_latest"
    if not task_manager.start_task(task_name):
        logger.info("scheduled emotion-cycle repair skipped: task is already running")
        return
    repair_emotion_cycle_latest_task()


def refresh_market_sentiment_snapshot_task():
    """Pre-aggregate the homepage's provisional intraday market snapshot."""
    try:
        from services.market_sentiment_cache import refresh_daily_history, refresh_intraday_snapshot

        daily_rows = refresh_daily_history()
        intraday_written = refresh_intraday_snapshot()
        logger.info(
            "market sentiment snapshot refreshed: daily_rows=%s intraday_written=%s",
            daily_rows,
            intraday_written,
        )
    except Exception as exc:
        logger.warning("market sentiment snapshot refresh failed: %s", exc)


def refresh_confirmed_market_sentiment_snapshot_task():
    """Publish post-close turnover from daily bars, or a complete 15:00 minute close."""
    try:
        from services.market_sentiment_cache import refresh_daily_history, refresh_intraday_snapshot

        daily_rows = refresh_daily_history(force=True)
        final_minute_written = refresh_intraday_snapshot(finalize_if_closed=True)
        logger.info(
            "confirmed market sentiment snapshot refresh: daily_rows=%s final_minute_written=%s",
            daily_rows,
            final_minute_written,
        )
    except Exception as exc:
        logger.warning("confirmed market sentiment snapshot refresh failed: %s", exc)


def _run_homepage_integrity_after_close():
    """Finalize market turnover first, then repair all completed emotion gaps."""
    refresh_confirmed_market_sentiment_snapshot_task()
    repair_emotion_cycle_latest_task()
    try:
        from services.margin_sentiment import refresh_margin_sentiment

        result = refresh_margin_sentiment(days=30)
        logger.info("homepage margin sentiment refresh: %s", result)
        if result.get("stale"):
            logger.warning("homepage margin sentiment source is stale: %s", result)
    except Exception as exc:
        logger.warning("homepage margin sentiment refresh failed: %s", exc)


def _run_homepage_margin_refresh():
    """Retry delayed exchange margin reports without rerunning other jobs."""
    try:
        from services.margin_sentiment import refresh_margin_sentiment

        result = refresh_margin_sentiment(days=30)
        logger.info("scheduled homepage margin refresh: %s", result)
        if result.get("stale"):
            logger.warning("scheduled homepage margin refresh remains stale: %s", result)
    except Exception as exc:
        logger.warning("scheduled homepage margin refresh failed: %s", exc)


def start_homepage_integrity_scheduler():
    """Run homepage-derived data jobs even when core ingestion scheduling is off.

    This scheduler only reads the QMT/ClickHouse warehouse and writes display
    snapshots plus emotion-cycle derivatives.  It deliberately never invokes
    stock, minute, or daily data collection, so it remains safe alongside the
    Windows host collector.
    """
    global _homepage_integrity_scheduler
    from services.operations.schedulers import ObservedScheduler as BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    with _homepage_integrity_scheduler_lock:
        if _homepage_integrity_scheduler is None:
            _homepage_integrity_scheduler = BackgroundScheduler(timezone="Asia/Shanghai", owner="数据维护")
            _homepage_integrity_scheduler.start()
        scheduler = _homepage_integrity_scheduler

    scheduler.add_job(
        refresh_market_sentiment_snapshot_task,
        CronTrigger(day_of_week="mon-fri", hour="9-11,13-14", minute="4/5"),
        id="homepage_market_intraday_snapshot",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_homepage_integrity_after_close,
        CronTrigger(day_of_week="mon-fri", hour="15-17", minute="35,40,45,50,55"),
        id="homepage_after_close_integrity",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_homepage_margin_refresh,
        CronTrigger(day_of_week="mon-fri", hour="15-18", minute="*/20"),
        id="homepage_margin_sentiment_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    threading.Thread(
        target=_recover_homepage_market_cache_after_start,
        name="homepage-integrity-startup-recovery",
        daemon=True,
    ).start()
    return {"enabled": True, "jobs": ["homepage_market_intraday_snapshot", "homepage_after_close_integrity", "homepage_margin_sentiment_refresh"]}


def _recover_homepage_market_cache_after_start():
    """Catch up missed homepage snapshots after a backend restart.

    APScheduler coalesces missed cron executions, so a process that was down
    after close must explicitly rebuild confirmed history on its next start.
    Without this, a complete daily bar can exist while the homepage turnover
    curve remains stale until the next scheduled window.
    """
    # Reconcile both daily homepage contracts before any intraday append is
    # shown.  A restart must not leave a missed after-close job undiscovered.
    repair_emotion_cycle_latest_task()
    refresh_confirmed_market_sentiment_snapshot_task()
    refresh_market_sentiment_snapshot_task()
    try:
        from services.margin_sentiment import refresh_margin_sentiment

        refresh_margin_sentiment(days=30)
    except Exception as exc:
        logger.warning("homepage margin sentiment startup recovery failed: %s", exc)


def _emotion_auto_job():
    """
    盘中?分钟自动修复丢次（仅交易日 + 交易时段）?
    """
    task_name = "emotion_cycle_auto_5m"
    from utils.database import db
    from sqlalchemy import func
    from models.stock_models import KlineDaily

    session = next(db.get_session())
    try:
        now_dt = datetime.now()

        # 仅交易日
        if not _is_trading_day_today(session):
            task_manager.update_progress(task_name, {"message": f"非交易日跳过: {now_dt.strftime('%F %T')}"})
            return

        # 仅交易时段（A?9:30-11:30, 13:00-15:00?
        hm = now_dt.strftime("%H:%M")
        in_am = "09:30" <= hm <= "11:30"
        in_pm = "13:00" <= hm <= "15:00"
        if not (in_am or in_pm):
            task_manager.update_progress(task_name, {"message": f"非交易时段跳? {now_dt.strftime('%F %T')}"})
            return

        dates = _get_recent_complete_kline_dates(session, 1)
        latest_date = dates[0] if dates else None
        if not latest_date:
            task_manager.update_progress(task_name, {"message": "无日线数据，跳过"})
            return

        task_manager.tasks[task_name]["is_running"] = True
        task_manager.tasks[task_name]["started_at"] = datetime.now().isoformat()
        task_manager.update_progress(task_name, {"current": 1, "total": 1, "message": f"自动重算: {latest_date}"})

        r = _repair_emotion_for_dates([latest_date])
        task_manager.set_results(
            task_name,
            {
                "message": "自动修复完成",
                "stats": {"date": str(latest_date), "saved": r.get("saved", 0), "skipped": r.get("skipped", 0)},
                "ran_at": datetime.now().isoformat(),
            },
        )
    except Exception as e:
        logger.error(f"自动修复情绪周期失败: {e}")
        task_manager.set_error(task_name, str(e))
    finally:
        try:
            session.close()
        except Exception:
            pass


def _run_core_maintenance_bootstrap_refresh():
    refresh_plan = [
        ("update_trade_calendar", update_trade_calendar_task),
        ("update_stock_list", update_stock_list_task),
        ("update_index_list", update_index_list_task),
        ("sync_sectors", sync_sectors_task),
    ]

    for task_name, task_fn in refresh_plan:
        current = task_manager.get_task_status(task_name)
        if current.get("is_running"):
            logger.info("task message")
            continue
        if not task_manager.start_task(task_name, trigger_source="startup"):
            logger.info("task message")
            continue
        try:
            task_fn()
        except Exception as exc:
            logger.error(f"启动快刷执行 {task_name} 失败: {exc}")
            task_manager.set_error(task_name, str(exc))


def _kickoff_core_maintenance_bootstrap_refresh():
    global _core_maintenance_bootstrap_thread

    with _core_maintenance_bootstrap_lock:
        if _core_maintenance_bootstrap_thread is not None and _core_maintenance_bootstrap_thread.is_alive():
            return
        _core_maintenance_bootstrap_thread = threading.Thread(
            target=_run_core_maintenance_bootstrap_refresh,
            name="core-maintenance-bootstrap",
            daemon=True,
        )
        _core_maintenance_bootstrap_thread.start()


def _run_manual_core_data_sync():
    master_task_name = "core_data_manual_sync"
    refresh_plan = [
        ("update_trade_calendar", "同步交易日历", lambda: update_trade_calendar_task()),
        ("update_stock_list", "同步股票列表", lambda: update_stock_list_task()),
        ("update_index_list", "同步指数列表", lambda: update_index_list_task()),
        ("sync_sectors", "同步板块列表", lambda: sync_sectors_task()),
        ("update_stock_today_data", "同步股票/指数当日日K", lambda: update_stock_today_data_task(["1d"])),
        ("update_sector_intraday_stats", "刷新板块当日统计", lambda: update_sector_intraday_stats_task()),
    ]
    now_dt = datetime.now()
    if _is_intraday_auto_update_window(now_dt):
        refresh_plan.insert(
            5,
            (
                "update_market_today_minute_data",
                "同步盘中股票/指数分钟快照",
                lambda: update_market_today_minute_data_task(["5m", "15m", "30m", "60m"]),
            ),
        )
    else:
        refresh_plan = refresh_plan[:4] + [
            (
                "minute_kline_daily_repair_validate",
                "?日分钟K线补全验?",
                lambda: minute_kline_daily_repair_validate_task(task_already_started=True),
            ),
            (
                "official_daily_close_sync",
                "盘后正式日线写库",
                lambda: official_daily_close_sync_task(task_already_started=True),
            ),
        ]

    if not task_manager.start_task(master_task_name, trigger_source="manual"):
        return

    task_manager.update_progress(
        master_task_name,
        {"current": 0, "total": len(refresh_plan), "message": "弢始串行同步核心数?.."},
    )

    step_results: List[Dict[str, Any]] = []
    try:
        for index, (task_name, label, task_fn) in enumerate(refresh_plan, start=1):
            current = task_manager.get_task_status(task_name)
            if current.get("is_running"):
                raise RuntimeError(f"{label}正在运行，请稍后再试")
            if not task_manager.start_task(task_name, trigger_source="manual"):
                raise RuntimeError(f"{label}启动失败，请稍后再试")

            task_manager.update_progress(
                master_task_name,
                {"current": index - 1, "total": len(refresh_plan), "message": f"running: {label}"},
            )

            try:
                task_fn()
            except Exception as exc:
                logger.error(f"手动核心同步执行 {task_name} 失败: {exc}")
                task_manager.set_error(task_name, str(exc))

            task_status = task_manager.get_task_status(task_name)
            step_results.append(
                {
                    "task_name": task_name,
                    "label": label,
                    "success": not bool(task_status.get("error")),
                    "error": task_status.get("error"),
                    "last_success_at": task_status.get("last_success_at"),
                }
            )
            if task_status.get("error"):
                raise RuntimeError(f"{label}澶辫触: {task_status.get('error')}")

            task_manager.update_progress(
                master_task_name,
                {"current": index, "total": len(refresh_plan), "message": f"已完成：{label}"},
            )

        task_manager.set_results(
            master_task_name,
            {
                "message": "task message",
                "steps": step_results,
            },
        )
    except Exception as exc:
        task_manager.set_error(master_task_name, str(exc))
    finally:
        task_status = task_manager.get_task_status(master_task_name)
        if not task_status.get("error") and not task_status.get("results"):
            task_manager.set_results(
                master_task_name,
                "task message",
            )


def _run_manual_today_full_market_refresh():
    master_task_name = "today_full_market_refresh"
    now_dt = datetime.now()
    after_close = now_dt.time() >= dt_time(15, 5)
    refresh_plan = [
        ("update_trade_calendar", "同步交易日历", lambda: update_trade_calendar_task(), True),
        ("update_stock_list", "同步股票列表", lambda: update_stock_list_task(), True),
        ("update_index_list", "同步指数列表", lambda: update_index_list_task(), True),
        ("sync_sectors", "Sync sector list and constituents", lambda: sync_sectors_task(), True),
        ("update_stock_today_data", "Update current stock/index daily data", lambda: update_stock_today_data_task(["1d"], force=True), True),
        (
            "update_market_today_minute_data",
            "同步当天股票/指数分钟快照",
            lambda: update_market_today_minute_data_task(["5m", "15m", "30m", "60m"], force=True),
            True,
        ),
        (
            "sync_today_intraday_kline",
            "task message",
            lambda: sync_today_intraday_kline_task(task_already_started=True),
            True,
        ),
        ("update_sector_intraday_stats", "刷新当天板块统计", lambda: update_sector_intraday_stats_task(force=True), True),
    ]
    if after_close:
        refresh_plan.extend(
            [
                (
                    "official_daily_close_sync",
                    "task message",
                    lambda: official_daily_close_sync_task(task_already_started=True),
                    True,
                ),
                (
                    "minute_kline_daily_repair_validate",
                    "当天分钟K线补全自棢",
                    lambda: minute_kline_daily_repair_validate_task(task_already_started=True),
                    True,
                ),
                (
                    "repair_previous_daily_kline",
                    "次日巡检修复复核",
                    lambda: repair_previous_daily_kline_task(task_already_started=True),
                    False,
                ),
            ]
        )
    else:
        refresh_plan.append(
            (
                "minute_kline_daily_repair_validate",
                "盘中分钟K线可用自棢",
                lambda: minute_kline_daily_repair_validate_task(task_already_started=True, days=1),
                False,
            )
        )

    if not task_manager.start_task(master_task_name, trigger_source="manual"):
        return

    preflight = _ensure_trade_calendar_fresh_for_today(master_task_name)
    if not preflight.get("ok") or not preflight.get("trading_day"):
        return

    kickoff_message = "弢始一键更新当天全市场数据并自棢..."
    if not after_close:
        kickoff_message = "弢始一键更新当天全市场数据，盘后完整校验仅?15:05 后执?.."
    task_manager.update_progress(
        master_task_name,
        {"current": 0, "total": len(refresh_plan), "message": kickoff_message},
    )

    step_results: List[Dict[str, Any]] = []
    failed_steps: List[Dict[str, Any]] = []
    try:
        for index, (task_name, label, task_fn, critical) in enumerate(refresh_plan, start=1):
            current = task_manager.get_task_status(task_name)
            if current.get("is_running"):
                failure = {
                    "task_name": task_name,
                    "label": label,
                    "success": False,
                    "critical": bool(critical),
                    "error": f"{label}正在运行，请稍后再试",
                }
                step_results.append(failure)
                failed_steps.append(failure)
                if critical:
                    continue
                continue
            if not task_manager.start_task(task_name, trigger_source="manual"):
                failure = {
                    "task_name": task_name,
                    "label": label,
                    "success": False,
                    "critical": bool(critical),
                    "error": f"{label}启动失败，请稍后再试",
                }
                step_results.append(failure)
                failed_steps.append(failure)
                if critical:
                    continue
                continue

            task_manager.update_progress(
                master_task_name,
                {"current": index - 1, "total": len(refresh_plan), "message": f"running: {label}"},
            )

            try:
                task_fn()
            except Exception as exc:
                logger.error(f"当天全市场更新执?{task_name} 失败: {exc}")
                task_manager.set_error(task_name, str(exc))

            task_status = task_manager.get_task_status(task_name)
            step_result = {
                "task_name": task_name,
                "label": label,
                "success": not bool(task_status.get("error")),
                "critical": bool(critical),
                "error": task_status.get("error"),
                "last_success_at": task_status.get("last_success_at"),
                "message": (task_status.get("results") or {}).get("message") or task_status.get("progress", {}).get("message"),
                "validation_status": (task_status.get("results") or {}).get("validation_status"),
                "validation_reason": (task_status.get("results") or {}).get("validation_reason"),
            }
            step_results.append(step_result)
            if task_status.get("error"):
                failed_steps.append(step_result)

            task_manager.update_progress(
                master_task_name,
                {"current": index, "total": len(refresh_plan), "message": f"已完成：{label}"},
            )

        critical_failed_steps = [item for item in failed_steps if item.get("critical")]
        done_message = "当天扢有核心数据源更新并自棢完成"
        if not after_close:
            done_message = "core data update completed"
        if failed_steps:
            done_message = f"当天全市场更新完成但存在失败步骤: failed={len(failed_steps)}, critical_failed={len(critical_failed_steps)}"
        results = {
            "message": done_message,
            "after_close_validation": after_close,
            "total_steps": len(refresh_plan),
            "success_steps": len([item for item in step_results if item.get("success")]),
            "failed_steps": failed_steps,
            "critical_failed_steps": critical_failed_steps,
            "steps": step_results,
            "coverage": {
                "reference_data": ["trade_calendar", "stock_list", "index_list", "sectors", "sector_members"],
                "today_market_data": [
                    "stock_daily",
                    "index_daily",
                    "sector_intraday_stats",
                    "intraday_minute_snapshot",
                    "kline_minute_15",
                    "kline_minute_30",
                ],
                "strategy_sources": ["gen2_v4_event_dataset"],
                "after_close_checks": ["official_daily_close_sync", "minute_kline_daily_repair_validate"] if after_close else [],
            },
        }
        task_manager.set_results(master_task_name, results, mark_success=not critical_failed_steps)
        if critical_failed_steps:
            task_manager.set_error(
                master_task_name,
                "; ".join(f"{item.get('label')}: {item.get('error')}" for item in critical_failed_steps[:3]),
            )
    except Exception as exc:
        task_manager.set_error(master_task_name, str(exc))
    finally:
        task_status = task_manager.get_task_status(master_task_name)
        if not task_status.get("error") and not task_status.get("results"):
            fallback_message = "当天扢有核心数据源更新并自棢完成"
            if not after_close:
                fallback_message = "core data update completed"
            task_manager.set_results(
                master_task_name,
                {
                    "message": fallback_message,
                    "after_close_validation": after_close,
                    "steps": step_results,
                },
            )


def kickoff_manual_core_data_sync() -> bool:
    global _core_data_manual_sync_thread

    with _core_data_manual_sync_lock:
        if _core_data_manual_sync_thread is not None and _core_data_manual_sync_thread.is_alive():
            return False
        _core_data_manual_sync_thread = threading.Thread(
            target=_run_manual_core_data_sync,
            name="core-data-manual-sync",
            daemon=True,
        )
        _core_data_manual_sync_thread.start()
        return True


def kickoff_manual_today_full_market_refresh() -> bool:
    global _core_data_manual_sync_thread

    with _core_data_manual_sync_lock:
        if _core_data_manual_sync_thread is not None and _core_data_manual_sync_thread.is_alive():
            return False
        _core_data_manual_sync_thread = threading.Thread(
            target=_run_manual_today_full_market_refresh,
            name="today-full-market-refresh",
            daemon=True,
        )
        _core_data_manual_sync_thread.start()
        return True


def maybe_run_startup_reference_sync() -> bool:
    if not get_startup_reference_sync_enabled():
        logger.info("Startup reference data sync is disabled by configuration")
        return False
    _kickoff_core_maintenance_bootstrap_refresh()
    logger.info("Startup reference data sync triggered")
    return True


def start_core_data_maintenance_scheduler():
    enabled = str(os.environ.get("AISTOCK_CORE_DATA_MAINTENANCE_SCHEDULER_ENABLED", "1")).strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        logger.info("Core ingestion scheduler disabled; starting homepage integrity scheduler only")
        return start_homepage_integrity_scheduler()
    scheduler = _ensure_core_maintenance_scheduler()
    from apscheduler.triggers.cron import CronTrigger

    try:
        scheduler.remove_all_jobs()
    except Exception:
        pass

    scheduler.add_job(
        update_trade_calendar_task,
        CronTrigger(hour=8, minute=45),
        id=_core_maintenance_job_ids["trade_calendar"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar("update_stock_list", update_stock_list_task),
        CronTrigger(day_of_week="mon-fri", hour=18, minute=12),
        id=_core_maintenance_job_ids["stock_list_sync"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar("update_index_list", update_index_list_task),
        CronTrigger(day_of_week="mon-fri", hour=18, minute=14),
        id=_core_maintenance_job_ids["index_list_sync"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar("sync_sectors", sync_sectors_task),
        CronTrigger(day_of_week="mon-fri", hour=18, minute=16),
        id=_core_maintenance_job_ids["sector_list_sync"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar(
            "update_stock_today_data",
            lambda: update_stock_today_data_task(["1d"]),
        ),
        # Intraday 1d snapshots mainly serve UI freshness and same-day audit data.
        # G2/G3 live confirmation depends on minute snapshots / completed 30m bars,
        # so a 15-minute cadence is enough and reduces repeated TDX daily pulls.
        CronTrigger(day_of_week="mon-fri", hour="9-11,13-14", minute="*/15"),
        id=_core_maintenance_job_ids["stock_intraday"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar(
            "update_market_today_minute_data",
            lambda: update_market_today_minute_data_task(["5m", "15m", "30m", "60m"]),
        ),
        CronTrigger(day_of_week="mon-fri", hour="9-11,13-14", minute="3/5"),
        id=_core_maintenance_job_ids["market_intraday_minutes"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar(
            "sync_today_intraday_kline",
            lambda: sync_today_intraday_kline_task(task_already_started=False),
        ),
        # Strategy refresh depends on ClickHouse 15m/30m bars, not only quote snapshots.
        # Run shortly after 30-minute slots so G2/G3 can consume fresh same-day bars.
        CronTrigger(day_of_week="mon-fri", hour="10-11,13-15", minute="2,32"),
        id=_core_maintenance_job_ids["market_intraday_kline_refresh"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar(
            "update_sector_intraday_stats",
            update_sector_intraday_stats_task,
        ),
        CronTrigger(day_of_week="mon-fri", hour="9-11,13-14", minute="4/5"),
        id=_core_maintenance_job_ids["sector_intraday_stats_refresh"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar(
            "minute_kline_daily_repair_validate",
            minute_kline_daily_repair_validate_task,
        ),
        CronTrigger(day_of_week="mon-fri", hour=15, minute=45),
        id=_core_maintenance_job_ids["minute_kline_daily_repair_validate"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        refresh_market_sentiment_snapshot_task,
        # Run one minute after the QMT 5-minute source refresh.  This shifts
        # full-market aggregation out of the homepage request path.
        CronTrigger(day_of_week="mon-fri", hour="9-11,13-14", minute="4/5"),
        id=_core_maintenance_job_ids["market_sentiment_snapshot"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        refresh_confirmed_market_sentiment_snapshot_task,
        # Daily ingestion can still be writing at 15:40.  A rejected partial
        # batch is never published; the retry window self-heals once coverage
        # is complete instead of preserving a false low-turnover bar.
        CronTrigger(day_of_week="mon-fri", hour=15, minute="40,45,50,55"),
        id=_core_maintenance_job_ids["market_sentiment_after_close"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        refresh_confirmed_market_sentiment_snapshot_task,
        CronTrigger(day_of_week="mon-fri", hour=16, minute="0,5,10,15,20,25,30"),
        id="core_market_sentiment_after_close_retry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        refresh_confirmed_market_sentiment_snapshot_task,
        CronTrigger(day_of_week="mon-fri", hour=17, minute="0,5"),
        id="core_market_sentiment_after_close_final_retry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar(
            "official_daily_close_sync",
            official_daily_close_sync_task,
        ),
        CronTrigger(day_of_week="mon-fri", hour=15, minute=35),
        id=_core_maintenance_job_ids["official_daily"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_scheduled_repair_emotion_cycle_latest,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=45),
        id=_core_maintenance_job_ids["emotion_cycle_after_close"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar(
            "repair_previous_daily_kline",
            repair_previous_daily_kline_task,
        ),
        CronTrigger(day_of_week="tue-sat", hour=0, minute=30),
        id=_core_maintenance_job_ids["repair_daily"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        lambda: _run_core_data_update_after_trade_calendar(
            "daily_kline_coverage_maintenance",
            daily_kline_coverage_maintenance_task,
        ),
        # Start after the next-day single-date repair.  The worker audits the
        # current-year SH calendar and safely repairs at most 60 incomplete codes.
        CronTrigger(day_of_week="tue-sat", hour=1, minute=10),
        id=_core_maintenance_job_ids["daily_coverage"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_scheduled_repair_emotion_cycle_latest,
        CronTrigger(day_of_week="tue-sat", hour=0, minute=45),
        id=_core_maintenance_job_ids["emotion_cycle_overnight"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _set_core_maintenance_status(True, "core maintenance enabled")
    threading.Thread(
        target=_recover_homepage_market_cache_after_start,
        name="homepage-market-cache-recovery",
        daemon=True,
    ).start()
    results = task_manager.tasks["core_data_maintenance"].get("results") or {}
    results["jobs"] = _core_scheduler_jobs_status()
    task_manager.tasks["core_data_maintenance"]["results"] = results
    return results


def stop_core_data_maintenance_scheduler():
    scheduler = _ensure_core_maintenance_scheduler()
    for job_id in _core_maintenance_job_ids.values():
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
    _set_core_maintenance_status(False, "core maintenance disabled")
    results = task_manager.tasks["core_data_maintenance"].get("results") or {}
    results["jobs"] = _core_scheduler_jobs_status()
    task_manager.tasks["core_data_maintenance"]["results"] = results
    return results


def _empty_duration_profile() -> Dict[str, Any]:
    return {
        "recent_run_count": 0,
        "recent_avg_duration_sec": None,
        "recent_min_duration_sec": None,
        "recent_max_duration_sec": None,
    }


def _build_core_data_maintenance_payload(timeline_limit: int = 20, include_history: bool = True) -> Dict[str, Any]:
    results = task_manager.tasks["core_data_maintenance"].get("results") or {}
    jobs = _core_scheduler_jobs_status()
    results["jobs"] = jobs
    task_manager.tasks["core_data_maintenance"]["results"] = results
    extra_task_names = ["core_data_maintenance", "today_full_market_refresh"]
    tracked_task_names = list(_core_maintenance_task_map.values()) + extra_task_names
    if include_history:
        persisted_runs = _load_persisted_task_runs(tracked_task_names, limit=max(200, timeline_limit * 10))
    else:
        persisted_runs = {"by_task": {}, "timeline": []}
    persisted_by_task = persisted_runs.get("by_task") or {}

    status_task_names = dict(_core_maintenance_task_map)
    status_task_names["today_full_market_refresh"] = "today_full_market_refresh"
    task_status = {
        key: task_manager.get_task_status(task_name)
        for key, task_name in status_task_names.items()
    }
    task_status_normalized = {}
    for key, task in task_status.items():
        task_name = status_task_names[key]
        profile = _calc_duration_profile(task_name, recent_limit=10) if include_history else _empty_duration_profile()
        persisted = persisted_by_task.get(task_name) or {}
        persisted_latest = persisted.get("latest") or {}
        persisted_success = persisted.get("last_success") or {}
        persisted_failed = persisted.get("last_failed") or {}
        last_duration_sec = _safe_float(task.get("last_duration_sec"))
        if last_duration_sec is None:
            last_duration_sec = _safe_float(persisted_latest.get("duration_sec"))
        memory_status = _task_status_from_payload(task)
        persisted_status = persisted_latest.get("status")
        display_status = persisted_status if memory_status == "idle" and persisted_status in {"success", "failed"} else memory_status
        normalized_task = {
            "status": display_status,
            "is_running": bool(task.get("is_running")),
            "progress": task.get("progress") or {},
            "last_run_at": task.get("last_run_at") or persisted_latest.get("last_run_at"),
            "last_success_at": task.get("last_success_at") or persisted_success.get("last_success_at") or persisted_success.get("created_at"),
            "last_success_message": persisted_success.get("message") or "",
            "last_success_results_json": persisted_success.get("results_json") or "",
            "last_error": task.get("last_error")
            or (
                (persisted_failed.get("error") or persisted_failed.get("message"))
                if persisted_latest.get("status") == "failed"
                else None
            ),
            "last_error_at": task.get("last_error_at")
            or (
                (persisted_failed.get("last_error_at") or persisted_failed.get("created_at"))
                if persisted_latest.get("status") == "failed"
                else None
            ),
            "last_duration_sec": round(last_duration_sec, 3) if last_duration_sec is not None else None,
            "last_duration_ms": int(round(last_duration_sec * 1000)) if last_duration_sec is not None else None,
            "recent_run_count": profile["recent_run_count"],
            "recent_avg_duration_sec": profile["recent_avg_duration_sec"],
            "recent_min_duration_sec": profile["recent_min_duration_sec"],
            "recent_max_duration_sec": profile["recent_max_duration_sec"],
            "trigger_source": task.get("trigger_source"),
        }
        if (
            key == "official_daily"
            and str(normalized_task.get("status") or "").lower() in {"failed", "error"}
            and datetime.now().time() < dt_time(15, 5)
        ):
            normalized_task["status"] = "pending"
            normalized_task["suppressed_last_error"] = normalized_task.get("last_error")
            normalized_task["suppressed_last_error_at"] = normalized_task.get("last_error_at")
            normalized_task["last_error"] = None
            normalized_task["last_error_at"] = None
            progress = dict(normalized_task.get("progress") or {})
            progress["message"] = "after-close validation is not due yet; waiting for 15:05"
            normalized_task["progress"] = progress
        elif _is_qmt_transition_failure(key, normalized_task):
            normalized_task["status"] = "pending"
            normalized_task["suppressed_last_error"] = normalized_task.get("last_error")
            normalized_task["suppressed_last_error_at"] = normalized_task.get("last_error_at")
            normalized_task["last_error"] = None
            normalized_task["last_error_at"] = None
            progress = dict(normalized_task.get("progress") or {})
            progress["message"] = "QMT/xtquant 已切到 Windows 主机统一采集器；旧链路失败不再计入自动维护成功率"
            normalized_task["progress"] = progress
            results_payload = dict(normalized_task.get("results") or {})
            results_payload.setdefault("validation_status", "delegated_to_host_collector")
            results_payload.setdefault("validation_reason", "legacy_backend_qmt_transition_failure")
            normalized_task["results"] = results_payload
        task_status_normalized[key] = normalized_task
    enabled = bool(results.get("enabled"))
    running_task = next((key for key, task in task_status.items() if task.get("is_running")), None)

    today = datetime.now().strftime("%Y-%m-%d")
    today_total = 0
    today_success = 0
    latest_failed_task = None
    latest_failed_at = None

    for key, task in task_status.items():
        normalized = task_status_normalized.get(key) or {}
        last_run_at = _to_datetime(normalized.get("last_run_at") or task.get("last_run_at"))
        current_status = str(normalized.get("status") or "").strip().lower()
        excluded_from_success_rate = bool(normalized.get("suppressed_last_error")) and current_status == "pending"
        if last_run_at and last_run_at.strftime("%Y-%m-%d") == today and not excluded_from_success_rate:
            today_total += 1
            if normalized.get("last_success_at") and not normalized.get("last_error"):
                today_success += 1
        last_error_at = _to_datetime(normalized.get("last_error_at") or task.get("last_error_at"))
        last_success_at = _to_datetime(normalized.get("last_success_at") or task.get("last_success_at"))
        if last_error_at and last_success_at and last_success_at >= last_error_at:
            last_error_at = None
        if current_status not in {"failed", "error"}:
            last_error_at = None
        if last_error_at and (latest_failed_at is None or last_error_at > latest_failed_at):
            latest_failed_at = last_error_at
            latest_failed_task = key

    today_success_rate = round((today_success / today_total) * 100, 2) if today_total else None
    freshness = {
        key: {
            "last_success_at": task_status[key].get("last_success_at"),
            "last_error": task_status[key].get("last_error"),
            "status": _task_status_from_payload(task_status[key]),
        }
        for key in ("stock_intraday", "trade_calendar")
    }

    if not enabled:
        overall_status = "paused"
    elif running_task:
        overall_status = "running"
    elif latest_failed_task:
        overall_status = "failed"
    else:
        overall_status = "success"

    timeline = task_manager.latest_timeline(limit=timeline_limit, task_names=tracked_task_names)
    if include_history and len(timeline) < timeline_limit:
        seen = {(item.get("task_name"), item.get("status"), item.get("created_at")) for item in timeline}
        for item in persisted_runs.get("timeline") or []:
            key = (item.get("task_name"), item.get("status"), item.get("created_at"))
            if key in seen:
                continue
            timeline.append(item)
            seen.add(key)
            if len(timeline) >= timeline_limit:
                break
    task_status_by_name = {
        status_task_names[key]: task
        for key, task in task_status.items()
    }
    timeline = _dedupe_timeline_events(timeline)
    timeline = _filter_stale_running_timeline(timeline, task_status_by_name)[:timeline_limit]

    return {
        "enabled": enabled,
        "is_enabled": enabled,
        "startup_reference_sync_enabled": get_startup_reference_sync_enabled(),
        "startup_reference_sync_default_enabled": _startup_reference_sync_default_enabled(),
        "overall_status": overall_status,
        "running_task": running_task,
        "jobs": jobs,
        "task_status": task_status,
        "task_status_normalized": task_status_normalized,
        "today_success_rate": today_success_rate,
        "latest_failed_task": latest_failed_task,
        "latest_failed_at": latest_failed_at.isoformat() if latest_failed_at else None,
        "data_freshness": freshness,
        "timeline": timeline,
    }


@router.get("/startup-reference-sync-setting")
async def get_startup_reference_sync_setting():
    return {
        "success": True,
        "data": {
            "enabled": get_startup_reference_sync_enabled(),
            "default_enabled": _startup_reference_sync_default_enabled(),
            "takes_effect_on_restart": True,
        },
    }


@router.get("/qmt-after-close-execution")
async def get_qmt_after_close_execution():
    data = _latest_qmt_after_close_execution()
    data["overnight_repair"] = _latest_qmt_overnight_repair_execution()
    return {"success": True, "data": data}


@router.post("/startup-reference-sync-setting")
async def update_startup_reference_sync_setting(request: StartupReferenceSyncToggleRequest):
    try:
        enabled = set_startup_reference_sync_enabled(request.enabled)
        return {
            "success": True,
            "message": "启动初始化基硢数据弢关已更新，重启后生效",
            "data": {
                "enabled": enabled,
                "default_enabled": _startup_reference_sync_default_enabled(),
                "takes_effect_on_restart": True,
            },
        }
    except Exception as exc:
        logger.error(f"更新启动初始化基硢数据弢关失? {exc}")
        raise HTTPException(status_code=500, detail="task is already running")


@router.post("/core-data-maintenance/start")
async def start_core_data_maintenance():
    try:
        results = start_core_data_maintenance_scheduler()
        return {"success": True, "message": "ok", "data": results}
    except Exception as exc:
        logger.error(f"启动核心数据自动维护失败: {exc}")
        task_manager.set_error("core_data_maintenance", str(exc))
        raise HTTPException(status_code=500, detail="启动核心数据自动维护失败")


@router.post("/core-data-maintenance/stop")
async def stop_core_data_maintenance():
    try:
        results = stop_core_data_maintenance_scheduler()
        return {"success": True, "message": "ok", "data": results}
    except Exception as exc:
        logger.error(f"停止核心数据自动维护失败: {exc}")
        raise HTTPException(status_code=500, detail="停止核心数据自动维护失败")


@router.get("/core-data-maintenance/status")
async def get_core_data_maintenance_status():
    return {"success": True, "data": _build_core_data_maintenance_payload(timeline_limit=20, include_history=False)}


@router.get("/data-source-counts")
async def get_data_source_counts(trade_date: Optional[str] = Query(default=None)):
    return {"success": True, "data": _build_data_source_counts_payload(trade_date)}


@router.post("/data-source-counts/repair-date")
async def repair_data_source_counts_date(trade_date: str = Query(...)):
    normalized_date = _parse_data_source_count_date(trade_date)
    task_name = "data_source_date_repair"
    current = task_manager.get_task_status(task_name)
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="task is already running")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")
    task_manager.update_progress(task_name, {"message": f"任务已入队，准备修复 {normalized_date} 数据?.."})

    thread = threading.Thread(target=data_source_date_repair_task, args=(normalized_date, True), daemon=True)
    thread.start()
    return {
        "success": True,
        "message": f"{normalized_date} 数据源修复任务已启动",
        "task_name": task_name,
        "task": task_manager.get_task_status(task_name),
    }


@router.get("/core-data-maintenance/timeline")
async def get_core_data_maintenance_timeline(limit: int = 30):
    safe_limit = max(1, min(limit, 100))
    data = _build_core_data_maintenance_payload(timeline_limit=safe_limit)
    return {
        "success": True,
        "data": {
            "timeline": data.get("timeline", []),
            "overall_status": data.get("overall_status"),
            "running_task": data.get("running_task"),
        },
    }


@router.post("/core-data-maintenance/run-official-daily-close")
async def run_official_daily_close_now(background_tasks: BackgroundTasks):
    """Manually trigger official daily close sync immediately."""
    task_name = "official_daily_close_sync"
    current = task_manager.get_task_status(task_name)
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="task is already running")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")
    task_manager.update_progress(task_name, {"message": "Task queued and ready to execute..."})

    thread = threading.Thread(target=official_daily_close_sync_task, args=(True,), daemon=True)
    thread.start()
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name),
    }


@router.post("/core-data-maintenance/run-task/{task_key}")
async def run_core_maintenance_task_now(
    task_key: str,
    force: bool = Query(default=False, description="Force intraday tasks even outside trading window"),
    days: int = Query(default=30, ge=1, le=365, description="Repair lookback trading days"),
    periods: Optional[List[str]] = Query(default=None, description="Minute periods to repair"),
):
    """Manually trigger a single core maintenance task immediately."""
    task_configs = {
        "trade_calendar": {
            "task_name": "update_trade_calendar",
            "runner": lambda force=False: threading.Thread(target=update_trade_calendar_task, daemon=True).start(),
            "message": "task message",
        },
        "stock_list_sync": {
            "task_name": "update_stock_list",
            "runner": lambda force=False: threading.Thread(target=update_stock_list_task, daemon=True).start(),
            "message": "task message",
        },
        "index_list_sync": {
            "task_name": "update_index_list",
            "runner": lambda force=False: threading.Thread(target=update_index_list_task, daemon=True).start(),
            "message": "task message",
        },
        "sector_list_sync": {
            "task_name": "sync_sectors",
            "runner": lambda force=False: threading.Thread(target=sync_sectors_task, daemon=True).start(),
            "message": "task message",
        },
        "stock_intraday": {
            "task_name": "update_stock_today_data",
            "runner": lambda force=False: threading.Thread(
                target=update_stock_today_data_task, args=(["1d"], force), daemon=True
            ).start(),
            "message": "task message",
        },
        "market_intraday_minutes": {
            "task_name": "update_market_today_minute_data",
            "runner": lambda force=False: threading.Thread(
                target=update_market_today_minute_data_task, args=(["5m", "15m", "30m", "60m"], force), daemon=True
            ).start(),
            "message": "盘中股票/指数分钟级快照任务已手动启动",
        },
        "market_intraday_kline_refresh": {
            "task_name": "sync_today_intraday_kline",
            "runner": lambda force=False: threading.Thread(
                target=sync_today_intraday_kline_task, kwargs={"task_already_started": True}, daemon=True
            ).start(),
            "message": "当天全市?5m/30m分钟K线落库任务已手动启动",
        },
        "minute_kline_daily_repair_validate": {
            "task_name": "minute_kline_daily_repair_validate",
            "runner": lambda force=False: threading.Thread(
                target=minute_kline_daily_repair_validate_task, args=(True,), daemon=True
            ).start(),
        "message": "朢?个交易日分钟K线补全验证任务已手动启动",
        },
        "market_minute_history_repair": {
            "task_name": "market_minute_history_repair",
            "runner": lambda force=False: threading.Thread(
                target=market_minute_history_repair_task, args=(True, days, periods), daemon=True
            ).start(),
            "message": "股票/指数历史分钟级修复任务已手动启动",
        },
        "sector_intraday_stats_refresh": {
            "task_name": "update_sector_intraday_stats",
            "runner": lambda force=False: threading.Thread(
                target=update_sector_intraday_stats_task, kwargs={"force": force}, daemon=True
            ).start(),
            "message": "task message",
        },
        "official_daily": {
            "task_name": "official_daily_close_sync",
            "runner": lambda force=False: threading.Thread(
                target=official_daily_close_sync_task, args=(True,), daemon=True
            ).start(),
            "message": "task message",
        },
        "repair_daily": {
            "task_name": "repair_previous_daily_kline",
            "runner": lambda force=False: threading.Thread(
                target=repair_previous_daily_kline_task, args=(True,), daemon=True
            ).start(),
            "message": "task message",
        },
        "daily_coverage": {
            "task_name": "daily_kline_coverage_maintenance",
            "runner": lambda force=False: threading.Thread(
                target=daily_kline_coverage_maintenance_task, args=(True,), daemon=True
            ).start(),
            "message": "daily K-line calendar coverage audit and bounded repair started",
        },
    }

    force_tasks = {"stock_intraday", "market_intraday_minutes", "market_intraday_kline_refresh", "sector_intraday_stats_refresh"}
    force_for_task = bool(force) or task_key in force_tasks

    config = task_configs.get(task_key)
    if not config:
        raise HTTPException(status_code=404, detail="未找到对应的维护任务")

    task_name = config["task_name"]
    current = task_manager.get_task_status(task_name)
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="task is already running")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")
    task_manager.update_progress(task_name, {"message": "Task queued and ready to execute..."})

    config["runner"](force=force_for_task)
    return {
        "success": True,
        "message": config["message"],
        "task_name": task_name,
        "task": task_manager.get_task_status(task_name),
    }


@router.post("/tdx-gateway/initialize")
async def initialize_tdx_gateway():
    return {
        "success": False,
        "message": "TDX Gateway is a legacy diagnostics path; QMT/xtquant is the active market-data source.",
        "data": {
            "deprecated": True,
            "replacement": "QMT/xtquant data source maintenance",
            "active_source": "qmt_xtquant",
        },
    }


@router.get("/tdx-gateway/diagnostics")
async def get_tdx_gateway_diagnostics(run_probe: bool = True):
    return {
        "success": True,
        "message": "TDX Gateway diagnostics are legacy and disabled; use QMT/xtquant data-source diagnostics.",
        "data": {
            "deprecated": True,
            "requested_run_probe": run_probe,
            "active_source": "qmt_xtquant",
            "replacement": "core-data-maintenance and QMT minute gap repair tasks",
        },
    }


@router.post("/tdx-gateway/probe")
async def probe_tdx_gateway():
    return {
        "success": False,
        "message": "TDX Gateway probe is legacy and disabled; QMT/xtquant is the active market-data source.",
        "data": {
            "deprecated": True,
            "active_source": "qmt_xtquant",
        },
    }


@router.post("/tdx-gateway/recover")
async def recover_tdx_gateway(allow_restart: bool = True):
    return {
        "success": False,
        "message": "TDX Gateway recovery is legacy and disabled; use QMT/xtquant repair paths.",
        "data": {
            "deprecated": True,
            "allow_restart_requested": allow_restart,
            "active_source": "qmt_xtquant",
        },
    }


@router.post("/tdx-gateway/restart")
async def restart_tdx_gateway():
    return {
        "success": False,
        "message": "TDX Gateway restart is legacy and disabled; QMT/xtquant is the active market-data source.",
        "data": {
            "deprecated": True,
            "active_source": "qmt_xtquant",
        },
    }


@router.post("/core-data-maintenance/sync-core-assets")
async def sync_core_assets_now():
    """Manually run the core asset sync pipeline in sequence."""
    current = task_manager.get_task_status("core_data_manual_sync")
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="core data sync task is already running")

    if not kickoff_manual_core_data_sync():
        raise HTTPException(status_code=400, detail="core data sync task is already running")

    return {
        "success": True,
        "message": "核心数据同步任务已启动，将按顺序同步股票、指数板块的列表和当日日K",
        "task_name": "core_data_manual_sync",
        "task": task_manager.get_task_status("core_data_manual_sync"),
    }


@router.post("/core-data-maintenance/refresh-today-full-market")
async def refresh_today_full_market_now():
    """Refresh today's stock/index/sector data and run integrity checks when available."""
    current = task_manager.get_task_status("today_full_market_refresh")
    if current and current.get("is_running"):
        raise HTTPException(status_code=400, detail="today full market refresh task is already running")

    if not kickoff_manual_today_full_market_refresh():
        raise HTTPException(status_code=400, detail="today full market refresh task is already running")

    return {
        "success": True,
        "message": "当天股票、指数板块数据一键更新任务已启动，将按顺序刷新并在盘后执行完整自棢",
        "task_name": "today_full_market_refresh",
        "task": task_manager.get_task_status("today_full_market_refresh"),
    }


@router.post("/sync-sectors")
async def trigger_sync_sectors(background_tasks: BackgroundTasks):
    """
    触发同步丢二三行业板块任务
    
    Returns:
        任务状?
    """
    task_name = "sync_sectors"
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=sync_sectors_task, daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/sync-sector-history")
async def trigger_sync_sector_history(background_tasks: BackgroundTasks, days: int = 30):
    """
    触发同步板块历史涨跌数据任务
    
    同步扢有板块的历史涨跌幅成交量、成交额，并计算板块当天上涨家数、下跌家数?
    平盘家数、涨停家数跌停家数等统计信息
    
    Args:
        days: 同步多少天的历史数据，默?0?
    
    Returns:
        任务状?
    """
    if os.getenv('AISTOCK_SECTOR_DAILY_OWNER', 'host') == 'host':
        raise HTTPException(status_code=409, detail='板块日线已由任务中心统一维护，旧同步入口已停用')
    task_name = "sync_sector_history"
    
    logger.info(f"接收到同步板块历史数据请求，天数: {days}")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=sync_sector_history_task, args=(days,), daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": f"板块历史涨跌数据同步任务已启动（同步{days}天数据）",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/repair-history-klines")
async def trigger_repair_history_klines(background_tasks: BackgroundTasks, request: RepairKlinesRequest):
    """
    触发修复历史K线数据任?
    
    Args:
        request: 请求体，包含periods字段（要同步的K线级别列表，?["1min", "5min", "daily"]?
                和type字段（要同步的类型，?"index" ?"stock"?
    
    Returns:
        任务状?
    """
    task_name = "repair_history_klines"
    
    # 处理type参数，支持字符串或数组形?
    type_value = request.type
    if isinstance(type_value, list) and len(type_value) > 0:
        type_value = type_value[0]
    
    logger.info(f"接收到K线同步请求，周期参数: {request.periods}，类? {type_value}")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=repair_history_klines_task, args=(request.periods, type_value), daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "历史K线数据同步任务已启动",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/repair-daily-klines")
async def trigger_repair_daily_klines(background_tasks: BackgroundTasks):
    """
    触发修复日K线数据任?
    
    修复扢有股票的日K线涨跌幅、振幅涨跌额等数?
    
    Returns:
        任务状?
    """
    task_name = "repair_daily_klines"
    
    logger.info("task message")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=repair_daily_klines_task, daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "日K线数据修复任务已启动",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/repair-all-history-klines")
async def trigger_repair_all_history_klines(background_tasks: BackgroundTasks):
    """
    触发修复全量历史K线数据任?
    
    结合修复日K线数据和同步全量历史K线，修复扢有股票的历史K线数?
    
    Returns:
        任务状?
    """
    task_name = "repair_all_history_klines"
    
    logger.info("task message")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=repair_all_history_klines_task, daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "修复全量历史K线数据任务已启动",
        "task": task_manager.get_task_status(task_name)
    }

@router.post("/update-trade-calendar")
async def trigger_update_trade_calendar(background_tasks: BackgroundTasks):
    """
    触发更新股市日历任务
    
    从达信获取历史交易日期并更新到数据库
    
    Returns:
        任务状?
    """
    task_name = "update_trade_calendar"
    
    logger.info("task message")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=update_trade_calendar_task, daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/update-stock-list")
async def trigger_update_stock_list(background_tasks: BackgroundTasks):
    task_name = "update_stock_list"

    logger.info("task message")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")

    thread = threading.Thread(target=update_stock_list_task, daemon=True)
    thread.start()

    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/update-sector-intraday-stats")
async def trigger_update_sector_intraday_stats(
    background_tasks: BackgroundTasks,
    force: bool = Query(default=False, description="Force intraday stats refresh even outside trading window"),
):
    task_name = "update_sector_intraday_stats"

    logger.info("task message")

    if not task_manager.start_task(task_name, trigger_source="manual"):
        raise HTTPException(status_code=400, detail="task is already running")

    thread = threading.Thread(target=update_sector_intraday_stats_task, kwargs={"force": force}, daemon=True)
    thread.start()

    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.get("/trade-calendar/status")
async def get_trade_calendar_status():
    """
    获取交易日历落库状（只读）?
    """
    try:
        from utils.database import db

        with db.engine.connect() as conn:
            try:
                row = conn.execute(
                    text(
                        """
                        SELECT
                            COUNT(1) AS cnt,
                            MIN(trade_date) AS min_date,
                            MAX(trade_date) AS max_date,
                            MAX(updated_at) AS last_updated_at
                        FROM trade_calendar
                        WHERE market = 'SH' AND is_trading = 1
                        """
                    )
                ).fetchone()
            except Exception as exc:
                if "updated_at" not in str(exc):
                    raise
                row = conn.execute(
                    text(
                        """
                        SELECT
                            COUNT(1) AS cnt,
                            MIN(trade_date) AS min_date,
                            MAX(trade_date) AS max_date,
                            NULL AS last_updated_at
                        FROM trade_calendar
                        WHERE market = 'SH' AND is_trading = 1
                        """
                    )
                ).fetchone()

        cnt = int(row[0] or 0) if row else 0
        min_date = row[1].isoformat() if row and row[1] else None
        max_date = row[2].isoformat() if row and row[2] else None
        last_updated_at = row[3].isoformat() if row and row[3] else None

        return {
            "success": True,
            "data": {
                "count": cnt,
                "min_trade_date": min_date,
                "max_trade_date": max_date,
                "last_updated_at": last_updated_at,
                "market": "SH",
            },
        }
    except Exception as e:
        logger.error(f"获取交易日历状失? {e}")
        raise HTTPException(status_code=500, detail="task is already running")


@router.post("/update-today-data")
async def trigger_update_today_data(background_tasks: BackgroundTasks, request: RepairKlinesRequest):
    """
    触发更新当天朢新数据任务（只更新指数）
    
    更新扢有指数的当天朢新数据，包括分钟级别K?
    
    Args:
        request: 请求体，包含periods字段（要更新的K线级别列表，?["1m", "5m", "1d"]?
    
    Returns:
        任务状?
    """
    task_name = "update_today_data"
    
    logger.info(f"接收到当天指数数据更新请求，周期参数: {request.periods}")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=update_today_data_task, args=(request.periods, request.force), daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/update-stock-today-data")
async def trigger_update_stock_today_data(background_tasks: BackgroundTasks, request: RepairKlinesRequest):
    """
    触发更新当天朢新股票数据任?
    
    更新扢有个股的当天朢新数据，包括扢有周期的K?
    
    Args:
        request: 请求体，包含periods字段（要更新的K线级别列表，?["1m", "5m", "1d"]?
    
    Returns:
        任务状?
    """
    task_name = "update_stock_today_data"
    
    logger.info(f"接收到当天股票数据更新请求，周期参数: {request.periods}")
    
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    
    # 在后台线程中执行任务
    thread = threading.Thread(target=update_stock_today_data_task, args=(request.periods, request.force), daemon=True)
    thread.start()
    
    return {
        "success": True,
        "message": "task message",
        "task": task_manager.get_task_status(task_name)
    }


@router.post("/repair-emotion-cycle-30d")
async def trigger_repair_emotion_cycle_30d(background_tasks: BackgroundTasks, days: int = 30):
    """
    丢键修复近 N 个交易日情绪周期数据（默?0）?
    """
    task_name = "repair_emotion_cycle_30d"
    logger.info(f"接收到修复近{days}个交易日情绪周期请求")
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    thread = threading.Thread(target=repair_emotion_cycle_30d_task, args=(days,), daemon=True)
    thread.start()
    return {"success": True, "message": "情绪周期30天修复任务已启动", "task": task_manager.get_task_status(task_name)}


@router.post("/repair-emotion-cycle-latest")
async def trigger_repair_emotion_cycle_latest(background_tasks: BackgroundTasks):
    """
    手动：根据最新日线数据更新情绪指标（朢新交易日）?
    """
    task_name = "repair_emotion_cycle_latest"
    logger.info("接收到重算最新交易日情绪周期请求")
    if not task_manager.start_task(task_name):
        raise HTTPException(status_code=400, detail="task is already running")
    thread = threading.Thread(target=repair_emotion_cycle_latest_task, daemon=True)
    thread.start()
    return {"success": True, "message": "朢新情绪周期更新任务已启动", "task": task_manager.get_task_status(task_name)}


@router.post("/emotion-cycle-auto-5m/start")
async def start_emotion_cycle_auto_5m():
    """
    交易日盘中每5分钟自动修复丢次情绪周期（基于朢新日线数据）?
    """
    task_name = "emotion_cycle_auto_5m"
    sched = _ensure_emotion_scheduler()
    try:
        from apscheduler.triggers.cron import CronTrigger

        # Remove an existing task before adding it again to prevent duplicates.
        try:
            sched.remove_job(_emotion_job_id)
        except Exception:
            pass

        # ?分钟触发丢次，实际 job 内会判断是否交易?交易时段
        sched.add_job(
            _emotion_auto_job,
            CronTrigger(minute="*/5"),
            id=_emotion_job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

        task_manager.tasks[task_name]["is_running"] = True
        task_manager.tasks[task_name]["started_at"] = datetime.now().isoformat()
        task_manager.update_progress(task_name, {"message": "已开启：交易日盘中每5分钟自动修复"})
        return {"success": True, "message": "ok", "task": task_manager.get_task_status(task_name)}
    except Exception as e:
        logger.error(f"弢启自动修复失? {e}")
        task_manager.set_error(task_name, str(e))
        raise HTTPException(status_code=500, detail="task is already running")


@router.post("/emotion-cycle-auto-5m/stop")
async def stop_emotion_cycle_auto_5m():
    """
    关闭情绪周期自动修复?
    """
    task_name = "emotion_cycle_auto_5m"
    try:
        sched = _ensure_emotion_scheduler()
        try:
            sched.remove_job(_emotion_job_id)
        except Exception:
            pass
        task_manager.tasks[task_name]["is_running"] = False
        task_manager.update_progress(task_name, {"message": "task progress"})
        return {"success": True, "message": "ok", "task": task_manager.get_task_status(task_name)}
    except Exception as e:
        logger.error(f"关闭自动修复失败: {e}")
        task_manager.set_error(task_name, str(e))
        raise HTTPException(status_code=500, detail="关闭自动修复失败")


@router.get("/task-status/{task_name}")
async def get_task_status(task_name: str):
    """
    获取指定任务的状?
    
    Args:
        task_name: 任务名称 (sync_sectors ?repair_history_klines)
    
    Returns:
        任务状?
    """
    if task_name not in task_manager.tasks:
        raise HTTPException(status_code=404, detail="task is already running")
    
    return {
        "success": True,
        "task": task_manager.get_task_status(task_name)
    }


@router.get("/all-tasks-status")
async def get_all_tasks_status():
    """
    获取扢有系统任务的状?
    
    Returns:
        扢有任务状?
    """
    return {
        "success": True,
        "tasks": task_manager.tasks
    }


@router.get("/ingestion-owners")
def ingestion_owners():
    owner = os.getenv("AISTOCK_SECTOR_DAILY_OWNER", "host")
    state = task_manager.get_task_status("sync_sector_history") or {}
    return {
        "version": os.getenv("AISTOCK_RELEASE_VERSION", "unknown"),
        "legacy_sector_history_enabled": owner != "host",
        "legacy_sector_history_running": bool(state.get("is_running")),
        "sector_daily_owner": owner,
    }

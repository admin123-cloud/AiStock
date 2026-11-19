"""系统只读状态与轻量任务管理 API（精简版，替代原 8,400 行旧手动同步与 QMT 僵尸路由）。"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter

from utils.common.logger import get_logger

router = APIRouter(prefix="/system", tags=["system"])
logger = get_logger("system_config")
BJ_TZ = timezone(timedelta(hours=8))


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _persisted_datetime_iso(value: Any) -> Optional[str]:
    """R27b：DateTime('Asia/Shanghai') 读回 aware 北京墙钟直接透出，naive UTC 墙钟 +8 还原。"""
    parsed = _to_datetime(value)
    if not parsed:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(BJ_TZ).replace(tzinfo=None).isoformat()
    return (parsed + timedelta(hours=8)).isoformat()


class SystemTaskManager:
    """轻量进程内任务状态管理器（兼容 api/stocks.py 选股进度追踪）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}

    def start_task(self, name: str, message: str = "任务已启动", total: int = 0) -> dict[str, Any]:
        now = datetime.now(BJ_TZ).isoformat(timespec="seconds")
        with self._lock:
            state = {
                "task_name": name,
                "status": "running",
                "message": message,
                "progress": 0,
                "total": total,
                "started_at": now,
                "updated_at": now,
                "finished_at": None,
                "error": None,
                "result": None,
            }
            self._tasks[name] = state
            return dict(state)

    def update_task(self, name: str, **kwargs: Any) -> dict[str, Any]:
        now = datetime.now(BJ_TZ).isoformat(timespec="seconds")
        with self._lock:
            state = self._tasks.setdefault(name, {"task_name": name, "status": "running"})
            state.update(kwargs)
            state["updated_at"] = now
            return dict(state)

    def finish_task(self, name: str, result: Any = None, message: str = "已完成") -> dict[str, Any]:
        return self.update_task(
            name,
            status="completed",
            message=message,
            result=result,
            finished_at=datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        )

    def fail_task(self, name: str, error: str) -> dict[str, Any]:
        return self.update_task(
            name,
            status="failed",
            message=error,
            error=error,
            finished_at=datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        )

    def get_task(self, name: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._tasks.get(name) or {"task_name": name, "status": "idle"})

    def get_all_tasks(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._tasks.items()}


task_manager = SystemTaskManager()


@router.get("/trade-calendar/status")
def get_trade_calendar_status() -> dict[str, Any]:
    """查询 ClickHouse 交易日历覆盖范围与最近交易日（纯只读）。"""
    try:
        from utils.db.database import DatabaseManager

        client = DatabaseManager().get_clickhouse_client()
        res = client.query(
            "SELECT count(), min(trade_date), max(trade_date), "
            "maxIf(trade_date, is_open = 1 AND trade_date <= today()) "
            "FROM stock.trade_calendar"
        )
        row = res.result_rows[0] if res.result_rows else (0, None, None, None)
        return {
            "status": "ok",
            "total_days": int(row[0] or 0),
            "min_date": str(row[1]) if row[1] else None,
            "max_date": str(row[2]) if row[2] else None,
            "latest_open_trade_date": str(row[3]) if row[3] else None,
        }
    except Exception as exc:
        logger.warning("trade_calendar status query failed: %s", exc)
        return {"status": "unavailable", "error": str(exc)}


@router.get("/all-tasks-status")
def get_all_tasks_status() -> dict[str, Any]:
    """返回进程内轻量任务状态摘要（纯只读）。"""
    return {
        "status": "ok",
        "mode": "readface_preview",
        "tasks": task_manager.get_all_tasks(),
    }


@router.get("/task-status/{task_name}")
def get_single_task_status(task_name: str) -> dict[str, Any]:
    return {"status": "ok", "task": task_manager.get_task(task_name)}


@router.get("/core-data-maintenance/status")
def get_core_data_maintenance_status() -> dict[str, Any]:
    """返回盘后自动数据管道架构说明（已由 aistock-pipeline-runner 容器接管）。"""
    return {
        "status": "ok",
        "running": True,
        "owner": "aistock-pipeline-runner",
        "mode": "container_scheduler",
        "note": "盘中/盘后数据采集与治理已统一由 aistock-pipeline-runner 容器编排，Web 端保持只读观察。",
    }


@router.get("/startup-reference-sync-setting")
def get_startup_reference_sync_setting() -> dict[str, Any]:
    return {"enable_reference_data_sync_on_launch": False, "read_only": True}

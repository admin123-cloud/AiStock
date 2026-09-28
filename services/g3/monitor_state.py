"""G3 monitor defaults, normalization and bounded persistence payloads."""
from typing import Any
from services.operations.result_compaction import _compact_monitor_last_result


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


def normalize_monitor_state(state: dict[str, Any]) -> dict[str, Any]:
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


def normalize_exit_monitor_state(state: dict[str, Any]) -> dict[str, Any]:
    base = _default_exit_monitor_state()
    if isinstance(state, dict):
        base.update(state)
    base["enabled"] = bool(base.get("enabled"))
    base["interval_seconds"] = max(120, int(base.get("interval_seconds") or 120))
    base["trading_hours_only"] = bool(base.get("trading_hours_only", True))
    base["paper_exit_enabled"] = bool(base.get("paper_exit_enabled", True))
    base["update_ledger_enabled"] = bool(base.get("update_ledger_enabled", True))
    return base


def normalize_daily_trend_exit_monitor_state(state: dict[str, Any]) -> dict[str, Any]:
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


def normalize_observation_scheduler_state(state: dict[str, Any]) -> dict[str, Any]:
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


def normalize_broker_sync_state(state: dict[str, Any]) -> dict[str, Any]:
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


def save_monitor_state(path, state, writer, *, compact=False):
    payload = dict(state) if compact else state
    if compact and "last_result" in payload:
        payload["last_result"] = _compact_monitor_last_result(payload.get("last_result"))
    writer(path,payload)

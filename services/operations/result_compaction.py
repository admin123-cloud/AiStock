"""Domain logic extracted without changing existing API behavior."""
from typing import Any


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


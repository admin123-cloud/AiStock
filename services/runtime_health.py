"""Runtime health snapshot primitives for the AiStock data and strategy chain.

The module is deliberately read-only.  It turns the files produced by existing
collectors into one small, versioned status document; repair workers can later
consume the same document without the API process having to run repairs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class ArtifactRule:
    """A required runtime artifact and the contract used to judge it."""

    name: str
    path: Path
    max_age_seconds: int
    require_closed: bool = False
    purpose: str = ""
    remediation_owner: str = ""
    require_payload_healthy: bool = False
    defer_when_non_trading_day: bool = False


def _now() -> datetime:
    return datetime.now(BUSINESS_TZ)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # A broken artifact must degrade safely, never crash the API.
        return None, str(exc)
    if not isinstance(value, dict):
        return None, "JSON root must be an object"
    return value, None


def evaluate_artifact(
    rule: ArtifactRule,
    *,
    now: datetime | None = None,
    non_trading_day: bool = False,
) -> dict[str, Any]:
    """Evaluate one artifact without invoking a data source or a repair path."""

    checked_at = now or _now()
    result: dict[str, Any] = {
        "name": rule.name,
        "path": str(rule.path),
        "purpose": rule.purpose,
        "remediation_owner": rule.remediation_owner,
        "max_age_seconds": rule.max_age_seconds,
        "checked_at": _timestamp(checked_at),
    }
    if not rule.path.exists():
        return {
            **result,
            "status": "blocked",
            "reason": "artifact_missing",
            "recommended_action": "rebuild_or_run_upstream_collector",
        }

    modified_at = datetime.fromtimestamp(rule.path.stat().st_mtime, tz=BUSINESS_TZ)
    age_seconds = max(0, round((checked_at - modified_at).total_seconds()))
    result.update({"modified_at": _timestamp(modified_at), "age_seconds": age_seconds})
    payload, parse_error = _read_json(rule.path)
    if parse_error:
        return {
            **result,
            "status": "blocked",
            "reason": "artifact_unreadable",
            "error": parse_error,
            "recommended_action": "rebuild_artifact_from_upstream",
        }
    if rule.require_closed and payload.get("closed") is not True:
        return {
            **result,
            "status": "blocked",
            "reason": "final_validation_not_closed",
            "closed": payload.get("closed"),
            "recommended_action": "run_targeted_gap_repair_then_final_validation",
        }
    if rule.require_payload_healthy and payload.get("status") != "healthy":
        repair = payload.get("repair") if isinstance(payload.get("repair"), dict) else {}
        no_progress = bool(repair.get("stop_continuous")) or repair.get("reason") == "no_progress_after_full_cycle"
        return {
            **result,
            "status": "blocked",
            "reason": "artifact_reports_data_gap",
            "artifact_status": payload.get("status"),
            "missing_code_dates": ((payload.get("after") or payload.get("before") or {}).get("missing_code_dates")),
            "repair_status": repair.get("status"),
            "stop_continuous": no_progress,
            "recommended_action": (
                "review_unresolved_daily_coverage_business_confirmation"
                if no_progress
                else "run_bounded_daily_kline_coverage_repair"
            ),
        }
    if age_seconds > rule.max_age_seconds:
        if non_trading_day and rule.defer_when_non_trading_day:
            return {
                **result,
                "status": "deferred",
                "reason": "non_trading_day_refresh_not_due",
                "source_status": "stale",
                "recommended_action": "resume_scheduled_refresh_on_next_trading_day",
            }
        return {
            **result,
            "status": "stale",
            "reason": "artifact_stale",
            "recommended_action": "refresh_upstream_then_recompute_dependents",
        }
    return {**result, "status": "healthy", "reason": "artifact_fresh"}


def build_snapshot(
    rules: list[ArtifactRule],
    *,
    now: datetime | None = None,
    non_trading_day: bool = False,
) -> dict[str, Any]:
    """Build a stable health contract consumed by UI, strategy gates and workers."""

    checked_at = now or _now()
    components = [evaluate_artifact(rule, now=checked_at, non_trading_day=non_trading_day) for rule in rules]
    statuses = {item["status"] for item in components}
    status = (
        "blocked"
        if "blocked" in statuses
        else "degraded"
        if "stale" in statuses
        else "deferred"
        if "deferred" in statuses
        else "healthy"
    )
    return {
        "schema_version": 1,
        "generated_at": _timestamp(checked_at),
        "timezone": "Asia/Shanghai",
        "status": status,
        "strategy_actionable": status == "healthy",
        "market_state": "non_trading_day" if non_trading_day else "trading_day",
        "components": components,
    }


def write_snapshot(snapshot: dict[str, Any], path: Path) -> Path:
    """Atomically publish a snapshot so readers never see partial JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return path


def read_snapshot(path: Path) -> dict[str, Any]:
    """Read a published snapshot without making any external call or repair."""

    payload, error = _read_json(path)
    if error:
        return {
            "schema_version": 1,
            "status": "blocked",
            "strategy_actionable": False,
            "reason": "runtime_health_snapshot_unavailable",
            "error": error,
            "path": str(path),
        }
    return payload

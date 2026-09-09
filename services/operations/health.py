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
from utils.trading_sessions import BUSINESS_TZ




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
    expected_business_date: str | None = None
    business_date_field: str = "end_date"


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
    if rule.expected_business_date:
        actual_date = str(payload.get(rule.business_date_field) or "")[:10]
        result.update(expected_business_date=rule.expected_business_date, business_date=actual_date)
        if actual_date != rule.expected_business_date:
            return {**result, "status": "blocked", "reason": "business_date_mismatch",
                    "recommended_action": "validate_expected_trading_date"}
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
            "repair_backlog_code_dates": ((payload.get("after") or payload.get("before") or {}).get("repair_backlog_code_dates")),
            "source_absent_code_dates": ((payload.get("after") or payload.get("before") or {}).get("source_absent_code_dates")),
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
    import tempfile
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return path


def read_snapshot(path: Path, *, now: datetime | None = None, max_age_seconds: int = 900) -> dict[str, Any]:
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
    checked_at = now or _now()
    try:
        published = datetime.fromisoformat(str(payload.get("generated_at") or ""))
        if published.tzinfo is None:
            published = published.replace(tzinfo=BUSINESS_TZ)
        age = (checked_at - published).total_seconds()
    except (ValueError, TypeError):
        age = None
    valid = age is not None and -60 <= age <= max_age_seconds
    return {**payload, "publisher_age_seconds": round(age) if age is not None else None,
            "publisher_status": "healthy" if valid else "stale",
            **({} if valid else {"status": "blocked", "strategy_actionable": False,
                                "reason": "runtime_health_publisher_stale"})}


def strategy_data_checks(summary: dict[str, Any], health: dict[str, Any]) -> list[dict[str, Any]]:
    """The same data gate drives workflow, UI and notification decisions, including zero-ticket days."""
    try:
        failures = int(summary.get("minute_data_failure_rows") or 0)
    except (TypeError, ValueError, OverflowError):
        failures = 1  # Malformed producer evidence cannot pass the gate.
    diagnosis = summary.get("diagnosis_code")
    blocked = failures > 0 or diagnosis == "MINUTE_DATA_UNAVAILABLE"
    source_blocked = bool(summary.get("source_builder_failure") or summary.get("candidate_snapshot_failure"))
    return [
        {"name": "runtime_data_health", "ok": health.get("strategy_actionable") is True,
         "status": "pass" if health.get("strategy_actionable") is True else "blocked",
         "message": "数据验收通过" if health.get("strategy_actionable") is True else "数据交付未通过或健康发布过期",
         "detail": health},
        {"name": "minute_source_visibility", "ok": not blocked,
         "status": "blocked" if blocked else "pass",
         "message": f"{failures}个候选分钟来源不可用，不能解释为没有机会" if blocked else "未报告分钟来源故障",
         "failure_count": failures},
        {"name": "candidate_source_build", "ok": not source_blocked,
         "status": "blocked" if source_blocked else "pass",
         "message": "候选构建失败" if source_blocked else "候选构建未报告失败"},
    ]


def operations_notification_owner(path: Path, *, now: datetime | None = None) -> bool:
    """Transfer notification ownership only after a completed, fresh notify-enabled poll."""
    snapshot = read_snapshot(path, now=now)
    return (snapshot.get('publisher_status') == 'healthy'
            and snapshot.get('notifications_enabled') is True
            and snapshot.get('notification_transport_ok') is True
            and snapshot.get('notification', {}).get('status') in ('idle', 'smtp_accepted'))


def backup_health(path: Path, *, now=None) -> dict:
    """Judge backup delivery dates independently from a freshly rewritten status file."""
    now = now or _now()
    payload = read_snapshot(path,now=now,max_age_seconds=25*3600)
    def age(value):
        try:
            stamp = datetime.fromisoformat(str(value or ''))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=BUSINESS_TZ)
            seconds = (now-stamp).total_seconds()
            return seconds if seconds >= 0 else None
        except (ValueError,TypeError):
            return None
    backup_age = age(payload.get('last_backup_at'))
    restore_age = age(payload.get('last_restore_verified_at'))
    source_status = payload.get('status')
    reasons = []
    if payload.get('publisher_status') != 'healthy': reasons.append('backup_status_unavailable_or_stale')
    if source_status == 'failed': reasons.append('backup_execution_failed')
    if source_status == 'degraded': reasons.append(payload.get('reason') or 'backup_reports_degraded')
    if backup_age is None: reasons.append('backup_success_not_verified')
    elif backup_age > 25*3600: reasons.append('backup_older_than_25_hours')
    if restore_age is None: reasons.append('restore_drill_not_verified')
    elif restore_age > 7*86400: reasons.append('restore_drill_older_than_7_days')
    if payload.get('host_copy_verified') is not True: reasons.append('host_archive_not_verified')
    if source_status not in ('healthy','degraded','failed','running'): reasons.append('backup_execution_unknown')
    status = 'failed' if source_status=='failed' else 'unknown' if backup_age is None else 'degraded' if reasons else 'healthy'
    return {**payload,'status':status,'execution_status':source_status or 'unknown','reasons':reasons,
            'backup_age_hours':None if backup_age is None else round(backup_age/3600,2),
            'restore_age_days':None if restore_age is None else round(restore_age/86400,2),
            'delivery_ok':not reasons,'repair_policy':'operator_review_only'}


def g3_row_source_unavailable(row: dict) -> bool:
    """Shared negative source evidence; missing legacy optional fields are not invented."""
    import math
    try:
        conflicts = float(row.get('m30_conflict_rows') or 0)
    except (ValueError, TypeError, OverflowError):
        return True
    source_ok = row.get('m30_source_ok')
    return (not math.isfinite(conflicts) or conflicts < 0 or conflicts > 0
            or row.get('m30_visibility_status') in ('data_conflict', 'data_unavailable', 'source_unavailable', 'missing')
            or row.get('m30_status') in ('data_unavailable', 'source_unavailable', 'data_conflict', 'missing')
            or (source_ok not in (None, '') and str(source_ok).strip().lower() not in ('true', '1', '1.0')))


def g3_batch_source_checks(candidates, tickets=()) -> list[dict]:
    checks = []
    for name, rows in (('candidate_row_sources', candidates), ('ticket_row_sources', tickets)):
        bad = [row for row in rows if g3_row_source_unavailable(row)]
        checks.append({'name':name, 'ok':not bad, 'status':'blocked' if bad else 'pass',
                       'failure_count':len(bad), 'codes':[str(row.get('code') or '') for row in bad],
                       'message':f'{len(bad)}条逐行来源证据冲突或不可用' if bad else '逐行来源未报告故障'})
    return checks

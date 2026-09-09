import json
import sys
from datetime import datetime, timedelta

from services.runtime_health import ArtifactRule, BUSINESS_TZ, build_snapshot, evaluate_artifact, read_snapshot, write_snapshot


def test_closed_fresh_artifact_is_healthy(tmp_path):
    artifact = tmp_path / "final_validation.json"
    artifact.write_text(json.dumps({"closed": True}), encoding="utf-8")

    result = evaluate_artifact(ArtifactRule("minute", artifact, 60, require_closed=True, remediation_owner="collector"), now=datetime.now(BUSINESS_TZ))

    assert result["status"] == "healthy"


def test_daily_coverage_gap_blocks_health_even_when_report_is_fresh(tmp_path):
    path = tmp_path / "daily_coverage.json"
    path.write_text(json.dumps({"status": "degraded", "before": {"missing_code_dates": 42}}), encoding="utf-8")
    result = evaluate_artifact(
        ArtifactRule("daily_kline_coverage", path, 3600, require_payload_healthy=True),
        now=datetime.now(BUSINESS_TZ),
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "artifact_reports_data_gap"
    assert result["missing_code_dates"] == 42
    assert result["remediation_owner"] == ""


def test_no_progress_daily_coverage_requires_business_confirmation(tmp_path):
    path = tmp_path / "daily_coverage.json"
    path.write_text(
        json.dumps(
            {
                "status": "degraded",
                "after": {"missing_code_dates": 42},
                "repair": {"status": "completed", "reason": "no_progress_after_full_cycle", "stop_continuous": True},
            }
        ),
        encoding="utf-8",
    )

    result = evaluate_artifact(ArtifactRule("daily_kline_coverage", path, 3600, require_payload_healthy=True))

    assert result["status"] == "blocked"
    assert result["stop_continuous"] is True
    assert result["recommended_action"] == "review_unresolved_daily_coverage_business_confirmation"


def test_open_final_validation_blocks_strategy(tmp_path):
    artifact = tmp_path / "final_validation.json"
    artifact.write_text(json.dumps({"closed": False}), encoding="utf-8")

    snapshot = build_snapshot([ArtifactRule("minute", artifact, 60, require_closed=True)], now=datetime.now(BUSINESS_TZ))

    assert snapshot["status"] == "blocked"
    assert snapshot["strategy_actionable"] is False
    assert snapshot["components"][0]["reason"] == "final_validation_not_closed"


def test_stale_artifact_degrades_and_snapshot_write_is_valid_json(tmp_path):
    artifact = tmp_path / "summary.json"
    artifact.write_text("{}", encoding="utf-8")
    checked_at = datetime.now(BUSINESS_TZ)
    old_time = (checked_at - timedelta(seconds=61)).timestamp()
    import os
    os.utime(artifact, (old_time, old_time))

    snapshot = build_snapshot([ArtifactRule("summary", artifact, 60)], now=checked_at)
    output = write_snapshot(snapshot, tmp_path / "health" / "latest.json")

    assert snapshot["status"] == "degraded"
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "degraded"


def test_non_trading_day_defers_only_explicitly_scheduled_artifacts(tmp_path):
    artifact = tmp_path / "summary.json"
    artifact.write_text("{}", encoding="utf-8")
    checked_at = datetime.now(BUSINESS_TZ)
    old_time = (checked_at - timedelta(seconds=61)).timestamp()
    import os
    os.utime(artifact, (old_time, old_time))

    deferred = build_snapshot(
        [ArtifactRule("summary", artifact, 60, defer_when_non_trading_day=True)],
        now=checked_at,
        non_trading_day=True,
    )
    still_stale = build_snapshot(
        [ArtifactRule("coverage", artifact, 60)],
        now=checked_at,
        non_trading_day=True,
    )

    assert deferred["status"] == "deferred"
    assert deferred["strategy_actionable"] is False
    assert deferred["market_state"] == "non_trading_day"
    assert deferred["components"][0]["reason"] == "non_trading_day_refresh_not_due"
    assert still_stale["status"] == "degraded"


def test_missing_published_snapshot_is_a_safe_strategy_block(tmp_path):
    result = read_snapshot(tmp_path / "missing.json")

    assert result["status"] == "blocked"
    assert result["strategy_actionable"] is False


def test_health_publisher_succeeds_when_it_publishes_a_blocked_snapshot(tmp_path, monkeypatch):
    from scripts import publish_runtime_health

    artifact = tmp_path / "daily_coverage.json"
    artifact.write_text(json.dumps({"status": "degraded"}), encoding="utf-8")
    output = tmp_path / "health" / "latest.json"
    monkeypatch.setattr(
        publish_runtime_health,
        "default_rules",
        lambda: [ArtifactRule("daily_kline_coverage", artifact, 3600, require_payload_healthy=True)],
    )
    monkeypatch.setattr(sys, "argv", ["publish_runtime_health.py", "--output", str(output)])

    assert publish_runtime_health.main() == 0
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "blocked"

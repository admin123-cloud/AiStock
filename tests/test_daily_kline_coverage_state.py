import json

from scripts import daily_kline_coverage_maintenance as maintenance
from scripts.daily_kline_coverage_maintenance import _cross_source_fallback_command, _retryable_prior_unresolved_codes, _unresolved_records, _unresolved_summary, _write_rolling_state


def test_legacy_blocked_codes_are_preserved_as_business_confirmation_items():
    state = {"schema_version": 2, "blocked_codes": ["600000.SH"]}

    records = _unresolved_records(state)

    assert records["600000.SH"]["classification"] == "prior_unresolved_source_outcome_not_persisted"
    assert records["600000.SH"]["requires_business_confirmation"] is True


def test_unresolved_summary_counts_each_persisted_classification():
    state = {
        "blocked_codes": ["000001.SZ", "600000.SH"],
        "unresolved": {
            "000001.SZ": {"classification": "source_no_daily_bars_pending_business_confirmation", "requires_business_confirmation": True},
            "600000.SH": {"classification": "partial_or_unverified_source_coverage_pending_confirmation", "requires_business_confirmation": True},
        },
    }

    summary = _unresolved_summary(state)

    assert summary["unresolved_codes"] == 2
    assert summary["requires_business_confirmation_codes"] == 2
    assert summary["classification_counts"] == {
        "partial_or_unverified_source_coverage_pending_confirmation": 1,
        "source_no_daily_bars_pending_business_confirmation": 1,
    }


def test_terminal_cycle_state_is_persisted_in_schema_three(tmp_path):
    path = tmp_path / "rolling_state.json"
    _write_rolling_state(path, {"schema_version": 2, "blocked_codes": ["600000.SH"]}, {"600000.SH"})

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 3
    assert persisted["unresolved"]["600000.SH"]["requires_business_confirmation"] is True


def test_legacy_unresolved_codes_are_retryable_once():
    assert _retryable_prior_unresolved_codes({"blocked_codes": ["600000.SH"]}) == {"600000.SH"}


def test_confirmed_qmt_empty_codes_remain_in_bounded_retry_rotation():
    state = {
        "blocked_codes": ["000001.SZ"],
        "unresolved": {
            "000001.SZ": {"classification": "source_no_daily_bars_pending_business_confirmation"},
        },
    }

    assert _retryable_prior_unresolved_codes(state) == {"000001.SZ"}


def test_cross_source_fallback_is_bounded_to_the_qmt_repair_batch(tmp_path):
    command = _cross_source_fallback_command("2026-01-01", "2026-01-31", ["600000.SH"], tmp_path / "fallback.json")

    assert "--candidate-mode" in command
    assert command[command.index("--candidate-mode") + 1] == "qmt_absence"
    assert command[command.index("--codes") + 1] == "600000.SH"
    assert "--apply" in command


def test_coverage_maintenance_unit_audit_is_report_only(tmp_path, monkeypatch):
    calls = []

    def fake_audit(start_date, end_date, *, tdx_root, apply):
        calls.append(apply)
        return {"status": "healthy", "unresolved_mismatch_rows": 0}

    monkeypatch.setattr(maintenance, "audit_tdx_units", fake_audit)
    monkeypatch.setattr(
        maintenance,
        "build_unit_audit_report",
        lambda start_date, end_date, *, mode, tdx, qmt_stage_before, qmt_stage_after: {
            "status": "healthy",
            "unresolved_mismatch_rows": 0,
        },
    )
    monkeypatch.setattr(maintenance, "write_unit_audit_report", lambda path, payload: None)

    args = type(
        "Args",
        (),
        {
            "unit_audit": True,
            "mode": "repair",
            "tdx_root": tmp_path,
            "report": tmp_path / "latest.json",
        },
    )()
    payload = {}

    maintenance._attach_unit_audit(
        payload,
        args,
        "1990-01-01",
        "2026-08-05",
        None,
        None,
    )

    assert calls == [False]
    assert payload["unit_audit"]["status"] == "healthy"

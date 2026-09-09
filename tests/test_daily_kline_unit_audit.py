from datetime import date, datetime

import pytest

from scripts import audit_kline_daily_units as audit
from utils.kline_units import classify_daily_unit_pair


def _comparison(code: str, trade_date: date, multiplier: float = 1.0, *, amount_multiplier: float | None = None):
    return {
        "code": code,
        "trade_date": trade_date,
        "db_volume": 100.0 * multiplier,
        "source_volume": 100.0,
        "db_amount": 1_000_000.0 * (amount_multiplier if amount_multiplier is not None else multiplier),
        "source_amount": 1_000_000.0,
    }


def test_daily_unit_classifier_distinguishes_100x_and_single_field_errors():
    assert classify_daily_unit_pair(100, 1_000_000, 100, 1_000_000) == "ok"
    assert classify_daily_unit_pair(10_000, 100_000_000, 100, 1_000_000) == "too_large_100x"
    assert classify_daily_unit_pair(1, 10_000, 100, 1_000_000) == "too_small_100x"
    assert classify_daily_unit_pair(100, 100_000_000, 100, 1_000_000) == "amount_large_only"
    assert classify_daily_unit_pair(10_000, 100, 100, 1_000_000) == "amount_small_10000_only"
    assert classify_daily_unit_pair(10_000, 1_000_000, 100, 1_000_000) == "volume_large_only"


def test_unit_comparison_summary_reports_latest_day_and_mismatch_samples():
    rows = [
        _comparison("000001.SZ", date(2026, 8, 4)),
        _comparison("600000.SH", date(2026, 8, 5), multiplier=100.0),
        _comparison("000002.SZ", date(2026, 8, 5), multiplier=1.0, amount_multiplier=100.0),
    ]

    summary = audit.summarize_unit_comparisons(rows, source="test", sample_limit=5)

    assert summary["status"] == "degraded"
    assert summary["rows_checked"] == 3
    assert summary["mismatch_rows"] == 2
    assert summary["latest_trade_date"] == "2026-08-05"
    assert summary["latest_trade_date_rows"] == 2
    assert summary["latest_trade_date_mismatch_rows"] == 2
    assert summary["classification_counts"]["too_large_100x"] == 1
    assert summary["classification_counts"]["amount_large_only"] == 1


def test_temporary_qmt_stage_requires_current_refresh_timestamp():
    class Result:
        first_row = (2, date(2026, 8, 5), datetime(2026, 8, 5, 1, 0, 0))
        result_rows = []

    class Client:
        def query(self, _sql):
            return Result()

    result = audit.audit_qmt_stage_units(
        Client(),
        "2026-01-01",
        "2026-08-05",
    )

    assert result["status"] == "skipped"
    assert result["temporary_stage"] is True
    assert result["trusted_for_auto_repair"] is False
    assert result["stage_rows"] == 2


def test_tdx_apply_is_followed_by_a_second_read_only_audit(monkeypatch, tmp_path):
    calls = []

    def fake_repair(start_date, end_date, root, *, apply):
        calls.append(apply)
        if apply:
            return {"ok": True, "rows_to_repair": 1, "rows_repaired": 1}
        return {"ok": True, "rows_to_repair": 0}

    monkeypatch.setattr(audit, "repair_from_tdx", fake_repair)
    result = audit.audit_tdx_units(
        "2026-01-01",
        "2026-08-05",
        tdx_root=tmp_path,
        apply=True,
    )

    assert calls == [True, False]
    assert result["status"] == "healthy"
    assert result["mismatch_rows"] == 1
    assert result["unresolved_mismatch_rows"] == 0


def test_unit_audit_report_keeps_unresolved_source_rows_visible():
    report = audit.build_unit_audit_report(
        "2026-01-01",
        "2026-08-05",
        mode="repair",
        tdx={"status": "healthy", "unresolved_mismatch_rows": 1, "rows_repaired": 2},
        qmt_stage_before={"status": "degraded", "mismatch_rows": 2, "unresolved_mismatch_rows": 2},
        qmt_stage_after={"status": "healthy", "mismatch_rows": 0, "unresolved_mismatch_rows": 0},
    )

    assert report["status"] == "degraded"
    assert report["unresolved_mismatch_rows"] == 1
    assert report["repair_actions"]["tdx_rows_repaired"] == 2
    assert report["repair_actions"]["qmt_stage_rows_repaired"] == 2


def test_stage_table_identifier_is_restricted_before_sql_is_built():
    with pytest.raises(ValueError):
        audit.audit_qmt_stage_units(
            object(),
            "2026-01-01",
            "2026-08-05",
            stage_table="stage; DROP TABLE stocks",
        )

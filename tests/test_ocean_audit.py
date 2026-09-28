from __future__ import annotations

from datetime import date, datetime

import pytest

from utils.ocean_audit import (
    DailySnapshotReader,
    OceanAuditError,
    build_daily_snapshot_lookup,
    dryrun_missing_formal_keys,
    find_code_date_conflicts,
    reconcile_stage_rows,
    select_manifest_scope,
)
from utils.ocean_audit import LocalOceanManifest
from utils.ocean_stage import STANDARD_5M_MINUTES
from pathlib import Path


def _stage_rows() -> list[dict[str, object]]:
    trade_date = date(2001, 1, 2)
    rows: list[dict[str, object]] = []
    for index, slot in enumerate(STANDARD_5M_MINUTES):
        hour, minute = divmod(slot, 60)
        dt = datetime(2001, 1, 2, hour, minute)
        rows.append({
            "batch_id": "ocean_vps_5m_2001_fixture",
            "source_entry": "sh600000_2001.csv",
            "code": "600000.SH",
            "security_type": "stock",
            "universe_decision": "accepted_stock",
            "trade_date": trade_date.isoformat(),
            "datetime": dt.isoformat(),
            "open": 20.75,
            "high": 20.8,
            "low": 20.75,
            "close": 20.8,
            "source_volume_lots": 172,
            "source_amount_yuan": 356920,
            "row_hash": f"row-{index}",
        })
    return rows


def _audit(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "batch_id": "ocean_vps_5m_2001_fixture",
        "source_entry": "sh600000_2001.csv",
        "code": "600000.SH",
        "security_type": "stock",
        "universe_decision": "accepted_stock",
        "trade_date": "2001-01-02",
        "expected_bar_count": 48,
        "source_row_count": 48,
        "valid_ohlc_row_count": 48,
        "accepted_row_count": 48,
        "unique_datetime_count": 48,
        "invalid_row_count": 0,
        "quantity_invalid_row_count": 0,
        "status": "pass",
        "reason": "",
    }
    value.update(overrides)
    return value


def _daily(amount: float = 356920 * 48) -> dict[str, object]:
    return {
        "code": "600000.SH",
        "security_type": "stock",
        "quality_status": "valid",
        "trade_date": date(2001, 1, 2),
        "open": 20.75,
        "high": 20.8,
        "low": 20.75,
        "close": 20.8,
        "volume_lots": 172 * 48,
        "amount_yuan": amount,
    }


def test_local_stage_daily_reconciliation_is_read_only_structure_plus_snapshot() -> None:
    rows = _stage_rows()
    result = reconcile_stage_rows(
        rows, {("600000.SH", date(2001, 1, 2)): _daily()}, day_audits=[_audit()]
    )
    assert len(result) == 1
    assert result[0].structure_pass is True
    assert result[0].overall_status == "pass"
    assert result[0].minute_rows == 48
    assert result[0].daily_gate_pass is True
    assert result[0].units_contract == "volume_lots/amount_yuan; raw_unadjusted_price"


def test_snapshot_difference_blocks_candidate_dryrun_input() -> None:
    result = reconcile_stage_rows(
        _stage_rows(),
        {("600000.SH", date(2001, 1, 2)): _daily(amount=1)},
        day_audits=[_audit()],
    )
    assert result[0].structure_pass is True
    assert result[0].overall_status == "review"
    assert "amount_relative_diff" in result[0].reason


def test_pre2010_amount_policy_can_relax_amount_without_relaxing_other_gates() -> None:
    daily = _daily(amount=(356920 * 48) * 1.005)
    result = reconcile_stage_rows(
        _stage_rows(),
        {("600000.SH", date(2001, 1, 2)): daily},
        day_audits=[_audit()],
        amount_tolerance=0.01,
    )
    assert result[0].structure_pass is True
    assert result[0].daily_gate_pass is True
    assert result[0].overall_status == "pass"


def test_missing_formal_key_dryrun_only_reports_difference_and_duplicates() -> None:
    rows = _stage_rows()[:2]
    formal = [("600000.SH", rows[0]["datetime"])]
    dryrun = dryrun_missing_formal_keys(rows, formal)
    assert dryrun.candidate_rows == 2
    assert dryrun.candidate_unique_keys == 2
    assert dryrun.existing_formal_keys == 1
    assert dryrun.missing_keys == (("600000.SH", "2001-01-02T09:40:00+08:00"),)
    assert dryrun.missing_keys_status == "checked"

    duplicate = dryrun_missing_formal_keys(rows + [rows[1]], formal)
    assert duplicate.duplicate_candidate_keys == (("600000.SH", "2001-01-02T09:40:00+08:00"),)


def test_missing_formal_keys_without_export_is_unknown_not_empty_formal_table() -> None:
    result = dryrun_missing_formal_keys(_stage_rows()[:1], None)
    assert result.existing_formal_keys is None
    assert result.missing_keys is None
    assert result.missing_keys_status == "not_checked"


def test_datetime_keys_are_canonicalized_to_shanghai() -> None:
    rows = _stage_rows()[:1]
    result = dryrun_missing_formal_keys(
        rows, [("600000.SH", "2001-01-02T01:35:00Z")]
    )
    assert result.missing_keys == ()


def test_day_audit_bad_source_evidence_cannot_be_hidden_by_48_stage_rows() -> None:
    result = reconcile_stage_rows(
        _stage_rows(),
        {("600000.SH", date(2001, 1, 2)): _daily()},
        day_audits=[_audit(
            source_row_count=49,
            invalid_row_count=1,
            status="review",
            reason="one_bad_source_row",
        )],
    )
    assert result[0].structure_pass is False
    assert result[0].overall_status == "review"
    assert "day_audit_status=review" in result[0].reason
    assert "day_audit_invalid_rows=1" in result[0].reason
    assert "day_audit_source_accepted_mismatch=49!=48" in result[0].reason


def test_stage_seconds_security_and_values_are_explicit_gates() -> None:
    rows = _stage_rows()
    rows[0]["datetime"] = "2001-01-02T09:35:01+08:00"
    rows[1]["universe_decision"] = "historical_stock_pending"
    rows[2]["open"] = 0
    rows[3]["source_volume_lots"] = float("nan")
    result = reconcile_stage_rows(
        rows,
        {("600000.SH", date(2001, 1, 2)): _daily()},
        day_audits=[_audit()],
    )
    assert result[0].structure_pass is False
    assert "datetime_seconds_not_zero" in result[0].reason
    assert "stage_universe_not_accepted_stock" in result[0].reason
    assert "minute_positive_ohlc_failed" in result[0].reason
    assert "minute_source_volume_lots_finite_nonnegative_failed" in result[0].reason


def test_daily_positive_and_quantity_gates_are_not_quality_status_only() -> None:
    daily = _daily()
    daily["high"] = 0
    daily["amount_yuan"] = -1
    result = reconcile_stage_rows(
        _stage_rows(),
        {("600000.SH", date(2001, 1, 2)): daily},
        day_audits=[_audit()],
    )
    assert result[0].daily_gate_pass is False
    assert result[0].overall_status == "review"
    assert "daily_positive_ohlc_failed" in result[0].reason
    assert "daily_amount_yuan_finite_nonnegative_failed" in result[0].reason


def test_snapshot_lookup_rejects_duplicate_key_without_fabricating_one_row() -> None:
    row = _daily()
    with pytest.raises(OceanAuditError, match="daily_snapshot_rows=2"):
        build_daily_snapshot_lookup([row, dict(row)])


def test_manifest_scope_rejects_multiple_policy_or_batch_groups() -> None:
    def manifest(batch: str, policy: str) -> LocalOceanManifest:
        return LocalOceanManifest(
            Path(f"{batch}-{policy}") / "manifest.json",
            {
                "batch_id": batch,
                "accepted_stock_policy_hash": policy,
            },
        )

    with pytest.raises(OceanAuditError, match="multiple_batch_or_policy_scope"):
        select_manifest_scope([manifest("b1", "p1"), manifest("b1", "p2")])


def test_code_date_conflict_isolated_by_source_entry() -> None:
    first = reconcile_stage_rows(
        _stage_rows(),
        {("600000.SH", date(2001, 1, 2)): _daily()},
        day_audits=[_audit()],
    )[0]
    second_rows = [dict(row, source_entry="sh600000_alt_2001.csv") for row in _stage_rows()]
    second = reconcile_stage_rows(
        second_rows,
        {("600000.SH", date(2001, 1, 2)): _daily()},
        day_audits=[_audit(source_entry="sh600000_alt_2001.csv")],
    )[0]
    assert find_code_date_conflicts([first, second]) == (("600000.SH", date(2001, 1, 2)),)


def test_daily_snapshot_reader_loads_one_year_and_filters_requested_codes(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    year_root = tmp_path / "trade_year=2001"
    year_root.mkdir()
    table = pa.table({
        "code": ["600000.SH", "600001.SZ", "600000.SH"],
        "security_type": ["stock", "stock", "bond"],
        "quality_status": ["valid", "valid", "valid"],
        "trade_date": [date(2001, 1, 2), date(2001, 1, 2), date(2001, 1, 2)],
        "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0],
        "low": [1.0, 2.0, 3.0], "close": [1.0, 2.0, 3.0],
        "volume_lots": [1, 2, 3], "amount_yuan": [10, 20, 30],
    })
    pq.write_table(table, year_root / "part.parquet")
    reader = DailySnapshotReader(tmp_path)
    result = reader.load_code_year(2001, codes=["600000.SH", "600001.SZ"])
    assert set(result) == {("600000.SH", date(2001, 1, 2)), ("600001.SZ", date(2001, 1, 2))}
    assert result[("600000.SH", date(2001, 1, 2))]["amount_yuan"] == 10

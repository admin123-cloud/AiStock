from __future__ import annotations

from datetime import datetime, timezone

from scripts.ocean_formal_key_diff_dryrun import _conflicts, _formal_key
from utils.ocean_formal_units import classify_formal_conflict


def _candidate() -> dict[str, object]:
    return {
        "code": "600000.SH",
        "datetime": "2000-06-30T09:35:00+08:00",
        "open": 20.0,
        "high": 20.2,
        "low": 19.9,
        "close": 20.1,
        "source_volume_lots": 10,
        "source_amount_yuan": 2000,
    }


def test_formal_diff_canonicalizes_key_and_isolates_value_conflict() -> None:
    formal = [("600000.SH", datetime(2000, 6, 30, 1, 35, tzinfo=timezone.utc), 20.0, 20.2, 19.9, 20.1, 10, 1999)]
    assert _formal_key(formal[0]) == ("600000.SH", "2000-06-30T09:35:00+08:00")
    conflicts = _conflicts([_candidate()], formal, {"source_entry": "sh600000_2000.csv"})
    assert len(conflicts) == 1
    assert conflicts[0]["reason"] == "formal_existing_value_conflict"
    assert conflicts[0]["different_fields"] == ["source_amount_yuan"]
    assert conflicts[0]["conflict_class"] == "unit_unconfirmed_amount"
    assert conflicts[0]["unit_status"] == "unconfirmed"


def test_formal_diff_detects_duplicate_formal_key_evidence() -> None:
    formal = [
        ("600000.SH", "2000-06-30T09:35:00+08:00", 20, 20, 20, 20, 10, 2000),
        ("600000.SH", "2000-06-30T09:35:00+08:00", 20, 20, 20, 20, 10, 2000),
    ]
    conflicts = _conflicts([_candidate()], formal, {"source_entry": "sh600000_2000.csv"})
    assert any(item["reason"] == "formal_duplicate_key" for item in conflicts)
    assert any(item["conflict_class"] == "formal_duplicate_key" for item in conflicts)


def test_formal_unit_audit_does_not_call_quantity_difference_a_real_ohlc_conflict() -> None:
    result = classify_formal_conflict({
        "different_fields": ["source_volume_lots", "source_amount_yuan"],
        "candidate": {"source_volume_lots": 10, "source_amount_yuan": 2000},
        "formal": {"source_volume_lots": 1000, "source_amount_yuan": 20000000},
    })
    assert result["conflict_class"] == "unit_unconfirmed_quantity_units"
    assert result["unit_status"] == "unconfirmed"
    assert result["volume_scale_signal"] == "near_100"
    assert result["amount_scale_signal"] == "near_10000"


def test_formal_unit_audit_separates_ohlc_from_quantity_uncertainty() -> None:
    result = classify_formal_conflict({
        "different_fields": ["close", "source_amount_yuan"],
        "candidate": {"close": 20, "source_amount_yuan": 2000},
        "formal": {"close": 21, "source_amount_yuan": 1999},
    })
    assert result["conflict_class"] == "real_ohlc_and_unit_unconfirmed_quantity"

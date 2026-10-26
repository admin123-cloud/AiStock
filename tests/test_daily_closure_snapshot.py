from datetime import date

from utils.daily_closure_snapshot import (
    CLASS_OHLC_ENVELOPE,
    CLASS_VOLUME_POSITIVE_AMOUNT_ZERO_LIFECYCLE,
    CLASS_VOLUME_POSITIVE_ZERO_OHLC,
    CLOSURE_BLOCKED,
    CLOSURE_HEALTHY,
    CLOSURE_REVIEW,
    build_daily_closure_snapshot,
    build_daily_closure_snapshot_from_reader,
    classify_daily_rows,
)
from utils.daily_date_universe import DailyDateUniverseReader, build_date_universe_payload


def _permit(*days):
    return build_date_universe_payload(
        code="000001.SZ",
        security_type="stock",
        identity_status="verified",
        source_kind="fixture",
        source_ref="fixture://daily",
        source_sha256="source-sha",
        dates=days,
    ).exact_key_permit(source="fixture-registry-member")


def _row(day="2026-01-05", **values):
    row = {
        "code": "000001.SZ",
        "trade_date": day,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 100.0,
        "amount": 1000.0,
    }
    row.update(values)
    return row


def test_snapshot_uses_permit_as_expected_denominator_and_can_be_healthy():
    permit = _permit(date(2026, 1, 5))
    row = _row()
    snapshot = build_daily_closure_snapshot(
        permit=permit,
        formal_rows=[row],
        source_rows=[row],
    )

    assert snapshot.status == CLOSURE_HEALTHY
    assert snapshot.expected_key_count == 1
    assert snapshot.gates["registry_member_exact_key"]["status"] == CLOSURE_HEALTHY
    assert snapshot.gates["formal_coverage_and_physical"]["status"] == CLOSURE_HEALTHY
    assert snapshot.gates["source_reconciliation"]["status"] == CLOSURE_HEALTHY
    assert snapshot.as_dict()["readonly"] is True


def test_snapshot_blocks_missing_or_outside_keys_and_requires_permit():
    permit = _permit(date(2026, 1, 5))
    outside = _row(day="2026-01-06")
    missing = build_daily_closure_snapshot(
        permit=permit,
        formal_rows=[outside],
        source_rows=[outside],
    )
    assert missing.status == CLOSURE_BLOCKED
    assert missing.gates["formal_coverage_and_physical"]["missing_key_count"] == 1
    assert missing.gates["formal_coverage_and_physical"]["outside_key_count"] == 1

    no_permit = build_daily_closure_snapshot(
        permit=None,
        expected_keys=[("000001.SZ", date(2026, 1, 5))],
        formal_rows=[_row()],
        source_rows=[_row()],
    )
    assert no_permit.status == CLOSURE_BLOCKED
    assert no_permit.gates["registry_member_exact_key"]["status"] == CLOSURE_BLOCKED

    bypass_attempt = build_daily_closure_snapshot(
        permit=permit,
        expected_keys=[("000001.SZ", date(2026, 1, 6))],
        formal_rows=[outside],
        source_rows=[outside],
    )
    assert bypass_attempt.status == CLOSURE_BLOCKED
    assert bypass_attempt.gates["registry_member_exact_key"]["errors"]


def test_snapshot_classifies_ohlc_and_quantity_exceptions():
    rows = [
        _row(high=10.0, low=10.0, close=9.5),
        _row(day="2026-01-06", open=0.0, high=0.0, low=0.0, close=0.0, volume=10.0, amount=0.0),
        _row(day="2026-01-07", volume=10.0, amount=0.0),
    ]
    classified = classify_daily_rows(
        rows,
        status_rows=[
            {
                "code": "000001.SZ",
                "start_date": "2026-01-07",
                "end_date": "2026-01-07",
                "status": "suspended",
                "review_state": "verified",
            }
        ],
    )
    classes = {item["classification"] for item in classified}
    assert CLASS_OHLC_ENVELOPE in classes
    assert CLASS_VOLUME_POSITIVE_ZERO_OHLC in classes
    assert CLASS_VOLUME_POSITIVE_AMOUNT_ZERO_LIFECYCLE in classes


def test_snapshot_marks_source_mismatch_blocked_and_missing_source_review():
    permit = _permit(date(2026, 1, 5))
    formal = _row()
    mismatch = dict(formal, amount=1001.0)
    blocked = build_daily_closure_snapshot(
        permit=permit,
        formal_rows=[formal],
        source_rows=[mismatch],
    )
    assert blocked.status == CLOSURE_BLOCKED
    assert blocked.gates["source_reconciliation"]["mismatched_rows"] == 1

    review = build_daily_closure_snapshot(permit=permit, formal_rows=[formal])
    assert review.status == CLOSURE_REVIEW
    assert review.gates["source_reconciliation"]["reason"] == "source_rows_not_supplied"


def test_snapshot_reads_registry_member_permit_before_building_gates():
    payload = build_date_universe_payload(
        code="000001.SZ",
        security_type="stock",
        identity_status="verified",
        source_kind="fixture",
        source_ref="fixture://daily",
        source_sha256="source-sha",
        dates=[date(2026, 1, 5)],
    )
    reader = DailyDateUniverseReader(
        registry_loader=lambda codes: [payload.registry.registry_row()],
        member_loader=lambda codes, keys: list(payload.member_rows),
    )
    row = _row()
    snapshot = build_daily_closure_snapshot_from_reader(
        reader=reader,
        expected_keys=[("000001.SZ", date(2026, 1, 5))],
        formal_rows=[row],
        source_rows=[row],
        source="test-snapshot-reader",
    )
    assert snapshot.status == CLOSURE_HEALTHY
    assert snapshot.permit_digest


def test_snapshot_keeps_local_permit_blocked_when_online_registry_is_missing():
    permit = _permit(date(2026, 1, 5))
    row = _row()
    snapshot = build_daily_closure_snapshot(
        permit=permit,
        expected_keys=[("000001.SZ", date(2026, 1, 5))],
        formal_rows=[row],
        source_rows=[row],
        permit_error="online_registry_member_tables_missing; local_accepted_evidence_only",
    )
    assert snapshot.gates["registry_member_exact_key"]["status"] == CLOSURE_BLOCKED
    assert snapshot.gates["formal_coverage_and_physical"]["status"] == CLOSURE_HEALTHY


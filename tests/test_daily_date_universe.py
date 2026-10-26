from datetime import date, datetime

import pandas as pd
import pytest

from utils.daily_date_universe import (
    DATE_UNIVERSE_COMPLETE,
    DATE_UNIVERSE_UNKNOWN,
    DailyDateUniverseReader,
    DateUniverseContractError,
    DateUniversePublisher,
    build_date_universe_payload,
    build_exact_key_permit_from_rows,
)
from utils.daily_coverage_audit import (
    AUDIT_FORMAL_MISSING_UNCONFIRMED,
    AUDIT_FORMAL_OUTSIDE_UNIVERSE,
    AUDIT_FORMAL_PRESENT,
    AUDIT_STATUS_CONFLICT,
    AUDIT_SUSPENDED_VERIFIED,
    DailyCoverageAuditPublisher,
    build_daily_coverage_audit,
    derive_formal_scope_sets,
    summarize_formal_scope,
)
from utils.daily_write_guard import (
    DailyWriteBlockedError,
    make_daily_write_scope,
    validate_daily_dataframe,
    validate_daily_keys,
)


def _payload():
    return build_date_universe_payload(
        code="000001.sz",
        security_type="stock",
        identity_status="verified",
        source_kind="accepted_test_source",
        source_ref="fixture://000001.SZ",
        source_sha256="source-sha",
        dates=[date(2026, 1, 5), "2026-01-06", date(2026, 1, 5)],
        published_at=datetime(2026, 1, 7, 9, 0),
    )


def test_payload_freezes_registry_members_and_digest():
    payload = _payload()

    assert payload.registry.code == "000001.SZ"
    assert payload.registry.completeness_state == DATE_UNIVERSE_COMPLETE
    assert payload.registry.member_count == 2
    assert payload.registry.coverage_start == date(2026, 1, 5)
    assert payload.registry.coverage_end == date(2026, 1, 6)
    assert [row["trade_date"] for row in payload.member_rows] == [date(2026, 1, 5), date(2026, 1, 6)]
    assert payload.registry.date_set_sha256
    assert payload.exact_key_permit().missing([("000001.SZ", date(2026, 1, 7))])


def test_unknown_date_universe_cannot_contain_members():
    payload = build_date_universe_payload(
        code="000001.SZ",
        security_type="stock",
        identity_status="verified",
        source_kind="none",
        source_ref="none",
        source_sha256="none",
        dates=[],
        completeness_state=DATE_UNIVERSE_UNKNOWN,
    )
    assert payload.member_rows == ()
    with pytest.raises(DateUniverseContractError):
        payload.exact_key_permit().assert_allows(
            [("000001.SZ", date(2026, 1, 5))], operation="insert"
        )


def test_publisher_writes_members_before_registry_and_reads_back():
    payload = _payload()
    calls = []

    publisher = DateUniversePublisher(
        member_sink=lambda rows: calls.append(("members", len(rows))),
        registry_sink=lambda row: calls.append(("registry", row["dataset_id"])),
        readback=lambda received: calls.append(("readback", received.registry.member_count)),
    )
    published = publisher.publish(payload)

    assert published.dataset_id == payload.registry.dataset_id
    assert [item[0] for item in calls] == ["members", "registry", "readback"]


def test_reader_requires_active_complete_registry_and_exact_members():
    payload = _payload()
    registry = payload.registry.registry_row()
    members = list(payload.member_rows)
    reader = DailyDateUniverseReader(
        registry_loader=lambda codes: [registry],
        member_loader=lambda codes, keys: members,
    )
    permit = reader.exact_key_permit(
        [("000001.SZ", date(2026, 1, 5))], source="test-reader"
    )
    assert permit.keys == frozenset({("000001.SZ", date(2026, 1, 5))})
    assert permit.dataset_ids == frozenset({payload.registry.dataset_id})


def test_reader_fails_when_member_is_not_in_registry():
    payload = _payload()
    registry = payload.registry.registry_row()
    reader = DailyDateUniverseReader(
        registry_loader=lambda codes: [registry],
        member_loader=lambda codes, keys: [],
    )
    with pytest.raises(DateUniverseContractError, match="member readback missing"):
        reader.exact_key_permit(
            [("000001.SZ", date(2026, 1, 5))], source="test-reader"
        )


def test_clickhouse_reader_uses_readonly_selects():
    payload = _payload()

    class Result:
        def __init__(self, names, rows):
            self.column_names = names
            self.result_rows = rows

    class Client:
        def __init__(self):
            self.calls = []

        def query(self, sql, settings=None):
            self.calls.append((sql, settings))
            if "daily_date_universe_registry" in sql:
                row = payload.registry.registry_row()
                return Result(
                    ["dataset_id", "code", "completeness_state", "date_set_sha256", "is_active"],
                    [[row[key] for key in ["dataset_id", "code", "completeness_state", "date_set_sha256", "is_active"]]],
                )
            return Result(
                ["dataset_id", "code", "trade_date", "date_set_sha256"],
                [[
                    payload.registry.dataset_id,
                    "000001.SZ",
                    date(2026, 1, 5),
                    payload.registry.date_set_sha256,
                ]],
            )

    client = Client()
    reader = DailyDateUniverseReader.from_clickhouse(client)
    permit = reader.exact_key_permit(
        [("000001.SZ", date(2026, 1, 5))], source="test-clickhouse-reader"
    )

    assert permit.keys == frozenset({("000001.SZ", date(2026, 1, 5))})
    assert all(call[1] == {"readonly": 1} for call in client.calls)


def test_daily_dataframe_requires_exact_key_permit_and_rejects_outside_key():
    frame = pd.DataFrame(
        [{"code": "000001.sz", "trade_date": "2026-01-05", "close": 1.0}]
    )
    scope = make_daily_write_scope(
        source="test",
        current_stock_codes={"000001.SZ"},
        exact_key_permit=_payload().exact_key_permit(source="test-permit"),
    )
    validated = validate_daily_dataframe(frame, scope=scope, operation="insert")
    assert validated.iloc[0]["code"] == "000001.SZ"
    with pytest.raises(DailyWriteBlockedError, match="exact-key permit"):
        validate_daily_keys(
            [("000001.SZ", date(2026, 1, 7))], scope=scope, operation="insert"
        )


def test_daily_dataframe_without_permit_blocks_before_write():
    frame = pd.DataFrame(
        [{"code": "000001.SZ", "trade_date": "2026-01-05", "close": 1.0}]
    )
    scope = make_daily_write_scope(source="test", current_stock_codes={"000001.SZ"})
    with pytest.raises(DailyWriteBlockedError, match="exact-key permit"):
        validate_daily_dataframe(frame, scope=scope, operation="insert")


def test_row_digest_mismatch_blocks_permit_construction():
    payload = _payload()
    bad_member = dict(payload.member_rows[0])
    bad_member["date_set_sha256"] = "wrong"
    with pytest.raises(DateUniverseContractError, match="digest mismatch"):
        build_exact_key_permit_from_rows(
            [("000001.SZ", date(2026, 1, 5))],
            registry_rows=[payload.registry.registry_row()],
            member_rows=[bad_member],
            source="test",
        )


def test_daily_coverage_audit_records_present_suspension_conflict_and_gap():
    class Statuses:
        def decide(self, code, day):
            day = str(day)[:10]
            if day == "2026-01-06":
                return "exempt"
            if day == "2026-01-07":
                return "conflict"
            return "unregistered"

    rows = build_daily_coverage_audit(
        code="000001.SZ",
        expected_dates=["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"],
        formal_dates=["2026-01-05", "2026-01-08", "2026-01-09"],
        date_universe_id="dataset-1",
        date_set_sha256="date-sha",
        status_index=Statuses(),
    )

    states = {row.trade_date.isoformat(): row for row in rows}
    assert states["2026-01-05"].audit_state == AUDIT_FORMAL_PRESENT
    assert states["2026-01-06"].audit_state == AUDIT_SUSPENDED_VERIFIED
    assert states["2026-01-06"].minute_reference_required is False
    assert states["2026-01-07"].audit_state == AUDIT_STATUS_CONFLICT
    assert states["2026-01-08"].audit_state == AUDIT_FORMAL_PRESENT
    assert states["2026-01-09"].audit_state == AUDIT_FORMAL_OUTSIDE_UNIVERSE
    assert states["2026-01-07"].minute_reference_required is True
    assert states["2026-01-07"].coverage_start.isoformat() == "2026-01-05"
    assert states["2026-01-07"].coverage_end.isoformat() == "2026-01-08"
    assert any(row.audit_state == AUDIT_FORMAL_MISSING_UNCONFIRMED for row in rows) is False


def test_daily_coverage_audit_unregistered_gap_requires_minute_reference():
    class Statuses:
        def decide(self, code, day):
            return "unregistered"

    rows = build_daily_coverage_audit(
        code="000001.SZ",
        expected_dates=["2026-01-05"],
        formal_dates=[],
        date_universe_id="dataset-1",
        date_set_sha256="date-sha",
        status_index=Statuses(),
    )
    assert rows[0].audit_state == AUDIT_FORMAL_MISSING_UNCONFIRMED
    assert rows[0].minute_reference_required is True


def test_daily_coverage_audit_publisher_validates_keys_and_reads_back():
    class Statuses:
        def decide(self, code, day):
            return "unregistered"

    rows = build_daily_coverage_audit(
        code="000001.SZ",
        expected_dates=["2026-01-05"],
        formal_dates=[],
        date_universe_id="dataset-1",
        date_set_sha256="date-sha",
        status_index=Statuses(),
    )
    calls = []
    publisher = DailyCoverageAuditPublisher(
        audit_sink=lambda payload: calls.append(("sink", payload)),
        readback=lambda payload: calls.append(("readback", len(payload))),
    )
    assert publisher.publish(rows) == 1
    assert [item[0] for item in calls] == ["sink", "readback"]
    assert calls[0][1][0]["audit_state"] == AUDIT_FORMAL_MISSING_UNCONFIRMED


def test_formal_scope_summary_keeps_only_current_historical_and_core_categories():
    spans = summarize_formal_scope(
        [
            {"code": "000001.SZ", "trade_date": "2026-01-05"},
            {"code": "000001.SZ", "trade_date": "2026-01-06"},
            {"code": "000022.SZ", "trade_date": "2018-12-20"},
            {"code": "000001.SH", "trade_date": "1990-12-19"},
            {"code": "399999.SZ", "trade_date": "2026-01-05"},
        ],
        current_stock_codes={"000001.SZ"},
        historical_stock_codes={"000022.SZ"},
        core_index_codes={"000001.SH"},
    )
    by_code = {row.code: row for row in spans}
    assert by_code["000001.SZ"].scope_category == "current_stock"
    assert by_code["000022.SZ"].scope_category == "historical_stock"
    assert by_code["000001.SH"].scope_category == "core_index"
    assert by_code["399999.SZ"].scope_category == "out_of_scope"
    assert by_code["000001.SZ"].first_trade_date.isoformat() == "2026-01-05"
    assert by_code["000001.SZ"].last_trade_date.isoformat() == "2026-01-06"


def test_formal_scope_derives_master_delisted_rows_as_historical():
    scope = derive_formal_scope_sets(
        [
            {"code": "000001.SZ", "type": "stock", "quit": 0, "delist_date": None},
            {"code": "000002.SZ", "type": "stock", "quit": 1, "delist_date": "2024-01-05"},
            {"code": "000003.SZ", "type": "stock", "quit": 0, "delist_date": "2026-12-31"},
            {"code": "301569.SZ", "type": "stock", "quit": 0, "listing_status": "pending_listing", "delist_date": None},
            {"code": "000001.SH", "type": "index", "quit": 0, "delist_date": None},
        ],
        historical_candidate_codes={"835305.SZ"},
        core_index_codes={"000001.SH"},
        as_of="2026-10-23",
    )
    assert scope.current_stock_codes == frozenset({"000001.SZ", "000003.SZ"})
    assert scope.historical_stock_codes == frozenset({"000002.SZ", "835305.SZ"})
    assert scope.core_index_codes == frozenset({"000001.SH"})
    assert "301569.SZ" not in scope.allowed_codes

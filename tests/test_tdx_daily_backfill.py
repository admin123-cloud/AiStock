from datetime import date, datetime, timedelta
import csv
import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import multiprocessing

import pytest

from utils.daily_date_universe import (
    BUSINESS_TZ,
    DailyExactKeyPermit,
    DateUniverseContractError,
    build_date_universe_payload,
)

try:
    from utils.tdx_daily_backfill import (
        build_daily_insert_frame,
        build_tdx_daily_backfill_plan,
        member_rows_by_month,
        parse_windows_tcp_listener_addresses,
        rollback_sql_for_ids,
        stable_tdx_daily_id,
        validate_single_writer_deployment,
    )
except ImportError as import_error:  # Expected RED before the implementation exists.
    build_daily_insert_frame = None
    build_tdx_daily_backfill_plan = None
    member_rows_by_month = None
    parse_windows_tcp_listener_addresses = None
    member_rollback_sql_for_dataset_ids = None
    rollback_sql_for_ids = None
    stable_tdx_daily_id = None
    validate_single_writer_deployment = None
    status_conflicts_for_candidates = None
    validate_baostock_crosscheck = None
    validate_member_readback = None
    load_reviewed_tdx_candidate_bundle = None
    validate_tdx_formal_overlap = None
    validate_tdx_source_target_coverage = None
    rollback_sql_for_ids_created_at = None
    validate_owned_kline_rows_for_rollback = None
    TdxDailyBackfillApplyLock = None
    publish_tdx_date_universe = None
    validate_kline_daily_exact_readback = None
    execute_tdx_daily_apply = None
    build_tdx_rollback_registry_rows = None
    _IMPLEMENTATION_IMPORT_ERROR = import_error
else:
    _IMPLEMENTATION_IMPORT_ERROR = None
    import utils.tdx_daily_backfill as _backfill
    member_rollback_sql_for_dataset_ids = getattr(_backfill, "member_rollback_sql_for_dataset_ids", None)
    status_conflicts_for_candidates = getattr(_backfill, "status_conflicts_for_candidates", None)
    validate_baostock_crosscheck = getattr(_backfill, "validate_baostock_crosscheck", None)
    validate_member_readback = getattr(_backfill, "validate_member_readback", None)
    load_reviewed_tdx_candidate_bundle = getattr(_backfill, "load_reviewed_tdx_candidate_bundle", None)
    validate_tdx_formal_overlap = getattr(_backfill, "validate_tdx_formal_overlap", None)
    validate_tdx_source_target_coverage = getattr(_backfill, "validate_tdx_source_target_coverage", None)
    rollback_sql_for_ids_created_at = getattr(_backfill, "rollback_sql_for_ids_created_at", None)
    validate_owned_kline_rows_for_rollback = getattr(_backfill, "validate_owned_kline_rows_for_rollback", None)
    TdxDailyBackfillApplyLock = getattr(_backfill, "TdxDailyBackfillApplyLock", None)
    publish_tdx_date_universe = getattr(_backfill, "publish_tdx_date_universe", None)
    validate_kline_daily_exact_readback = getattr(_backfill, "validate_kline_daily_exact_readback", None)
    execute_tdx_daily_apply = getattr(_backfill, "execute_tdx_daily_apply", None)
    build_tdx_rollback_registry_rows = getattr(_backfill, "build_tdx_rollback_registry_rows", None)


def _require_implementation():
    assert build_tdx_daily_backfill_plan is not None, str(_IMPLEMENTATION_IMPORT_ERROR)


def _candidate(code="600000.SH", trade_day="2026-10-26", **overrides):
    row = {
        "code": code,
        "trade_date": trade_day,
        "open": "10.00",
        "high": "10.50",
        "low": "9.50",
        "close": "10.25",
        "volume": "1000",
        "amount": "10250",
        "amplitude": "10.0",
        "change_pct": "2.5",
        "change_amount": "0.25",
        "turnover_rate": "0",
        "prev_close_from_tdx": "10.00",
        "tdx_file": r"D:\TDX\vipdoc\sh\lday\sh600000.day",
        "tdx_file_sha256": "c" * 64,
        "source_volume_unit": "lots (TDX shares / 100)",
        "source_amount_unit": "yuan",
        "overlap_control_rows": "2",
    }
    row.update(overrides)
    return row


def _stored_row(code, trade_day, *, identity, close=10.25, created_at=None):
    return {
        "code": code,
        "trade_date": date.fromisoformat(trade_day),
        "open": 10.0,
        "high": 10.5,
        "low": 9.5,
        "close": close,
        "volume": 1000.0,
        "amount": 10250.0,
        "amplitude": 10.0,
        "change_pct": 2.5,
        "change_amount": 0.25,
        "turnover_rate": 0.0,
        "created_at": created_at or datetime(2026, 10, 31, 12, 0, 0),
        "id": identity,
    }


def _old_payload(code="600000.SH"):
    return build_date_universe_payload(
        code=code,
        security_type="stock",
        identity_status="verified",
        source_kind="accepted_test_source",
        source_ref=f"fixture://{code}",
        source_sha256="old-source-sha",
        dates=[date(2026, 10, 20), date(2026, 10, 21)],
        published_at=datetime(2026, 10, 22, 9, 0),
    )


def _build(rows, *, registry_rows=(), member_rows=(), existing_formal_keys=(), **kwargs):
    return build_tdx_daily_backfill_plan(
        rows,
        candidate_sha256="a" * 64,
        bundle_ref="bundle://tdx/manifest.json",
        active_stock_codes={row["code"] for row in rows},
        registry_rows=registry_rows,
        member_rows=member_rows,
        existing_formal_keys=existing_formal_keys,
        expected_candidate_rows=len(rows),
        expected_candidate_codes=len({row["code"] for row in rows}),
        expected_trade_dates={row["trade_date"] for row in rows},
        **kwargs,
    )


def test_plan_nonce_separates_registry_dataset_ids_for_concurrent_attempts():
    _require_implementation()
    row = _candidate()

    first = _build([row], plan_nonce="run-alpha")
    second = _build([row], plan_nonce="run-beta")

    assert first.dataset_ids != second.dataset_ids
    assert first.payloads[0].registry.source_ref.endswith("&run=run-alpha")
    assert second.payloads[0].registry.source_ref.endswith("&run=run-beta")


def test_plan_extends_a_valid_accepted_universe_without_losing_old_members():
    _require_implementation()
    old = _old_payload()

    plan = _build(
        [_candidate()],
        registry_rows=[old.registry.registry_row()],
        member_rows=list(old.member_rows),
    )

    assert len(plan.candidate_rows) == 1
    payload = plan.payloads[0]
    assert payload.registry.code == "600000.SH"
    assert payload.registry.source_kind == "tdx_local_daily_window_extension"
    assert payload.registry.member_count == 3
    assert [row["trade_date"] for row in payload.member_rows] == [
        date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 26)
    ]
    assert plan.retired_registry_rows[0]["is_active"] == 0
    assert plan.registry_rows[0]["is_active"] == 1
    assert plan.keys == frozenset({("600000.SH", date(2026, 10, 26))})


def test_plan_includes_verified_source_window_dates_that_are_already_formal():
    _require_implementation()
    row = _candidate()

    plan = build_tdx_daily_backfill_plan(
        [row],
        candidate_sha256="a" * 64,
        bundle_ref="bundle://tdx/manifest.json",
        active_stock_codes={row["code"]},
        registry_rows=[],
        member_rows=[],
        existing_formal_keys=[],
        expected_candidate_rows=1,
        expected_candidate_codes=1,
        expected_trade_dates={"2026-10-22", "2026-10-26"},
        member_dates_by_code={"600000.SH": ["2026-10-22", "2026-10-26"]},
    )

    assert plan.keys == frozenset({("600000.SH", date(2026, 10, 26))})
    assert [row["trade_date"] for row in plan.member_rows] == [
        date(2026, 10, 22), date(2026, 10, 26)
    ]


def test_plan_keeps_legacy_digest_mismatch_codes_blocked_but_processes_other_codes():
    _require_implementation()
    old = _old_payload("600000.SH")
    legacy_registry = old.registry.registry_row()
    legacy_registry["date_set_sha256"] = "f" * 64
    legacy_members = [dict(row, date_set_sha256="f" * 64) for row in old.member_rows]
    rows = [
        _candidate("600000.SH", "2026-10-26"),
        _candidate("000001.SZ", "2026-10-26", tdx_file=r"D:\TDX\vipdoc\sz\lday\sz000001.day"),
    ]

    plan = _build(rows, registry_rows=[legacy_registry], member_rows=legacy_members)

    assert [row["code"] for row in plan.candidate_rows] == ["000001.SZ"]
    assert plan.blocked_by_code == {"600000.SH": "active_member_digest_recompute_mismatch"}
    assert len(plan.payloads) == 1
    assert plan.payloads[0].registry.code == "000001.SZ"
    assert plan.retired_registry_rows == ()


def test_plan_rejects_candidates_that_are_no_longer_missing():
    _require_implementation()
    row = _candidate()

    with pytest.raises(DateUniverseContractError, match="already exists in formal snapshot"):
        _build([row], existing_formal_keys=[(row["code"], row["trade_date"])])


def test_plan_rejects_candidate_without_overlap_control():
    _require_implementation()

    with pytest.raises(DateUniverseContractError, match="overlap control"):
        _build([_candidate(overlap_control_rows="0")])


def test_plan_rejects_candidate_outside_current_active_stock_scope():
    _require_implementation()
    row = _candidate()

    with pytest.raises(DateUniverseContractError, match="outside current active stock scope"):
        build_tdx_daily_backfill_plan(
            [row],
            candidate_sha256="a" * 64,
            bundle_ref="bundle://tdx/manifest.json",
            active_stock_codes=set(),
            registry_rows=[],
            member_rows=[],
            existing_formal_keys=[],
            expected_candidate_rows=1,
            expected_candidate_codes=1,
            expected_trade_dates={row["trade_date"]},
        )


def test_daily_insert_frame_matches_tdx_importer_schema_and_stable_id():
    _require_implementation()
    from research.cross_strategy.import_local_tdx_daily_to_clickhouse import COLUMNS, _stable_id

    row = _candidate()
    plan = _build([row])
    created_at = datetime(2026, 10, 31, 12, 0, 0)

    frame = build_daily_insert_frame(plan, created_at=created_at)

    assert list(frame.columns) == COLUMNS
    assert frame.iloc[0]["trade_date"] == date(2026, 10, 26)
    assert frame.iloc[0]["volume"] == 1000.0
    assert frame.iloc[0]["amount"] == 10250.0
    assert frame.iloc[0]["created_at"] == created_at
    assert int(frame.iloc[0]["id"]) == _stable_id("600000.SH", date(2026, 10, 26))
    assert stable_tdx_daily_id("600000.SH", date(2026, 10, 26)) == int(frame.iloc[0]["id"])


def test_apply_attempt_namespace_separates_row_rollback_identity():
    _require_implementation()
    row = _candidate()
    plan = _build([row])
    created_at = datetime(2026, 10, 31, 12, 0, 0)

    first = build_daily_insert_frame(plan, created_at=created_at, attempt_nonce="attempt-a")
    second = build_daily_insert_frame(plan, created_at=created_at, attempt_nonce="attempt-b")

    first_id = int(first.iloc[0]["id"])
    second_id = int(second.iloc[0]["id"])
    assert first_id != second_id
    assert first_id == stable_tdx_daily_id("600000.SH", date(2026, 10, 26), attempt_nonce="attempt-a")


def test_member_rows_are_batched_by_month_partition():
    _require_implementation()
    rows = [
        _candidate("000001.SZ", "2026-09-30", tdx_file=r"D:\TDX\vipdoc\sz\lday\sz000001.day"),
        _candidate("000001.SZ", "2026-10-26", tdx_file=r"D:\TDX\vipdoc\sz\lday\sz000001.day"),
    ]
    plan = _build(rows)

    grouped = member_rows_by_month(plan)

    assert set(grouped) == {"202609", "202610"}
    assert sum(map(len, grouped.values())) == sum(len(payload.member_rows) for payload in plan.payloads)


def test_date_universe_publisher_reads_back_members_before_registry_activation(monkeypatch):
    _require_implementation()
    assert callable(publish_tdx_date_universe), "source-bound date-universe publisher is not implemented"
    monkeypatch.setattr(
        importlib.import_module("utils.tdx_daily_backfill"),
        "require_tdx_daily_distributed_lock",
        lambda: None,
    )
    old = _old_payload()
    plan = _build(
        [_candidate()],
        registry_rows=[old.registry.registry_row()],
        member_rows=list(old.member_rows),
    )
    events = []
    member_store = list(old.member_rows)
    registry_versions = [old.registry.registry_row()]

    def member_partition_sink(partition, rows):
        events.append(("members", partition))
        member_store.extend(dict(row) for row in rows)

    def member_loader(dataset_ids):
        events.append(("member_readback", tuple(dataset_ids)))
        wanted = set(dataset_ids)
        return [row for row in member_store if row["dataset_id"] in wanted]

    def retire_registry_sink(rows):
        events.append(("retire", len(rows)))
        registry_versions.extend(dict(row) for row in rows)

    def registry_sink(rows):
        events.append(("registry", len(rows)))
        registry_versions.extend(dict(row) for row in rows)

    def registry_loader(codes):
        events.append(("registry_readback", tuple(codes)))
        latest = {}
        for row in registry_versions:
            key = (row["code"], row["dataset_id"])
            if key not in latest or row["published_at"] > latest[key]["published_at"]:
                latest[key] = row
        return [row for row in latest.values() if row["code"] in set(codes)]

    result = publish_tdx_date_universe(
        plan,
        initial_registry_rows=[old.registry.registry_row()],
        member_partition_sink=member_partition_sink,
        member_loader=member_loader,
        retire_registry_sink=retire_registry_sink,
        registry_sink=registry_sink,
        registry_loader=registry_loader,
    )

    labels = [event[0] for event in events]
    assert max(i for i, label in enumerate(labels) if label == "members") < labels.index("member_readback")
    assert labels.index("member_readback") < labels.index("retire") < labels.index("registry") < labels.index("registry_readback")
    assert result["member_rows_verified"] == len(plan.member_rows)
    assert result["registry_rows_verified"] == len(plan.registry_rows)


def test_date_universe_publisher_requires_distributed_lock_before_any_sink():
    _require_implementation()
    plan = _build([_candidate()])
    events = []

    with pytest.raises(DateUniverseContractError, match="distributed apply lock"):
        publish_tdx_date_universe(
            plan,
            initial_registry_rows=(),
            member_partition_sink=lambda *args: events.append("members"),
            member_loader=lambda *args: events.append("member_readback") or [],
            retire_registry_sink=lambda *args: events.append("retire"),
            registry_sink=lambda *args: events.append("registry"),
            registry_loader=lambda *args: events.append("registry_readback") or [],
        )

    assert events == []


def test_kline_daily_exact_readback_closes_new_and_existing_keys():
    _require_implementation()
    assert callable(validate_kline_daily_exact_readback), "exact-key readback verifier is not implemented"
    old = _stored_row("000001.SZ", "2026-10-22", identity=202)
    new = _stored_row("600000.SH", "2026-10-26", identity=101)

    result = validate_kline_daily_exact_readback(
        [new], [old, new], [old, new], [old],
        existing_raw_counts={("000001.SZ", date(2026, 10, 22)): 1},
    )

    assert result["new_keys_verified"] == 1
    assert result["existing_keys_preserved"] == 1
    assert result["raw_rows_verified"] == 2


def test_kline_daily_exact_readback_rejects_duplicate_new_raw_key():
    _require_implementation()
    assert callable(validate_kline_daily_exact_readback), "exact-key readback verifier is not implemented"
    new = _stored_row("600000.SH", "2026-10-26", identity=101)

    with pytest.raises(DateUniverseContractError, match="raw duplicate exact key"):
        validate_kline_daily_exact_readback(
            [new], [new, new], [new], [], existing_raw_counts={}
        )


def test_kline_daily_exact_readback_rejects_changed_preexisting_bar():
    _require_implementation()
    assert callable(validate_kline_daily_exact_readback), "exact-key readback verifier is not implemented"
    old = _stored_row("000001.SZ", "2026-10-22", identity=202)
    changed_old = _stored_row("000001.SZ", "2026-10-22", identity=202, close=11.0)
    new = _stored_row("600000.SH", "2026-10-26", identity=101)

    with pytest.raises(DateUniverseContractError, match="preexisting formal row changed"):
        validate_kline_daily_exact_readback(
            [new], [changed_old, new], [changed_old, new], [old],
            existing_raw_counts={("000001.SZ", date(2026, 10, 22)): 1},
        )


def test_apply_executor_requires_exact_permit_before_append_and_verifies_afterward(monkeypatch):
    _require_implementation()
    assert callable(execute_tdx_daily_apply), "fail-closed apply executor is not implemented"
    monkeypatch.setattr(
        importlib.import_module("utils.tdx_daily_backfill"),
        "require_tdx_daily_distributed_lock",
        lambda: None,
    )
    plan = _build([_candidate()])
    events = []
    permit = DailyExactKeyPermit.from_keys(
        plan.keys,
        source="test:registry-readback",
        dataset_ids=plan.dataset_ids,
        date_set_digests={payload.registry.code: payload.registry.date_set_sha256 for payload in plan.payloads},
    )

    def preflight_check():
        events.append("preflight")

    def publish_registry():
        events.append("publish")

    def permit_builder():
        events.append("permit")
        return permit

    def append_formal_rows(observed_permit):
        events.append("append")
        observed_permit.assert_allows(plan.keys, operation="insert")
        return 1

    def verify_formal_rows():
        events.append("verify")
        return {"new_keys_verified": 1}

    def rollback(**kwargs):
        events.append(("rollback", kwargs))

    result = execute_tdx_daily_apply(
        plan,
        preflight_check=preflight_check,
        publish_registry=publish_registry,
        permit_builder=permit_builder,
        append_formal_rows=append_formal_rows,
        verify_formal_rows=verify_formal_rows,
        rollback=rollback,
    )

    assert events == ["preflight", "publish", "permit", "append", "verify"]
    assert result["verified"] is True
    assert result["permit_digest"] == permit.permit_digest


def test_apply_executor_requires_distributed_lock_before_any_callback():
    _require_implementation()
    plan = _build([_candidate()])
    events = []

    with pytest.raises(DateUniverseContractError, match="distributed apply lock"):
        execute_tdx_daily_apply(
            plan,
            preflight_check=lambda: events.append("preflight"),
            publish_registry=lambda: events.append("publish"),
            permit_builder=lambda: events.append("permit"),
            append_formal_rows=lambda *args: events.append("append"),
            verify_formal_rows=lambda: events.append("verify"),
            rollback=lambda **kwargs: events.append("rollback"),
        )

    assert events == []


def test_apply_executor_rolls_back_after_formal_readback_failure(monkeypatch):
    _require_implementation()
    assert callable(execute_tdx_daily_apply), "fail-closed apply executor is not implemented"
    monkeypatch.setattr(
        importlib.import_module("utils.tdx_daily_backfill"),
        "require_tdx_daily_distributed_lock",
        lambda: None,
    )
    plan = _build([_candidate()])
    rollback_calls = []
    permit = DailyExactKeyPermit.from_keys(
        plan.keys,
        source="test:registry-readback",
        dataset_ids=plan.dataset_ids,
        date_set_digests={payload.registry.code: payload.registry.date_set_sha256 for payload in plan.payloads},
    )

    def fail_verification():
        raise DateUniverseContractError("raw/FINAL readback mismatch")

    with pytest.raises(DateUniverseContractError, match="readback mismatch"):
        execute_tdx_daily_apply(
            plan,
            preflight_check=lambda: None,
            publish_registry=lambda: None,
            permit_builder=lambda: permit,
            append_formal_rows=lambda observed_permit: len(observed_permit.keys),
            verify_formal_rows=fail_verification,
            rollback=lambda **kwargs: rollback_calls.append(kwargs),
        )

    assert rollback_calls == [
        {
            "publication_started": True,
            "formal_write_started": True,
            "error": "raw/FINAL readback mismatch",
        }
    ]


def test_rollback_registry_rows_restore_old_version_after_newer_timestamp():
    _require_implementation()
    assert callable(build_tdx_rollback_registry_rows), "registry rollback payload builder is not implemented"
    old = _old_payload()
    plan = _build(
        [_candidate()],
        registry_rows=[old.registry.registry_row()],
        member_rows=list(old.member_rows),
    )
    plan_timestamp = plan.registry_rows[0]["published_at"]
    too_early = plan_timestamp.replace(year=2026, month=1, day=1)

    result = build_tdx_rollback_registry_rows(
        plan,
        active_new_dataset_ids=plan.dataset_ids,
        rollback_at=too_early,
    )

    assert result["rollback_at"] > plan_timestamp
    assert result["deactivate_new_rows"][0]["is_active"] == 0
    assert result["reactivate_old_rows"][0]["is_active"] == 1
    assert result["deactivate_new_rows"][0]["published_at"] == result["rollback_at"]
    assert result["reactivate_old_rows"][0]["published_at"] == result["rollback_at"]


def test_rollback_sql_targets_only_unique_stable_ids():
    _require_implementation()

    sql = rollback_sql_for_ids([101, 202])

    assert "ALTER TABLE stock.kline_daily DELETE WHERE id IN (101,202)" in sql
    assert "mutations_sync = 2" in sql
    assert "trade_date" not in sql
    assert "code" not in sql


def test_rollback_sql_rejects_duplicate_or_empty_id_sets():
    _require_implementation()

    with pytest.raises(ValueError, match="unique"):
        rollback_sql_for_ids([101, 101])
    with pytest.raises(ValueError, match="non-empty"):
        rollback_sql_for_ids([])


def test_rollback_sql_uses_insert_timestamp_as_second_identity_guard():
    _require_implementation()
    assert callable(rollback_sql_for_ids_created_at), "timestamp-scoped rollback is not implemented"

    sql = rollback_sql_for_ids_created_at(
        [202, 101], datetime(2026, 10, 31, 12, 30, 5)
    )

    assert "id IN (101,202)" in sql
    assert "created_at = toDateTime('2026-10-31 12:30:05')" in sql
    assert "mutations_sync = 2" in sql


def test_member_readback_requires_every_new_versioned_member_exactly_once():
    _require_implementation()
    plan = _build([_candidate()])

    assert validate_member_readback(plan, plan.member_rows) is None
    with pytest.raises(DateUniverseContractError, match="member readback mismatch"):
        validate_member_readback(plan, plan.member_rows[:-1])
    bad = [dict(row) for row in plan.member_rows]
    bad[0]["date_set_sha256"] = "f" * 64
    with pytest.raises(DateUniverseContractError, match="member readback mismatch"):
        validate_member_readback(plan, bad)


def test_baostock_sample_gate_requires_every_sample_to_match_and_trade():
    _require_implementation()
    results = [
        {
            "code": code,
            "trade_date": day,
            "tradestatus": "1",
            "overlap_match": True,
            "diffs": {},
        }
        for code in ("000001.SZ", "600000.SH")
        for day in ("2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30")
    ]
    report = {"checked": 14, "results": results}

    assert validate_baostock_crosscheck(report, expected_rows=14) is None
    report["results"][0]["tradestatus"] = "0"
    with pytest.raises(DateUniverseContractError, match="Baostock cross-check"):
        validate_baostock_crosscheck(report, expected_rows=14)


def test_baostock_crosscheck_rejects_an_empty_sample():
    _require_implementation()
    with pytest.raises(DateUniverseContractError, match="non-empty"):
        validate_baostock_crosscheck({"checked": 0, "results": []}, expected_rows=0)


def test_baostock_crosscheck_rejects_keys_outside_the_tdx_source_sample():
    _require_implementation()
    report = {
        "checked": 1,
        "results": [{
            "code": "000001.SZ", "trade_date": "2026-10-26", "tradestatus": "1",
            "overlap_match": True, "diffs": {},
        }],
    }
    allowed_keys = {("600000.SH", date(2026, 10, 26))}

    with pytest.raises(DateUniverseContractError, match="source sample"):
        validate_baostock_crosscheck(report, expected_rows=1, allowed_keys=allowed_keys)


def test_baostock_crosscheck_must_match_the_declared_sample_keys():
    _require_implementation()
    report = {
        "checked": 1,
        "results": [{
            "code": "000001.SZ", "trade_date": "2026-10-26", "tradestatus": "1",
            "overlap_match": True, "diffs": {},
        }],
    }
    allowed_keys = {("000001.SZ", date(2026, 10, 26)), ("600000.SH", date(2026, 10, 26))}
    expected_keys = {("600000.SH", date(2026, 10, 26))}

    with pytest.raises(DateUniverseContractError, match="declared sample keys"):
        validate_baostock_crosscheck(
            report, expected_rows=1, allowed_keys=allowed_keys, expected_keys=expected_keys
        )


def test_status_lookup_conflicts_block_a_positive_trade_candidate():
    _require_implementation()
    row = _candidate()
    candidate_key = (row["code"], date.fromisoformat(row["trade_date"]))
    status_rows = [
        {
            "code": row["code"],
            "start_date": row["trade_date"],
            "end_date": row["trade_date"],
            "status": "suspended",
            "evidence_url": "https://evidence.invalid/notice",
            "review_state": "verified",
        }
    ]

    conflicts = status_conflicts_for_candidates([row], status_rows)

    assert conflicts == [candidate_key]
    assert status_conflicts_for_candidates([row], []) == []


def test_member_rollback_is_scoped_to_immutable_new_dataset_ids():
    _require_implementation()

    sql = member_rollback_sql_for_dataset_ids(["b" * 32, "a" * 32])

    assert "ALTER TABLE stock.daily_date_universe_member DELETE WHERE dataset_id IN" in sql
    assert "'" + "a" * 32 + "'" in sql
    assert "'" + "b" * 32 + "'" in sql
    assert "mutations_sync = 2" in sql


def _write_test_bundle(bundle_dir):
    tdx_root = bundle_dir / "vipdoc"
    source_path = tdx_root / "sh" / "lday" / "sh600000.day"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"verified source file fixture")
    row = _candidate(
        tdx_file=str(source_path),
        tdx_file_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
    )
    candidate_path = bundle_dir / "tdx_daily_candidates_20261022_20261030.csv"
    with candidate_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    candidate_sha = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    crosscheck_path = bundle_dir / "tdx_baostock_crosscheck_20261022_20261030.json"
    crosscheck_path.write_text(
        json.dumps(
            {
                "checked": 1,
                "results": [
                    {
                        "code": row["code"],
                        "trade_date": row["trade_date"],
                        "tradestatus": "1",
                        "overlap_match": True,
                        "diffs": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "tdx_root": str(tdx_root),
                "target_trade_dates": [row["trade_date"]],
                "candidate_csv_rows": 1,
                "candidate_validation": {
                    "rows": 1,
                    "candidate_codes": 1,
                    "unique_code_date_keys": 1,
                    "candidate_csv_sha256": candidate_sha,
                    "invalid_rows": {},
                },
                "candidate_gate": {
                    "append_only_missing_keys": True,
                    "positive_volume_and_amount_required": True,
                    "valid_ohlc_envelope_required": True,
                    "at_least_one_formal_overlap_per_code": True,
                    "all_overlap_rows_within_tolerance": True,
                    "candidate_rows_after_gates": 1,
                    "candidate_codes_after_gates": 1,
                    "gates_passed": True,
                },
                "overlap_validation": {"mismatch_fields": {}},
                "independent_baostock_crosscheck": {
                    "records_checked": 1,
                    "records_within_existing_tolerances": 1,
                    "all_tradestatus_trading": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return source_path, candidate_sha


def test_bundle_loader_binds_candidate_to_manifest_and_source_hashes(tmp_path):
    _require_implementation()
    assert callable(load_reviewed_tdx_candidate_bundle), "bundle loader is not implemented"
    source_path, candidate_sha = _write_test_bundle(tmp_path)

    loaded = load_reviewed_tdx_candidate_bundle(
        tmp_path, expected_candidate_sha256=candidate_sha, verify_source_files=True
    )

    assert loaded["candidate_sha256"] == candidate_sha
    assert loaded["candidate_rows"][0]["code"] == "600000.SH"
    assert loaded["target_trade_dates"] == {date(2026, 10, 26)}
    source_path.write_bytes(b"source changed after candidate review")
    with pytest.raises(DateUniverseContractError, match="source file SHA-256 mismatch"):
        load_reviewed_tdx_candidate_bundle(
            tmp_path, expected_candidate_sha256=candidate_sha, verify_source_files=True
        )


def test_bundle_loader_rejects_candidate_manifest_hash_mismatch(tmp_path):
    _require_implementation()
    assert callable(load_reviewed_tdx_candidate_bundle), "bundle loader is not implemented"
    _write_test_bundle(tmp_path)

    with pytest.raises(DateUniverseContractError, match="candidate CSV SHA-256 mismatch"):
        load_reviewed_tdx_candidate_bundle(
            tmp_path, expected_candidate_sha256="b" * 64, verify_source_files=True
        )


def test_bundle_loader_can_pin_independent_crosscheck_bytes(tmp_path):
    _require_implementation()
    assert callable(load_reviewed_tdx_candidate_bundle), "bundle loader is not implemented"
    _write_test_bundle(tmp_path)

    with pytest.raises(DateUniverseContractError, match="Baostock cross-check SHA-256 mismatch"):
        load_reviewed_tdx_candidate_bundle(
            tmp_path, expected_crosscheck_sha256="d" * 64, verify_source_files=True
        )


def test_tdx_formal_overlap_is_recomputed_and_bound_to_each_candidate_code():
    _require_implementation()
    assert callable(validate_tdx_formal_overlap), "formal overlap verifier is not implemented"
    candidate = _candidate(overlap_control_rows="1")
    source = {
        "code": "600000.SH",
        "trade_date": "2026-10-20",
        "open": 10.0,
        "high": 10.5,
        "low": 9.5,
        "close": 10.25,
        "volume": 1000,
        "amount": 10250,
    }
    formal = dict(source)

    result = validate_tdx_formal_overlap([candidate], [source], [formal])

    assert result["overlap_rows"] == 1
    assert result["overlap_codes"] == 1
    formal["amount"] = 9000
    with pytest.raises(DateUniverseContractError, match="overlap mismatch"):
        validate_tdx_formal_overlap([candidate], [source], [formal])


def test_tdx_source_window_members_are_exactly_candidates_or_existing_formal_rows():
    _require_implementation()
    assert callable(validate_tdx_source_target_coverage), "source-window verifier is not implemented"
    candidate = _candidate()
    source_rows = [
        {
            "code": "600000.SH", "trade_date": "2026-10-22",
            "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.25,
            "volume": 1000.0, "amount": 10250.0, "prev_close": 10.0,
        },
        {
            "code": "600000.SH", "trade_date": "2026-10-26",
            "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.25,
            "volume": 1000.0, "amount": 10250.0, "prev_close": 10.0,
        },
    ]
    formal_rows = [dict(source_rows[0])]

    result = validate_tdx_source_target_coverage(
        [candidate], source_rows, formal_rows,
        target_trade_dates={"2026-10-22", "2026-10-26"},
    )

    assert result["member_dates_by_code"] == {
        "600000.SH": (date(2026, 10, 22), date(2026, 10, 26))
    }
    with pytest.raises(DateUniverseContractError, match="unaccounted TDX target key"):
        validate_tdx_source_target_coverage(
            [candidate], source_rows, [],
            target_trade_dates={"2026-10-22", "2026-10-26"},
        )


def test_source_window_marks_a_missing_target_session_unverified():
    _require_implementation()
    candidate = _candidate("600000.SH", "2026-10-26")
    source_rows = [{
        "code": "600000.SH", "trade_date": "2026-10-26",
        "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.25,
        "volume": 1000.0, "amount": 10250.0, "prev_close": 10.0,
    }]

    result = validate_tdx_source_target_coverage(
        [candidate], source_rows, [],
        target_trade_dates={"2026-10-26", "2026-10-27"},
        expected_dates_by_code={"600000.SH": {"2026-10-26", "2026-10-27"}},
        status_rows=[],
    )

    assert result["incomplete_codes"] == {"600000.SH": (date(2026, 10, 27),)}
    assert result["verified_nontrading_keys"] == ()


def test_plan_blocks_all_candidate_rows_for_an_incomplete_source_window_code():
    _require_implementation()
    rows = [
        _candidate("600000.SH", "2026-10-26"),
        _candidate(
            "000001.SZ", "2026-10-26",
            tdx_file=r"D:\\TDX\\vipdoc\\sz\\lday\\sz000001.day",
        ),
    ]

    plan = _build(
        rows,
        source_blocked_codes={"600000.SH": "incomplete_source_target_window"},
    )

    assert [row["code"] for row in plan.candidate_rows] == ["000001.SZ"]
    assert [row["code"] for row in plan.blocked_candidate_rows] == ["600000.SH"]
    assert plan.blocked_by_code == {"600000.SH": "incomplete_source_target_window"}


def test_source_window_accepts_missing_target_session_only_with_verified_status():
    _require_implementation()
    candidate = _candidate("600000.SH", "2026-10-26")
    source_rows = [{
        "code": "600000.SH", "trade_date": "2026-10-26",
        "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.25,
        "volume": 1000.0, "amount": 10250.0, "prev_close": 10.0,
    }]
    verified_status = [{
        "code": "600000.SH", "start_date": "2026-10-27", "end_date": "2026-10-27",
        "status": "suspended", "evidence_url": "https://evidence.invalid/notice",
        "reason": "verified suspension", "review_state": "verified", "evidence_json": "{}",
    }]

    result = validate_tdx_source_target_coverage(
        [candidate], source_rows, [],
        target_trade_dates={"2026-10-26", "2026-10-27"},
        expected_dates_by_code={"600000.SH": {"2026-10-26", "2026-10-27"}},
        status_rows=verified_status,
    )

    assert result["incomplete_codes"] == {}
    assert result["verified_nontrading_keys"] == (("600000.SH", date(2026, 10, 27)),)


def test_rollback_ownership_ignores_a_foreign_id_for_the_same_exact_key():
    _require_implementation()
    assert validate_owned_kline_rows_for_rollback is not None, "rollback ownership verifier is missing"
    created_at = datetime(2026, 10, 31, 12, 0, 0)
    expected = [{
        "id": 11, "code": "600000.SH", "trade_date": date(2026, 10, 26),
        "created_at": created_at,
    }]
    observed = [
        dict(expected[0]),
        {
            "id": 12, "code": "600000.SH", "trade_date": date(2026, 10, 26),
            "created_at": created_at,
        },
    ]

    owned = validate_owned_kline_rows_for_rollback(expected, observed, created_at=created_at)

    assert [int(row["id"]) for row in owned] == [11]


def test_rollback_ownership_rejects_a_planned_id_with_a_different_timestamp():
    _require_implementation()
    assert validate_owned_kline_rows_for_rollback is not None, "rollback ownership verifier is missing"
    created_at = datetime(2026, 10, 31, 12, 0, 0)
    expected = [{
        "id": 11, "code": "600000.SH", "trade_date": date(2026, 10, 26),
        "created_at": created_at,
    }]
    observed = [{
        "id": 11, "code": "600000.SH", "trade_date": date(2026, 10, 26),
        "created_at": datetime(2026, 10, 31, 12, 0, 1),
    }]

    with pytest.raises(DateUniverseContractError, match="rollback ownership"):
        validate_owned_kline_rows_for_rollback(expected, observed, created_at=created_at)


def _hold_tdx_backfill_lock_in_child(path, started, release):
    from utils.tdx_daily_backfill import TdxDailyBackfillApplyLock as Lock

    with Lock(path):
        started.set()
        release.wait(15)


def test_apply_file_lock_rejects_a_second_owner(tmp_path):
    _require_implementation()
    assert TdxDailyBackfillApplyLock is not None, "apply lock implementation is missing"
    path = tmp_path / "daily-write.lock"
    second = TdxDailyBackfillApplyLock(path)

    with TdxDailyBackfillApplyLock(path):
        with pytest.raises(DateUniverseContractError, match="lock is already held"):
            second.acquire()

    with TdxDailyBackfillApplyLock(path):
        pass


def test_apply_file_lock_rejects_a_separate_runner_process(tmp_path):
    _require_implementation()
    assert TdxDailyBackfillApplyLock is not None, "apply lock implementation is missing"
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    release = context.Event()
    path = str(tmp_path / "daily-write-cross-process.lock")
    process = context.Process(
        target=_hold_tdx_backfill_lock_in_child,
        args=(path, started, release),
    )
    process.start()
    try:
        assert started.wait(10), "child process failed to acquire apply lock"
        with pytest.raises(DateUniverseContractError, match="lock is already held"):
            TdxDailyBackfillApplyLock(path).acquire()
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_single_writer_deployment_gate_accepts_a_loopback_only_clickhouse():
    _require_implementation()
    gate = validate_single_writer_deployment
    assert callable(gate), "single-writer deployment gate is missing"

    result = gate(
        clickhouse_url="http://127.0.0.1:8123",
        default_cluster_rows=[{
            "cluster": "default", "host_name": "localhost", "port": 9000, "is_local": 1,
        }],
        listener_addresses=["127.0.0.1"],
    )

    assert result["mode"] == "single_writer_local"
    assert result["endpoint_host"] == "127.0.0.1"
    assert result["endpoint_port"] == 8123


def test_single_writer_deployment_gate_rejects_a_remote_clickhouse_endpoint():
    _require_implementation()
    gate = validate_single_writer_deployment
    assert callable(gate), "single-writer deployment gate is missing"

    with pytest.raises(DateUniverseContractError, match="not loopback-only"):
        gate(
            clickhouse_url="http://10.20.30.40:8123",
            default_cluster_rows=[{
                "cluster": "default", "host_name": "localhost", "port": 9000, "is_local": 1,
            }],
            listener_addresses=["127.0.0.1"],
        )


def test_single_writer_deployment_gate_rejects_a_multihost_default_cluster():
    _require_implementation()
    gate = validate_single_writer_deployment
    assert callable(gate), "single-writer deployment gate is missing"

    rows = [
        {"cluster": "default", "host_name": "localhost", "port": 9000, "is_local": 1},
        {"cluster": "default", "host_name": "10.20.30.40", "port": 9000, "is_local": 0},
    ]
    with pytest.raises(DateUniverseContractError, match="exactly one local server"):
        gate(
            clickhouse_url="http://127.0.0.1:8123",
            default_cluster_rows=rows,
            listener_addresses=["127.0.0.1"],
        )


def test_single_writer_deployment_gate_rejects_a_wildcard_listener():
    _require_implementation()
    gate = validate_single_writer_deployment
    assert callable(gate), "single-writer deployment gate is missing"

    with pytest.raises(DateUniverseContractError, match="reachable beyond loopback"):
        gate(
            clickhouse_url="http://127.0.0.1:8123",
            default_cluster_rows=[{
                "cluster": "default", "host_name": "localhost", "port": 9000, "is_local": 1,
            }],
            listener_addresses=["0.0.0.0"],
        )


def test_windows_netstat_parser_returns_only_listeners_for_the_clickhouse_http_port():
    _require_implementation()
    parser = parse_windows_tcp_listener_addresses
    assert callable(parser), "Windows listener parser is missing"

    output = """\
  TCP    127.0.0.1:8123         0.0.0.0:0              LISTENING       26440
  TCP    127.0.0.1:9000         0.0.0.0:0              LISTENING       26440
  TCP    0.0.0.0:8123           0.0.0.0:0              ESTABLISHED     26440
  TCP    [::1]:8123             [::]:0                 LISTENING       26440
"""

    assert parser(output, 8123) == ("127.0.0.1", "::1")


def _load_tdx_apply_runner_for_test():
    script = Path(__file__).resolve().parents[1] / "scripts" / "apply_tdx_daily_backfill_bundle_20261031.py"
    spec = importlib.util.spec_from_file_location("tdx_daily_backfill_apply_runner_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_checks_local_windows_listener_and_default_cluster_before_preflight(monkeypatch):
    runner = _load_tdx_apply_runner_for_test()
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    cluster_rows = [{
        "cluster": "default", "host_name": "localhost", "port": 9000, "is_local": 1,
    }]
    monkeypatch.setattr(runner, "_dict_rows", lambda client, sql: cluster_rows)
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            stdout="TCP 127.0.0.1:8123 0.0.0.0:0 LISTENING 26440\\n",
            returncode=0,
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    class Client:
        url = "http://127.0.0.1:8123"

    result = runner._single_writer_topology(Client())

    assert result["mode"] == "single_writer_local"
    assert result["listener_addresses"] == ("127.0.0.1",)
    assert calls[0][0] == ["netstat", "-ano", "-p", "tcp"]
    assert calls[0][1]["check"] is True
    assert calls[0][1]["timeout"] <= 10


def test_runner_identity_reads_effective_principal_readonly_setting():
    runner = _load_tdx_apply_runner_for_test()

    class Client:
        def __init__(self):
            self.settings: dict = {}

        def query(self, sql, *, settings):
            self.settings = dict(settings)
            readonly = self.settings.get("readonly", 0)
            return SimpleNamespace(
                column_names=["user", "readonly", "version", "timezone", "database", "server_now"],
                result_rows=[("default", readonly, "24.8.14.39", "UTC", "stock", "2026-10-31 00:00:00")],
            )

    client = Client()
    identity = runner._identity(client)

    assert "readonly" not in client.settings
    assert identity["readonly"] == 0


def test_runner_apply_fails_closed_when_the_distributed_lock_is_held(
    monkeypatch, tmp_path, capsys
):
    from tests.test_tdx_daily_lock_provider import _FakeLockClickHouse

    runner = _load_tdx_apply_runner_for_test()
    fake_client = _FakeLockClickHouse(
        holder="another-writer",
        expires=datetime.now(BUSINESS_TZ) + timedelta(hours=1),
    )
    monkeypatch.setattr(runner, "clickhouse_client", lambda: fake_client)
    monkeypatch.setattr(
        runner, "runtime_path", lambda *parts: tmp_path.joinpath(*parts)
    )

    result = runner.main([
        "--bundle", str(tmp_path),
        "--apply",
        "--plan-nonce", "blockwhenheld",
        "--expected-candidate-sha256", "a" * 64,
        "--expected-crosscheck-sha256", "b" * 64,
        "--expected-plan-sha256", "c" * 64,
        "--expected-write-rows", "1",
        "--expected-blocked-rows", "0",
        "--expected-payload-codes", "1",
    ])

    output = json.loads(capsys.readouterr().out)
    assert result == 2
    assert output["status"] == "failed_closed"
    assert output["write_performed"] is False
    assert output["database_final_state"] == "unchanged"
    assert "database-scoped distributed apply lock is already held" in output["error"]
    # the held row was never mutated by the rejected claimant
    assert fake_client.row["holder_id"] == "another-writer"


def test_runner_internal_apply_fails_closed_before_context_or_journal_access(tmp_path):
    runner = _load_tdx_apply_runner_for_test()
    journal_path = tmp_path / "journal.json"

    with pytest.raises(DateUniverseContractError, match="distributed apply lock"):
        runner._apply(None, {}, None, "nonce", journal_path)

    assert not journal_path.exists()


def test_runner_rollback_fails_closed_before_client_or_context_access():
    runner = _load_tdx_apply_runner_for_test()
    with pytest.raises(DateUniverseContractError, match="distributed apply lock"):
        runner._rollback(
            None,
            {},
            lambda *args, **kwargs: None,
            publication_started=True,
            formal_write_started=True,
            error="test",
        )


def test_runner_schema_inspection_rejects_missing_code_check_before_apply(monkeypatch):
    runner = _load_tdx_apply_runner_for_test()
    from utils.clickhouse_schema import MARKET_TABLE_DDL

    ddl_by_table = {}
    for table_name in runner.EXPECTED_TABLES:
        ddl_by_table[table_name] = next(
            (
                statement
                for statement in MARKET_TABLE_DDL
                if re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(table_name)}\b", statement, re.IGNORECASE)
            ),
            "",
        )
    ddl_by_table["kline_daily"] = "\n".join(
        line for line in ddl_by_table["kline_daily"].splitlines()
        if "codex_no_neeq_legacy_bj_code" not in line
    )
    table_rows = []
    for table_name, (engine, partition_key, sorting_key, engine_full_prefix) in runner.EXPECTED_TABLES.items():
        table_rows.append({
            "name": table_name,
            "engine": engine,
            "engine_full": engine_full_prefix,
            "partition_key": partition_key,
            "sorting_key": sorting_key,
            "primary_key": sorting_key,
            # system.tables.create_table_query renders string literals with
            # backslashes doubled
            "create_table_query": ddl_by_table[table_name].replace("\\", "\\\\"),
        })
    column_rows = [
        {"table": table_name, "name": column_name, "type": column_type}
        for table_name, columns in runner.EXPECTED_COLUMNS.items()
        for column_name, column_type in columns.items()
    ]

    def fake_dict_rows(_client, sql):
        return table_rows if "FROM system.tables" in sql else column_rows

    monkeypatch.setattr(runner, "_dict_rows", fake_dict_rows)

    with pytest.raises(RuntimeError, match="codex_no_neeq_legacy_bj_code"):
        runner._inspect_schema(object())


def test_runner_formal_append_column_contract_requires_exact_tuple():
    runner = _load_tdx_apply_runner_for_test()
    expected = tuple(runner.KLINE_DAILY_TDX_COLUMNS)

    runner._assert_formal_append_columns(SimpleNamespace(columns=expected))

    with pytest.raises(DateUniverseContractError, match="formal append columns do not match exact TDX contract"):
        runner._assert_formal_append_columns(SimpleNamespace(columns=expected[:-1] + ("unexpected",)))


def test_runner_raw_insert_helper_requires_lock_before_client_insert():
    runner = _load_tdx_apply_runner_for_test()

    class Client:
        insert_calls = []

        def insert(self, *args, **kwargs):
            self.insert_calls.append((args, kwargs))

    client = Client()
    with pytest.raises(DateUniverseContractError, match="distributed apply lock"):
        runner._insert_dict_rows(
            client,
            "stock.kline_daily",
            [{"code": "600000.SH"}],
            ["code"],
        )

    assert client.insert_calls == []


def test_runner_raw_insert_helper_restricts_destination_and_columns(monkeypatch):
    runner = _load_tdx_apply_runner_for_test()
    monkeypatch.setattr(runner, "require_tdx_daily_distributed_lock", lambda: None)

    class Client:
        insert_calls = []

        def insert(self, *args, **kwargs):
            self.insert_calls.append((args, kwargs))

    client = Client()
    with pytest.raises(DateUniverseContractError, match="not allowlisted"):
        runner._insert_dict_rows(
            client,
            "stock.kline_daily",
            [{"code": "600000.SH"}],
            ["code"],
        )
    with pytest.raises(DateUniverseContractError, match="columns do not match"):
        runner._insert_dict_rows(
            client,
            "stock.daily_date_universe_registry",
            [{"code": "600000.SH"}],
            ["code"],
        )

    assert client.insert_calls == []


def test_runner_raw_insert_helper_rejects_surplus_row_keys(monkeypatch):
    runner = _load_tdx_apply_runner_for_test()
    monkeypatch.setattr(runner, "require_tdx_daily_distributed_lock", lambda: None)

    class Client:
        insert_calls = []

        def insert(self, *args, **kwargs):
            self.insert_calls.append((args, kwargs))

    client = Client()
    columns = list(runner.REGISTRY_COLUMNS)
    row = {column: "value" for column in columns}
    row["unexpected_column"] = "not_allowlisted"

    with pytest.raises(DateUniverseContractError, match="non-allowlisted"):
        runner._insert_dict_rows(
            client,
            "stock.daily_date_universe_registry",
            [row],
            columns,
        )

    assert client.insert_calls == []


def test_runner_stocks_schema_contract_matches_bootstrap_and_startup():
    runner = _load_tdx_apply_runner_for_test()
    from utils.clickhouse_schema import MARKET_TABLE_DDL, STARTUP_TABLE_CONTRACTS

    stocks_ddl = next(
        statement
        for statement in MARKET_TABLE_DDL
        if re.search(r"CREATE TABLE IF NOT EXISTS stocks\s", statement, re.IGNORECASE)
    )
    startup_contract = next(
        contract for contract in STARTUP_TABLE_CONTRACTS if contract.name == "stocks"
    )
    expected_columns = runner.EXPECTED_COLUMNS["stocks"]

    assert dict(startup_contract.required_column_types) == expected_columns
    assert runner.EXPECTED_TABLES["stocks"][3] == "ReplacingMergeTree(updated_at) ORDER BY code"
    for column, column_type in expected_columns.items():
        pattern = rf"(?m)^\s*{re.escape(column)}\s+{re.escape(column_type)}(?:\s|,|$)"
        assert re.search(pattern, stocks_ddl), f"bootstrap DDL disagrees on {column}: {column_type}"


def test_runner_kline_schema_contract_matches_bootstrap_ddl():
    runner = _load_tdx_apply_runner_for_test()
    from utils.clickhouse_schema import (
        KLINE_DAILY_ENGINE_FULL_PREFIX,
        KLINE_DAILY_REQUIRED_COLUMN_TYPES,
        MARKET_TABLE_DDL,
        STARTUP_TABLE_CONTRACTS,
    )

    daily_ddl = next(
        statement
        for statement in MARKET_TABLE_DDL
        if re.search(r"CREATE TABLE IF NOT EXISTS kline_daily\s", statement, re.IGNORECASE)
    )
    engine, partition_key, sorting_key, engine_prefix = runner.EXPECTED_TABLES["kline_daily"]
    startup_contract = next(
        contract for contract in STARTUP_TABLE_CONTRACTS if contract.name == "kline_daily"
    )
    assert engine.lower().startswith(startup_contract.engine_prefix.lower())
    assert partition_key == startup_contract.partition_key
    normalize_sorting_key = lambda value: value.replace(" ", "").strip("()").lower()
    assert normalize_sorting_key(sorting_key) == normalize_sorting_key(startup_contract.sorting_key)
    assert engine_prefix.lower().startswith(startup_contract.engine_prefix.lower())
    assert startup_contract.engine_full_prefix is not None
    assert engine_prefix.lower() == startup_contract.engine_full_prefix.lower()
    assert engine_prefix.lower() == KLINE_DAILY_ENGINE_FULL_PREFIX.lower()
    assert startup_contract.required_columns == frozenset(runner.EXPECTED_COLUMNS["kline_daily"])
    assert dict(startup_contract.required_column_types) == runner.EXPECTED_COLUMNS["kline_daily"]
    assert dict(KLINE_DAILY_REQUIRED_COLUMN_TYPES) == runner.EXPECTED_COLUMNS["kline_daily"]

    for column, column_type in runner.EXPECTED_COLUMNS["kline_daily"].items():
        pattern = rf"(?m)^\s*{re.escape(column)}\s+{re.escape(column_type)}(?:\s|,|$)"
        assert re.search(pattern, daily_ddl), f"bootstrap DDL disagrees on {column}: {column_type}"

    normalized_ddl = " ".join(daily_ddl.split()).lower()
    expected_engine_layout = (
        f"engine = {engine_prefix.lower()} partition by {partition_key.lower()} "
        f"order by ({sorting_key.lower()})"
    )
    assert expected_engine_layout in normalized_ddl

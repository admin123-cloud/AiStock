from __future__ import annotations

import gzip
import json
from datetime import date, timedelta

import pytest

from scripts.dry_run_index_schema_migration import _payload_checks, _payload_stats, _read_live_schema, build_plan
from utils.daily_date_universe import date_set_sha256


class _Result:
    def __init__(self, names, rows):
        self.column_names = names
        self.result_rows = rows


class _ReadonlyClient:
    def __init__(
        self,
        *,
        include_registry=False,
        missing_columns=(),
        type_overrides=None,
        first_trade_date="2020-01-01",
        row_count=2,
        unique_keys=2,
        unique_ids=2,
        null_created_at=1,
        nonnull_created_at=1,
        forbidden_neeq_rows=0,
        engine="ReplacingMergeTree",
        engine_full="ReplacingMergeTree ORDER BY (code, trade_date)",
        partition_key="",
        sorting_key="code, trade_date",
    ):
        self.include_registry = include_registry
        self.missing_columns = set(missing_columns)
        self.type_overrides = dict(type_overrides or {})
        self.first_trade_date = first_trade_date
        self.row_count = row_count
        self.unique_keys = unique_keys
        self.unique_ids = unique_ids
        self.null_created_at = null_created_at
        self.nonnull_created_at = nonnull_created_at
        self.forbidden_neeq_rows = forbidden_neeq_rows
        self.engine = engine
        self.engine_full = engine_full
        self.partition_key = partition_key
        self.sorting_key = sorting_key
        self.queries = []
        self.query_count = 0

    def query(self, sql, settings=None):
        self.queries.append((sql, settings))
        self.query_count += 1
        if "FROM system.tables" in sql and "database='stock'" in sql and "name IN" in sql:
            rows = [
                ("stock", "kline_daily", self.engine, self.engine_full, self.partition_key, self.sorting_key, self.sorting_key, "CREATE TABLE stock.kline_daily ..."),
            ]
            if self.include_registry:
                rows.extend(
                    [
                        ("stock", "daily_date_universe_registry", "ReplacingMergeTree", "ReplacingMergeTree(published_at)", "", "code, dataset_id", "code, dataset_id", ""),
                        ("stock", "daily_date_universe_member", "MergeTree", "MergeTree", "toYYYYMM(trade_date)", "dataset_id, code, trade_date", "dataset_id, code, trade_date", ""),
                    ]
                )
            return _Result(["database", "name", "engine", "engine_full", "partition_key", "sorting_key", "primary_key", "create_table_query"], rows)
        if "FROM system.columns" in sql:
            column_types = {
                "code": "String", "trade_date": "Date", "open": "Float64", "high": "Float64",
                "low": "Float64", "close": "Float64", "volume": "Float64", "amount": "Float64",
                "amplitude": "Float64", "change_pct": "Float64", "change_amount": "Float64",
                "turnover_rate": "Float64", "created_at": "Nullable(DateTime)", "id": "UInt64",
            }
            rows = [
                ("stock", "kline_daily", name, self.type_overrides.get(name, type_name), "", "", 0, 1, 1)
                for name, type_name in column_types.items()
                if name not in self.missing_columns
            ]
            return _Result(["database", "table", "name", "type", "default_kind", "default_expression", "is_in_partition_key", "is_in_sorting_key", "is_in_primary_key"], rows)
        if "FROM system.parts" in sql:
            return _Result(["database", "table", "partition", "active_parts", "rows", "bytes", "min_date", "max_date"], [("stock", "kline_daily", "tuple()", 1, 2, 100, "1970-01-01", "2026-10-21")])
        if "lower(name) LIKE" in sql:
            return _Result(["database", "name", "engine", "partition_key", "sorting_key"], [])
        if "uniqExact(toYYYYMM" in sql:
            return _Result(["distinct_month_partitions"], [(2,)])
        if "uniqExact(id)" in sql:
            return _Result(
                ["rows", "unique_keys", "unique_ids", "null_created_at", "nonnull_created_at", "first_trade_date", "last_trade_date", "target_neeq_rejected_rows"],
                [(self.row_count, self.unique_keys, self.unique_ids, self.null_created_at, self.nonnull_created_at, self.first_trade_date, "2026-10-21", self.forbidden_neeq_rows)],
            )
        raise AssertionError(f"unexpected query: {sql}")


def test_read_live_schema_rejects_wrong_engine_version_and_sorting_key():
    target_partition = "toYYYYMM(trade_date)"
    valid_engine_full = "ReplacingMergeTree(created_at) PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)"
    cases = (
        (
            _ReadonlyClient(
                include_registry=True,
                engine="MergeTree",
                engine_full="MergeTree PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)",
                partition_key=target_partition,
            ),
            "kline_daily:engine_incompatible",
        ),
        (
            _ReadonlyClient(
                include_registry=True,
                engine_full="ReplacingMergeTree(id) PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)",
                partition_key=target_partition,
            ),
            "kline_daily:version_expression_incompatible",
        ),
        (
            _ReadonlyClient(
                include_registry=True,
                engine_full=valid_engine_full.replace("(code, trade_date)", "code"),
                partition_key=target_partition,
                sorting_key="code",
            ),
            "kline_daily:sorting_key_incompatible",
        ),
    )

    for client, expected_problem in cases:
        schema = _read_live_schema(client)
        assert schema["status"] == "blocked"
        assert expected_problem in schema["problems"]


def _write_gz(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _registry_row(code="000001.SZ", *, dataset_id="d", dates=("2020-01-01",)):
    dates = tuple(dates)
    return {
        "dataset_id": dataset_id,
        "code": code,
        "security_type": "stock",
        "identity_status": "verified",
        "source_kind": "tdx",
        "source_ref": "test-bundle",
        "source_sha256": "a" * 64,
        "coverage_start": dates[0] if dates else None,
        "coverage_end": dates[-1] if dates else None,
        "completeness_state": "complete" if dates else "unknown",
        "date_set_sha256": date_set_sha256(code, dates),
        "member_count": len(dates),
        "published_at": "2026-10-31T08:00:00+08:00",
        "is_active": 1,
    }


def _member_rows(code="000001.SZ", *, dataset_id="d", dates=("2020-01-01",)):
    dates = tuple(dates)
    digest = date_set_sha256(code, dates)
    return [
        {
            "dataset_id": dataset_id,
            "code": code,
            "trade_date": trade_date,
            "member_ordinal": ordinal,
            "date_set_sha256": digest,
            "published_at": "2026-10-31T08:00:00+08:00",
        }
        for ordinal, trade_date in enumerate(dates, start=1)
    ]


def _payloads(tmp_path, *, member_rows=2):
    registry = tmp_path / "registry.jsonl.gz"
    member = tmp_path / "member.jsonl.gz"
    accepted = tmp_path / "accepted.jsonl.gz"
    summary = tmp_path / "summary.json"
    dates = tuple((date(2020, 1, 1) + timedelta(days=offset)).isoformat() for offset in range(member_rows))
    _write_gz(registry, [_registry_row(dates=dates)])
    _write_gz(member, _member_rows(dates=dates))
    _write_gz(accepted, [{"code": "000001.SZ", "dates": ["2020-01-01"]}])
    summary.write_text(json.dumps({
        "candidate_code_count": 1,
        "candidate_member_total_across_source_backed_codes": member_rows,
        "accepted_code_count": 1,
        "accepted_member_count": 1,
    }), encoding="utf-8")
    return registry, member, summary, accepted


@pytest.mark.parametrize(
    "kind,code",
    [
        ("registry", "830001.SH"),
        ("member", "830001.SH"),
        ("accepted", "830001.SH"),
    ],
)
def test_payload_stats_blocks_neeq_codes(tmp_path, kind, code):
    path = tmp_path / f"{kind}.jsonl.gz"
    if kind == "registry":
        rows = [_registry_row(code=code)]
    elif kind == "member":
        rows = _member_rows(code=code)
    else:
        rows = [{"code": code, "dates": ["2020-01-01"]}]
    _write_gz(path, rows)

    stats = _payload_stats(path, kind)

    assert stats["neeq_code_count"] == 1
    assert stats["status"] == "blocked"


@pytest.mark.parametrize("kind", ["registry", "member", "accepted"])
def test_payload_stats_blocks_empty_payload(tmp_path, kind):
    path = tmp_path / f"{kind}.jsonl.gz"
    _write_gz(path, [])

    stats = _payload_stats(path, kind)

    assert stats["row_count"] == 0
    assert stats["status"] == "blocked"


@pytest.mark.parametrize(
    ("kind", "missing_field"),
    [
        ("registry", "source_sha256"),
        ("registry", "is_active"),
        ("member", "member_ordinal"),
        ("member", "published_at"),
    ],
)
def test_payload_stats_requires_target_table_columns(tmp_path, kind, missing_field):
    path = tmp_path / f"{kind}.jsonl.gz"
    if kind == "registry":
        row = _registry_row()
    else:
        row = _member_rows()[0]
    del row[missing_field]
    _write_gz(path, [row])

    stats = _payload_stats(path, kind)

    assert stats["missing_fields"] == 1
    assert stats["status"] == "blocked"


@pytest.mark.parametrize("kind", ["registry", "member"])
def test_payload_stats_blocks_invalid_published_at(tmp_path, kind):
    path = tmp_path / f"{kind}.jsonl.gz"
    row = _registry_row() if kind == "registry" else _member_rows()[0]
    row["published_at"] = "not-a-datetime"
    _write_gz(path, [row])

    stats = _payload_stats(path, kind)

    assert stats["invalid_metadata_count"] == 1
    assert stats["status"] == "blocked"


def test_payload_stats_blocks_non_string_code_values(tmp_path):
    path = tmp_path / "registry.jsonl.gz"
    row = _registry_row()
    row["code"] = 1
    _write_gz(path, [row])

    stats = _payload_stats(path, "registry")

    assert stats["noncanonical_code_count"] == 1
    assert stats["status"] == "blocked"


@pytest.mark.parametrize("code", ["920489.BJ", "880001.SH"])
def test_payload_stats_allows_non_neeq_bse_and_index_codes(tmp_path, code):
    path = tmp_path / "registry.jsonl.gz"
    _write_gz(path, [_registry_row(code=code)])

    stats = _payload_stats(path, "registry")

    assert stats["neeq_code_count"] == 0
    assert stats["status"] == "ready"


def test_payload_checks_blocks_incomplete_summary_count_contract(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    summary.write_text(json.dumps({
        "candidate_code_count": 1,
        "candidate_member_total_across_source_backed_codes": 2,
    }), encoding="utf-8")

    checks = _payload_checks(
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    assert checks["status"] == "blocked"
    assert checks["comparisons"]["accepted_code_count_matches_summary"] is False
    assert checks["comparisons"]["accepted_member_count_matches_summary"] is False


def test_payload_checks_blocks_registry_member_digest_mismatch(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    members = _member_rows(dates=("2020-01-01", "2020-01-02"))
    members[0]["date_set_sha256"] = "0" * 64
    _write_gz(member, members)

    checks = _payload_checks(
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    assert checks["status"] == "blocked"
    assert checks["registry_member_consistency"]["digest_mismatch_count"] == 1


def test_payload_checks_blocks_registry_member_count_mismatch(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    row = _registry_row(dates=("2020-01-01", "2020-01-02"))
    row["member_count"] = 3
    _write_gz(registry, [row])

    checks = _payload_checks(
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    assert checks["status"] == "blocked"
    assert checks["registry_member_consistency"]["member_count_mismatch_count"] == 1


def test_payload_checks_blocks_registry_member_dataset_id_mismatch(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    members = _member_rows(dates=("2020-01-01", "2020-01-02"))
    members[0]["dataset_id"] = "wrong-dataset"
    _write_gz(member, members)

    checks = _payload_checks(
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    assert checks["status"] == "blocked"
    assert checks["registry_member_consistency"]["dataset_id_mismatch_count"] == 1


def test_payload_checks_blocks_accepted_keys_without_active_complete_registry(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    row = _registry_row(dates=("2020-01-01", "2020-01-02"))
    row["is_active"] = 0
    _write_gz(registry, [row])

    checks = _payload_checks(
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    assert checks["status"] == "blocked"
    assert checks["accepted_member_compatibility"]["keys_without_active_complete_registry"] == 1


def test_payload_checks_blocks_member_publication_timestamp_mismatch(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    with gzip.open(member, "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    rows[0]["published_at"] = "2026-10-31T09:00:00+08:00"
    _write_gz(member, rows)

    checks = _payload_checks(
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    assert checks["registry_member_consistency"]["published_at_mismatch_count"] == 1
    assert checks["registry_member_consistency"]["status"] == "blocked"
    assert checks["status"] == "blocked"


def test_payload_checks_requires_accepted_keys_to_exist_in_candidate_members(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    _write_gz(accepted, [{"code": "000001.SZ", "dates": ["2021-01-01"]}])

    checks = _payload_checks(
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    assert checks["status"] == "blocked"
    assert checks["accepted_member_compatibility"]["accepted_keys_missing_from_candidate"] == 1


def test_payload_stats_blocks_duplicate_registry_codes(tmp_path):
    path = tmp_path / "registry.jsonl.gz"
    row = _registry_row(dates=("2020-01-01",))
    _write_gz(path, [row, row])

    stats = _payload_stats(path, "registry")

    assert stats["duplicate_code_count"] == 1
    assert stats["status"] == "blocked"


def test_payload_stats_blocks_duplicate_member_keys(tmp_path):
    path = tmp_path / "member.jsonl.gz"
    row = _member_rows()[0]
    _write_gz(path, [row, row])

    stats = _payload_stats(path, "member")

    assert stats["duplicate_key_count"] == 1
    assert stats["status"] == "blocked"


def test_payload_stats_blocks_unordered_member_keys(tmp_path):
    path = tmp_path / "member.jsonl.gz"
    rows = _member_rows(dates=("2020-01-02", "2020-01-01"))
    _write_gz(path, rows)

    stats = _payload_stats(path, "member")

    assert stats["order_violation_count"] == 1
    assert stats["status"] == "blocked"


def test_payload_stats_blocks_invalid_member_trade_dates(tmp_path):
    path = tmp_path / "member.jsonl.gz"
    row = _member_rows()[0]
    row["trade_date"] = "2020-02-30"
    _write_gz(path, [row])

    stats = _payload_stats(path, "member")

    assert stats["invalid_trade_date_count"] == 1
    assert stats["status"] == "blocked"


def test_payload_stats_blocks_invalid_accepted_dates(tmp_path):
    path = tmp_path / "accepted.jsonl.gz"
    _write_gz(path, [{"code": "000001.SZ", "dates": ["2020-02-30"]}])

    stats = _payload_stats(path, "accepted")

    assert stats["invalid_trade_date_count"] == 1
    assert stats["status"] == "blocked"


@pytest.mark.parametrize("mutation", ["missing_registry", "wrong_member_type"])
def test_candidate_schema_validation_blocks_bad_target_ddl_and_suppresses_copy_select(tmp_path, monkeypatch, mutation):
    import scripts.dry_run_index_schema_migration as migration

    registry, member, summary, accepted = _payloads(tmp_path)
    original_ddl_for = migration._ddl_for

    def altered_ddl_for(table_name):
        ddl = original_ddl_for(table_name)
        if mutation == "missing_registry" and table_name == migration.REGISTRY_TABLE:
            return ""
        if mutation == "wrong_member_type" and table_name == migration.MEMBER_TABLE:
            return ddl.replace("member_ordinal UInt32", "member_ordinal String")
        return ddl

    monkeypatch.setattr(migration, "_ddl_for", altered_ddl_for)
    plan = build_plan(
        client=_ReadonlyClient(
            include_registry=True,
            engine_full="ReplacingMergeTree(created_at) PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)",
            partition_key="toYYYYMM(trade_date)",
        ),
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    assert plan["candidate_schema_validation"]["status"] == "blocked"
    assert plan["candidate_schema_validation"]["blockers"]
    assert plan["data_copy_plan"]["status"] == "blocked"
    assert plan["data_copy_plan"]["select_expression"] is None
    assert plan["status"] == "blocked"


def test_plan_reads_only_and_blocks_live_schema(tmp_path):
    from utils.clickhouse_schema import MARKET_TABLE_DDL

    registry, member, summary, accepted = _payloads(tmp_path)
    client = _ReadonlyClient()
    plan = build_plan(client=client, registry_candidate=registry, member_candidate=member, candidate_summary=summary, accepted_manifest=accepted)

    assert plan["status"] == "blocked"
    assert plan["data_copy_plan"]["select_expression"] is None
    assert "kline_daily__migration_candidate" in plan["expected_ddl_proposal_not_executed"]["kline_daily_contract_ddl"]
    assert "PARTITION BY toYYYYMM(trade_date)" in plan["expected_ddl_proposal_not_executed"]["kline_daily_contract_ddl"]
    assert "ReplacingMergeTree(created_at)" in plan["expected_ddl_proposal_not_executed"]["kline_daily_contract_ddl"]
    candidate_ddl = plan["expected_ddl_proposal_not_executed"]["kline_daily_contract_ddl"]
    canonical_ddl = next(
        ddl for ddl in MARKET_TABLE_DDL
        if "CREATE TABLE IF NOT EXISTS kline_daily" in ddl
    )
    expected_candidate_ddl = " ".join(
        line.strip() for line in canonical_ddl.strip().splitlines()
    ).replace(
        "CREATE TABLE IF NOT EXISTS kline_daily",
        "CREATE TABLE stock.kline_daily__migration_candidate",
        1,
    )
    assert candidate_ddl == expected_candidate_ddl
    assert "CONSTRAINT codex_no_neeq_code CHECK" in candidate_ddl
    assert "CONSTRAINT codex_no_neeq_legacy_bj_code CHECK" in candidate_ddl
    assert "CONSTRAINT codex_kline_maintenance_from_2000 CHECK" in candidate_ddl
    assert plan["candidate_schema_validation"]["status"] == "ready"
    assert plan["local_payload_checks"]["status"] == "ready"
    assert plan["rollback_validation"]["commands_issued"]["ddl"] == 0
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in client.queries)


def test_candidate_schema_validation_matches_the_full_runner_contract(tmp_path):
    from utils.clickhouse_schema import KLINE_DAILY_REQUIRED_COLUMN_TYPES

    registry, member, summary, accepted = _payloads(tmp_path)
    plan = build_plan(
        client=_ReadonlyClient(),
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    candidate_schema = plan["candidate_schema_validation"]
    assert candidate_schema["status"] == "ready"
    assert candidate_schema["column_types"] == dict(KLINE_DAILY_REQUIRED_COLUMN_TYPES)
    assert plan["source_copy_compatibility"]["status"] == "ready"
    assert plan["status"] == "blocked"  # The current live engine/partition are not migration-ready.


def test_candidate_schema_validation_blocks_missing_expected_id(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    plan = build_plan(
        client=_ReadonlyClient(missing_columns={"id"}),
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    compatibility = plan["source_copy_compatibility"]
    assert compatibility["status"] == "blocked"
    assert compatibility["missing_columns"] == ["id"]
    assert plan["data_copy_plan"]["status"] == "blocked"
    assert plan["data_copy_plan"]["select_expression"] is None
    assert plan["status"] == "blocked"


def test_plan_blocks_source_rows_before_candidate_date_constraint_floor(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    plan = build_plan(
        client=_ReadonlyClient(first_trade_date="1999-12-31"),
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    compatibility = plan["source_copy_compatibility"]
    assert compatibility["status"] == "blocked"
    assert compatibility["date_constraint_compatibility"]["status"] == "blocked"
    assert plan["data_copy_plan"]["select_expression"] is None
    assert plan["status"] == "blocked"


def test_plan_blocks_source_rows_violating_target_code_check_constraints(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    client = _ReadonlyClient(
        include_registry=True,
        engine_full="ReplacingMergeTree(created_at) PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)",
        partition_key="toYYYYMM(trade_date)",
        forbidden_neeq_rows=1,
    )

    plan = build_plan(
        client=client,
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    compatibility = plan["source_copy_compatibility"]
    assert compatibility["status"] == "blocked"
    assert compatibility["security_constraint_compatibility"]["forbidden_neeq_rows"] == 1
    assert "source_rows_violate_target_neeq_constraints" in compatibility["blockers"]
    assert plan["data_copy_plan"]["select_expression"] is None
    assert plan["status"] == "blocked"
    aggregate_query = next(sql for sql, _ in client.queries if "uniqExact(id)" in sql)
    assert "target_neeq_rejected_rows" in aggregate_query
    assert "match(upper(trimBoth(ifNull(toString(code), '')))," in aggregate_query
    assert "match(upper(code)," not in aggregate_query
    assert "countIf(" in aggregate_query


def test_plan_blocks_source_with_duplicate_exact_keys_before_replacement_migration(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    plan = build_plan(
        client=_ReadonlyClient(row_count=3, unique_keys=2, unique_ids=3, null_created_at=1, nonnull_created_at=2),
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    compatibility = plan["source_copy_compatibility"]
    identity = compatibility["row_identity_compatibility"]
    assert compatibility["status"] == "blocked"
    assert "source_rows_exceed_unique_exact_keys" in identity["blockers"]
    assert plan["data_copy_plan"]["select_expression"] is None
    assert plan["status"] == "blocked"


def test_plan_proposes_registry_and_member_contracts_without_publish(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path, member_rows=1)
    plan = build_plan(client=_ReadonlyClient(), registry_candidate=registry, member_candidate=member, candidate_summary=summary, accepted_manifest=accepted)

    expected = plan["expected_ddl_proposal_not_executed"]
    assert "daily_date_universe_registry__migration_candidate" in expected["registry_contract_ddl"]
    assert "daily_date_universe_member__migration_candidate" in expected["member_contract_ddl"]
    assert plan["production_mutation"] == {"ddl": False, "dml": False, "rename": False, "mutation": False, "registry_publish": False}


def test_payload_summary_mismatch_blocks_plan_and_suppresses_copy_select(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path, member_rows=2)
    summary.write_text(json.dumps({
        "candidate_code_count": 1,
        "candidate_member_total_across_source_backed_codes": 99,
        "accepted_code_count": 1,
        "accepted_member_count": 1,
    }), encoding="utf-8")
    plan = build_plan(
        client=_ReadonlyClient(
            include_registry=True,
            engine_full="ReplacingMergeTree(created_at) PARTITION BY toYYYYMM(trade_date) ORDER BY (code, trade_date)",
            partition_key="toYYYYMM(trade_date)",
        ),
        registry_candidate=registry,
        member_candidate=member,
        candidate_summary=summary,
        accepted_manifest=accepted,
    )

    assert plan["live_schema_readback"]["status"] == "ready"
    assert plan["local_payload_checks"]["status"] == "blocked"
    assert plan["data_copy_plan"]["select_expression"] is None
    assert plan["status"] == "blocked"

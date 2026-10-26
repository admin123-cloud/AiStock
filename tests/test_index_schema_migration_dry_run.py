from __future__ import annotations

import gzip
import json

from scripts.dry_run_index_schema_migration import build_plan


class _Result:
    def __init__(self, names, rows):
        self.column_names = names
        self.result_rows = rows


class _ReadonlyClient:
    def __init__(self, *, include_registry=False):
        self.include_registry = include_registry
        self.queries = []
        self.query_count = 0

    def query(self, sql, settings=None):
        self.queries.append((sql, settings))
        self.query_count += 1
        if "FROM system.tables" in sql and "database='stock'" in sql and "name IN" in sql:
            rows = [
                ("stock", "kline_daily", "ReplacingMergeTree", "ReplacingMergeTree ORDER BY (code, trade_date)", "", "code, trade_date", "code, trade_date", "CREATE TABLE stock.kline_daily ..."),
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
            rows = [
                ("stock", "kline_daily", "code", "String", "", "", 0, 1, 1),
                ("stock", "kline_daily", "trade_date", "Date", "", "", 0, 1, 1),
                ("stock", "kline_daily", "open", "Float64", "", "", 0, 0, 0),
                ("stock", "kline_daily", "high", "Float64", "", "", 0, 0, 0),
                ("stock", "kline_daily", "low", "Float64", "", "", 0, 0, 0),
                ("stock", "kline_daily", "close", "Float64", "", "", 0, 0, 0),
                ("stock", "kline_daily", "volume", "Float64", "", "", 0, 0, 0),
                ("stock", "kline_daily", "amount", "Float64", "", "", 0, 0, 0),
                ("stock", "kline_daily", "created_at", "Nullable(DateTime)", "", "", 0, 0, 0),
                ("stock", "kline_daily", "id", "UInt64", "", "", 0, 0, 0),
            ]
            return _Result(["database", "table", "name", "type", "default_kind", "default_expression", "is_in_partition_key", "is_in_sorting_key", "is_in_primary_key"], rows)
        if "FROM system.parts" in sql:
            return _Result(["database", "table", "partition", "active_parts", "rows", "bytes", "min_date", "max_date"], [("stock", "kline_daily", "tuple()", 1, 2, 100, "1970-01-01", "2026-10-21")])
        if "lower(name) LIKE" in sql:
            return _Result(["database", "name", "engine", "partition_key", "sorting_key"], [])
        if "uniqExact(toYYYYMM" in sql:
            return _Result(["distinct_month_partitions"], [(2,)])
        if "uniqExact(id)" in sql:
            return _Result(["rows", "unique_keys", "unique_ids", "null_created_at", "nonnull_created_at", "first_trade_date", "last_trade_date"], [(2, 2, 1, 1, 1, "2020-01-01", "2026-10-21")])
        raise AssertionError(f"unexpected query: {sql}")


def _write_gz(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _payloads(tmp_path, *, member_rows=2):
    registry = tmp_path / "registry.jsonl.gz"
    member = tmp_path / "member.jsonl.gz"
    accepted = tmp_path / "accepted.jsonl.gz"
    summary = tmp_path / "summary.json"
    _write_gz(registry, [{"code": "000001.SZ", "dataset_id": "d", "date_set_sha256": "h", "member_count": member_rows, "completeness_state": "complete"}])
    _write_gz(member, [{"code": "000001.SZ", "dataset_id": "d", "trade_date": f"2020-01-0{i}", "date_set_sha256": "h"} for i in range(1, member_rows + 1)])
    _write_gz(accepted, [{"code": "000001.SZ", "dates": ["2020-01-01"]}])
    summary.write_text(json.dumps({"candidate_code_count": 1, "candidate_member_total_across_source_backed_codes": member_rows}), encoding="utf-8")
    return registry, member, summary, accepted


def test_plan_reads_only_and_blocks_live_schema(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path)
    client = _ReadonlyClient()
    plan = build_plan(client=client, registry_candidate=registry, member_candidate=member, candidate_summary=summary, accepted_manifest=accepted)

    assert plan["status"] == "blocked"
    assert "kline_daily__migration_candidate" in plan["expected_ddl_proposal_not_executed"]["kline_daily_contract_ddl"]
    assert "PARTITION BY toYYYYMM(trade_date)" in plan["expected_ddl_proposal_not_executed"]["kline_daily_contract_ddl"]
    assert "ReplacingMergeTree(created_at)" in plan["expected_ddl_proposal_not_executed"]["kline_daily_contract_ddl"]
    assert plan["local_payload_checks"]["status"] == "ready"
    assert plan["rollback_validation"]["commands_issued"]["ddl"] == 0
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in client.queries)


def test_plan_proposes_registry_and_member_contracts_without_publish(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path, member_rows=1)
    plan = build_plan(client=_ReadonlyClient(), registry_candidate=registry, member_candidate=member, candidate_summary=summary, accepted_manifest=accepted)

    expected = plan["expected_ddl_proposal_not_executed"]
    assert "daily_date_universe_registry__migration_candidate" in expected["registry_contract_ddl"]
    assert "daily_date_universe_member__migration_candidate" in expected["member_contract_ddl"]
    assert plan["production_mutation"] == {"ddl": False, "dml": False, "rename": False, "mutation": False, "registry_publish": False}


def test_payload_summary_mismatch_blocks_plan(tmp_path):
    registry, member, summary, accepted = _payloads(tmp_path, member_rows=2)
    summary.write_text(json.dumps({"candidate_code_count": 1, "candidate_member_total_across_source_backed_codes": 99}), encoding="utf-8")
    plan = build_plan(client=_ReadonlyClient(), registry_candidate=registry, member_candidate=member, candidate_summary=summary, accepted_manifest=accepted)

    assert plan["local_payload_checks"]["status"] == "blocked"
    assert plan["status"] == "blocked"

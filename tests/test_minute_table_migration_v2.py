"""Pure-function safety contracts for the minute-table v2 migration."""

import pytest

from scripts.minute_table_migration_v2 import (
    V2_DDL,
    VALID_5M_TIMES,
    _ref_alias,
    aggregate_sql,
    quote_identifier,
    safe_month,
    safe_run_id,
    source_cutoff_clause,
    aggregate_reference_sql,
    require_complete_month,
)


@pytest.mark.parametrize("identifier", ["table-name", "table name", "table;DROP", "1table", "table'", ""])
def test_quote_identifier_rejects_sql_injection_and_invalid_names(identifier):
    with pytest.raises(ValueError):
        quote_identifier(identifier)


@pytest.mark.parametrize("identifier", ["kline_minute_60", "candidate_202601", "_private"])
def test_quote_identifier_accepts_simple_identifiers(identifier):
    assert quote_identifier(identifier) == identifier


@pytest.mark.parametrize("month", ["202601", "199912", "000001"])
def test_safe_month_accepts_six_digit_calendar_month(month):
    assert safe_month(month) == month


@pytest.mark.parametrize(
    "month",
    ["202600", "202613", "20261", "2026011", "2026-01", "202601;DROP TABLE x", "' OR 1=1 --"],
)
def test_safe_month_rejects_invalid_or_injected_values(month):
    with pytest.raises(ValueError):
        safe_month(month)


@pytest.mark.parametrize("builder", [aggregate_sql, _ref_alias])
def test_sql_builders_reject_injected_period_and_month(builder):
    with pytest.raises((KeyError, ValueError)):
        builder("60; DROP TABLE stock", "202601")
    # Month is interpolated into SQL by these pure builders and must be validated
    # before interpolation.  This contract intentionally guards against regressions.
    with pytest.raises(ValueError):
        builder("60", "202601 OR 1=1")


@pytest.mark.parametrize("period", ["15", "30", "60"])
def test_sql_builders_emit_valid_period_and_month_without_mutation_tokens(period):
    for builder in (aggregate_sql, _ref_alias):
        sql = builder(period, "202601")
        assert "202601" in sql
        assert "DROP TABLE" not in sql
        assert f"'{int(period)}m'" in sql or builder is _ref_alias


def test_valid_5m_times_contains_only_complete_session_five_minute_marks():
    times = [part.strip(" '") for part in VALID_5M_TIMES.split(",")]
    assert len(times) == 48
    assert times[0] == "09:35"
    assert times[23] == "11:30"
    assert times[24] == "13:05"
    assert times[-1] == "15:00"
    assert all(len(value) == 5 and value[2] == ":" for value in times)
    assert all((int(value[:2]) * 60 + int(value[3:])) % 5 == 0 for value in times)
    assert not {"09:30", "13:00", "11:35", "15:05"}.intersection(times)


@pytest.mark.parametrize('value', ['../escape', 'a;DROP', 'a/b', 'a\\b', 'a-b', '', '中文'])
def test_run_id_rejects_unsafe_sql_and_paths(value):
    with pytest.raises(ValueError):
        safe_run_id(value)


def test_all_source_builders_use_cutoff():
    clause = source_cutoff_clause({'cutoff': '2026-09-12T12:16:37'})
    for sql in (aggregate_sql('60', '202003', clause),
                _ref_alias('60', '202003', clause),
                aggregate_reference_sql(60, 12, '202003', clause)):
        assert 'WHERE toYYYYMM(datetime) = 202003' + clause in sql


def test_v2_ddl_uses_month_partition_and_code_datetime_sorting_key():
    normalized = " ".join(V2_DDL.split()).lower()
    assert "partition by toyyyymm(datetime)" in normalized
    assert "order by (code, datetime)" in normalized


def test_require_complete_month_accepts_clean_baseline_entry():
    entry = {"month": "202601", "daily_anomaly_rows": 0}
    assert require_complete_month({"months": [entry]}, "202601") is entry


@pytest.mark.parametrize("manifest", [
    {"months": [{"month": "202601", "daily_anomaly_rows": 1}]},
    {"months": [{"month": "202601", "daily_anomaly_rows": 0},
                 {"month": "202601", "daily_anomaly_rows": 0}]},
    {"months": []},
])
def test_require_complete_month_fails_closed_for_incomplete_or_ambiguous_month(manifest):
    with pytest.raises(SystemExit):
        require_complete_month(manifest, "202601")

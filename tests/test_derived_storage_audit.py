from scripts.audit_derived_storage_parts import report


def test_checksum_audit_never_infers_unreadable_row_range():
    result = report('kline_minute_60', [{'name': 'all_2_9_0', 'rows': 10}], [{"exception": "Checksum doesn't match"}])
    assert result['state'] == 'physical_corruption_evidence_recorded'
    assert 'unknown' in result['scope_limit']
    assert result['write_authority'] == 'read_only_audit'


def test_clean_query_log_is_not_reported_as_repaired_data():
    result = report('kline_minute_60', [], [])
    assert result['state'] == 'no_checksum_failure_in_query_log'
    assert 'replacement candidate' in result['allowed_next_step']

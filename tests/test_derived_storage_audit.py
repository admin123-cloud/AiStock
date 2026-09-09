from scripts.audit_derived_storage_parts import report
from scripts.rebuild_derived_history import revalidation_status
from scripts.run_preserve_old_derived_keys import months_between


def test_checksum_audit_never_infers_unreadable_row_range():
    result = report('kline_minute_60', [{'name': 'all_2_9_0', 'rows': 10}], [{"exception": "Checksum doesn't match"}])
    assert result['state'] == 'physical_corruption_evidence_recorded'
    assert 'unknown' in result['scope_limit']
    assert result['write_authority'] == 'read_only_audit'


def test_clean_query_log_is_not_reported_as_repaired_data():
    result = report('kline_minute_60', [], [])
    assert result['state'] == 'no_checksum_failure_in_query_log'
    assert 'replacement candidate' in result['allowed_next_step']


def test_revalidation_marks_source_changes_stale_without_rebuilding():
    accepted = [10, 10, '20', '30']
    assert revalidation_status(accepted, accepted, [11, 11, '21', '31']) == 'source_changed_since_acceptance'
    assert revalidation_status(accepted, accepted, accepted) == 'matched'


def test_preservation_driver_keeps_81_month_range_explicit():
    months=months_between('202001','202609')
    assert len(months)==81 and months[0]=='202001' and months[-1]=='202609'

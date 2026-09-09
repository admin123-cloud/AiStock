import pytest
from datetime import datetime, timedelta
import json
import os
from services.operations.health import BUSINESS_TZ
from services.operations.read_models import recovery_summary

from scripts.rebuild_derived_history import bounds, build_month, source_sql, table_name


def test_cutoff_and_complete_sessions():
    assert str(bounds('202609', '2026-09-08')[1]) == '2026-09-08'
    sql = source_sql(60, '202609', '2026-09-08')
    assert 'source_bars = 12' in sql
    assert 'FINAL' in sql and 'session_start' in sql
    assert '2026-09-08' in sql
    assert 'WHERE toYYYYMM(datetime) = 202609 AND ' in sql
    assert 'toYYYYMM(datetime) >= 202609' in sql
    assert 'toYYYYMM(datetime) <= 202609' in sql
    assert 'arraySort' in sql and 'sum(amount)' not in sql
    with pytest.raises(ValueError):
        table_name(60, 'unsafe;drop')


class Result:
    def __init__(self, rows):
        self.result_rows = rows


class Client:
    def __init__(self, fail=False, changed=False):
        self.fail, self.changed, self.writes, self.digests = fail, changed, 0, 0

    def query(self, sql, **kwargs):
        if 'sumWithOverflow' in sql:
            self.digests += 1
            return Result([(2, 2, 'different' if self.changed and self.digests == 3 else 'a', 'b')])
        return Result([(0,)])

    def command(self, sql, **kwargs):
        assert sql.startswith('INSERT INTO kline_minute_15_recovery_test ')
        self.writes += 1
        if self.fail:
            raise TimeoutError('uncertain server outcome')


def state():
    return {'run_id': 'test', 'cutoff': '2026-09-08', 'jobs': {}}


def test_verified_resume_does_not_reinsert():
    s, c = state(), Client()
    build_month(c, s, 15, '202001', lambda: None)
    build_month(c, s, 15, '202001', lambda: None)
    assert c.writes == 1
    assert s['jobs']['15:202001']['state'] == 'verified'


def test_resume_detects_candidate_changed_after_verification():
    s, c = state(), Client()
    build_month(c, s, 15, '202001', lambda: None)
    c.changed, c.digests = True, 2
    with pytest.raises(RuntimeError, match='verified candidate changed'):
        build_month(c, s, 15, '202001', lambda: None)
    assert c.writes == 1


@pytest.mark.parametrize('client', [Client(fail=True), Client(changed=True)])
def test_uncertain_or_changed_source_blocks_retry(client):
    s = state()
    with pytest.raises((TimeoutError, RuntimeError)):
        build_month(client, s, 15, '202001', lambda: None)
    assert s['jobs']['15:202001']['state'] == 'blocked'
    with pytest.raises(RuntimeError, match='uncertain previous attempt'):
        build_month(client, s, 15, '202001', lambda: None)
    assert client.writes == 1


def test_recovery_read_model_does_not_claim_process_health(tmp_path):
    p = tmp_path / 'operations/derived_recovery/example/state.json'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'run_id': 'example', 'months': ['202001'], 'jobs': {
        '15:202001': {'state': 'verified', 'actual': [20], 'incomplete_buckets': 2},
        '30:202001': {'state': 'blocked', 'error': 'checksum mismatch'}}}), encoding='utf-8')
    now = datetime.now(BUSINESS_TZ)
    old = (now - timedelta(hours=1)).timestamp()
    os.utime(p, (old, old))
    result = recovery_summary(tmp_path, now=now)
    assert result['verified'] == 1 and result['total'] == 3
    assert result['rows'] == 20 and result['record_stale']
    assert not result['candidate_complete']
    assert result['promotion'] == 'not_performed'
    assert result['pending'][0]['error'] == 'checksum mismatch'

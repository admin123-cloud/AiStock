from datetime import date
import gzip
import json
from types import SimpleNamespace

from utils.market_status import StatusIndex, load_status_index, partition_status_keys, find_status_trade_conflicts


def row(code, state='verified', status='suspended', url='https://notice.example/document'):
    return dict(code=code, start_date='2026-10-14', end_date='2026-10-14', status=status,
                review_state=state, evidence_url=url, reason='reviewed evidence')






class FixtureClient:
    def __init__(self):
        self.calls = []
    def query(self, sql, **kwargs):
        self.calls.append(sql)
        if 'market_status_audit' in sql:
            entries = [row('000001.SZ'), row('000002.SZ', 'conflict', 'suspended_candidate'),
                       row('000003.SZ', 'unconfirmed', 'source_absent')]
            fields = ('code', 'start_date', 'end_date', 'status', 'evidence_url', 'reason', 'review_state')
            return SimpleNamespace(result_rows=[tuple(r[k] for k in fields) for r in entries])
        if 'FROM stocks' in sql:
            return SimpleNamespace(result_rows=[(f'{n:06d}.SZ', 'SZ', 'stock', 'active', date(1990, 1, 1), None, 0) for n in range(1, 4)])
        return SimpleNamespace(result_rows=[])


def test_minute_expected_set_keeps_missing_daily_and_conflicting_dates():
    import pandas as pd
    from scripts.qmt_xtquant_minute_gap_audit_repair import expected_codes_for_date
    universe = pd.DataFrame([dict(code=f'{n:06d}.SZ', list_date=date(1990, 1, 1)) for n in range(1, 4)])
    client = FixtureClient()
    codes, decisions = expected_codes_for_date(client, universe, '2026-10-14', return_status=True)
    assert codes == ['000002.SZ', '000003.SZ']
    assert decisions['exempt'] == {('000001.SZ', '2026-10-14')}
    assert 'market_status_audit' in client.calls[0]


def baseline_fixture(tmp_path):
    detail = tmp_path / 'counts.gz'
    with gzip.open(detail, 'wt', encoding='utf-8') as handle:
        for n in range(1, 4):
            handle.write(f'2026-10-14\t{n:06d}.SZ\t0\n')
    baseline = dict(run_id='fixture', fingerprint='fixture', months=[dict(daily_file=str(detail))])
    return baseline


def test_5m_repair_manifest_exempts_only_registered_and_retains_unknown(tmp_path, monkeypatch):
    from scripts import prepare_5m_gap_manifest as manifest
    baseline = tmp_path / 'baseline.json'
    baseline.write_text(json.dumps(baseline_fixture(tmp_path)), encoding='utf-8')
    output = tmp_path / 'manifest.json'
    monkeypatch.setattr(manifest, 'clickhouse_client', FixtureClient)
    monkeypatch.setattr('sys.argv', ['manifest', '--baseline', str(baseline), '--output', str(output)])
    assert manifest.main() == 0
    result = json.loads(output.read_text('utf-8'))
    assert [r['code'] for r in result['gaps']] == ['000003.SZ']
    assert [r['code'] for r in result['status_conflicts']] == ['000002.SZ']
    assert result['gaps'][0]['requires_notice_review']


def test_scope_5m_local_manual_json_cannot_create_unregistered_exemption(tmp_path, monkeypatch):
    from scripts import scope_5m_anomalies as scope
    root = tmp_path / 'minute_migration_v2'
    root.mkdir()
    (root / 'baseline_fixture.json').write_text(json.dumps(baseline_fixture(tmp_path)), encoding='utf-8')
    (root / 'manual_scope_overrides_20260913.json').write_text(json.dumps({'overrides': [dict(code='000003.SZ', trade_date='2026-10-14', classification='confirm_suspension')]}), encoding='utf-8')
    output = tmp_path / 'scope.json'
    monkeypatch.setattr(scope, 'runtime_path', lambda: tmp_path)
    monkeypatch.setattr(scope, 'clickhouse_client', FixtureClient)
    monkeypatch.setattr('sys.argv', ['scope', '--run-id', 'fixture', '--output', str(output)])
    assert scope.main() == 0
    items = {r['code']: r for r in json.loads(output.read_text('utf-8'))['items']}
    assert items['000001.SZ']['category'] == 'status:verified_business_absence'
    assert items['000002.SZ']['category'] == 'status_conflict_requires_notice'
    assert items['000003.SZ']['category'] == 'manual_status_not_registered'
    assert items['000003.SZ']['validity'] == 'blocked_evidence'

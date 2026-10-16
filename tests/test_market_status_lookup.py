from datetime import date
from types import SimpleNamespace
from utils.market_status import StatusIndex, partition_status_keys, find_status_trade_conflicts


def row(code, state='verified', status='suspended', url='https://notice.example/document'):
    return dict(code=code, start_date='2026-10-14', end_date='2026-10-14', status=status,
                review_state=state, evidence_url=url, reason='reviewed evidence')


def test_only_confirmed_exact_date_exempts_and_trade_conflict_wins():
    index = StatusIndex([row('000001.SZ'), row('000002.SZ', 'unconfirmed', 'source_absent'),
                         row('000003.SZ', 'pending_exchange_confirmation', 'suspended_candidate'),
                         row('000004.SZ', url=''), row('000005.SZ', 'revoked'),
                         row('000006.SZ', 'conflict', 'suspended_candidate')])
    keys = {(f'{n:06d}.SZ', '2026-10-14') for n in range(1, 8)}
    groups = partition_status_keys(keys, index)
    assert groups['exempt'] == {('000001.SZ', '2026-10-14')}
    assert groups['conflict'] == {('000006.SZ', '2026-10-14')}
    assert len(groups['lookup']) == 6
    assert index.decide('000001.SZ', '2026-10-15') == 'unregistered'
    assert index.decide('000001.SZ', '2026-10-14', conflicting_trade=True) == 'conflict'


def test_current_conflict_reads_both_final_tables_only_for_registered_dates():
    calls = []
    class Client:
        def query(self, sql, **kwargs):
            calls.append(sql)
            return SimpleNamespace(result_rows=[('000001.SZ', date(2026, 10, 14))] if 'kline_minute_5' in sql else [])
    keys = {('000001.SZ', date(2026, 10, 14)), ('000002.SZ', date(2026, 10, 14))}
    index = StatusIndex([row('000001.SZ')])
    conflicts = find_status_trade_conflicts(Client(), keys, index)
    assert conflicts == {('000001.SZ', date(2026, 10, 14))}
    assert len(calls) == 2 and all(' FINAL ' in sql and '000002.SZ' not in sql for sql in calls)
    assert partition_status_keys(keys, index, conflicts)['exempt'] == set()

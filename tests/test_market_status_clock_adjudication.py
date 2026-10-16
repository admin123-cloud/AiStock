from copy import deepcopy
from datetime import date
import json
from types import SimpleNamespace

import pytest

from utils.market_status import StatusIndex, find_status_trade_conflicts, minute_rows_fingerprint


DAY = '2010-01-27'
CODE = '600063.SH'
ROWS = [(CODE, 1264613700, 12.14, 12.18, 12.02, 12.14, 2327, 2822551.0, None, 0),
        (CODE, 1264614000, 12.14, 12.15, 12.01, 12.05, 1222, 1470000.0, None, 0)]


def resolved_row():
    proof = dict(outcome='confirmed_suspension', reason_code='legacy_timestamp_maps_to_next_session',
                 code=CODE, trade_date=DAY, notice_confirmed=True, minute_original_verified=True,
                 retained_minute_rows=dict(schema_version=1, rows=2, source_date='2010-01-28',
                                           sha256=minute_rows_fingerprint(ROWS)))
    return dict(code=CODE, start_date=DAY, end_date=DAY, status='suspended', review_state='verified',
                evidence_url='https://notice.example/original', evidence_json=json.dumps({'adjudication': proof}))


class Client:
    def __init__(self, rows=ROWS, daily_positive=False):
        self.rows, self.daily_positive, self.calls = rows, daily_positive, []
    def query(self, sql, **kwargs):
        self.calls.append(sql)
        if 'stored_epoch' in sql:
            return SimpleNamespace(result_rows=self.rows)
        if 'kline_daily FINAL' in sql:
            return SimpleNamespace(result_rows=[(CODE, date.fromisoformat(DAY))] if self.daily_positive else [])
        return SimpleNamespace(result_rows=[(CODE, date.fromisoformat(DAY))])


def test_only_exact_unchanged_original_clock_rows_are_resolved():
    client = Client()
    index = StatusIndex([resolved_row()])
    assert find_status_trade_conflicts(client, {(CODE, DAY)}, index) == set()
    readback = next(sql for sql in client.calls if 'stored_epoch' in sql)
    assert "datetime>=toDate('2010-01-27')" in readback
    assert "datetime<toDate('2010-01-28')" in readback and ' FINAL ' in readback


@pytest.mark.parametrize('column,value', [(1, 1264584900), (2, 12.15), (6, 2328), (7, None), (8, 1264000000), (9, 42)])
def test_any_changed_field_reopens_conflict(column, value):
    changed = [list(r) for r in ROWS]
    changed[0][column] = value
    assert find_status_trade_conflicts(Client(changed), {(CODE, DAY)}, StatusIndex([resolved_row()])) == {(CODE, DAY)}


def test_extra_or_removed_minute_row_reopens_conflict():
    index = StatusIndex([resolved_row()])
    for rows in (ROWS[:1], ROWS + [ROWS[-1]]):
        assert find_status_trade_conflicts(Client(rows), {(CODE, DAY)}, index) == {(CODE, DAY)}


def test_daily_trading_always_blocks_even_when_minutes_are_bound():
    client = Client(daily_positive=True)
    assert find_status_trade_conflicts(client, {(CODE, DAY)}, StatusIndex([resolved_row()])) == {(CODE, DAY)}
    assert not any('stored_epoch' in sql for sql in client.calls)


def test_unreviewed_or_wrong_date_mapping_cannot_bypass_trade_conflict():
    row = resolved_row()
    for change in [dict(notice_confirmed=False), dict(minute_original_verified=False),
                   dict(trade_date='2010-01-26'), dict(reason_code='source_absent')]:
        bad = deepcopy(row)
        proof = json.loads(bad['evidence_json'])
        proof['adjudication'].update(change)
        bad['evidence_json'] = json.dumps(proof)
        assert find_status_trade_conflicts(Client(), {(CODE, DAY)}, StatusIndex([bad])) == {(CODE, DAY)}


def test_fingerprint_preserves_nulls_float_bits_and_all_row_fields():
    assert minute_rows_fingerprint(ROWS) == minute_rows_fingerprint(reversed(ROWS))
    negative_zero = [list(r) for r in ROWS]
    positive_zero = deepcopy(negative_zero)
    negative_zero[0][2], positive_zero[0][2] = -0.0, 0.0
    assert minute_rows_fingerprint(negative_zero) != minute_rows_fingerprint(positive_zero)
    with pytest.raises(ValueError):
        minute_rows_fingerprint([ROWS[0][:-1]])
    fractional = [list(r) for r in ROWS]
    fractional[0][6] = 2327.5
    with pytest.raises(ValueError):
        minute_rows_fingerprint(fractional)

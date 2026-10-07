from datetime import datetime, timezone

import pandas as pd

from scripts.repair_qmt_builtin_window import (
    FIELDS, SLOTS, aggregate, epoch, same, validate_day, validate_daily,
)


def sample():
    frame = pd.DataFrame([[10., 11., 9., 10.5, 100., 100000.] for _ in SLOTS],
                         columns=FIELDS, index=['20260930' + slot for slot in SLOTS])
    daily = pd.Series(dict(open=10., high=11., low=9., close=10.5,
                           volume=4800., amount=4800000., suspendFlag=0))
    return frame, daily


def test_native_units_reject_hundredfold_daily_volume():
    frame, daily = sample()
    assert validate_daily(daily, 'stock') is None
    assert validate_day('20260930', frame, daily, 'stock') is None
    daily.volume *= 100
    assert validate_daily(daily, 'stock') == 'native_daily_lots_yuan_contract_mismatch'
    assert validate_day('20260930', frame, daily, 'stock') == 'native_minute_daily_volume_mismatch'


def test_closed_slots_and_aggregation_do_not_cross_lunch():
    frame, daily = sample()
    assert validate_day('20260930', frame.iloc[1:], daily, 'stock') == 'minute_slots_not_closed'
    rows = aggregate(frame, 60)
    assert [row[0][-6:] for row in rows] == ['103000', '113000', '140000', '150000']
    assert len(rows) == 4 and sum(row[1][4] for row in rows) == daily.volume


def test_fractional_five_minute_lots_are_not_truncated():
    frame, daily = sample()
    frame.iloc[0, 4] = 100.5
    assert validate_day('20260930', frame, daily, 'stock') == 'native_minute_volume_not_integer_lots'


def test_aggregation_preserves_open_close_and_extremes():
    frame, _ = sample()
    frame.iloc[:3] = [[10,12,9,11,100,100000], [11,13,10,12,200,220000], [12,14,8,10,300,330000]]
    timestamp, values = aggregate(frame, 15)[0]
    assert timestamp == '20260930094500'
    assert values == [10,14,8,10,600,650000]


def test_epoch_is_independent_of_host_timezone():
    assert epoch('20260930150000') == int(datetime(2026, 9, 30, 7, tzinfo=timezone.utc).timestamp())


def test_comparison_detects_units_without_rewriting_float_noise():
    assert same([10, 11, 9, 10.5, 100, 100000], [10.0000002, 11, 9, 10.5, 100, 100000.001])
    assert not same([10, 11, 9, 10.5, 100, 100000], [10, 11, 9, 10.5, 10000, 10])


def test_source_suspension_is_not_a_filled_trading_day():
    _, daily = sample()
    daily.suspendFlag = 1
    assert validate_daily(daily, 'stock') == 'native_suspended'

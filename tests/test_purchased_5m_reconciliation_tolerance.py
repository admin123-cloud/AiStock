import pytest

from utils.purchased_5m_reconciliation import (
    PRICE_RELATIVE_TOLERANCE,
    max_price_abs_diff,
    price_reference,
    price_relative_diff,
    within_relative_tolerance,
)


def test_price_reference_uses_highest_daily_ohlc_and_floor():
    assert price_reference((29.11, 29.22, 28.90, 29.14)) == 29.22
    assert price_reference((0.02, 0.03, 0.01, 0.02)) == 1.0


def test_003_price_difference_at_2911_is_micro_and_passes():
    daily = (29.11, 29.20, 28.90, 29.11)
    minute = (29.11, 29.20, 28.90, 29.14)
    absolute = max_price_abs_diff(minute, daily)
    ratio = price_relative_diff(absolute, daily)
    assert absolute == pytest.approx(0.03)
    assert ratio is not None and ratio < PRICE_RELATIVE_TOLERANCE


def test_same_absolute_difference_on_low_price_remains_review():
    daily = (2.00, 2.05, 1.95, 2.00)
    minute = (2.00, 2.05, 1.95, 2.03)
    ratio = price_relative_diff(max_price_abs_diff(minute, daily), daily)
    assert ratio == pytest.approx(0.03 / 2.05)
    assert ratio > PRICE_RELATIVE_TOLERANCE


def test_volume_and_amount_keep_zero_point_one_percent_rule():
    assert within_relative_tolerance(1000.9, 1000.0, 0.001)
    assert not within_relative_tolerance(1002.0, 1000.0, 0.001)

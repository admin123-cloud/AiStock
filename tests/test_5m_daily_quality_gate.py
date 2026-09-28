from __future__ import annotations

from datetime import date, datetime

import pytest

from utils.minute_daily_quality_gate import (
    EXPECTED_UNITS,
    aggregate_5m_to_daily,
    evaluate_code_day,
    expected_datetimes,
)
from utils.ocean_stage import STANDARD_5M_MINUTES


TRADE_DATE = date(2020, 1, 2)


def _rows(*, slots=None, **overrides):
    timestamps = slots or expected_datetimes(TRADE_DATE)
    rows = []
    for index, timestamp in enumerate(timestamps):
        rows.append(
            {
                "datetime": timestamp,
                "open": 10.0 + index / 100,
                "high": 10.1 + index / 100,
                "low": 9.9 + index / 100,
                "close": 10.05 + index / 100,
                "source_volume_lots": 100.0,
                "source_amount_yuan": 10000.0,
                "price_adjustment": "none",
                **overrides,
            }
        )
    return rows


def _daily(rows):
    aggregate = aggregate_5m_to_daily(rows)
    return {
        "open": aggregate.open,
        "high": aggregate.high,
        "low": aggregate.low,
        "close": aggregate.close,
        "volume_lots": aggregate.volume_lots,
        "amount_yuan": aggregate.amount_yuan,
    }


def test_standard_contract_is_exactly_48_slots_and_two_sessions():
    assert len(STANDARD_5M_MINUTES) == 48
    assert STANDARD_5M_MINUTES[:1] == (9 * 60 + 35,)
    assert STANDARD_5M_MINUTES[23:25] == (11 * 60 + 30, 13 * 60 + 5)
    assert STANDARD_5M_MINUTES[-1:] == (15 * 60,)
    assert len(expected_datetimes(TRADE_DATE)) == 48
    assert len(expected_datetimes(TRADE_DATE, "15m")) == 16
    assert expected_datetimes(TRADE_DATE, "15m")[0].strftime("%H:%M") == "09:45"
    assert expected_datetimes(TRADE_DATE, "15m")[-1].strftime("%H:%M") == "15:00"
    assert len(expected_datetimes(TRADE_DATE, "30m")) == 8
    assert len(expected_datetimes(TRADE_DATE, "60m")) == 4


def test_aggregate_uses_first_last_and_extrema_and_sums_quantities():
    rows = _rows()
    rows[3]["high"] = 99.0
    rows[7]["low"] = 1.0
    result = aggregate_5m_to_daily(rows)
    assert result.open == rows[0]["open"]
    assert result.close == rows[-1]["close"]
    assert result.high == 99.0
    assert result.low == 1.0
    assert result.volume_lots == pytest.approx(4800.0)
    assert result.amount_yuan == pytest.approx(480000.0)


def test_complete_code_day_with_daily_match_is_accepted():
    rows = _rows()
    result = evaluate_code_day(
        rows,
        trade_date=TRADE_DATE,
        daily=_daily(rows),
        source_units=EXPECTED_UNITS,
        price_adjustment="none",
        minute_source_family="purchased_a_share_history",
        daily_source_family="purchased_a_share_history",
    )
    assert result.status == "accepted"
    assert result.reasons == ()
    assert result.aggregate is not None


def test_missing_slot_is_review_and_never_accepted():
    rows = _rows()[:-1]
    result = evaluate_code_day(rows, trade_date=TRADE_DATE, daily=None)
    assert result.status == "review"
    assert any("minute_rows=47" in reason for reason in result.reasons)
    assert "daily_missing" not in result.reasons  # no aggregate means no daily gate yet


def test_duplicate_datetime_is_rejected():
    rows = _rows()
    rows[-1]["datetime"] = rows[-2]["datetime"]
    result = evaluate_code_day(rows, trade_date=TRADE_DATE, daily=None)
    assert result.status == "rejected"
    assert "duplicate_datetime" in result.reasons


def test_out_of_session_timestamp_is_review_when_structure_is_not_complete():
    rows = _rows()
    rows[-1]["datetime"] = datetime(2020, 1, 2, 15, 5)
    result = evaluate_code_day(rows, trade_date=TRADE_DATE, daily=None)
    assert result.status == "review"
    assert any("standard_5m_slots_mismatch" in reason for reason in result.reasons)


@pytest.mark.parametrize("field,value", [("open", 0.0), ("high", -1.0), ("source_volume_lots", -1.0), ("source_amount_yuan", float("nan"))])
def test_invalid_ohlcv_is_rejected(field, value):
    rows = _rows()
    rows[0][field] = value
    result = evaluate_code_day(rows, trade_date=TRADE_DATE, daily=None)
    assert result.status == "rejected"


def test_units_or_adjustment_uncertainty_is_review():
    rows = _rows()
    result = evaluate_code_day(
        rows,
        trade_date=TRADE_DATE,
        daily=_daily(rows),
        source_units="volume_shares/amount_yuan",
        price_adjustment="front",
    )
    assert result.status == "review"
    assert any("units_unconfirmed" in reason for reason in result.reasons)
    assert any("price_adjustment_unconfirmed" in reason for reason in result.reasons)

    rows[0]["price_adjustment"] = "front"
    row_mismatch = evaluate_code_day(
        rows,
        trade_date=TRADE_DATE,
        daily=_daily(rows),
    )
    assert row_mismatch.status == "review"
    assert any("price_adjustment_unconfirmed='front'" in reason for reason in row_mismatch.reasons)


def test_auction_residual_over_two_percent_is_informational_when_inside_daily_envelope():
    rows = _rows()
    missing = evaluate_code_day(rows, trade_date=TRADE_DATE, daily=None)
    assert missing.status == "review"
    assert "daily_missing" in missing.reasons

    daily = _daily(rows)
    daily["open"] -= 0.2
    daily["high"] += 0.2
    daily["low"] -= 0.2
    daily["close"] += 0.1
    daily["volume_lots"] *= 1.25
    daily["amount_yuan"] *= 1.25
    accepted = evaluate_code_day(
        rows,
        trade_date=TRADE_DATE,
        daily=daily,
        minute_source_family="purchased_a_share_history",
        daily_source_family="purchased_a_share_history",
    )
    assert accepted.status == "accepted"
    assert accepted.volume_relative_diff is not None and accepted.volume_relative_diff > 0.02
    assert accepted.amount_relative_diff is not None and accepted.amount_relative_diff > 0.02
    assert accepted.auction_residual_volume_lots == pytest.approx(
        daily["volume_lots"] - accepted.aggregate.volume_lots
    )
    assert accepted.auction_residual_amount_yuan == pytest.approx(
        daily["amount_yuan"] - accepted.aggregate.amount_yuan
    )


def test_minute_price_outside_daily_envelope_or_quantity_above_daily_remains_review():
    rows = _rows()
    aggregate = aggregate_5m_to_daily(rows)
    daily = _daily(rows)
    daily["high"] = aggregate.high - 0.01
    price_result = evaluate_code_day(
        rows,
        trade_date=TRADE_DATE,
        daily=daily,
        minute_source_family="purchased_a_share_history",
        daily_source_family="purchased_a_share_history",
    )
    assert price_result.status == "review"
    assert "minute_high_exceeds_daily_high" in price_result.reasons

    daily = _daily(rows)
    daily["volume_lots"] = aggregate.volume_lots - 1.0
    quantity_result = evaluate_code_day(
        rows,
        trade_date=TRADE_DATE,
        daily=daily,
        minute_source_family="purchased_a_share_history",
        daily_source_family="purchased_a_share_history",
    )
    assert quantity_result.status == "review"
    assert "minute_volume_exceeds_daily" in quantity_result.reasons


def test_source_family_must_match_before_acceptance():
    rows = _rows()
    result = evaluate_code_day(
        rows,
        trade_date=TRADE_DATE,
        daily=_daily(rows),
        minute_source_family="harry",
        daily_source_family="purchased_a_share_history",
    )
    assert result.status == "review"
    assert any("source_family_mismatch" in reason for reason in result.reasons)


def test_unconfirmed_source_families_are_explicitly_blocked():
    rows = _rows()
    result = evaluate_code_day(rows, trade_date=TRADE_DATE, daily=_daily(rows))
    assert result.status == "review"
    assert "minute_source_family_unconfirmed" in result.reasons
    assert "daily_source_family_unconfirmed" in result.reasons


def test_incomplete_evidence_is_review_and_preserves_no_candidate_status():
    rows = _rows()
    result = evaluate_code_day(
        rows,
        trade_date=TRADE_DATE,
        daily=_daily(rows),
        evidence_complete=False,
    )
    assert result.status == "review"
    assert "evidence_incomplete" in result.reasons

from datetime import date, datetime

import pandas as pd

from scripts.baostock_daily_fallback_repair import (
    is_suspension_placeholder,
    rows_for_missing,
    validation_metrics,
)


def test_suspension_placeholder_is_excluded_from_amount_validation():
    frame = pd.DataFrame(
        [
            {
                "trade_date": date(2026, 7, 22),
                "open": 7.48,
                "high": 7.69,
                "low": 6.90,
                "close": 7.23,
                "volume": 123608323,
                "amount": 888461618.30,
            },
            {
                "trade_date": date(2026, 7, 28),
                "open": 7.23,
                "high": 7.23,
                "low": 7.23,
                "close": 7.23,
                "volume": None,
                "amount": None,
            },
        ]
    )
    existing = {
        date(2026, 7, 22): (7.48, 7.69, 6.90, 7.23, 12360.8323, 888461618.0),
        date(2026, 7, 28): (7.23, 7.23, 7.23, 7.23, 0.0, 0.0),
    }

    placeholder = next(row for row in frame.itertuples(index=False) if row.trade_date == date(2026, 7, 28))
    assert is_suspension_placeholder(placeholder)
    assert validation_metrics(frame, existing) == (1, 1.0, 1.0, 0.0001, 1.0)


def test_suspension_placeholder_is_not_written_as_daily_bar():
    frame = pd.DataFrame(
        [
            {
                "trade_date": date(2026, 7, 28),
                "open": 7.23,
                "high": 7.23,
                "low": 7.23,
                "close": 7.23,
                "volume": None,
                "amount": None,
            }
        ]
    )

    rows = rows_for_missing("002036.SZ", frame, {date(2026, 7, 28)}, datetime(2026, 7, 31), 0.0001)

    assert rows == []

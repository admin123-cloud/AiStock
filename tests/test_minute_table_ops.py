import pandas as pd

from utils.minute_table_ops import aggregate_from_5m, derive_higher_frame


def _base_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["920001.BJ"] * 6,
            "datetime": pd.date_range("2026-09-14 09:35:00", periods=6, freq="5min"),
            "open": [1, 2, 3, 4, 5, 6],
            "high": [2, 3, 4, 5, 6, 7],
            "low": [0.5, 1.5, 2.5, 3.5, 4.5, 5.5],
            "close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5],
            "volume": [10, 20, 30, 40, 50, 60],
            "amount": [100, 200, 300, 400, 500, 600],
        }
    )


def test_aggregate_from_5m_groups_rows_into_target_bars():
    result = aggregate_from_5m(_base_frame(), 15)

    assert len(result) == 2
    assert result.iloc[0]["open"] == 1
    assert result.iloc[0]["high"] == 4
    assert result.iloc[0]["low"] == 0.5
    assert result.iloc[0]["close"] == 3.5
    assert result.iloc[0]["volume"] == 60
    assert result.iloc[0]["amount"] == 600


def test_derive_higher_frame_adds_storage_identity_columns():
    result = derive_higher_frame(_base_frame(), "920001.BJ", 30)

    assert len(result) == 1
    assert list(result.columns) == [
        "code", "datetime", "open", "high", "low", "close", "volume", "amount", "created_at", "id"
    ]
    assert result.iloc[0]["code"] == "920001.BJ"
    assert int(result.iloc[0]["volume"]) == 210

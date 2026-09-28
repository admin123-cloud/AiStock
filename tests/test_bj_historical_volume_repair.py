import struct
from datetime import date

from scripts.repair_bj_historical_volume import (
    DAY_RECORD,
    SourceBar,
    StoredBar,
    read_tdx_day_file,
    validate_source_bar,
)


def test_read_tdx_day_file_converts_raw_shares_to_lots(tmp_path):
    path = tmp_path / "bj920039.day"
    with path.open("wb") as handle:
        handle.write(
            DAY_RECORD.pack(
                20220412,
                700,
                700,
                679,
                684,
                185367.265625,
                27113,
                65536,
            )
        )

    rows = read_tdx_day_file(path, date(2022, 4, 12), date(2022, 4, 12))

    assert rows[date(2022, 4, 12)].volume == 271
    assert rows[date(2022, 4, 12)].amount == 185367.265625


def test_validate_source_bar_requires_price_and_amount_agreement():
    stored = StoredBar(
        code="920039.BJ",
        trade_date=date(2022, 4, 12),
        open=7.0,
        high=7.0,
        low=6.79,
        close=6.84,
        volume=1,
        amount=185367.27,
    )
    source = SourceBar(
        trade_date=date(2022, 4, 12),
        open=7.0,
        high=7.0,
        low=6.79,
        close=6.84,
        volume=271,
        amount=185367.265625,
    )

    assert validate_source_bar(stored, source) == (True, "validated")

    rejected = SourceBar(
        trade_date=source.trade_date,
        open=source.open + 1,
        high=source.high,
        low=source.low,
        close=source.close,
        volume=source.volume,
        amount=source.amount,
    )
    assert validate_source_bar(stored, rejected) == (False, "open_mismatch")


def test_validate_source_bar_allows_a_source_volume_smaller_than_storage():
    stored = StoredBar(
        code="920167.BJ",
        trade_date=date(2020, 7, 27),
        open=15.5,
        high=16.2,
        low=15.4,
        close=15.82,
        volume=6966074,
        amount=118591822,
    )
    source = SourceBar(
        trade_date=stored.trade_date,
        open=stored.open,
        high=stored.high,
        low=stored.low,
        close=stored.close,
        volume=69660,
        amount=stored.amount,
    )

    assert validate_source_bar(stored, source) == (True, "validated")

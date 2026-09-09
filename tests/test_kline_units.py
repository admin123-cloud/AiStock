import pandas as pd

from utils.kline_units import (
    normalize_akshare_daily_units,
    normalize_baostock_daily_units,
    normalize_qmt_daily_units,
    normalize_tdxquant_daily_units,
    normalize_tushare_daily_units,
)


def test_normalize_qmt_daily_stock_units_are_already_in_storage_contract():
    source = pd.DataFrame(
        [
            {
                "volume": 1_221_130,
                "amount": 1_401_213_600,
            }
        ]
    )

    normalized = normalize_qmt_daily_units(source)

    assert normalized.loc[0, "volume"] == 1_221_130
    assert normalized.loc[0, "amount"] == 1_401_213_600
    assert source.loc[0, "volume"] == 1_221_130
    assert source.loc[0, "amount"] == 1_401_213_600


def test_normalize_qmt_index_volume_keeps_legacy_index_contract():
    source = pd.DataFrame(
        [
            {
                "volume": 540_324_922,
                "amount": 1_008_382_536_905,
            }
        ]
    )

    normalized = normalize_qmt_daily_units(source, instrument_type="index")

    assert normalized.loc[0, "volume"] == 5_403_249.22
    assert normalized.loc[0, "amount"] == 1_008_382_536_905
    assert source.loc[0, "volume"] == 540_324_922


def test_provider_daily_units_are_explicitly_normalized_to_lots_and_yuan():
    source = pd.DataFrame([{"volume": 100_000, "amount": 12_345.6}])

    baostock = normalize_baostock_daily_units(source)
    akshare = normalize_akshare_daily_units(source)
    tdxquant = normalize_tdxquant_daily_units(source)
    tushare = normalize_tushare_daily_units(source)

    assert baostock.loc[0, "volume"] == 1_000
    assert baostock.loc[0, "amount"] == 12_345.6
    assert akshare.loc[0, "volume"] == 100_000
    assert akshare.loc[0, "amount"] == 12_345.6
    assert tdxquant.loc[0, "volume"] == 100_000
    assert tdxquant.loc[0, "amount"] == 12_345.6
    assert tushare.loc[0, "volume"] == 100_000
    assert tushare.loc[0, "amount"] == 12_345_600

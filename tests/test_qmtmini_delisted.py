from data_fetcher.sources.qmtmini import _detail_to_stock_row, _has_past_expire_date


def test_past_qmt_expire_date_marks_stock_as_quit():
    detail = {
        "InstrumentName": "国华退",
        "OpenDate": "19901201",
        "ExpireDate": "20000101",
        "FloatVolume": 1,
        "TotalVolume": 2,
    }

    row = _detail_to_stock_row("000004.SZ", detail, "stock")

    assert _has_past_expire_date("20000101") is True
    assert row["quit"] == 1
    assert row["name"] == "国华退"
    assert row["delist_date"] == "20000101"


def test_open_contract_expire_sentinel_is_not_delisted():
    assert _has_past_expire_date("99999999") is False
    assert _has_past_expire_date("0") is False
    assert _has_past_expire_date("10011011") is False

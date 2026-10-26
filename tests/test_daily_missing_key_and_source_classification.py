from scripts.close_formal_bar_missing_keys import classify_key
from scripts.classify_daily_independent_source_differences import (
    ratio_bucket,
    substantive_fields,
    unit_layers,
)


def test_formal_bar_missing_without_lifecycle_status_stays_blocked():
    disposition, action = classify_key(
        accepted_member=True,
        candidate_member=True,
        formal_row=None,
        tdx_row=None,
        status_rows=[{"status": "formal_bar_missing", "review_state": "verified"}],
        ths_row={"trade_date": "1992-03-25"},
    )
    assert disposition == "blocked_formal_bar_missing_lifecycle_unresolved"
    assert action == "requires_independent_lifecycle_or_exchange_calendar_evidence"


def test_substantive_fields_exclude_precision_only_differences():
    row = {
        "mismatch_fields": ["open", "volume", "amount"],
        "formal_values": {"open": 10.0005, "volume": 100.0, "amount": 1000.0},
        "source_values": {"open": 10.0, "volume": 100.005, "amount": 1000.6},
    }
    assert substantive_fields(row) == ("amount",)


def test_ratio_buckets_and_unit_layers_are_explicit():
    assert ratio_bucket(100.0, 100.0) == "0.5-2"
    assert ratio_bucket(0.0, 10.0) == "formal_zero"
    assert unit_layers("stock_shares_div_100", "stock")["volume_unit"] == "stock_volume_shares_div_100_to_formal_lots"
    assert unit_layers("index_raw", "index")["volume_unit"] == "index_volume_native_raw"

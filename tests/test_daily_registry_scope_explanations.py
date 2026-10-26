from scripts.build_daily_registry_scope_explanations import _record_meta, _scale_tdx_source, _source_dates


def test_source_dates_union_and_local_candidate_meta():
    record = {
        "code": "000001.SZ",
        "category": "stock",
        "status": "source_date_set_available",
        "formal": {"date_count": 3, "first_date": "2026-01-01", "last_date": "2026-01-04"},
        "sources": {
            "ths_cache": {"dates": ["2026-01-01", "2026-01-03"]},
            "wstock_tdx_evidence": {"dates": ["2026-01-03", "2026-01-04"]},
        },
    }
    sources, union = _source_dates(record)
    meta = _record_meta(record)
    assert union == ("2026-01-01", "2026-01-03", "2026-01-04")
    assert sources["ths_cache"] == ("2026-01-01", "2026-01-03")
    assert meta["completeness_state"] == "complete"
    assert meta["acceptance_state"] == "local_candidate_pending_online_publish"
    assert meta["member_count"] == 3


def test_tdx_volume_mapping_is_type_aware():
    row = {"code": "000001.SZ", "trade_date": "2026-01-01", "volume_shares": 12345.0}
    stock = _scale_tdx_source(row, category="stock")
    index = _scale_tdx_source(row, category="index")
    unknown = _scale_tdx_source(row, category="formal_unmapped")
    assert stock["volume"] == 123.45
    assert stock["volume_unit_mapping"] == "stock_shares_div_100"
    assert index["volume"] == 12345.0
    assert index["volume_unit_mapping"] == "index_raw"
    assert unknown["volume_unit_mapping"] == "unknown_raw_review"

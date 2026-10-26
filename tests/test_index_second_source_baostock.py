from __future__ import annotations

from scripts.reconcile_index_second_source_baostock import compare_row, load_candidates


def _candidate() -> dict:
    return {
        "code": "000001.SH",
        "trade_date": "2020-01-02",
        "source_table": "stock.wstock_daily_tdx_evidence_20261014_v2",
        "source_file_sha": "abc",
        "source_row": 1,
        "formal_values": {"open": 3066.336, "high": 3098.1, "low": 3066.336, "close": 3085.198, "volume": 2924702.08, "amount": 327197122605.0},
        "source_values_raw": {"open": 3066.34, "high": 3098.1, "low": 3066.34, "close": 3085.2, "volume_shares": 292470208.0, "amount": 327197130752.0},
    }


def test_compare_row_normalizes_baostock_volume_and_accepts_small_differences():
    provider = {
        "provider_code": "sh.000001",
        "trade_date": "2020-01-02",
        "open": "3066.3350",
        "high": "3098.1000",
        "low": "3066.3350",
        "close": "3085.1970",
        "volume_raw": "29247020800",
        "amount": "327197122560.0000",
        "adjustflag": "3",
        "tradestatus": "1",
    }
    row = compare_row(_candidate(), provider)
    assert row["baostock_normalized"]["volume"] == 2924702.08
    assert row["disposition"] == "two_source_consensus"
    assert all(item["within_tolerance"] for item in row["field_comparison"].values())


def test_compare_row_marks_missing_exact_key():
    row = compare_row(_candidate(), None)
    assert row["disposition"] == "baostock_missing_exact_key"
    assert row["baostock_normalized"]["volume"] is None


def test_compare_row_rejects_material_price_difference():
    provider = {"provider_code": "sh.000001", "trade_date": "2020-01-02", "open": "3067.00", "high": "3098.1", "low": "3066.34", "close": "3085.2", "volume_raw": "29247020800", "amount": "327197130752"}
    row = compare_row(_candidate(), provider)
    assert row["disposition"] == "two_source_residual_review"
    assert not row["field_comparison"]["open"]["within_tolerance"]


def test_load_candidates_rejects_duplicates(tmp_path):
    import gzip
    import json

    path = tmp_path / "candidates.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(_candidate()) + "\n")
        handle.write(json.dumps(_candidate()) + "\n")
    try:
        load_candidates(path)
    except ValueError as exc:
        assert "duplicate candidate key" in str(exc)
    else:
        raise AssertionError("duplicate key was not rejected")

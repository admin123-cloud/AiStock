from __future__ import annotations

import gzip
import json
from decimal import Decimal
from pathlib import Path

from scripts.reconcile_index_missing_keys_ths import (
    _bar_values,
    _compare_values,
    load_candidates,
)


class _Bar:
    open = Decimal("100.0000")
    high = Decimal("101.0000")
    low = Decimal("99.0000")
    close = Decimal("100.5000")
    volume = Decimal("1234500")
    amount = Decimal("1000000")


def test_ths_index_volume_is_normalized_to_formal_lots():
    values = _bar_values(_Bar())
    assert values["volume"] == 123.45
    assert values["open"] == 100.0


def test_compare_values_accepts_price_and_amount_micro_difference():
    left = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 123.45, "amount": 1_000_000.0}
    right = {"open": 100.004, "high": 101.004, "low": 98.996, "close": 100.496, "volume": 123.451, "amount": 1_000_000.5}
    comparison = _compare_values(left, right)
    assert all(item["within_tolerance"] for item in comparison.values())


def test_compare_values_rejects_material_amount_difference():
    left = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 123.45, "amount": 1_000_000.0}
    right = dict(left, amount=1_010_000.0)
    assert not _compare_values(left, right)["amount"]["within_tolerance"]


def test_load_candidates_filters_baostock_missing_and_rejects_duplicates(tmp_path: Path):
    path = tmp_path / "input.jsonl.gz"
    base = {"code": "000680.SH", "trade_date": "2020-01-02", "disposition": "baostock_missing_exact_key"}
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(base) + "\n")
        handle.write(json.dumps({"code": "000001.SH", "trade_date": "2020-01-02", "disposition": "baostock_missing_exact_key"}) + "\n")
    rows = load_candidates(path)
    assert len(rows) == 1
    assert rows[0]["code"] == "000680.SH"

    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(base) + "\n")
        handle.write(json.dumps(base) + "\n")
    try:
        load_candidates(path)
    except ValueError as exc:
        assert "duplicate missing exact key" in str(exc)
    else:
        raise AssertionError("duplicate key was not rejected")

from __future__ import annotations

import gzip
import json
from pathlib import Path

from scripts.reconcile_index_missing_keys_sina import _compare, _query_url, load_candidates, reconcile


def _write_candidates(path: Path, rows: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _formal() -> dict:
    return {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 123.45, "amount": 1_000_000}


def test_compare_marks_amount_unavailable_and_matches_ohlcv():
    comparison = _compare(_formal(), {"open": 100.004, "high": 101.004, "low": 98.996, "close": 100.496, "volume": 123.451})
    assert all(comparison[field]["within_tolerance"] for field in ("open", "high", "low", "close", "volume"))
    assert comparison["amount"]["within_tolerance"] is None
    assert comparison["amount"]["status"] == "provider_field_unavailable"


def test_load_candidates_excludes_pre2000(tmp_path: Path):
    path = tmp_path / "input.jsonl.gz"
    _write_candidates(
        path,
        [
            {"code": "000680.SH", "trade_date": "1999-12-31", "disposition": "baostock_missing_exact_key"},
            {"code": "000680.SH", "trade_date": "2000-01-03", "disposition": "baostock_missing_exact_key"},
        ],
    )
    active, excluded = load_candidates(path)
    assert [row["trade_date"] for row in active] == ["2000-01-03"]
    assert excluded == [{"code": "000680.SH", "trade_date": "1999-12-31", "reason": "user_cutoff_before_2000"}]


def test_reconcile_offline_records_consensus_missing_and_exclusion(tmp_path: Path):
    input_path = tmp_path / "input.jsonl.gz"
    _write_candidates(
        input_path,
        [
            {"code": "000680.SH", "trade_date": "1999-12-31", "disposition": "baostock_missing_exact_key", "candidate_formal_values": _formal()},
            {"code": "000680.SH", "trade_date": "2020-01-02", "disposition": "baostock_missing_exact_key", "candidate_formal_values": _formal()},
            {"code": "000688.SH", "trade_date": "2020-01-02", "disposition": "baostock_missing_exact_key", "candidate_formal_values": _formal()},
        ],
    )
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    rows = [{"day": "2020-01-02", "open": "100", "high": "101", "low": "99", "close": "100.5", "volume": "1234500"}]
    (raw_dir / "000680_SH.json").write_text(json.dumps(rows), encoding="utf-8")
    summary = reconcile(
        input_path=input_path,
        output_jsonl=tmp_path / "out.jsonl.gz",
        output_summary=tmp_path / "summary.json",
        exclusion_jsonl=tmp_path / "excluded.jsonl.gz",
        raw_dir=raw_dir,
        offline=True,
    )
    assert summary["candidate_rows_after_cutoff"] == 2
    assert summary["pre2000_excluded_rows"] == 1
    assert summary["dispositions"] == {"sina_ohlcv_consensus_amount_unavailable": 1, "sina_source_missing": 1}


def test_query_url_uses_sh_provider_symbol():
    assert "symbol=sh000852" in _query_url("https://example.test/api", "000852.SH")

from __future__ import annotations

import gzip
import json
from pathlib import Path

from scripts.reconcile_index_missing_keys_ths_export import (
    _parse_ths_export,
    compare_values,
    load_candidates,
)


def _write_input(path: Path, rows: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_parse_gbk_ths_index_export_normalizes_volume(tmp_path: Path):
    path = tmp_path / "Table.txt"
    path.write_bytes(("时间\t开盘\t最高\t最低\t收盘\t涨跌\t涨幅%\t振幅%\t总手(万)\t金额(亿)\r\n\r\n" "2011-01-04,二\t5179.35\t5230.73\t5160.53\t5230.73\t+75.18\t1.46\t1.36\t2628744300\t38963753000\t\r\n").encode("gb18030"))
    parsed = _parse_ths_export(path)
    assert parsed["encoding"] == "gb18030"
    assert parsed["first_date"] == "2011-01-04"
    assert parsed["rows"]["2011-01-04"]["normalized_values"]["volume"] == 262874.43


def test_compare_values_accepts_export_rounding():
    formal = {"open": 5179.355, "high": 5230.735, "low": 5160.53, "close": 5230.735, "volume": 262874.44, "amount": 38963752960.0}
    source = {"open": 5179.35, "high": 5230.73, "low": 5160.53, "close": 5230.73, "volume": 262874.43, "amount": 38963753000.0}
    assert all(item["within_tolerance"] for item in compare_values(formal, source).values())


def test_load_candidates_filters_to_000852_and_rejects_duplicates(tmp_path: Path):
    path = tmp_path / "input.jsonl.gz"
    base = {"code": "000852.SH", "trade_date": "2011-01-04", "disposition": "sina_source_missing", "formal_values": {}}
    _write_input(path, [base, {"code": "000001.SH", "trade_date": "2011-01-04", "disposition": "sina_source_missing"}])
    assert len(load_candidates(path)) == 1
    _write_input(path, [base, base])
    try:
        load_candidates(path)
    except ValueError as exc:
        assert "duplicate missing exact key" in str(exc)
    else:
        raise AssertionError("duplicate key was not rejected")


def test_load_candidates_supports_other_core_index_code(tmp_path: Path):
    path = tmp_path / "input.jsonl.gz"
    _write_input(path, [{"code": "000680.SH", "trade_date": "2019-12-31", "disposition": "sina_source_missing", "formal_values": {}}])
    rows = load_candidates(path, "000680.SH")
    assert len(rows) == 1
    assert rows[0]["code"] == "000680.SH"

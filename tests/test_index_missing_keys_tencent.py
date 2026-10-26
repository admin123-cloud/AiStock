from __future__ import annotations

import gzip
import json
from pathlib import Path

from scripts.reconcile_index_missing_keys_tencent import (
    _parse_tencent_file,
    _source_paths,
    load_candidates,
    reconcile,
)


def _write_tencent(path: Path, rows: list[list[object]], key: str = "sh000680") -> None:
    payload = {"code": 0, "msg": "", "data": {key: {"day": rows}}}
    path.write_text("kline_day2024=" + json.dumps(payload), encoding="utf-8")


def test_parse_tencent_normalizes_observed_index_units(tmp_path: Path):
    path = tmp_path / "sh000680.txt"
    _write_tencent(path, [["2023-05-17", "1233.23", "1241.89", "1242.17", "1233.23", "16864851.00", {}, "0", "6095685.10"]])
    parsed = _parse_tencent_file(path)
    assert parsed["code"] == "000680.SH"
    assert parsed["rows"] == 1
    values = parsed["observations"]["2023-05-17"]["normalized"]
    assert values["volume"] == 168648.51
    assert values["amount"] == 60956851000.0


def test_reconcile_classifies_consensus_residual_and_missing(tmp_path: Path):
    source = tmp_path / "sh000680.txt"
    _write_tencent(
        source,
        [
            ["2023-05-17", "1233.23", "1241.89", "1242.17", "1233.23", "16864851", {}, "0", "6095685.10"],
            ["2023-05-18", "9999", "1249.13", "1251.51", "1242.22", "19118822", {}, "0", "7159089.68"],
        ],
    )
    input_path = tmp_path / "input.jsonl.gz"
    rows = [
        {
            "code": "000680.SH",
            "trade_date": "2023-05-17",
            "disposition": "baostock_missing_exact_key",
            "candidate_formal_values": {"open": 1233.23, "high": 1242.17, "low": 1233.23, "close": 1241.89, "volume": 168648.51, "amount": 60956852224.0},
        },
        {
            "code": "000680.SH",
            "trade_date": "2023-05-18",
            "disposition": "baostock_missing_exact_key",
            "candidate_formal_values": {"open": 1243.55, "high": 1251.51, "low": 1242.22, "close": 1249.13, "volume": 191188.22, "amount": 71590896800.0},
        },
        {
            "code": "000680.SH",
            "trade_date": "2020-01-02",
            "disposition": "baostock_missing_exact_key",
            "candidate_formal_values": {"open": 1000, "high": 1000, "low": 1000, "close": 1000, "volume": 1, "amount": 1},
        },
    ]
    with gzip.open(input_path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    summary = reconcile(
        input_path=input_path,
        output_jsonl=tmp_path / "out.jsonl.gz",
        output_summary=tmp_path / "summary.json",
        source_paths=[source],
    )
    assert summary["dispositions"] == {"tencent_exact_consensus": 1, "tencent_residual_review": 1, "tencent_source_missing": 1}


def test_load_candidates_rejects_duplicates(tmp_path: Path):
    path = tmp_path / "input.jsonl.gz"
    row = {"code": "000680.SH", "trade_date": "2020-01-02", "disposition": "baostock_missing_exact_key"}
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
        handle.write(json.dumps(row) + "\n")
    try:
        load_candidates(path)
    except ValueError as exc:
        assert "duplicate missing exact key" in str(exc)
    else:
        raise AssertionError("duplicate key was not rejected")


def test_source_paths_are_deterministic(tmp_path: Path):
    p = tmp_path / "daily_missing_append_20261016_v1" / "recent_tencent" / "sh000680.txt"
    p.parent.mkdir(parents=True)
    p.write_text("{}", encoding="utf-8")
    assert _source_paths(tmp_path) == [p]

from pathlib import Path

from scripts.build_index_volume_reconciliation_candidates import _candidate_row, load_stable_mismatch_rows
from scripts.verify_index_exception_dates_second_source import parse_ths_rows


def test_candidate_row_scales_only_volume_and_preserves_amount():
    mismatch = {
        "code": "000001.SH",
        "trade_date": "2020-01-02",
        "mismatch_fields": ["volume"],
        "formal_values": {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 10.0, "amount": 20.0},
        "source_values": {"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 1000.0, "amount": 20.0},
        "source_volume_unit_mapping": "index_raw",
    }
    row = _candidate_row(mismatch, mismatch["formal_values"], {**mismatch["source_values"], "volume_shares": 1000.0, "file_sha": "abc", "source_row": 3})
    assert row["candidate_values"]["volume"] == 10.0
    assert row["candidate_values"]["amount"] == 20.0
    assert row["residual_substantive_fields"] == []


def test_load_stable_mismatch_rows_rejects_other_factor(tmp_path: Path):
    path = tmp_path / "mismatch.jsonl"
    path.write_text(
        '{"code":"000001.SH","trade_date":"2020-01-02","mismatch_fields":["volume"],"formal_values":{"volume":10},"source_values":{"volume":1000}}\n'
        '{"code":"000016.SH","trade_date":"2012-08-28","mismatch_fields":["volume"],"formal_values":{"volume":10},"source_values":{"volume":20}}\n',
        encoding="utf-8",
    )
    rows, summary = load_stable_mismatch_rows(path)
    assert list(rows) == [("000001.SH", "2020-01-02")]
    assert summary["skipped_factor_counts"] == {"1/2": 1}


def test_parse_ths_rows_extracts_displayed_volume(tmp_path: Path):
    path = tmp_path / "ths.txt"
    path.write_text(
        "时间\t\t开盘\t\t最高\t\t最低\t\t收盘\t\t涨跌\t\t涨幅%\t\t振幅%\t\t总手(万)\t\t金额(亿)\n"
        "2012-08-28,二\t850.47\t\t861.32\t\t848.20\t\t856.09\t\t+3.75\t\t0.44\t\t1.54\t\t4471324600\t46814821000\n",
        encoding="utf-8",
    )
    rows = parse_ths_rows(path)
    assert rows["2012-08-28"]["volume"] == 4471324600.0
    assert rows["2012-08-28"]["amount"] == 46814821000.0

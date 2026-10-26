from pathlib import Path

from scripts.audit_daily_closure_snapshot import _load_keys, _load_rows, build_report


def test_loads_json_key_payload(tmp_path: Path):
    path = tmp_path / "keys.json"
    path.write_text('{"keys":[{"code":"000001.sz","trade_date":"2026-01-05"}]}', encoding="utf-8")
    assert _load_keys(path) == [("000001.SZ", __import__("datetime").date(2026, 1, 5))]


def test_loads_jsonl_rows(tmp_path: Path):
    path = tmp_path / "rows.jsonl"
    path.write_text('{"code":"000001.SZ","trade_date":"2026-01-05"}\n', encoding="utf-8")
    assert _load_rows(path)[0]["code"] == "000001.SZ"


def test_build_report_anomaly_mode_is_read_only_and_classifies_rows():
    class Result:
        def __init__(self, names, rows):
            self.column_names = names
            self.result_rows = rows

    class Client:
        def __init__(self):
            self.calls = []

        def query(self, sql, settings=None):
            self.calls.append((sql, settings))
            if "kline_daily_market_status_audit" in sql:
                return Result(
                    ["code", "start_date", "end_date", "status", "review_state", "evidence_url", "reason"],
                    [],
                )
            return Result(
                ["code", "trade_date", "open", "high", "low", "close", "volume", "amount"],
                [["000001.SZ", "2026-01-05", 1.0, 1.0, 1.0, 1.0, 10.0, 0.0]],
            )

    client = Client()
    report, classified = build_report(
        client=client,
        mode="anomalies",
        expected_keys_path=None,
        source_rows_path=None,
        start_date=None,
        end_date=None,
        source="test",
    )
    assert report["status"] == "blocked"
    assert report["anomaly_counts"]["volume_positive_amount_zero_value_domain"] == 1
    assert len(classified) == 1
    assert all(call[1]["readonly"] == 1 for call in client.calls)


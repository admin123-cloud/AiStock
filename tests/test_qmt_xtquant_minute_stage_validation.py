from types import SimpleNamespace

from scripts import qmt_xtquant_minute_backfill_validate as worker


class _Result:
    def __init__(self, row):
        self.first_row = row


class _Client:
    def __init__(self):
        self.sql = []

    def query(self, sql):
        self.sql.append(sql)
        if "uniqExact(code)" in sql:
            return _Result((10, 1, "2026-09-10 09:35:00", "2026-09-10 15:00:00"))
        if "countIf(abs(q.close" in sql:
            return _Result((10, 2, 0.5, 3.0, 4))
        return _Result((5,))


def test_validate_stage_chunks_final_queries_and_reduces_exact_totals(monkeypatch, tmp_path):
    client = _Client()
    monkeypatch.setattr(worker, "ch_client", lambda: client)
    monkeypatch.setattr(worker, "select_codes", lambda _client, _args: [f"{idx:06d}.SZ" for idx in range(401)])
    monkeypatch.setattr(worker, "write_report", lambda *_args: None)
    args = SimpleNamespace(
        periods="5m",
        stage_table="qmt_xtquant_minute_stage",
        start_date="2026-09-10",
        end_date="2026-09-10",
        price_tolerance=0.001,
        volume_tolerance=0.1,
        report=str(tmp_path / "report.json"),
    )

    result = worker.validate_stage(args)["periods"]["5m"]

    assert result["stage_rows"] == 20
    assert result["stage_codes"] == 2
    assert result["overlap_rows"] == 20
    assert result["close_diff_rows"] == 4
    assert result["max_close_abs_diff"] == 0.5
    assert result["avg_close_abs_diff"] == 0.3
    assert result["volume_diff_rows"] == 8
    assert result["qmt_only_rows"] == 10
    assert sum("FROM kline_minute_5 FINAL" in sql for sql in client.sql) == 4

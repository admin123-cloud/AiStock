from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_daily_write_contract import audit
from utils.daily_write_guard import DailyWriteBlockedError


ROOT = Path(__file__).resolve().parents[1]


def test_all_formal_daily_mutation_surfaces_are_fail_closed_or_exact_key_guarded():
    report = audit(ROOT)
    assert report["file_count"] > 0
    assert report["unverified_count"] == 0, report


def test_detector_distinguishes_formal_table_from_sector_daily_table(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "fixture.py").write_text(
        "client.insert('sector_kline_daily', rows)\n",
        encoding="utf-8",
    )
    assert audit(tmp_path)["file_count"] == 0


def test_local_tdx_formal_flush_blocks_before_client_insert(monkeypatch):
    import research.cross_strategy.import_local_tdx_daily_to_clickhouse as module

    rows = [("000001.SZ", "2026-10-20")]
    monkeypatch.setattr(module, "filter_trading_day_tuples", lambda *_args: rows)
    client = type("Client", (), {"insert": lambda *_args, **_kwargs: pytest.fail("insert must be blocked")})()

    with pytest.raises(DailyWriteBlockedError, match="kline_daily"):
        module._flush(client, rows, dry_run=False)


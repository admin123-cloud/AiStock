import pandas as pd

from scripts import qmt_fullpush_intraday_aggregator as fullpush

from services.market_sentiment_cache import _confirmed_daily_row_is_complete, _intraday_frame_is_complete
from datetime import datetime

from services.market_turnover_forecast import build_fast_intraday_turnover_fallback


def test_confirmed_daily_snapshot_rejects_partial_market_batch():
    baseline = {"SH": 2300, "SZ": 2890, "BJ": 330}
    partial = {"sh_covered_count": 60, "sz_covered_count": 0, "bj_covered_count": 0}

    assert not _confirmed_daily_row_is_complete(partial, baseline)


def test_confirmed_daily_snapshot_accepts_complete_market_batch():
    baseline = {"SH": 2300, "SZ": 2890, "BJ": 330}
    complete = {"sh_covered_count": 2307, "sz_covered_count": 2890, "bj_covered_count": 332}

    assert _confirmed_daily_row_is_complete(complete, baseline)


def test_fast_turnover_fallback_still_returns_a_forecast_before_close(monkeypatch):
    import services.market_turnover_forecast as turnover_forecast

    class _AtTwoPm(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 24, 14, 0, tzinfo=tz)

    monkeypatch.setattr(turnover_forecast, "datetime", _AtTwoPm)
    forecast = build_fast_intraday_turnover_fallback({"total": 570_000_000_000, "sh": 250_000_000_000, "sz": 310_000_000_000, "bj": 10_000_000_000})

    assert forecast["available"]
    assert forecast["fallback"]
    assert forecast["forecast_amount"] > forecast["current_amounts"]["total"]


def test_intraday_fact_requires_full_market_coverage_before_use():
    assert not _intraday_frame_is_complete(pd.DataFrame([{"covered_count": 4999}]), 5600)
    assert _intraday_frame_is_complete(pd.DataFrame([{"covered_count": 5040}]), 5600)


def test_fullpush_refuses_to_publish_partial_market_snapshot(monkeypatch):
    monkeypatch.setattr(
        fullpush,
        "clickhouse_query_df",
        lambda _sql: pd.DataFrame({"code": ["600000.SH", "000001.SZ", "430001.BJ"], "market": ["SH", "SZ", "BJ"]}),
    )
    rows = [
        ("600000.SH", datetime(2026, 7, 30).date(), 10, 10, 10, 10, 1, 100, 9, 1, 1, 1, 0, datetime.now(), "qmt", 1),
        ("000001.SZ", datetime(2026, 7, 30).date(), 10, 10, 10, 10, 1, 100, 9, 1, 1, 1, 0, datetime.now(), "qmt", 1),
    ]

    result = fullpush.publish_market_snapshot(rows, dry_run=True)

    assert not result["written"]
    assert result["reason"] == "coverage_below_threshold"

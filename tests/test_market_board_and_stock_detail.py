"""回归测试：
1. 盘中监控三件套（情绪雷达、板块热度、竞价异动）支持盘中采集快照优先、盘后回退正式表 kline_daily；
2. 个股详情页 /kline-stats 与 /kline/1d 直查 ClickHouse 基表（raw=True），避免废弃质量视图 500/503。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
import pandas as pd

import api.market_board as mb
import api.stocks as stocks_api


def test_sector_heat_switches_between_intraday_and_postclose(monkeypatch):
    mb._SECTOR_HEAT_CACHE.clear()
    monkeypatch.setattr(mb, "_latest_fresh_intraday_snapshot", lambda: ("2026-11-20 10:15:00", "2026-11-20"))
    monkeypatch.setattr(
        mb,
        "_query_sector_heat_from_intraday",
        lambda ts, bt, sb, n: [{"code": "880551.SH", "name": "大基金持股", "type": "concept", "change_pct": 2.35, "up_count": 40, "down_count": 10, "total_count": 50}],
    )

    intraday_res = mb.get_sector_heat(board_type="concept", sort_by="change_pct", top_n=10)
    assert intraday_res["data_mode"] == "intraday"
    assert intraday_res["source"] == mb.SNAPSHOT_TABLE
    assert intraday_res["total"] == 1

    mb._SECTOR_HEAT_CACHE.clear()
    monkeypatch.setattr(mb, "_latest_fresh_intraday_snapshot", lambda: None)
    monkeypatch.setattr(mb, "_latest_formal_trade_date", lambda: "2026-11-19")
    monkeypatch.setattr(
        mb,
        "_query_sector_heat_from_formal",
        lambda dt, bt, sb, n: [{"code": "880551.SH", "name": "大基金持股", "type": "concept", "change_pct": 0.74, "up_count": 28, "down_count": 24, "total_count": 52}],
    )

    postclose_res = mb.get_sector_heat(board_type="concept", sort_by="change_pct", top_n=10)
    assert postclose_res["data_mode"] == "postclose"
    assert postclose_res["source"] == "kline_daily"
    assert postclose_res["date"] == "2026-11-19"
    assert postclose_res["total"] == 1
    mb._SECTOR_HEAT_CACHE.clear()


def test_sentiment_radar_uses_formal_date_when_no_intraday(monkeypatch):
    mb._SENTIMENT_RADAR_CACHE.clear()
    monkeypatch.setattr(mb, "clickhouse_available", lambda: True)
    monkeypatch.setattr(mb, "_latest_fresh_intraday_snapshot", lambda: None)
    monkeypatch.setattr(mb, "_latest_formal_trade_date", lambda: "2026-11-19")
    monkeypatch.setattr(mb, "_latest_30_trade_dates", lambda up_to: ["2026-11-18", "2026-11-19"])
    monkeypatch.setattr(mb, "_dim_up_down_ratio", lambda dt: {"ratio": 12.0, "raw": 0.12})
    monkeypatch.setattr(mb, "_dim_limit_up_seal_rate", lambda dt: {"seal_rate": 24.0, "raw": 0.24})
    monkeypatch.setattr(mb, "_dim_max_board_height", lambda dt, cal: {"score": 57.1, "height": 4})
    monkeypatch.setattr(mb, "_dim_ma20_breadth", lambda dt, cal: {"score": 38.5, "raw": 0.385})
    monkeypatch.setattr(mb, "_dim_turnover_percentile", lambda dt, cal: {"score": 45.0, "raw": 0.45})

    res = mb.get_sentiment_radar()
    assert res["available"] is True
    assert res["data_mode"] == "postclose"
    assert res["source"] == "kline_daily"
    assert res["date"] == "2026-11-19"
    assert res["dimensions"]["ma20_breadth"]["value"] == 38.5
    mb._SENTIMENT_RADAR_CACHE.clear()


def test_stock_detail_kline_stats_and_daily_use_raw_clickhouse(monkeypatch):
    raw_flags: list[bool] = []

    class _DummySession:
        pass

    monkeypatch.setattr(stocks_api.DatabaseSessionManager, "get_session", lambda: _DummySession())
    monkeypatch.setattr(stocks_api.DatabaseSessionManager, "close_session", lambda s: None)
    monkeypatch.setattr(stocks_api, "_get_stock_by_code", lambda s, code: SimpleNamespace(code=code, name="平安银行"))
    monkeypatch.setattr(stocks_api, "clickhouse_available", lambda: True)
    monkeypatch.setattr(stocks_api, "_build_intraday_daily_bar", lambda code: None)

    def fake_scalar(sql: str, params=None, *, raw: bool = False):
        raw_flags.append(raw)
        return 123 if "kline_daily" in sql else 10

    def fake_query_df(sql: str, params=None, *, raw: bool = False):
        raw_flags.append(raw)
        assert "quality_state" not in sql
        return pd.DataFrame(
            [
                {
                    "date_value": date(2026, 11, 19),
                    "code": "000001.SZ",
                    "open": 11.2,
                    "high": 11.4,
                    "low": 11.1,
                    "close": 11.3,
                    "volume": 100000.0,
                    "amount": 1.13e8,
                    "amplitude": 2.68,
                    "change_pct": 0.89,
                    "change_amount": 0.1,
                    "turnover_rate": 0.52,
                }
            ]
        )

    monkeypatch.setattr(stocks_api, "clickhouse_scalar", fake_scalar)
    monkeypatch.setattr(stocks_api, "clickhouse_query_df", fake_query_df)

    stats = stocks_api.get_stock_kline_stats("000001.SZ")
    assert stats["periods"]["1d"]["count"] == 123
    assert stats["periods_with_data"] == 9

    kline = stocks_api.get_stock_kline("000001.SZ", "1d", limit=10)
    assert kline["total"] == 1
    assert kline["quality_source"] == "kline_daily"
    assert kline["data"][0]["quality_state"] == "verified"
    assert raw_flags and all(flag is True for flag in raw_flags)

"""回归测试：/api/market/indices 直查 ClickHouse 基表（raw=True），避免落入废弃质量视图与空结果缓存污染。"""

from __future__ import annotations

from datetime import date
import pandas as pd

import api.market_overview as mo


class _DummySession:
    def close(self) -> None:
        pass


def test_compute_market_indices_uses_raw_clickhouse_queries(monkeypatch):
    """_compute_market_indices 在 ClickHouse 路径下必须全部以 raw=True 直查基表。"""
    calls: list[tuple[str, bool, str]] = []

    def fake_query_df(sql: str, params=None, *, raw: bool = False):
        calls.append(("query_df", raw, sql))
        if "FROM stocks" in sql:
            return pd.DataFrame(
                [
                    {"code": "000001.SH", "name": "上证指数"},
                    {"code": "399001.SZ", "name": "深证成指"},
                ]
            )
        if "COUNT(DISTINCT k.code)" in sql:
            return pd.DataFrame([{"trade_date": date(2026, 11, 19), "row_count": 11}])
        if "FROM kline_daily FINAL" in sql:
            return pd.DataFrame(
                [
                    {"code": "000001.SH", "trade_date": date(2026, 11, 19), "close": 3577.62, "volume": 100, "amount": 6.3e11},
                    {"code": "000001.SH", "trade_date": date(2026, 11, 18), "close": 3550.00, "volume": 95, "amount": 6.0e11},
                    {"code": "399001.SZ", "trade_date": date(2026, 11, 19), "close": 10583.20, "volume": 120, "amount": 6.6e11},
                    {"code": "399001.SZ", "trade_date": date(2026, 11, 18), "close": 10500.00, "volume": 110, "amount": 6.2e11},
                ]
            )
        raise AssertionError(f"Unexpected SQL: {sql}")

    def fake_scalar(sql: str, params=None, *, raw: bool = False):
        calls.append(("scalar", raw, sql))
        if "FROM trade_calendar" in sql:
            return date(2026, 11, 18)
        raise AssertionError(f"Unexpected scalar SQL: {sql}")

    monkeypatch.setattr(mo.db, "get_session", lambda: iter([_DummySession()]))
    monkeypatch.setattr(mo, "clickhouse_available", lambda: True)
    monkeypatch.setattr(mo, "_resolve_table_name", lambda base, candidates=None: base)
    monkeypatch.setattr(mo, "_has_clickhouse_table", lambda name: True)
    monkeypatch.setattr(mo, "clickhouse_query_df", fake_query_df)
    monkeypatch.setattr(mo, "clickhouse_scalar", fake_scalar)
    monkeypatch.setattr(mo, "_latest_qmt_intraday_index_quotes", lambda codes: ({}, {"status": "unavailable"}))
    monkeypatch.setattr(mo, "_latest_qmt_home_index_quotes", lambda codes: ({}, {"status": "unavailable"}))
    monkeypatch.setattr(mo, "latest_index_quotes", lambda: ({}, {"status": "unavailable"}))

    result = mo._compute_market_indices()

    assert result["validation_status"] == "passed"
    assert result["trading_date"] == "2026-11-19"
    assert [item["code"] for item in result["indices"]] == ["000001.SH", "399001.SZ"]
    assert round(result["indices"][0]["change"], 2) == 27.62
    assert len(calls) == 4
    assert all(raw is True for _, raw, _ in calls)


def test_get_market_indices_does_not_cache_empty_payload(monkeypatch):
    """空结果不得写入 TTL 缓存，确保短暂异常后下一次请求可立即恢复。"""
    mo._MARKET_INDICES_RESULT_CACHE.clear()
    responses = iter(
        [
            {"indices": [], "trading_date": None, "validation_status": "failed", "validation_reason": "transient", "blocked_codes": []},
            {"indices": [{"code": "000001.SH", "name": "上证指数", "price": 3577.62}], "trading_date": "2026-11-19", "validation_status": "passed", "validation_reason": "ok", "blocked_codes": []},
        ]
    )
    monkeypatch.setattr(mo, "_compute_market_indices", lambda code=None: next(responses))

    first = mo.get_market_indices()
    assert first["indices"] == []
    assert "" not in mo._MARKET_INDICES_RESULT_CACHE

    second = mo.get_market_indices()
    assert len(second["indices"]) == 1
    assert "" in mo._MARKET_INDICES_RESULT_CACHE
    mo._MARKET_INDICES_RESULT_CACHE.clear()

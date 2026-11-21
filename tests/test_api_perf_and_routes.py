# -*- coding: utf-8 -*-
"""回归测试：API 性能优化（连接池/分区裁剪/SWR/并发）、路由遮蔽 404 与板块 500/pytdx 修复。"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException

import api.operations as operations_api
import api.sectors as sectors_api
import api.stocks as stocks_api
import services.market.market_breadth as mb
import services.mining.panic_rebound_monitor as prm


def test_preview_static_routes_precede_dynamic_code_routes():
    from api.operations_preview import app

    paths = [getattr(r, "path", "") for r in app.router.routes]
    assert "/api/sectors/stock-count" in paths
    assert "/api/sectors/{code}" in paths
    assert paths.index("/api/sectors/stock-count") < paths.index("/api/sectors/{code}")

    assert "/api/stocks/holdings" in paths
    assert "/api/stocks/{code}" in paths
    assert paths.index("/api/stocks/holdings") < paths.index("/api/stocks/{code}")


def test_database_pool_settings_expanded():
    import yaml
    from pathlib import Path

    cfg_path = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    db_cfg = cfg["database"]
    assert int(db_cfg["pool_size"]) >= 12
    assert int(db_cfg["max_overflow"]) >= 12

    db_py = (Path(__file__).resolve().parents[1] / "utils" / "db" / "database.py").read_text(encoding="utf-8")
    assert 'default_pool_size = 12 if db_type == "clickhouse"' in db_py
    assert 'default_max_overflow = 12 if db_type == "clickhouse"' in db_py


def test_sectors_clickhouse_helpers_use_raw_and_change_pct_no_tuple_crash(monkeypatch):
    sectors_api._LATEST_TRADE_DATE_CACHE["value"] = None
    sectors_api._LATEST_TRADE_DATE_CACHE["expires_at"] = 0.0

    scalar_calls: list[tuple[str, bool]] = []
    df_calls: list[tuple[str, bool]] = []

    def fake_scalar(sql: str, params=None, *, raw: bool = False):
        scalar_calls.append((sql, raw))
        return date(2026, 11, 20)

    def fake_query_df(sql: str, params=None, *, raw: bool = False):
        df_calls.append((sql, raw))
        return pd.DataFrame(
            [
                {"code": "600519.SH", "change_pct": 2.0, "volume": 1000.0, "amount": 1.0e8},
                {"code": "000858.SZ", "change_pct": -1.0, "volume": 500.0, "amount": 5.0e7},
            ]
        )

    monkeypatch.setattr(sectors_api, "clickhouse_available", lambda: True)
    monkeypatch.setattr(sectors_api, "clickhouse_table_exists", lambda name: False)
    monkeypatch.setattr(sectors_api, "clickhouse_scalar", fake_scalar)
    monkeypatch.setattr(sectors_api, "clickhouse_query_df", fake_query_df)

    class _FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return self._rows

    class _FakeSession:
        def query(self, *args, **kwargs):
            return _FakeQuery([SimpleNamespace(stock_code="600519.SH"), SimpleNamespace(stock_code="000858.SZ")])

    res = sectors_api._calculate_sector_change_pct(_FakeSession(), "880381.SH")
    assert scalar_calls and all(raw is True for _, raw in scalar_calls)
    assert df_calls and all(raw is True for _, raw in df_calls)
    assert res["change_pct"] == 0.5
    assert res["rise_count"] == 1
    assert res["fall_count"] == 1

    sectors_api._LATEST_TRADE_DATE_CACHE["value"] = None
    sectors_api._LATEST_TRADE_DATE_CACHE["expires_at"] = 0.0


def test_stock_kline_releases_session_early_and_prunes_partitions(monkeypatch):
    events: list[str] = []

    class _Session:
        pass

    monkeypatch.setattr(stocks_api.DatabaseSessionManager, "get_session", lambda: _Session())
    monkeypatch.setattr(stocks_api.DatabaseSessionManager, "close_session", lambda s: events.append("close_session"))
    monkeypatch.setattr(stocks_api, "_get_stock_by_code", lambda s, code: SimpleNamespace(code=code, name="贵州茅台"))
    monkeypatch.setattr(stocks_api, "clickhouse_available", lambda: True)
    monkeypatch.setattr(stocks_api, "_build_intraday_daily_bar", lambda code: None)

    query_sqls: list[str] = []

    def fake_query_df_enough(sql: str, params=None, *, raw: bool = False):
        events.append("query_df")
        query_sqls.append(sql)
        valid_15m_times = [
            "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30",
            "13:15", "13:30", "13:45", "14:00",
        ]
        rows = [
            {
                "date_value": datetime.strptime(f"2026-11-20 {hhmm}", "%Y-%m-%d %H:%M"),
                "code": "600519.SH",
                "open": 1500.0,
                "high": 1505.0,
                "low": 1495.0,
                "close": 1502.0,
                "volume": 100,
                "amount": 1.5e7,
            }
            for hhmm in valid_15m_times
        ]
        return pd.DataFrame(rows)

    monkeypatch.setattr(stocks_api, "clickhouse_query_df", fake_query_df_enough)
    res = stocks_api.get_stock_kline("600519.SH", "15m", limit=10)
    assert res["total"] == 10
    assert events[0] == "close_session", "进入 ClickHouse 查询前必须先释放 ORM session"
    assert len(query_sqls) == 1, "首次分区裁剪已满足 limit 时不应触发全历史回退"
    assert "datetime >= " in query_sqls[0]

    # 回退路径测试：首次裁剪返回为空时应回退全历史一次
    query_sqls.clear()
    calls = {"n": 0}

    def fake_query_df_fallback(sql: str, params=None, *, raw: bool = False):
        calls["n"] += 1
        query_sqls.append(sql)
        if calls["n"] == 1:
            return pd.DataFrame()
        return pd.DataFrame(
            [
                {
                    "date_value": date(2020, 1, 2),
                    "code": "600001.SH",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.9,
                    "close": 10.1,
                    "volume": 1000.0,
                    "amount": 1.0e6,
                    "amplitude": 3.0,
                    "change_pct": 1.0,
                    "change_amount": 0.1,
                    "turnover_rate": 0.5,
                }
            ]
        )

    monkeypatch.setattr(stocks_api, "clickhouse_query_df", fake_query_df_fallback)
    res_fb = stocks_api.get_stock_kline("600001.SH", "1d", limit=10)
    assert res_fb["total"] == 1
    assert calls["n"] == 2
    assert "trade_date >= " in query_sqls[0]
    assert "trade_date >= " not in query_sqls[1]


def test_stock_industry_uses_clickhouse_without_pytdx(monkeypatch):
    monkeypatch.setattr(stocks_api, "clickhouse_available", lambda: True)
    monkeypatch.setattr(
        stocks_api,
        "clickhouse_query_df",
        lambda sql, params=None, *, raw=False: pd.DataFrame(
            [{"industry_code": "880381.SH", "industry_name": "白酒"}]
        ),
    )
    res = stocks_api.get_stock_industry("600519.SH")
    assert res["industry"] == "白酒"
    assert res["industry_code"] == "880381.SH"


def test_data_calendar_swr_nonblocking_and_cold_503(tmp_path, monkeypatch):
    from utils.kline import market_warehouse

    monkeypatch.setattr(operations_api, "runtime_path", lambda *parts: tmp_path.joinpath(*parts))
    monkeypatch.setattr(market_warehouse, "clickhouse_client", lambda: object())
    operations_api._cache.clear()
    operations_api._calendar_refreshing.clear()

    def boom(*args, **kwargs):
        raise RuntimeError("ch down")

    monkeypatch.setattr(operations_api, "build_delivery_calendar", boom)
    with pytest.raises(HTTPException) as exc_info:
        operations_api.data_calendar(days=10)
    assert exc_info.value.status_code == 503

    # 已有过期缓存时走 SWR：即使后台构建阻塞，主请求必须立即返回旧快照
    stale_payload = {"dates": ["2026-11-20"], "datasets": []}
    operations_api._cache[10] = (time.monotonic() - 1000.0, stale_payload)
    gate = threading.Event()
    refreshed_payload = {"dates": ["2026-11-21"], "datasets": []}

    def slow_build(*args, **kwargs):
        gate.wait(timeout=2.0)
        return refreshed_payload

    monkeypatch.setattr(operations_api, "build_delivery_calendar", slow_build)
    t0 = time.perf_counter()
    res = operations_api.data_calendar(days=10)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    assert res is stale_payload
    assert elapsed_ms < 200.0
    gate.set()
    time.sleep(0.05)
    operations_api._cache.clear()
    operations_api._calendar_refreshing.clear()


def test_breadth_sql_has_150_day_partition_pruning():
    sql_daily = mb.build_breadth_sql()
    assert "INTERVAL 150 DAY" in sql_daily
    assert "k.trade_date >= toDate({d0}) - INTERVAL 150 DAY" in sql_daily

    sql_intra = mb.build_intraday_breadth_sql()
    assert "INTERVAL 150 DAY" in sql_intra
    assert "k.trade_date >= toDate({snap_date}) - INTERVAL 150 DAY" in sql_intra


def test_panic_rebound_index_query_pruned_and_cached(monkeypatch):
    with prm._IDX_CHG_LOCK:
        prm._IDX_CHG_CACHE["payload"] = None
        prm._IDX_CHG_CACHE["expires_at"] = 0.0
        prm._IDX_CHG_REFRESHING = False

    queries: list[str] = []

    class _FakeRes:
        result_rows = [
            ("000852.SH", "2026-11-20", 5600.0),
            ("000852.SH", "2026-11-19", 5500.0),
            ("399006.SZ", "2026-11-20", 2220.0),
            ("399006.SZ", "2026-11-19", 2200.0),
        ]

    class _FakeClient:
        def query(self, sql, parameters=None):
            queries.append(sql)
            return _FakeRes()

    monkeypatch.setattr(prm, "clickhouse_client", lambda: _FakeClient())
    out = prm._fetch_latest_pair_index_chg_from_ch()
    assert len(queries) == 1
    assert "today() - 30" in queries[0]
    assert out["000852.SH"]["date"] == "2026-11-20"
    assert out["399006.SZ"]["change_pct"] == pytest.approx(0.91, abs=0.01)

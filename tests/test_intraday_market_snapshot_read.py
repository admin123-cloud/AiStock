# -*- coding: utf-8 -*-
"""services/intraday_market_snapshot.py 读侧单元测试（R28）。

零 DB：clickhouse_query_df / clickhouse_available / clickhouse_table_exists
全 monkeypatch。契约（任务书 20261110_intraday_eastmoney_r28）：
  - 新鲜度：≤600s 可用、>600s/缺失/未来超容忍 → None（fail-closed 回退昨收）；
  - sentiment overlay 形状对齐 QMT 覆盖层消费键（date/as_of/up_count/.../
    change_distribution/source + mode/turnover_source）；
  - 涨跌停分类 = R20b 板块阈值（4.8/29.8/19.8/9.8 对称）作用于实时 pct（SQL 守卫）；
  - 指数 overlay 形状（price/pre_close/snapshot_date/snapshot_time/source）；
  - emotion 点形状（与 _intraday_emotion_snapshot 字段一致）；
  - 北京钟守卫：查询谓词用 now('Asia/Shanghai')，无 ±8h 补丁；
  - 覆盖率门禁：covered/expected < 0.90 或 covered < 1000 → None。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.intraday_market_snapshot as svc  # noqa: E402

BJ = ZoneInfo("Asia/Shanghai")


def _patch_ch(monkeypatch, df_by_sql, *, tables=None, available=True):
    queries = []

    def fake_query_df(sql, params=None, *, raw=False):
        queries.append(sql)
        for key, df in df_by_sql.items():
            if key in sql:
                return df
        return pd.DataFrame()

    def fake_available():
        return available

    def fake_exists(name):
        return tables is None or name in tables

    monkeypatch.setattr(svc, "clickhouse_query_df", fake_query_df)
    monkeypatch.setattr(svc, "clickhouse_available", fake_available)
    monkeypatch.setattr(svc, "clickhouse_table_exists", fake_exists)
    return queries


def _sentiment_df(ts, covered=5573, expected=5907):
    return pd.DataFrame([{
        "latest_ts": ts,
        "covered_count": covered,
        "up_count": 3100, "down_count": 2100, "unchanged_count": 373,
        "up_5_count": 400, "down_5_count": 120,
        "limit_up_count": 45, "limit_down_count": 3,
        "avg_change_percent": 0.53, "total_amount": 1.2e12,
        "sh_amount": 5.0e11, "sz_amount": 6.5e11, "bj_amount": 5.0e9,
        "bucket_up_7": 120, "bucket_up_5_7": 280, "bucket_up_3_5": 350,
        "bucket_up_0_3": 2350, "bucket_zero": 373, "bucket_down_0_3": 1600,
        "bucket_down_3_5": 300, "bucket_down_5_7": 100, "bucket_down_7": 100,
    }])


def _expected_df(n=5907):
    return pd.DataFrame([{"cnt": n}])


NOW = datetime(2026, 11, 10, 11, 0, 0, tzinfo=BJ)


class TestFreshness:
    def test_fresh_within_600s(self):
        assert svc.is_fresh(NOW - timedelta(seconds=599), NOW) is True
        assert svc.is_fresh(NOW - timedelta(seconds=601), NOW) is False

    def test_naive_treated_as_beijing_wall(self):
        # 驱动降级路径：naive ts 按北京墙补 +08:00
        naive = NOW.replace(tzinfo=None) - timedelta(minutes=2)
        assert svc.is_fresh(naive, NOW) is True

    def test_future_small_skew_tolerated(self):
        assert svc.is_fresh(NOW + timedelta(seconds=60), NOW) is True
        assert svc.is_fresh(NOW + timedelta(seconds=600), NOW) is False

    def test_none_and_garbage_not_fresh(self):
        assert svc.is_fresh(None, NOW) is False
        assert svc.is_fresh("not-a-date", NOW) is False

    def test_string_ts_parsed(self):
        assert svc.is_fresh("2026-11-10 10:55:00", NOW) is True


class TestSentimentOverlay:
    def test_success_shape(self, monkeypatch):
        ts = NOW - timedelta(minutes=3)
        queries = _patch_ch(monkeypatch, {
            "countIf(change_pct > 0)": _sentiment_df(ts),
            "FROM stocks WHERE type = 'stock'": _expected_df(),
        })
        payload = svc.latest_market_sentiment_intraday(now=NOW)
        assert payload is not None
        # QMT 覆盖层消费键全在（market_overview 合并块直接使用）
        for key in ("date", "as_of", "covered_count", "expected_count",
                    "coverage_ratio", "up_count", "down_count", "unchanged_count",
                    "up_5_percent_count", "down_5_percent_count", "limit_up_count",
                    "limit_down_count", "avg_change_percent", "total_turnover",
                    "sh_amount", "sz_amount", "bj_amount", "change_distribution",
                    "source", "mode", "turnover_source"):
            assert key in payload, key
        assert payload["date"] == "2026-11-10"
        assert payload["as_of"] == ts.strftime("%Y-%m-%d %H:%M")
        assert payload["source"] == svc.SOURCE_TAG
        assert payload["mode"] == "eastmoney_snapshot"
        assert len(payload["change_distribution"]) == 9
        assert payload["coverage_ratio"] == round(5573 / 5907, 4)

    def test_stale_over_10min_returns_none(self, monkeypatch):
        _patch_ch(monkeypatch, {
            "countIf(change_pct > 0)": _sentiment_df(NOW - timedelta(minutes=11)),
            "FROM stocks WHERE type = 'stock'": _expected_df(),
        })
        assert svc.latest_market_sentiment_intraday(now=NOW) is None

    def test_coverage_below_threshold_none(self, monkeypatch):
        ts = NOW - timedelta(minutes=2)
        _patch_ch(monkeypatch, {
            "countIf(change_pct > 0)": _sentiment_df(ts, covered=5000),
            "FROM stocks WHERE type = 'stock'": _expected_df(5907),
        })
        assert svc.latest_market_sentiment_intraday(now=NOW) is None  # 5000/5907 < 0.90

    def test_absolutely_small_universe_none(self, monkeypatch):
        ts = NOW - timedelta(minutes=2)
        _patch_ch(monkeypatch, {
            "countIf(change_pct > 0)": _sentiment_df(ts, covered=900),
            "FROM stocks WHERE type = 'stock'": _expected_df(901),
        })
        assert svc.latest_market_sentiment_intraday(now=NOW) is None  # < 1000

    def test_query_error_returns_none(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("ch down")
        monkeypatch.setattr(svc, "clickhouse_available", lambda: True)
        monkeypatch.setattr(svc, "clickhouse_table_exists", lambda _n: True)
        monkeypatch.setattr(svc, "clickhouse_query_df", boom)
        assert svc.latest_market_sentiment_intraday(now=NOW) is None

    def test_missing_table_returns_none(self, monkeypatch):
        _patch_ch(monkeypatch, {}, tables=[])
        assert svc.latest_market_sentiment_intraday(now=NOW) is None

    def test_limit_threshold_sql_is_board_aware(self, monkeypatch):
        ts = NOW - timedelta(minutes=3)
        queries = _patch_ch(monkeypatch, {
            "countIf(change_pct > 0)": _sentiment_df(ts),
            "FROM stocks WHERE type = 'stock'": _expected_df(),
        })
        svc.latest_market_sentiment_intraday(now=NOW)
        sql = " ".join("\n".join(queries).split()).replace("- ", "-")  # 压平空白后断言
        # R20b 板块阈值逻辑作用于实时 pct（对称上下限）
        for token in ("4.8", "29.8", "19.8", "9.8"):
            assert token in sql, token
        assert "change_pct >= multiIf" in sql
        assert "change_pct <= -multiIf" in sql
        assert "upperUTF8(name) LIKE 'ST%'" in sql
        assert "endsWith(code, '.BJ')" in sql


class TestIndexOverlay:
    def _index_df(self, ts):
        return pd.DataFrame([
            {"code": "000001.SH", "name": "上证指数", "price": 3671.35,
             "change_pct": 0.29, "change_amount": 10.56, "prev_close": 3660.79,
             "volume": 1.7e8, "amount": 3.0e11, "snapshot_ts": ts},
            {"code": "399006.SZ", "name": "创业板指", "price": 2508.9,
             "change_pct": 2.05, "change_amount": 50.34, "prev_close": 2458.56,
             "volume": 7.9e7, "amount": 1.9e11, "snapshot_ts": ts},
        ])

    def test_live_shape(self, monkeypatch):
        ts = NOW - timedelta(minutes=4)
        _patch_ch(monkeypatch, {"FROM intraday_index_snapshot": self._index_df(ts)})
        quotes, status = svc.latest_index_quotes(now=NOW)
        assert status["status"] == "live" and status["quote_count"] == 2
        sh = quotes["000001.SH"]
        for key in ("price", "pre_close", "change_pct", "volume", "amount",
                    "snapshot_date", "snapshot_time", "source"):
            assert key in sh, key
        assert sh["price"] == 3671.35 and sh["pre_close"] == 3660.79
        assert sh["snapshot_date"].isoformat() == "2026-11-10"
        assert sh["source"] == "eastmoney_live_snapshot"

    def test_stale_returns_empty(self, monkeypatch):
        ts = NOW - timedelta(minutes=15)
        _patch_ch(monkeypatch, {"FROM intraday_index_snapshot": self._index_df(ts)})
        quotes, status = svc.latest_index_quotes(now=NOW)
        assert quotes == {} and status["status"] == "stale"

    def test_missing_table_unavailable(self, monkeypatch):
        _patch_ch(monkeypatch, {}, tables=[])
        quotes, status = svc.latest_index_quotes(now=NOW)
        assert quotes == {} and status["status"] == "unavailable"


class TestEmotionPoint:
    def test_shape(self, monkeypatch):
        ts = NOW - timedelta(minutes=3)
        _patch_ch(monkeypatch, {
            "avg(change_pct > 0)": pd.DataFrame([{
                "latest_ts": ts, "total_stocks": 5573,
                "close_up_rate": 55.6, "sh_up_rate": 54.0, "sz_up_rate": 56.5,
                "cyb_up_rate": 62.1, "strong_up_rate": 12.3, "weak_up_rate": 4.2,
            }]),
        })
        point = svc.emotion_live_point(now=NOW)
        assert point is not None
        for key in ("date", "as_of", "total_stocks", "close_up_rate",
                    "intraday_up_rate", "sh_up_rate", "sz_up_rate", "cyb_up_rate",
                    "strong_up_rate", "weak_up_rate", "is_temporary", "source"):
            assert key in point, key
        assert point["is_temporary"] is True
        assert point["intraday_up_rate"] == point["close_up_rate"]
        assert point["source"] == "clickhouse:intraday_market_snapshot"

    def test_stale_none(self, monkeypatch):
        ts = NOW - timedelta(minutes=20)
        _patch_ch(monkeypatch, {
            "avg(change_pct > 0)": pd.DataFrame([{
                "latest_ts": ts, "total_stocks": 5573, "close_up_rate": 55.0,
                "sh_up_rate": 54.0, "sz_up_rate": 56.0, "cyb_up_rate": 60.0,
                "strong_up_rate": 10.0, "weak_up_rate": 4.0,
            }]),
        })
        assert svc.emotion_live_point(now=NOW) is None


class TestBeijingClockGuard:
    """快照链钟面守卫：谓词用 now('Asia/Shanghai')（R27 统一语义），无 ±8h 补丁。"""

    def test_queries_use_beijing_now_no_offset_patch(self, monkeypatch):
        ts = NOW - timedelta(minutes=3)
        queries = _patch_ch(monkeypatch, {
            "countIf(change_pct > 0)": _sentiment_df(ts),
            "FROM stocks WHERE type = 'stock'": _expected_df(),
            "FROM intraday_index_snapshot": pd.DataFrame([{
                "code": "000001.SH", "name": "上证指数", "price": 3671.35,
                "change_pct": 0.29, "change_amount": 10.56, "prev_close": 3660.79,
                "volume": 1.0, "amount": 1.0, "snapshot_ts": ts}]),
        })
        svc.latest_market_sentiment_intraday(now=NOW)
        svc.latest_index_quotes(now=NOW)
        svc.emotion_live_point(now=NOW)
        svc.latest_snapshot_ts()
        joined = "\n".join(queries)
        assert "now('Asia/Shanghai')" in joined
        # R27b 之后不得再出现 ±8h 读侧补丁
        assert "INTERVAL 8 HOUR" not in joined
        assert "+ INTERVAL 8" not in joined
        assert "- INTERVAL 8" not in joined
        # R20b 教训：不写 toDate(dt)=X 形谓词（对 toYYYYMM 分区不裁剪）
        assert "toDate(snapshot_ts) =" not in joined

    def test_latest_snapshot_ts_scalar(self, monkeypatch):
        ts = NOW - timedelta(minutes=2)
        _patch_ch(monkeypatch, {"max(snapshot_ts)": pd.DataFrame([{"latest_ts": ts}])})
        assert svc.latest_snapshot_ts() == ts

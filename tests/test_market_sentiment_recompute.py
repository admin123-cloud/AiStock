# -*- coding: utf-8 -*-
"""R20（2026-11-09）首页情绪快照按需重算的单元测试（先于实现编写，TDD）。

口径提取（不重写）：快照产物字段 ← services/market_sentiment_cache.refresh_daily_history
的 kline_daily 聚合 SQL（api/system_config.py refresh_market_sentiment_snapshot_task
调用的本体）：
  universe = kline_daily JOIN stocks WHERE s.type='stock' AND k.open>0
  up/down/unchanged = pct>0 / pct<0 / pct=0
  up_5/down_5       = pct>=5 / pct<=-5
  limit_up/down     = pct>=9.9 / pct<=-9.9
  9 桶分布边界      = >=7 / [5,7) / [3,5) / (0,3) / =0 / (-3,0) / (-5,-3] / (-7,-5] / <=-7
与 api/market_overview.py 既有 kline_daily 回退路径的 SQL 完全一致（R20 只是把
该路径变成唯一路径，停读 market_sentiment_snapshot 预计算快照）。

本文件用合成两日 kline 数据（全部经 monkeypatch 的数据访问层注入）验证：
  1) /market/sentiment 载荷计数/分布/日期与合成数据的手算期望一致，
     且发给 CH 的聚合 SQL 保留上述口径 token、日期参数=最新有数据交易日；
  2) R20 缓存分档：盘中 TTL=300s 到期重算、盘后绑定 kline_daily max(trade_date)
     命中缓存、数据日期推进触发重算、分支切换优先级、error 载荷不缓存；
  3) 快照表读取路径停用：源码不再引用 load_homepage_sentiment；
  4) 载荷字段结构与快照时代完全一致（含 intraday_status / latest_is_provisional）。

真实库上的口径对账由部署后独立 SQL 复算完成（任务书第 3 条），不在本文件范围。
"""
from datetime import date, datetime, time as dt_time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import api.market_overview as mo

SH_TZ = ZoneInfo("Asia/Shanghai")
D1 = "2026-11-06"  # 前一交易日
D2 = "2026-11-09"  # 最新有数据交易日
D3 = "2026-11-10"  # 补数后的新数据日

# 合成 kline_daily 行: (code, market, change_pct, amount, open_ok)
# D2 共 17 行，其中 1 行 open=0 必须被口径排除，其余 16 行手算期望见下。
D2_ROWS = [
    ("600001.SH", "SH", 2.5, 1.00e8, True),
    ("600002.SH", "SH", 6.2, 1.50e8, True),
    ("600003.SH", "SH", 10.0, 2.00e8, True),
    ("600004.SH", "SH", 3.0, 0.50e8, True),
    ("600005.SH", "SH", 7.5, 3.00e8, True),
    ("600006.SH", "SH", 4.9, 0.20e8, True),
    ("600007.SH", "SH", 0.4, 0.10e8, True),
    ("600009.SH", "SH", 9.95, 4.00e8, True),
    ("000001.SZ", "SZ", -1.0, 0.80e8, True),
    ("000002.SZ", "SZ", -5.5, 0.90e8, True),
    ("000003.SZ", "SZ", 0.0, 0.30e8, True),
    ("000004.SZ", "SZ", -10.0, 0.60e8, True),
    ("000005.SZ", "SZ", -3.0, 0.40e8, True),
    ("000006.SZ", "SZ", -4.9, 0.35e8, True),
    ("000007.SZ", "SZ", -6.9, 0.45e8, True),
    ("000008.SZ", "SZ", -0.4, 0.25e8, True),
    ("600010.SH", "SH", 3.3, 0.55e8, False),  # open=0 → 口径排除
]
D1_ROWS = [
    ("600001.SH", "SH", 1.0, 0.50e8, True),
    ("600002.SH", "SH", -2.0, 0.60e8, True),
    ("000001.SZ", "SZ", 4.9, 0.70e8, True),
    ("000002.SZ", "SZ", 0.0, 0.80e8, True),
]
ROWS_BY_DATE = {D2: D2_ROWS, D1: D1_ROWS}

# ── 手算期望（独立于下方 fake 聚合helper，双保险） ─────────────────────────
EXP_D2 = {
    "up": 8, "down": 7, "unchanged": 1,
    "up_5": 4, "down_5": 3,
    "limit_up": 2, "limit_down": 1,          # 9.95 与 10.0 均 >=9.9；-10.0 <=-9.9
    "buckets": [3, 1, 2, 2, 1, 2, 2, 2, 1],  # >=7,5~7,3~5,0~3,0,-3~0,-5~-3,-7~-5,<=-7
    "avg": 12.75 / 16,                        # (44.45 - 31.7) / 16 = 0.796875
    "sh_amount": 12.30e8, "sz_amount": 4.05e8, "total": 16.35e8,
}
EXP_D1 = {"up": 2, "down": 1, "unchanged": 1, "up_5": 0, "down_5": 0,
          "limit_up": 0, "limit_down": 0, "total": 2.60e8,
          "sh_amount": 1.10e8, "sz_amount": 1.50e8}

# 快照时代载荷的顶层键集合（响应字段结构不变的冻结契约）
SNAPSHOT_ERA_KEYS = {
    "date", "up_count", "down_count", "unchanged_count",
    "up_5_percent_count", "down_5_percent_count",
    "limit_up_count", "limit_down_count", "avg_change_percent",
    "total_turnover", "sh_amount", "sz_amount", "turnover_source",
    "change_distribution", "up_down_5_trend_7d", "emotion_curve_30d",
    "turnover_trend_30d", "turnover_forecast_intraday", "sentiment_score",
    "sentiment_level", "emotion_phase", "source", "intraday_status",
}

# 发往 CH 的汇总 SQL 必须保留的口径 token（防口径漂移）
CALIBER_TOKENS = [
    "s.type = 'stock'",
    "k.open > 0",
    "pct > 0", "pct < 0", "pct = 0",
    "pct >= 5", "pct <= -5",
    "pct >= 9.9", "pct <= -9.9",
    "pct >= 7",
    "pct >= 5 AND pct < 7",
    "pct >= 3 AND pct < 5",
    "pct > 0 AND pct < 3",
    "pct < 0 AND pct > -3",
    "pct <= -3 AND pct > -5",
    "pct <= -5 AND pct > -7",
    "pct <= -7",
]


def _aggregate(rows):
    """按提取口径把合成行聚合成 summary 行（仅喂给 fake 查询层）。"""
    valid = [r for r in rows if r[4]]
    pcts = [r[2] for r in valid]

    def cnt(fn):
        return sum(1 for p in pcts if fn(p))

    def amt(market=None):
        return float(sum(r[3] for r in valid if market is None or r[1] == market))

    return {
        "up_count": cnt(lambda p: p > 0),
        "down_count": cnt(lambda p: p < 0),
        "unchanged_count": cnt(lambda p: p == 0),
        "up_5_count": cnt(lambda p: p >= 5),
        "down_5_count": cnt(lambda p: p <= -5),
        "limit_up_count": cnt(lambda p: p >= 9.9),
        "limit_down_count": cnt(lambda p: p <= -9.9),
        "avg_change_percent": sum(pcts) / len(pcts) if pcts else 0.0,
        "total_amount": amt(),
        "sh_amount": amt("SH"),
        "sz_amount": amt("SZ"),
        "bucket_up_7": cnt(lambda p: p >= 7),
        "bucket_up_5_7": cnt(lambda p: 5 <= p < 7),
        "bucket_up_3_5": cnt(lambda p: 3 <= p < 5),
        "bucket_up_0_3": cnt(lambda p: 0 < p < 3),
        "bucket_zero": cnt(lambda p: p == 0),
        "bucket_down_0_3": cnt(lambda p: -3 < p < 0),
        "bucket_down_3_5": cnt(lambda p: -5 < p <= -3),
        "bucket_down_5_7": cnt(lambda p: -7 < p <= -5),
        "bucket_down_7": cnt(lambda p: p <= -7),
    }


class _FakeQuery:
    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return None


class _FakeSession:
    def query(self, *a, **k):
        return _FakeQuery()

    def close(self):
        pass


class _FakeClock:
    """替身 time 模块：仅提供 monotonic()。"""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


class _FakeClickhouse:
    """按 SQL 形状路由的 fake 数据访问层，记录全部 (sql, params, raw)。"""

    def __init__(self, latest_date):
        self.latest_date = latest_date
        self.calls = []

    def available(self):
        return True

    def scalar(self, sql, params=None, *, raw=False):
        self.calls.append(("scalar", sql, params, raw))
        if "MAX(k.trade_date)" in sql:
            # clickhouse_connect 对 Date 列返回 datetime.date（非 str）
            return date.fromisoformat(self.latest_date)
        raise AssertionError(f"unexpected scalar sql: {sql[:120]}")

    def query_df(self, sql, params=None, *, raw=False):
        self.calls.append(("df", sql, params, raw))
        if "GROUP BY trade_date" in sql:
            rows = []
            for date in (D1, D2):
                agg = _aggregate(ROWS_BY_DATE[date])
                rows.append({
                    "trade_date": date,
                    "total_amount": agg["total_amount"],
                    "sh_amount": agg["sh_amount"],
                    "sz_amount": agg["sz_amount"],
                    "bj_amount": 0.0,
                    "up_count": agg["up_count"],
                    "down_count": agg["down_count"],
                    "limit_up_count": agg["limit_up_count"],
                    "limit_down_count": agg["limit_down_count"],
                    "up_5_count": agg["up_5_count"],
                    "down_5_count": agg["down_5_count"],
                })
            return pd.DataFrame(rows)
        if "SELECT DISTINCT k.trade_date" in sql:
            return pd.DataFrame({"trade_date": [D2, D1]})
        if "SELECT s.market, SUM(k.amount)" in sql:
            agg = _aggregate(D2_ROWS)
            return pd.DataFrame({
                "market": ["SH", "SZ"],
                "amount": [agg["sh_amount"], agg["sz_amount"]],
            })
        if "AS up_count" in sql:
            assert params == [D2], f"summary 必须按最新有数据交易日查询: {params}"
            agg = _aggregate(D2_ROWS)
            return pd.DataFrame([agg])
        raise AssertionError(f"unexpected df sql: {sql[:120]}")


@pytest.fixture(autouse=True)
def _clear_caches():
    mo._MARKET_SENTIMENT_RESULT_CACHE.clear()
    yield
    mo._MARKET_SENTIMENT_RESULT_CACHE.clear()


@pytest.fixture
def fake_environment(monkeypatch):
    """把 _compute_market_sentiment 的真实数据访问全部替换为合成数据。"""
    fake = _FakeClickhouse(latest_date=D2)
    monkeypatch.setattr(mo, "clickhouse_available", fake.available)
    monkeypatch.setattr(mo, "_has_clickhouse_table", lambda name: True)
    monkeypatch.setattr(mo, "_resolve_table_name", lambda base, candidates=None: base)
    monkeypatch.setattr(mo, "clickhouse_query_df", fake.query_df)
    monkeypatch.setattr(mo, "clickhouse_scalar", fake.scalar)
    monkeypatch.setattr(mo, "_load_qmt_intraday_market_sentiment", lambda: None)
    monkeypatch.setattr(
        mo, "get_intraday_turnover_forecast",
        lambda **kw: {"available": False, "reason": "unit-test"},
    )
    monkeypatch.setattr(mo, "db", SimpleNamespace(get_session=lambda: iter([_FakeSession()])))
    # 端点缓存层默认走"盘后 + 日期绑定"分支，避免真实时钟/日历干扰口径测试
    monkeypatch.setattr(mo, "_is_sentiment_intraday_session", lambda now: False)
    monkeypatch.setattr(mo, "_kline_daily_latest_trade_date", lambda: D2)
    return fake


# ── 1) 口径：合成两日数据 → 计数/涨停/分布/日期 ────────────────────────────

def test_sentiment_counts_match_synthetic_caliber(fake_environment):
    payload = mo.get_market_sentiment()

    assert payload["date"] == D2
    assert payload["up_count"] == EXP_D2["up"]
    assert payload["down_count"] == EXP_D2["down"]
    assert payload["unchanged_count"] == EXP_D2["unchanged"]
    assert payload["up_5_percent_count"] == EXP_D2["up_5"]
    assert payload["down_5_percent_count"] == EXP_D2["down_5"]
    assert payload["limit_up_count"] == EXP_D2["limit_up"]
    assert payload["limit_down_count"] == EXP_D2["limit_down"]
    # open=0 的 600010.SH 不得进入任何口径（16 而非 17）
    assert payload["up_count"] + payload["down_count"] + payload["unchanged_count"] == 16
    # 载荷对均值保留 3 位小数（快照路径既有行为）
    assert payload["avg_change_percent"] == pytest.approx(round(EXP_D2["avg"], 3))
    assert payload["total_turnover"] == pytest.approx(EXP_D2["total"])
    assert payload["sh_amount"] == pytest.approx(EXP_D2["sh_amount"])
    assert payload["sz_amount"] == pytest.approx(EXP_D2["sz_amount"])


def test_sentiment_distribution_buckets_match_caliber(fake_environment):
    payload = mo.get_market_sentiment()
    labels = [">=7", "5~7", "3~5", "0~3", "0", "-3~0", "-5~-3", "-7~-5", "<=-7"]
    sides = ["up", "up", "up", "up", "flat", "down", "down", "down", "down"]
    distribution = payload["change_distribution"]
    assert [item["label"] for item in distribution] == labels
    assert [item["side"] for item in distribution] == sides
    assert [item["count"] for item in distribution] == EXP_D2["buckets"]
    assert sum(item["count"] for item in distribution) == 16


def test_sentiment_trend_and_curve_use_both_trade_dates(fake_environment):
    payload = mo.get_market_sentiment()
    trend = payload["up_down_5_trend_7d"]
    assert trend["trade_dates"] == [D1, D2]
    assert trend["up_5_counts"] == [EXP_D1["up_5"], EXP_D2["up_5"]]
    assert trend["down_5_counts"] == [EXP_D1["down_5"], EXP_D2["down_5"]]
    assert trend["latest_date"] == D2
    curve = payload["emotion_curve_30d"]
    assert curve["trade_dates"] == [D1, D2]
    assert curve["raw_series"]["up_count_series"] == [EXP_D1["up"], EXP_D2["up"]]
    assert curve["raw_series"]["limit_up_count_series"] == [EXP_D1["limit_up"], EXP_D2["limit_up"]]
    assert curve["latest_date"] == D2
    turnover = payload["turnover_trend_30d"]
    assert turnover["trade_dates"] == [D1, D2]
    assert turnover["amounts"] == pytest.approx([EXP_D1["total"], EXP_D2["total"]])
    assert turnover["latest_is_provisional"] is False


def test_sentiment_payload_structure_matches_snapshot_era(fake_environment):
    payload = mo.get_market_sentiment()
    assert set(payload.keys()) == SNAPSHOT_ERA_KEYS
    assert payload["intraday_status"] is None
    score = 50 + (EXP_D2["up"] - EXP_D2["down"]) / 16 * 50
    assert payload["sentiment_score"] == pytest.approx(score)
    assert payload["sentiment_level"]


def test_summary_sql_preserves_caliber_tokens(fake_environment):
    mo.get_market_sentiment()
    summary_calls = [
        call for call in fake_environment.calls
        if call[0] == "df" and "AS up_count" in call[1] and "GROUP BY trade_date" not in call[1]
    ]
    assert summary_calls, "必须存在对最新交易日的 kline_daily 汇总聚合"
    sql = summary_calls[0][1]
    for token in CALIBER_TOKENS:
        assert token in sql, f"汇总 SQL 丢失口径 token: {token}"


def test_snapshot_table_read_path_is_retired(fake_environment, monkeypatch):
    """停用预计算快照：compute 路径不得再调 load_homepage_sentiment。"""
    import services.market_sentiment_cache as cache_module

    def _boom(*a, **k):
        raise AssertionError("load_homepage_sentiment must not be called")

    monkeypatch.setattr(cache_module, "load_homepage_sentiment", _boom)
    payload = mo.get_market_sentiment()
    assert payload["date"] == D2


def test_market_overview_source_dropped_snapshot_dependency():
    """源码级守卫：market_overview.py 不得再引用快照读取与旧 45s 常量。"""
    source = Path(mo.__file__).read_text(encoding="utf-8")
    assert "load_homepage_sentiment" not in source
    assert "_MARKET_SENTIMENT_RESULT_CACHE_TTL_SECONDS" not in source
    assert "_SENTIMENT_INTRADAY_TTL_SECONDS = 300.0" in source


# ── 2) 缓存分档：盘中 TTL / 盘后日期绑定 ───────────────────────────────────

def _patch_endpoint_plumbing(monkeypatch, *, intraday, key_date, payload=None):
    calls = {"compute": 0}

    def _compute():
        calls["compute"] += 1
        return payload if payload is not None else {"date": key_date or D2, "up_count": 1}

    clock = _FakeClock()
    monkeypatch.setattr(mo, "time", clock)
    monkeypatch.setattr(mo, "_compute_market_sentiment", _compute)
    monkeypatch.setattr(mo, "_is_sentiment_intraday_session", lambda now: intraday)
    monkeypatch.setattr(mo, "_kline_daily_latest_trade_date", lambda: key_date)
    return calls, clock


def test_intraday_ttl_300s_recomputes_on_expiry(monkeypatch):
    calls, clock = _patch_endpoint_plumbing(monkeypatch, intraday=True, key_date=D2)

    assert mo.get_market_sentiment()  # t=1000 计算
    assert calls["compute"] == 1
    assert mo._MARKET_SENTIMENT_RESULT_CACHE[""]["expires_at"] == pytest.approx(1300.0)

    clock.now = 1000 + 299
    mo.get_market_sentiment()
    assert calls["compute"] == 1, "TTL 内必须命中缓存"

    clock.now = 1000 + 301
    mo.get_market_sentiment()
    assert calls["compute"] == 2, "TTL=300s 到期后必须重算"


def test_after_hours_cache_bound_to_max_trade_date(monkeypatch):
    calls, _ = _patch_endpoint_plumbing(monkeypatch, intraday=False, key_date=D2)

    mo.get_market_sentiment()
    assert calls["compute"] == 1

    mo.get_market_sentiment()
    mo.get_market_sentiment()
    assert calls["compute"] == 1, "盘后数据日期不变必须命中缓存"


def test_after_hours_date_advance_triggers_recompute(monkeypatch):
    holder = {"key": D2}
    calls, _ = _patch_endpoint_plumbing(
        monkeypatch, intraday=False, key_date=None,
    )
    monkeypatch.setattr(mo, "_kline_daily_latest_trade_date", lambda: holder["key"])

    mo.get_market_sentiment()
    assert calls["compute"] == 1

    holder["key"] = D3  # 当天补数完成，max(trade_date) 推进
    mo.get_market_sentiment()
    assert calls["compute"] == 2, "数据日期推进后的首请求必须重算"

    mo.get_market_sentiment()
    assert calls["compute"] == 2, "重算后同样数据日期必须命中缓存"


def test_branch_switch_priority_after_hours_ignores_intraday_ttl(monkeypatch):
    calls, clock = _patch_endpoint_plumbing(monkeypatch, intraday=True, key_date=D2)
    sentinel = {"date": D2, "sentinel": True}
    mo._MARKET_SENTIMENT_RESULT_CACHE[""] = {
        "expires_at": clock.now + 9999.0,  # 盘中 TTL 仍有效
        "data_date": D2,
        "payload": sentinel,
    }
    monkeypatch.setattr(mo, "_is_sentiment_intraday_session", lambda now: False)

    assert mo.get_market_sentiment() is sentinel
    assert calls["compute"] == 0, "盘后分支只看数据日期绑定，不吃盘中 TTL 余量"


def test_branch_switch_intraday_with_expired_entry_recomputes(monkeypatch):
    calls, clock = _patch_endpoint_plumbing(monkeypatch, intraday=False, key_date=D2)
    mo._MARKET_SENTIMENT_RESULT_CACHE[""] = {
        "expires_at": clock.now - 1.0,  # 早已过期
        "data_date": D2,
        "payload": {"date": D2},
    }
    monkeypatch.setattr(mo, "_is_sentiment_intraday_session", lambda now: True)

    mo.get_market_sentiment()
    assert calls["compute"] == 1, "盘中分支 TTL 过期即重算"


def test_error_payload_is_not_cached(monkeypatch):
    calls, _ = _patch_endpoint_plumbing(
        monkeypatch, intraday=True, key_date=D2, payload={"error": "boom"},
    )
    mo.get_market_sentiment()
    mo.get_market_sentiment()
    assert calls["compute"] == 2
    assert mo._MARKET_SENTIMENT_RESULT_CACHE == {}


def test_key_query_failure_falls_back_to_recompute(monkeypatch):
    calls, _ = _patch_endpoint_plumbing(monkeypatch, intraday=False, key_date=None)
    mo.get_market_sentiment()
    mo.get_market_sentiment()
    assert calls["compute"] == 2, "max(trade_date) 查不到（None）时不允许吃旧缓存"


# ── 3) 盘中判定：Asia/Shanghai 交易日 09:15~15:00 ──────────────────────────

def _patch_calendar(monkeypatch, is_trading_day):
    from scheduler.trading_calendar import TradingCalendar

    monkeypatch.setattr(
        TradingCalendar, "is_trading_day",
        staticmethod(lambda date=None: is_trading_day),
    )


def test_session_window_boundaries_on_trading_day(monkeypatch):
    _patch_calendar(monkeypatch, True)
    cases = [
        (dt_time(9, 14, 59), False),
        (dt_time(9, 15, 0), True),
        (dt_time(12, 0, 0), True),
        (dt_time(14, 59, 59), True),
        (dt_time(15, 0, 0), True),
        (dt_time(15, 0, 1), False),
        (dt_time(23, 0, 0), False),
    ]
    for moment, expected in cases:
        now_sh = datetime(2026, 11, 9, moment.hour, moment.minute, moment.second, tzinfo=SH_TZ)
        assert mo._is_sentiment_intraday_session(now_sh) is expected, f"{moment} 应为 {expected}"


def test_session_window_non_trading_day_is_after_hours(monkeypatch):
    _patch_calendar(monkeypatch, False)
    now_sh = datetime(2026, 11, 8, 10, 0, 0, tzinfo=SH_TZ)  # 周日盘中时刻
    assert mo._is_sentiment_intraday_session(now_sh) is False


def test_session_window_calendar_failure_fails_closed(monkeypatch):
    from scheduler.trading_calendar import TradingCalendar

    def _raise(date=None):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(TradingCalendar, "is_trading_day", staticmethod(_raise))
    now_sh = datetime(2026, 11, 9, 10, 0, 0, tzinfo=SH_TZ)
    assert mo._is_sentiment_intraday_session(now_sh) is False


# ── 4) 盘后缓存键：轻量 raw 查询通道 ───────────────────────────────────────

def test_latest_trade_date_key_uses_raw_lightweight_query(monkeypatch):
    seen = []

    def _scalar(sql, params=None, *, raw=False):
        seen.append((sql, raw))
        return D2

    monkeypatch.setattr(mo, "clickhouse_available", lambda: True)
    monkeypatch.setattr(mo, "_has_clickhouse_table", lambda name: True)
    monkeypatch.setattr(mo, "_resolve_table_name", lambda base, candidates=None: base)
    monkeypatch.setattr(mo, "clickhouse_scalar", _scalar)

    assert mo._kline_daily_latest_trade_date() == D2
    sql, raw = seen[0]
    assert raw is True, "缓存键查询必须走 raw 轻量通道（免 FINAL）"
    assert "max(trade_date)" in sql


def test_latest_trade_date_key_returns_none_when_clickhouse_down(monkeypatch):
    monkeypatch.setattr(mo, "clickhouse_available", lambda: False)
    assert mo._kline_daily_latest_trade_date() is None

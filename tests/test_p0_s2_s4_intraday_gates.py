# -*- coding: utf-8 -*-
"""P0 (S2 / S3 / S4) 盘中门禁、三态日历 fail-closed 与 API 零写入回放测试。

全部离线 mock，不连接实盘 ClickHouse 正式表、不发外网请求。
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BJ = timezone(timedelta(hours=8))


class TestIntradayPhaseGate:
    def test_window_and_cutoff_strict_lt_1505(self):
        from services import intraday_phase_gate as gate

        assert gate.in_intraday_schedule_window(datetime(2026, 11, 18, 9, 34, 59, tzinfo=BJ)) is False
        assert gate.in_intraday_schedule_window(datetime(2026, 11, 18, 9, 35, 0, tzinfo=BJ)) is True
        assert gate.in_intraday_schedule_window(datetime(2026, 11, 18, 15, 4, 59, tzinfo=BJ)) is True
        assert gate.in_intraday_schedule_window(datetime(2026, 11, 18, 15, 5, 0, tzinfo=BJ)) is False
        assert gate.is_before_postclose_cutoff(datetime(2026, 11, 18, 15, 4, 59, tzinfo=BJ)) is True
        assert gate.is_before_postclose_cutoff(datetime(2026, 11, 18, 15, 5, 0, tzinfo=BJ)) is False

    def test_three_state_calendar_unknown_raises_fail_closed(self, monkeypatch):
        from scheduler import trading_calendar as tc
        from services import intraday_phase_gate as gate

        monkeypatch.setattr(
            tc.TradingCalendar,
            "resolve_trading_day",
            classmethod(lambda cls, _dt: tc.CalendarDayResult("trading", "mock", "open")),
        )
        assert gate.resolve_three_state_trading_day(date(2026, 11, 18)) is True

        monkeypatch.setattr(
            tc.TradingCalendar,
            "resolve_trading_day",
            classmethod(lambda cls, _dt: tc.CalendarDayResult("closed", "mock", "holiday")),
        )
        assert gate.resolve_three_state_trading_day(date(2026, 11, 18)) is False

        monkeypatch.setattr(
            tc.TradingCalendar,
            "resolve_trading_day",
            classmethod(lambda cls, _dt: tc.CalendarDayResult("unknown", "ch_down", "timeout")),
        )
        with pytest.raises(tc.CalendarUnknownError):
            gate.resolve_three_state_trading_day(date(2026, 11, 18))

    def test_evaluate_intraday_phase_bridges_s1_policy(self):
        from services import intraday_phase_gate as gate

        ok = gate.evaluate_intraday_phase(datetime(2026, 11, 18, 10, 0, 0, tzinfo=BJ), "trading")
        assert ok.admit_new is True
        assert ok.reason == "intraday"

        unknown = gate.evaluate_intraday_phase(datetime(2026, 11, 18, 10, 0, 0, tzinfo=BJ), "unknown")
        assert unknown.admit_new is False
        assert unknown.reason == "calendar_unknown"

        cutoff = gate.evaluate_intraday_phase(datetime(2026, 11, 18, 15, 5, 0, tzinfo=BJ), "trading")
        assert cutoff.admit_new is False
        assert cutoff.may_write is False

        naive = gate.evaluate_intraday_phase(datetime(2026, 11, 18, 10, 0, 0), "trading")
        assert naive.admit_new is False


class TestS4ApiGetZeroDbWriteAndForecastCutoff:
    def test_build_intraday_daily_bar_never_inserts_clickhouse(self, monkeypatch):
        from api import stocks as stk
        from scheduler import trading_calendar as tc

        monkeypatch.delenv("AISTOCK_READFACE_PREVIEW", raising=False)

        class _TradingClock(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2026, 11, 18, 10, 30, 0, tzinfo=BJ)
                return base if tz is None else base.astimezone(tz)

        monkeypatch.setattr(stk, "datetime", _TradingClock)
        monkeypatch.setattr(stk, "clickhouse_available", lambda: True)
        monkeypatch.setattr(stk, "clickhouse_table_exists", lambda name: True)
        monkeypatch.setattr(
            tc.TradingCalendar,
            "resolve_trading_day",
            classmethod(lambda cls, _dt: tc.CalendarDayResult("trading", "mock", "open")),
        )

        # 构造最小分钟条，覆盖 _build_intraday_daily_bar 完整内存计算路径
        df = pd.DataFrame([
            {
                "as_of": datetime(2026, 11, 18, 10, 30, 0, tzinfo=BJ),
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 1000,
                "amount": 1010000.0,
            }
        ])
        monkeypatch.setattr(stk, "clickhouse_query_df", lambda sql, params=None, **kw: df)
        monkeypatch.setattr(stk, "clickhouse_scalar", lambda sql, params=None, **kw: 10.0)

        import utils.market_warehouse as mw

        def _forbid_write(*args, **kwargs):
            raise AssertionError("S4 禁止 API GET 路径获取写入客户端或执行 INSERT")

        monkeypatch.setattr(mw, "quality_clickhouse_client", _forbid_write)
        monkeypatch.setattr(mw, "clickhouse_client", _forbid_write)

        payload = stk._build_intraday_daily_bar("600000.SH")
        assert payload is not None
        assert payload["close"] == pytest.approx(10.1)
        assert payload["is_provisional"] is True

    def test_build_intraday_daily_bar_fails_closed_on_unknown_calendar(self, monkeypatch):
        from api import stocks as stk
        from scheduler import trading_calendar as tc

        class _TradingClock(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2026, 11, 18, 10, 30, 0, tzinfo=BJ)
                return base if tz is None else base.astimezone(tz)

        monkeypatch.setattr(stk, "datetime", _TradingClock)
        monkeypatch.setattr(stk, "clickhouse_available", lambda: True)
        monkeypatch.setattr(stk, "clickhouse_table_exists", lambda name: True)
        monkeypatch.setattr(
            tc.TradingCalendar,
            "resolve_trading_day",
            classmethod(lambda cls, _dt: tc.CalendarDayResult("unknown", "ch_down", "err")),
        )
        assert stk._build_intraday_daily_bar("600000.SH") is None

    def test_turnover_forecast_persist_cutoff_and_unknown_calendar_fail_closed(self, monkeypatch, tmp_path):
        from scheduler import trading_calendar as tc
        from services import market_turnover_forecast as mtf

        cache_path = tmp_path / "forecast" / "latest.json"
        monkeypatch.setattr(mtf, "_PERSISTED_FORECAST_PATH", cache_path)
        monkeypatch.setattr(mtf, "_CACHE", {"payload": None, "expires_at": 0.0})

        payload = {"available": True, "trade_date": "2026-11-18", "predicted_turnover": 1.5e12}
        # 15:05:00 禁止落盘缓存
        monkeypatch.setattr(mtf, "_now_bj", lambda: datetime(2026, 11, 18, 15, 5, 0, tzinfo=BJ))
        mtf._persist(payload)
        assert not cache_path.exists()

        # 15:04:59 允许落盘缓存
        monkeypatch.setattr(mtf, "_now_bj", lambda: datetime(2026, 11, 18, 15, 4, 59, tzinfo=BJ))
        mtf._persist(payload)
        assert json.loads(cache_path.read_text(encoding="utf-8")) == payload

        # unknown 日历必须 fail-closed，且不触发 CH 查询
        monkeypatch.setattr(mtf, "clickhouse_available", lambda: True)
        monkeypatch.setattr(mtf, "clickhouse_table_exists", lambda name: True)
        monkeypatch.setattr(
            tc.TradingCalendar,
            "resolve_trading_day",
            classmethod(lambda cls, _dt: tc.CalendarDayResult("unknown", "mock", "down")),
        )
        monkeypatch.setattr(
            mtf,
            "clickhouse_query_df",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unknown 日历不应查 CH")),
        )
        res = mtf.get_intraday_turnover_forecast()
        assert res["available"] is False


class TestS2PipelineSchedulerAndUpsertAndCollect:
    def test_pipeline_scheduler_default_is_trading_day_unknown_raises(self, monkeypatch):
        from scheduler import trading_calendar as tc
        from scripts import pipeline_scheduler as ps

        monkeypatch.setattr(
            tc.TradingCalendar,
            "resolve_trading_day",
            classmethod(lambda cls, _dt: tc.CalendarDayResult("unknown", "mock", "err")),
        )
        with pytest.raises(tc.CalendarUnknownError):
            ps._default_is_trading_day(date(2026, 11, 18))

    def test_register_intraday_kline_upsert_respects_window_and_unknown_calendar(self, monkeypatch):
        from scripts import intraday_kline_upsert as up
        from scripts import pipeline_scheduler as ps

        jobs = []

        class _FakeScheduler:
            def add_job(self, func, trigger, **kwargs):
                jobs.append((func, kwargs))

        called = []
        monkeypatch.setattr(up, "run_upsert", lambda *a, **kw: called.append(True) or {})
        ps._register_intraday_kline_upsert(_FakeScheduler())
        assert len(jobs) == 1
        job_fn = jobs[0][0]

        # 超过 15:05:00 不触发 run_upsert
        class _AfterCloseClock(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2026, 11, 18, 15, 5, 0, tzinfo=BJ)
                return base if tz is None else base.astimezone(tz)

        monkeypatch.setattr("datetime.datetime", _AfterCloseClock)
        monkeypatch.setattr(ps, "_default_is_trading_day", lambda _d: True)
        job_fn()
        assert called == []

        # 盘中 10:30 但日历 unknown 异常同样不触发 run_upsert
        class _IntradayClock(datetime):
            @classmethod
            def now(cls, tz=None):
                base = datetime(2026, 11, 18, 10, 30, 0, tzinfo=BJ)
                return base if tz is None else base.astimezone(tz)

        monkeypatch.setattr("datetime.datetime", _IntradayClock)
        monkeypatch.setattr(
            ps,
            "_default_is_trading_day",
            lambda _d: (_ for _ in ()).throw(RuntimeError("calendar_unknown")),
        )
        job_fn()
        assert called == []

    def test_intraday_snapshot_collect_blocks_at_1505_and_pre_write_tick(self, monkeypatch, tmp_path):
        from scripts import intraday_snapshot_collect as isc

        # 1) 入口 15:05:00 直接阻断，不拉源也不建锁写库
        report1 = isc.run(
            fetcher=lambda _u: (_ for _ in ()).throw(AssertionError("15:05 不得抓取")),
            client_factory=lambda: (_ for _ in ()).throw(AssertionError("15:05 不得连 CH")),
            out_dir=tmp_path,
            sleep_fn=lambda _s: None,
            now_fn=lambda: datetime(2026, 11, 18, 15, 5, 0, tzinfo=BJ),
        )
        assert report1["status"] == "blocked"
        assert report1["reason"] == "after_1505_cutoff"

        # 2) 15:04:55 开抓，抓完变成 15:05:01 → 写前二次复检阻断，零 CH 写入
        ticks = iter([
            datetime(2026, 11, 18, 15, 4, 55, tzinfo=BJ),
            datetime(2026, 11, 18, 15, 5, 1, tzinfo=BJ),
        ])

        def _fake_fetch(url):
            if "clist" in url:
                rows = [
                    {
                        "f2": 10.0,
                        "f3": 1.0,
                        "f5": 100,
                        "f6": 1.0e5,
                        "f12": f"{i:06d}",
                        "f13": 0,
                        "f14": f"S{i}",
                        "f18": 9.9,
                    }
                    for i in range(1, 1005)
                ]
                return {"data": {"total": len(rows), "diff": rows}}
            return {
                "data": {
                    "diff": [
                        {
                            "f2": 3000.0,
                            "f3": 0.1,
                            "f4": 3.0,
                            "f5": 1000,
                            "f6": 1.0e9,
                            "f12": "000001",
                            "f13": 1,
                            "f14": "上证指数",
                            "f18": 2997.0,
                        }
                    ]
                }
            }

        report2 = isc.run(
            fetcher=_fake_fetch,
            client_factory=lambda: (_ for _ in ()).throw(AssertionError("写前越过 15:05 不得获取锁/写 CH")),
            out_dir=tmp_path,
            sleep_fn=lambda _s: None,
            now_fn=lambda: next(ticks),
        )
        assert report2["status"] == "blocked"
        assert report2["reason"] == "after_1505_cutoff_pre_write"

    def test_intraday_kline_upsert_gates_and_lock(self, monkeypatch):
        from scripts import intraday_kline_upsert as up
        from utils.db_write_lock import LockHeldError

        # 1) 15:05:00 直接阻断
        res_cutoff = up.run_upsert(
            codes=["300054"],
            reconcile=False,
            now=datetime(2026, 11, 18, 15, 5, 0, tzinfo=BJ),
            trading_day_checker=lambda _d: True,
            lock_client_factory=lambda: (_ for _ in ()).throw(AssertionError("cutoff 不得加锁")),
        )
        assert res_cutoff["_blocked"]["reason"] == "after_1505_cutoff"

        # 2) unknown 日历阻断
        res_unknown = up.run_upsert(
            codes=["300054"],
            reconcile=False,
            now=datetime(2026, 11, 18, 10, 30, 0, tzinfo=BJ),
            trading_day_checker=lambda _d: (_ for _ in ()).throw(RuntimeError("unknown")),
            lock_client_factory=lambda: (_ for _ in ()).throw(AssertionError("unknown 不得加锁")),
        )
        assert res_unknown["_blocked"]["status"] == "blocked"
        assert "calendar_unknown" in res_unknown["_blocked"]["reason"]

        # 3) 写前时钟跨过 15:05:00 → 禁止调用 insert_rows 且释放锁
        events = []

        class _SpyLock:
            def acquire(self, scope, owner, *, purpose=""):
                events.append(("acquire", scope, owner))
                return True

            def release(self, scope, owner):
                events.append(("release", scope, owner))
                return True

        import utils.db_write_lock as dwl

        monkeypatch.setattr(dwl, "DbWriteLock", lambda _c: _SpyLock())
        monkeypatch.setattr(
            up,
            "fetch_tencent_m5",
            lambda code: {"rows": [{"datetime": "2026-11-18 14:55", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 100}]},
        )
        monkeypatch.setattr(
            up,
            "insert_rows",
            lambda rows: (_ for _ in ()).throw(AssertionError("跨过 15:05 不得插库")),
        )
        ticks = iter([
            datetime(2026, 11, 18, 15, 4, 55, tzinfo=BJ),  # pre-flight
            datetime(2026, 11, 18, 15, 4, 56, tzinfo=BJ),  # pre-fetch
            datetime(2026, 11, 18, 15, 5, 0, tzinfo=BJ),   # pre-write
        ])
        res_tick = up.run_upsert(
            codes=["300054"],
            reconcile=False,
            now_fn=lambda: next(ticks),
            trading_day_checker=lambda _d: True,
            lock_client_factory=lambda: SimpleNamespace(),
        )
        assert res_tick["300054"]["status"] == "blocked"
        assert res_tick["300054"]["reason"] == "after_1505_cutoff_pre_write"
        assert events == [
            ("acquire", up.LOCK_SCOPE, up.LOCK_OWNER),
            ("release", up.LOCK_SCOPE, up.LOCK_OWNER),
        ]

        # 4) DbWriteLock 被他人持有时，fail-closed 抛 LockHeldError 且绝不拉数写入
        class _ContendedLock:
            def acquire(self, scope, owner, *, purpose=""):
                raise LockHeldError("busy", scope=scope, table="t", report={})

            def release(self, scope, owner):
                return True

        monkeypatch.setattr(dwl, "DbWriteLock", lambda _c: _ContendedLock())
        monkeypatch.setattr(
            up,
            "fetch_tencent_m5",
            lambda code: (_ for _ in ()).throw(AssertionError("锁冲突时不得拉数")),
        )
        with pytest.raises(LockHeldError):
            up.run_upsert(
                codes=["300054"],
                reconcile=False,
                now=datetime(2026, 11, 18, 10, 30, 0, tzinfo=BJ),
                trading_day_checker=lambda _d: True,
                lock_client_factory=lambda: SimpleNamespace(),
            )

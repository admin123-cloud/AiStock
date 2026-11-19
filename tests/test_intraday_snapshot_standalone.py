# -*- coding: utf-8 -*-
"""scripts/intraday_snapshot_standalone.py 离线 TDD（P0 首页盘中行情恢复·方案 B·R1）。

零网络/零 DB/零 subprocess/零真实 runtime：collector、锁、ClickHouse、日历全部替身注入，
状态与报告只落 pytest tmp 目录。契约（架构方案 aistock-home-stale-20261117 §6/§7-R1）：
  - 硬窗口守卫 09:35–15:05 北京钟（本地实现，不 import pipeline_scheduler/APScheduler）；
  - 交易日守卫复用 _default_is_trading_day 语义（trade_calendar 主、异常 fail-closed 记失败跳拍）；
  - 直接调 intraday_snapshot_collect.run() 默认 fetcher（东财主源 + 腾讯自动降级）；
  - 锁 owner=aistock-intraday-standalone（scope 不变 intraday_snapshot_r28），可注入、进程退出恢复；
  - 单拍硬超时（默认 240s）可配置；
  - 状态写 runtime/intraday_snapshot/standalone_state.json（与 runner 版 scheduler_state.json 分文件）；
  - exit_code：0=ok/no-op；3=源不可达（透传 collector 语义）；1=其他失败（含日历异常/锁冲突/超时）；
  - 绝不 mood 尾挂 / scheduler 注册 / kline_* 触碰（断言零部分写入、只写两张运营表）。
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import intraday_snapshot_standalone as iss  # noqa: E402
import intraday_snapshot_collect as isc  # noqa: E402

BJ = timezone(timedelta(hours=8), "Asia/Shanghai")
NOW_IN = datetime(2026, 11, 10, 10, 35, 0, tzinfo=BJ)      # 周二，窗口内
SNAP_TS = NOW_IN
NOW_OUT = datetime(2026, 11, 10, 15, 6, 0, tzinfo=BJ)      # 窗口外（>15:05）
NOW_WEEKEND = datetime(2026, 11, 14, 10, 35, 0, tzinfo=BJ)  # 周六

STATE_NAME = "standalone_state.json"


def _stock_row(code="600519", f13=1, price=1436.2, name="贵州茅台"):
    return {"f2": price, "f3": -0.61, "f5": 21000, "f6": 3.0e9, "f12": code,
            "f13": f13, "f14": name, "f18": 1444.99}


def _page(pn, total, rows):
    return {"data": {"total": total, "diff": rows if rows else None}}


def _market(n=1200):
    return [_stock_row(str(600000 + i), 1) for i in range(n)]


class FakeFetcher:
    """clist 分页 + ulist 指数（同 test_intraday_snapshot_collect.py 手法）。"""

    INDEX_CODES = ("000001", "399001", "399106", "000300", "000016",
                   "000905", "000852", "399006", "000680", "000688")

    def __init__(self, pages, index_payload=None, fail_pages=()):
        self.pages = {i + 1: p for i, p in enumerate(pages)}
        if index_payload is None:
            index_payload = {"data": {"diff": [
                {"f2": 3671.35, "f3": 0.29, "f4": 10.56, "f5": 1.7e8, "f6": 3.0e11,
                 "f12": code, "f13": 1, "f14": f"指数{code}", "f18": 3660.79}
                for code in self.INDEX_CODES
            ]}}
        self.index_payload = index_payload
        self.fail_pages = set(fail_pages)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if "ulist" in url:
            return self.index_payload
        if "pn=" in url:
            pn = int(url.split("pn=")[1].split("&")[0])
            if pn in self.fail_pages:
                raise OSError("boom")
            if pn in self.pages:
                return self.pages[pn]
        raise AssertionError(f"unexpected url {url}")


class FakeClient:
    """ClickHouse 替身：command（DDL/readback/DELETE 回滚）+ insert + query（qt universe）。"""

    def __init__(self, universe=(), readback_stocks=None, readback_indices=None,
                 fail_on_insert=False, fail_table=None):
        self.universe = list(universe)
        self.commands = []
        self.inserts = []
        self.readback_stocks = readback_stocks
        self.readback_indices = readback_indices
        self.fail_on_insert = fail_on_insert
        self.fail_table = fail_table

    def query(self, sql, settings=None):
        class _R:
            result_rows = [(c,) for c in self.universe]
            column_names = ["code"]
        return _R()

    def command(self, sql):
        self.commands.append(sql)
        if "SELECT count()" in sql:
            if isc.STOCK_TABLE in sql:
                return self.readback_stocks
            return self.readback_indices
        return None

    def insert(self, table, rows, column_names=None):
        if self.fail_on_insert and (self.fail_table is None or self.fail_table == table):
            raise RuntimeError("insert boom")
        self.inserts.append((table, list(rows), list(column_names or [])))


class FakeLock:
    """DbWriteLock 替身：可配置 acquire 抛异常（锁冲突）。"""

    def __init__(self, acquire_exc=None):
        self.acquire_exc = acquire_exc
        self.acquired = []
        self.released = []

    def acquire(self, scope, owner, purpose=""):
        self.acquired.append((scope, owner, purpose))
        if self.acquire_exc is not None:
            raise self.acquire_exc
        return {"scope": scope, "owner": owner}

    def release(self, scope, owner):
        self.released.append((scope, owner))


# ── 辅助：run_standalone 调用包装 ──────────────────────────────

def _standalone(tmp_path, *, collector_run=None, now=None, trading=True,
                trading_exc=None, hard_timeout_sec=60.0, lock_owner=None,
                out_root=None):
    def check(_day):
        if trading_exc is not None:
            raise trading_exc
        return trading
    kwargs = dict(
        out_root=out_root if out_root is not None else tmp_path,
        is_trading_day=check,
        now_fn=lambda: (now if now is not None else NOW_IN),
        hard_timeout_sec=hard_timeout_sec,
    )
    if lock_owner is not None:
        kwargs["lock_owner"] = lock_owner
    if collector_run is not None:
        kwargs["collector_run"] = collector_run
    return iss.run_standalone(**kwargs)


def _real_collector(monkeypatch, tmp_path, *, client, lock, fetcher=None,
                    snap_ts=SNAP_TS):
    """真 isc.run 集成入口：锁/数据根替身 + fetcher/client/sleep/now 注入。"""
    import utils.db_write_lock as lock_mod
    import utils.paths as paths_mod
    monkeypatch.setattr(lock_mod, "DbWriteLock", lambda _c: lock)
    monkeypatch.setattr(paths_mod, "data_root", lambda: tmp_path)

    def cr(out_dir):
        kwargs = dict(client_factory=lambda: client, out_dir=out_dir,
                      sleep_fn=lambda _s: None, now_fn=lambda: snap_ts)
        if fetcher is not None:
            kwargs["fetcher"] = fetcher
        return isc.run(**kwargs)
    return cr


def _state_path(tmp_path):
    return tmp_path / "intraday_snapshot" / STATE_NAME


def _load_state(tmp_path):
    return json.loads(_state_path(tmp_path).read_text(encoding="utf-8"))


def _ok_market_fetcher():
    return FakeFetcher([_page(1, 1200, _market(1200)), _page(2, 1200, [])])


# ── 窗口守卫（本地实现，语义对齐 scheduler in_intraday_window）──

class TestWindow:
    @pytest.mark.parametrize("hh,mm,expected", [
        (9, 30, False), (9, 34, False), (9, 35, True), (11, 0, True),
        (14, 59, True), (15, 4, True), (15, 5, False), (15, 6, False), (15, 55, False),
    ])
    def test_window_boundaries(self, hh, mm, expected):
        now_bj = datetime(2026, 11, 10, hh, mm, tzinfo=BJ)
        assert iss.in_intraday_window(now_bj) is expected

    def test_naive_beijing_time_also_supported(self):
        assert iss.in_intraday_window(datetime(2026, 11, 10, 10, 0)) is True
        assert iss.in_intraday_window(datetime(2026, 11, 10, 16, 0)) is False

    def test_window_constants(self):
        assert iss.WINDOW_START == dt_time(9, 35)
        assert iss.WINDOW_END == dt_time(15, 5)


# ── 交易日守卫默认实现（_default_is_trading_day 语义复刻）───────

class TestDefaultTradingDay:
    def test_probes_noon_beijing_and_passes_through(self, monkeypatch):
        from scheduler.trading_calendar import CalendarDayResult, TradingCalendar
        seen = {}

        def fake(cls, probe):
            seen["probe"] = probe
            return CalendarDayResult("trading", "trade_calendar", "explicit row")
        monkeypatch.setattr(TradingCalendar, "resolve_trading_day",
                            classmethod(lambda cls, d=None: fake(cls, d)))
        assert iss._default_is_trading_day(NOW_IN.date()) is True
        assert seen["probe"] == datetime(2026, 11, 10, 12, 0, tzinfo=BJ)

    def test_exception_propagates_for_fail_closed_handling(self, monkeypatch):
        from scheduler.trading_calendar import TradingCalendar
        monkeypatch.setattr(
            TradingCalendar, "resolve_trading_day",
            classmethod(lambda cls, d=None: (_ for _ in ()).throw(RuntimeError("db down"))))
        with pytest.raises(RuntimeError, match="db down"):
            iss._default_is_trading_day(NOW_IN.date())

    @pytest.mark.parametrize("scenario,now,expected,rc,calls_expected", [
        ("open", NOW_IN, "success", 0, 1),
        ("closed", NOW_IN, "no_op_non_trading", 0, 0),
        ("weekend_open", NOW_WEEKEND, "success", 0, 1),
        ("unavailable", NOW_IN, "failure_calendar", 1, 0),
        ("missing", NOW_IN, "failure_calendar", 1, 0),
        ("query_error", NOW_IN, "failure_calendar", 1, 0),
        ("empty", NOW_IN, "failure_calendar", 1, 0),
        ("out_of_bounds", NOW_IN, "failure_calendar", 1, 0),
    ])
    def test_real_calendar_guard_with_fake_warehouse(
            self, monkeypatch, tmp_path, scenario, now, expected, rc, calls_expected):
        from scheduler import trading_calendar as calendar

        calendar.TradingCalendar._LOOKUP_CACHE.clear()
        monkeypatch.setattr(calendar, "clickhouse_available", lambda: scenario != "unavailable")
        monkeypatch.setattr(calendar, "clickhouse_table_exists", lambda table: scenario != "missing")

        def scalar(sql, params=None):
            if scenario == "query_error":
                raise RuntimeError("synthetic query error")
            if "SELECT is_trading" in sql:
                assert params == [now.date().isoformat()]
                return {"open": 1, "closed": 0, "weekend_open": 1}.get(scenario)
            if "min(trade_date)" in sql:
                return datetime(2026, 11, 1).date() if scenario == "out_of_bounds" else None
            if "max(trade_date)" in sql:
                return datetime(2026, 11, 9).date() if scenario == "out_of_bounds" else None
            raise AssertionError("unexpected SQL")

        monkeypatch.setattr(calendar, "clickhouse_scalar", scalar)
        calls = []
        try:
            report = iss.run_standalone(
                out_root=tmp_path, now_fn=lambda: now,
                collector_run=lambda out_dir: calls.append(out_dir) or {"status": "success"},
            )
            assert report["status"] == expected
            assert report["rc"] == rc
            assert len(calls) == calls_expected
        finally:
            calendar.TradingCalendar._LOOKUP_CACHE.clear()


# ── 守卫行为（零动作路径）──────────────────────────────────────

class TestGuards:
    def test_out_of_window_rc0_zero_action_no_state(self, tmp_path):
        calls = []

        def cr(out_dir):
            calls.append(out_dir)
            return {"status": "success"}
        report = _standalone(tmp_path, collector_run=cr, now=NOW_OUT)
        assert report["status"] == "no_op_window"
        assert report["rc"] == 0
        assert calls == []                       # collector 零调用
        assert not _state_path(tmp_path).exists()  # 窗口外不动状态文件

    def test_non_trading_day_rc0_zero_action_no_state(self, tmp_path):
        calls = []

        def cr(out_dir):
            calls.append(out_dir)
            return {"status": "success"}
        report = _standalone(tmp_path, collector_run=cr,
                             now=NOW_WEEKEND, trading=False)
        assert report["status"] == "no_op_non_trading"
        assert report["rc"] == 0
        assert calls == []
        assert not _state_path(tmp_path).exists()

    def test_calendar_error_fail_closed_skip_beat(self, tmp_path):
        calls = []

        def cr(out_dir):
            calls.append(out_dir)
            return {"status": "success"}
        report = _standalone(tmp_path, collector_run=cr,
                             trading_exc=RuntimeError("db down"))
        assert report["status"] == "failure_calendar"
        assert report["rc"] == 1
        assert calls == []                        # 判不出交易日绝不采集
        state = _load_state(tmp_path)
        assert state["consecutive_failures"] == 1
        assert state["last_status"] == "failure_calendar"
        assert "db down" in state["last_failure_reason"]

    def test_calendar_failures_raise_alarm_at_threshold(self, tmp_path):
        for _ in range(3):
            report = _standalone(tmp_path, collector_run=lambda out_dir: {},
                                 trading_exc=RuntimeError("db down"))
        assert report["consecutive_failures"] == 3
        assert report["alarm"] is True
        state = _load_state(tmp_path)
        assert state["alarm"] is True


# ── 锁 owner 参数化（scope 不变，进程退出恢复）─────────────────

class TestOwnerInjection:
    def test_lock_owner_injected_scope_unchanged_restored(self, tmp_path, monkeypatch):
        lock = FakeLock()
        client = FakeClient(readback_stocks=1200, readback_indices=10)
        cr = _real_collector(monkeypatch, tmp_path, client=client, lock=lock,
                             fetcher=_ok_market_fetcher())
        original_owner = isc.LOCK_OWNER
        report = _standalone(tmp_path, collector_run=cr)
        assert report["status"] == "success" and report["rc"] == 0
        # owner=注入值；scope 与 R28 完全一致
        assert lock.acquired[0][:2] == (isc.LOCK_SCOPE, "aistock-intraday-standalone")
        assert lock.released == [(isc.LOCK_SCOPE, "aistock-intraday-standalone")]
        # 独立入口绝不冒用 collector 默认 owner
        assert lock.acquired[0][1] != original_owner
        # 调用结束恢复 collector 模块常量（不留全局污染）
        assert isc.LOCK_OWNER == original_owner
        # 只写两张运营表，绝不 kline_*
        tables = {t for t, _r, _c in client.inserts}
        assert tables == {isc.STOCK_TABLE, isc.INDEX_TABLE}

    def test_custom_lock_owner_injectable(self, tmp_path, monkeypatch):
        lock = FakeLock()
        client = FakeClient(readback_stocks=1200, readback_indices=10)
        cr = _real_collector(monkeypatch, tmp_path, client=client, lock=lock,
                             fetcher=_ok_market_fetcher())
        _standalone(tmp_path, collector_run=cr, lock_owner="aistock-test-owner")
        assert lock.acquired[0][1] == "aistock-test-owner"


# ── 集成：真 isc.run 全链（替身注入 DB/锁/网络）────────────────

class TestCollectorIntegration:
    def test_success_rc0_and_state_file_content(self, tmp_path, monkeypatch):
        lock = FakeLock()
        client = FakeClient(readback_stocks=1200, readback_indices=10)
        cr = _real_collector(monkeypatch, tmp_path, client=client, lock=lock,
                             fetcher=_ok_market_fetcher())
        report = _standalone(tmp_path, collector_run=cr)
        assert report["status"] == "success"
        assert report["rc"] == 0
        state = _load_state(tmp_path)
        assert state["schema"] == "intraday_snapshot_standalone/1"
        assert state["last_status"] == "success"
        assert state["last_rc"] == 0
        assert state["consecutive_failures"] == 0 and state["alarm"] is False
        assert state["last_success_ts"]
        assert state["last_failure_reason"] is None
        summary = state["last_report_summary"]
        assert summary["status"] == "success"
        assert summary["valid_stock_rows"] == 1200 and summary["index_rows"] == 10
        # collector 报告与状态文件同目录、分文件
        beat_dir = tmp_path / "intraday_snapshot"
        reports = list(beat_dir.glob("report_*.json"))
        assert len(reports) == 1
        assert (beat_dir / STATE_NAME).name == STATE_NAME

    def test_east_down_falls_back_to_tencent(self, tmp_path, monkeypatch):
        """东财源败 → 腾讯 qt 降级（默认 fetcher 链，不显式注入 fetcher）。"""
        codes = [f"6{i:05d}" for i in range(1200)]

        def _qt_line(key, price="10.50"):
            parts = [f"x{i}" for i in range(50)]
            parts[1] = "测试股"
            parts[3] = price
            parts[4] = "10.37"
            parts[6] = "12345"
            parts[31] = "0.13"
            parts[32] = "1.23"
            parts[37] = "600.5"
            return f'v_{key}="' + "~".join(parts) + '";'

        monkeypatch.setattr(isc, "_default_fetch_json",
                            lambda url: (_ for _ in ()).throw(RuntimeError("east WAF")))
        monkeypatch.setattr(isc, "fetch_qt_batch",
                            lambda prefixed, url_open=None: isc.parse_qt_payload(
                                ";".join(_qt_line(k) for k in prefixed)))
        lock = FakeLock()
        client = FakeClient(universe=codes, readback_stocks=1200,
                            readback_indices=10)
        cr = _real_collector(monkeypatch, tmp_path, client=client, lock=lock)
        report = _standalone(tmp_path, collector_run=cr)
        assert report["status"] == "success" and report["rc"] == 0
        summary = report["collector_report"]
        assert summary["source"] == "tencent_qt_batch"
        # 降级通道同样只写两张运营表（同表/同锁/同幂等 id 治理）
        tables = {t for t, _r, _c in client.inserts}
        assert tables == {isc.STOCK_TABLE, isc.INDEX_TABLE}
        assert not any("kline" in t for t, _r, _c in client.inserts)

    def test_page_retry_exhausted_fail_closed_zero_write(self, tmp_path, monkeypatch):
        """单页重试 3 次耗尽 → 整轮中止零写入（显式注入 fetcher 不隐式降级）。"""
        qt_calls = []
        monkeypatch.setattr(isc, "fetch_qt_batch",
                            lambda prefixed, url_open=None: qt_calls.append(1) or {})
        lock = FakeLock()
        client = FakeClient()
        dead = FakeFetcher([_page(1, 1, [])], fail_pages={1})
        cr = _real_collector(monkeypatch, tmp_path, client=client, lock=lock,
                             fetcher=dead)
        report = _standalone(tmp_path, collector_run=cr)
        assert report["status"] == "failure"
        assert report["rc"] == 3                      # 源不可达语义透传
        assert client.inserts == []                   # 零部分写入
        assert lock.acquired == []                    # 源败未到锁段
        assert qt_calls == []                         # 显式 fetcher 不隐式降级
        state = _load_state(tmp_path)
        assert state["last_status"] == "failure" and state["last_rc"] == 3
        assert state["consecutive_failures"] == 1

    def test_insufficient_rows_rejected_zero_write(self, tmp_path, monkeypatch):
        lock = FakeLock()
        client = FakeClient()
        thin = FakeFetcher([_page(1, 900, _market(900)), _page(2, 900, [])])
        cr = _real_collector(monkeypatch, tmp_path, client=client, lock=lock,
                             fetcher=thin)
        report = _standalone(tmp_path, collector_run=cr)
        assert report["status"] == "failure"
        assert report["rc"] == 1
        assert report["collector_report"]["reason"] == "insufficient_rows"
        assert client.inserts == []
        assert lock.acquired == []

    def test_readback_mismatch_precise_ts_rollback(self, tmp_path, monkeypatch):
        lock = FakeLock()
        client = FakeClient(readback_stocks=1199, readback_indices=10)
        cr = _real_collector(monkeypatch, tmp_path, client=client, lock=lock,
                             fetcher=_ok_market_fetcher())
        report = _standalone(tmp_path, collector_run=cr)
        assert report["status"] == "failure"
        assert report["rc"] == 1
        assert report["collector_report"]["reason"] == "readback_mismatch"
        rollback_sql = "\n".join(client.commands)
        assert rollback_sql.count("ALTER TABLE") == 2
        assert ("WHERE snapshot_ts = toDateTime("
                "'2026-11-10 10:35:00', 'Asia/Shanghai')") in rollback_sql
        # 回滚后锁正常释放（不留死锁）
        assert lock.released == [(isc.LOCK_SCOPE, "aistock-intraday-standalone")]


# ── 锁冲突：acquire 抛 LockHeldError → 捕获退出 ────────────────

class TestLockConflict:
    def test_lock_held_exits_without_write(self, tmp_path, monkeypatch):
        from utils.db_write_lock import LockHeldError
        lock = FakeLock(acquire_exc=LockHeldError(
            "write lock stock.write_lock_intraday_snapshot_r28 is held by other",
            scope=isc.LOCK_SCOPE, table="stock.write_lock_intraday_snapshot_r28",
            report={"holder": {"owner": "runner"}}))
        client = FakeClient(readback_stocks=1200, readback_indices=10)
        cr = _real_collector(monkeypatch, tmp_path, client=client, lock=lock,
                             fetcher=_ok_market_fetcher())
        report = _standalone(tmp_path, collector_run=cr)
        assert report["status"] == "failure"
        assert report["reason"] == "lock_held"
        assert report["rc"] == 1
        assert client.inserts == []                  # 锁冲突绝不写入
        assert lock.released == []                   # 未取得的锁不释放
        state = _load_state(tmp_path)
        assert state["last_status"] == "failure"
        assert "lock" in state["last_failure_reason"]


# ── 单拍硬超时（默认 240s，可配置）─────────────────────────────

class TestHardTimeout:
    def test_timeout_kills_beat_and_records_failure(self, tmp_path):
        started = threading.Event()
        release = threading.Event()

        def slow_collector(out_dir):
            started.set()
            release.wait(5.0)
            return {"status": "success"}
        report = _standalone(tmp_path, collector_run=slow_collector,
                             hard_timeout_sec=0.2)
        try:
            assert report["status"] == "failure"
            assert report["reason"] == "hard_timeout"
            assert report["rc"] == 1
            state = _load_state(tmp_path)
            assert state["last_status"] == "failure"
            assert state["last_failure_reason"] == "hard_timeout"
        finally:
            release.set()                            # 放行 daemon 线程收尾

    def test_default_timeout_is_240s(self):
        assert iss.DEFAULT_HARD_TIMEOUT_SEC == 240.0


# ── 状态机迁移 ─────────────────────────────────────────────────

class TestStateTransitions:
    def test_failures_then_success_resets_counter_and_alarm(self, tmp_path):
        def dead(out_dir):
            return {"status": "failure", "reason": "source_unreachable"}
        for _ in range(3):
            r = _standalone(tmp_path, collector_run=dead)
        assert r["alarm"] is True and r["consecutive_failures"] == 3
        ok = _standalone(tmp_path, collector_run=lambda out_dir: {"status": "success"})
        assert ok["status"] == "success"
        assert ok["consecutive_failures"] == 0 and ok["alarm"] is False
        state = _load_state(tmp_path)
        assert state["consecutive_failures"] == 0 and state["alarm"] is False
        assert state["last_failure_reason"] is None
        assert len(state["recent_failures"]) == 3

    def test_recent_failures_capped(self, tmp_path):
        def dead(out_dir):
            return {"status": "failure", "reason": "source_unreachable"}
        for _ in range(25):
            _standalone(tmp_path, collector_run=dead)
        state = _load_state(tmp_path)
        assert len(state["recent_failures"]) == 20


# ── main()：CLI 入口与 exit_code 透传 ─────────────────────────

class TestMain:
    def test_main_success_returns_zero(self, tmp_path, monkeypatch):
        lock = FakeLock()
        client = FakeClient(readback_stocks=1200, readback_indices=10)
        cr = _real_collector(monkeypatch, tmp_path, client=client, lock=lock,
                             fetcher=_ok_market_fetcher())
        rc = iss.main(["--out-root", str(tmp_path), "--hard-timeout-sec", "30"],
                      now_fn=lambda: NOW_IN, is_trading_day=lambda d: True,
                      collector_run=cr)
        assert rc == 0
        assert _state_path(tmp_path).exists()

    def test_main_source_unreachable_returns_three(self, tmp_path):
        def dead(out_dir):
            return {"status": "failure", "reason": "source_unreachable"}
        rc = iss.main(["--out-root", str(tmp_path)],
                      now_fn=lambda: NOW_IN, is_trading_day=lambda d: True,
                      collector_run=dead)
        assert rc == 3

    def test_main_out_of_window_returns_zero_no_state(self, tmp_path):
        rc = iss.main(["--out-root", str(tmp_path)],
                      now_fn=lambda: NOW_OUT, is_trading_day=lambda d: True,
                      collector_run=lambda out_dir: {"status": "success"})
        assert rc == 0
        assert not _state_path(tmp_path).exists()

# -*- coding: utf-8 -*-
"""scripts/minute_collect_auto.py 的纯单元测试（R21 5m 自动采集器；fake 依赖，零真实 DB/网络）。

覆盖任务书 20261109_minute_auto_collect_r21 的契约：
  - --trade-date 缺省自动解析=正式表最近缺口日；--max-days 深度边界与超界提示；
  - 写路径复用 R16 治理（同锁 scope kline_minute_backfill_window、id 同源、
    锁内反连接、读回、精确 id 回滚、journal）；
  - 日线仲裁强制（无当日日线 → fail-closed 提示先跑 daily_kline_pipeline）；
  - 源不可达 → 退出码非 0；bar 缺失如实报告不造数（B 源无额 → amount=None）。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import minute_collect_auto as mca  # noqa: E402
import minute_backfill_1021_1030 as mb  # noqa: E402
from utils import tdx_lc5 as lc5  # noqa: E402

UTC = timezone.utc
CN = mb.CN_TZ
DAY = "2026-11-09"
CH_WIN = ("2026-11-09 00:00:00", "2026-11-11 00:00:00")  # R27b 北京钟字面量（ch_window(11-09,11-09)）


def bar_dt(hm: str, day: str = DAY) -> datetime:
    return datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M")


def make_bars(day: str = DAY, *, volume: float = 100.0, amount=None, close_scale: float = 1.0):
    """按标准 48 槽生成 bar 字典列表（内部统一格式：naive 北京时间）。"""
    out = []
    for t in sorted(lc5.STANDARD_5M_BAR_ENDS):
        out.append({
            "datetime": datetime.combine(date.fromisoformat(day), t),
            "open": 10.0 * close_scale, "high": 10.5 * close_scale,
            "low": 9.8 * close_scale, "close": 10.2 * close_scale,
            "volume": volume, "amount": amount,
        })
    return out


def daily_ref_row(volume: float = 4800.0, amount: float = 48000.0):
    return (10.0, 10.5, 9.8, 10.2, volume, amount)


CREATED_AT = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)


# ── 源选择与常量契约 ──────────────────────────────────────────

def test_auto_source_resolves_to_evaluated():
    # R22 评估结论（east=东财 push2his 带 amount）必须固化为常量，auto 按它选源
    assert mca.EVALUATED_SOURCE == "east"
    assert mca.resolve_source("auto") == "east"
    assert mca.resolve_source("east") == "east"
    assert mca.resolve_source("B") == "B"
    assert mca.resolve_source("A") == "A"
    # 回退顺序：east → B（腾讯无额兜底）；sina 评估未过不在生产选项
    assert mca.SOURCE_FALLBACK_ORDER == ("east", "B")


def test_resolve_source_rejects_sina_and_unknown():
    # sina 未过评估（48 槽不齐/股非整百/amount 噪声）→ 显式拒绝并指证据
    with pytest.raises(SystemExit, match="sina"):
        mca.resolve_source("sina")
    with pytest.raises(SystemExit, match="非法"):
        mca.resolve_source("C")


def test_depth_vs_source_east_limit():
    # 东财 push2his lmt 上限实测 1536 根≈32 交易日 → 安全深度 25
    with pytest.raises(SystemExit, match="1536"):
        mca.check_depth_vs_source("east", 26)
    mca.check_depth_vs_source("east", 25)  # 边界内不抛


def test_east_m5_fetch_parses_klines_with_amount():
    payload = {"data": {"klines": [
        "2026-11-09 09:35,10.00,10.20,10.50,9.80,100,100000.0",
        "2026-11-09 09:40,10.20,10.10,10.30,10.00,50,51000.5"]}}
    bars = mca.east_m5_fetch("600519.SH", fetcher=lambda code: payload)
    assert len(bars) == 2
    b0 = bars[0]
    assert b0["datetime"] == datetime(2026, 11, 9, 9, 35)
    assert (b0["open"], b0["close"], b0["high"], b0["low"]) == (10.0, 10.2, 10.5, 9.8)
    assert b0["volume"] == 100.0 and b0["amount"] == 100000.0


def test_east_m5_fetch_missing_amount_is_contract_violation():
    payload = {"data": {"klines": ["2026-11-09 09:35,10.0,10.2,10.5,9.8,100,-"]}}
    with pytest.raises(RuntimeError, match="缺 amount"):
        mca.east_m5_fetch("600519.SH", fetcher=lambda code: payload)


def test_east_m5_fetch_zero_bars_means_no_coverage():
    with pytest.raises(RuntimeError, match="返回 0 根"):
        mca.east_m5_fetch("600519.SH", fetcher=lambda code: {"data": {"klines": []}})
    with pytest.raises(RuntimeError, match="返回 0 根"):
        mca.east_m5_fetch("600519.SH", fetcher=lambda code: {"data": {}})


def test_pytdx_import_is_lazy():
    # 采集器模块导入不得触发 pytdx 连接栈（离线可测、A 路径惰性）
    import subprocess
    code = ("import sys; sys.path.insert(0, r'F:\\Aistock\\app'); "
            "sys.path.insert(0, r'F:\\Aistock\\app\\scripts'); "
            "import minute_collect_auto; "
            "print('pytdx' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "False"


# ── 交易日解析（纯函数）──────────────────────────────────────

CAL = {  # 11-05(四) 11-06(五) 11-09(一) 11-10(二)；11-07/08 周末非交易日
    "2026-11-05": 1, "2026-11-06": 1, "2026-11-07": 0, "2026-11-08": 0,
    "2026-11-09": 1, "2026-11-10": 1,
}


def test_trading_days_desc_orders_and_filters():
    days = mca.trading_days_desc(CAL, "2026-11-09", lookback_days=6)
    assert days == ["2026-11-09", "2026-11-06", "2026-11-05"]


def test_resolve_explicit_trading_day_ok():
    got = mca.resolve_trade_date(
        explicit=DAY, calendar=CAL, latest_daily=DAY,
        missing_by_day={DAY: 3}, daily_rows_by_day={DAY: 5583}, max_days=5)
    assert got["mode"] == "explicit" and got["trade_date"] == DAY
    assert got["missing"] == 3


def test_resolve_explicit_non_trading_fail_closed():
    with pytest.raises(SystemExit, match="非交易日"):
        mca.resolve_trade_date(
            explicit="2026-11-08", calendar=CAL, latest_daily=DAY,
            missing_by_day={}, daily_rows_by_day={DAY: 5583}, max_days=5)


def test_resolve_daily_line_missing_fail_closed():
    # 日线仲裁强制：当日 kline_daily 无行 → fail-closed 并提示先跑 daily_kline_pipeline
    with pytest.raises(SystemExit, match="daily_kline_pipeline"):
        mca.resolve_trade_date(
            explicit=DAY, calendar=CAL, latest_daily="2026-11-06",
            missing_by_day={DAY: 3}, daily_rows_by_day={"2026-11-06": 5583}, max_days=5)


def test_resolve_auto_picks_newest_gap():
    got = mca.resolve_trade_date(
        explicit=None, calendar=CAL, latest_daily=DAY,
        missing_by_day={"2026-11-09": 0, "2026-11-06": 12, "2026-11-05": 7},
        daily_rows_by_day={"2026-11-09": 5583, "2026-11-06": 5583, "2026-11-05": 5583},
        max_days=5)
    assert got == {"mode": "auto", "trade_date": "2026-11-06", "depth": 2, "missing": 12}


def test_resolve_auto_no_gap_returns_none():
    got = mca.resolve_trade_date(
        explicit=None, calendar=CAL, latest_daily=DAY,
        missing_by_day={"2026-11-09": 0, "2026-11-06": 0, "2026-11-05": 0},
        daily_rows_by_day={"2026-11-09": 5583}, max_days=5)
    assert got is None


def test_resolve_auto_deep_gap_fail_closed_with_tdx_guidance():
    # 最近缺口深于 --max-days → fail-closed：提示 TDX 客户端兜底 / 分日补
    with pytest.raises(SystemExit, match="TDX"):
        mca.resolve_trade_date(
            explicit=None, calendar=CAL, latest_daily=DAY,
            missing_by_day={"2026-11-09": 0, "2026-11-06": 0, "2026-11-05": 30},
            daily_rows_by_day={"2026-11-09": 5583, "2026-11-05": 5583},
            max_days=2)  # 11-05 深度 3 > 2


def test_depth_vs_source_b_window_limit():
    # 腾讯 m5 ≈ 320 根 = 6.67 交易日 → B 源深度 >6 fail-closed
    with pytest.raises(SystemExit, match="320"):
        mca.check_depth_vs_source("B", 7)
    mca.check_depth_vs_source("B", 6)   # 边界内不抛
    mca.check_depth_vs_source("A", 30)  # A 协议分页无此限


# ── bar 门禁与行构造 ─────────────────────────────────────────

def test_gate_accepts_b_bars_with_amount_none_and_id_parity():
    rows, reject = mca.gate_bars("600519.SH", DAY, make_bars(), daily_ref_row(),
                                 source="B", created_at=CREATED_AT)
    assert reject is None and len(rows) == 48
    r0 = rows[0]
    assert r0[7] is None, "B 源无额 → amount=None（如实，不造数）"
    assert isinstance(r0[6], int)
    # id 同源：与 R16/1021_1030 的 minute_row_id 完全一致（同 id 域）
    assert r0[9] == mb.minute_row_id("600519.SH", r0[1].astimezone(CN).replace(tzinfo=None), "5m")
    # datetime 为北京 aware（写入序列化口径与 R16 一致）
    assert r0[1].utcoffset() == timedelta(hours=8)


def test_gate_rejects_wrong_slot_count():
    bars = make_bars()[:-1]
    rows, reject = mca.gate_bars("600519.SH", DAY, bars, daily_ref_row(),
                                 source="B", created_at=CREATED_AT)
    assert rows == [] and reject and "48" in reject


def test_gate_rejects_ohlc_sanity_violation():
    bars = make_bars()
    bars[3]["high"] = 1.0  # high < open 破坏值域
    rows, reject = mca.gate_bars("600519.SH", DAY, bars, daily_ref_row(),
                                 source="B", created_at=CREATED_AT)
    assert rows == [] and reject and "high" in reject


def test_gate_rejects_fractional_volume_unit_anomaly():
    bars = make_bars(volume=100.5)  # 手出现小数 → 单位错配 fail-closed
    rows, reject = mca.gate_bars("600519.SH", DAY, bars, daily_ref_row(),
                                 source="B", created_at=CREATED_AT)
    assert rows == [] and reject and "volume" in reject


def test_gate_daily_arbitration_volume_mismatch():
    bars = make_bars(volume=100.0)           # 日合计 4800 手
    daily = daily_ref_row(volume=100000.0)   # 日线 100000 手 → 超 3%
    rows, reject = mca.gate_bars("600519.SH", DAY, bars, daily,
                                 source="B", created_at=CREATED_AT)
    assert rows == [] and reject and "volume" in reject


def test_gate_daily_row_missing_fail_closed():
    rows, reject = mca.gate_bars("600519.SH", DAY, make_bars(), None,
                                 source="B", created_at=CREATED_AT)
    assert rows == []
    assert reject and "daily_kline_pipeline" in reject


def test_gate_a_source_shares_to_lots_and_amount():
    # A 路：volume 股 → shares_to_lots（÷100、%100 校验），amount 直传（元）
    bars = make_bars(volume=10000.0, amount=1000.0)  # 10000 股 = 100 手
    rows, reject = mca.gate_bars("600519.SH", DAY, bars, daily_ref_row(volume=4800.0, amount=48000.0),
                                 source="A", created_at=CREATED_AT)
    assert reject is None
    assert rows[0][6] == 100 and rows[0][7] == 1000.0


def test_gate_a_source_non_integral_shares_rejected():
    bars = make_bars(volume=10050.0, amount=1000.0)  # 非整百股 → fail-closed
    rows, reject = mca.gate_bars("600519.SH", DAY, bars, daily_ref_row(),
                                 source="A", created_at=CREATED_AT)
    assert rows == [] and reject


def test_gate_east_source_carries_amount_with_id_parity():
    # east：volume 已手（整数校验）、amount=元直传、日线仲裁含额；id 与 lc5 同域
    bars = make_bars(volume=100.0, amount=1000.0)   # 日合计 vol 4800 手 / amt 48000 元
    rows, reject = mca.gate_bars("600519.SH", DAY, bars,
                                 daily_ref_row(volume=4800.0, amount=48000.0),
                                 source="east", created_at=CREATED_AT)
    assert reject is None and len(rows) == 48
    assert rows[0][7] == 1000.0, "east 行带额（元，与 lc5/日线同口径）"
    assert isinstance(rows[0][6], int)
    assert rows[0][9] == mb.minute_row_id("600519.SH",
                                          rows[0][1].astimezone(CN).replace(tzinfo=None), "5m")


def test_gate_east_daily_amount_mismatch_rejected():
    bars = make_bars(volume=100.0, amount=1000.0)   # 日额合计 48000
    daily = daily_ref_row(volume=4800.0, amount=40000.0)  # 日线额差 20% → 拒
    rows, reject = mca.gate_bars("600519.SH", DAY, bars, daily,
                                 source="east", created_at=CREATED_AT)
    assert rows == [] and reject and "amount" in reject


def test_gate_east_missing_amount_rejected():
    bars = make_bars(volume=100.0)                  # amount=None → 源契约破坏
    rows, reject = mca.gate_bars("600519.SH", DAY, bars, daily_ref_row(),
                                 source="east", created_at=CREATED_AT)
    assert rows == [] and reject and "amount" in reject


def test_gate_east_fractional_volume_rejected():
    bars = make_bars(volume=100.5, amount=1000.0)   # 手出现小数 → 单位错配
    rows, reject = mca.gate_bars("600519.SH", DAY, bars, daily_ref_row(),
                                 source="east", created_at=CREATED_AT)
    assert rows == [] and reject and "volume" in reject


# ── 源拉取（注入 fetcher，零网络）────────────────────────────

def test_fetch_day_bars_b_filters_to_day():
    payload = make_bars(DAY) + make_bars("2026-11-06")
    fetcher = lambda code: payload  # noqa: E731
    bars = mca.fetch_day_bars("600519.SH", DAY, source="B", fetcher=fetcher)
    assert len(bars) == 48
    assert all(b["datetime"].strftime("%Y-%m-%d") == DAY for b in bars)
    assert all(b["amount"] is None for b in bars)


def test_fetch_day_bars_b_no_coverage_returns_empty():
    fetcher = lambda code: make_bars("2026-11-06")  # noqa: E731
    bars = mca.fetch_day_bars("600519.SH", DAY, source="B", fetcher=fetcher)
    assert bars == []


def test_fetch_day_bars_east_filters_day_and_keeps_amount():
    payload = make_bars(DAY, amount=1000.0) + make_bars("2026-11-06", amount=999.0)
    fetcher = lambda code: payload  # noqa: E731
    bars = mca.fetch_day_bars("600519.SH", DAY, source="east", fetcher=fetcher)
    assert len(bars) == 48
    assert all(b["datetime"].strftime("%Y-%m-%d") == DAY for b in bars)
    assert all(b["amount"] == 1000.0 for b in bars), "east 行 amount 必须透传（元）"


def test_fetch_day_bars_east_amount_none_passthrough_for_gate():
    # fetch 层不吞错：源缺额透传 None，由 gate_bars 的源契约门禁 fail-closed
    fetcher = lambda code: make_bars(DAY)  # noqa: E731  amount=None
    bars = mca.fetch_day_bars("600519.SH", DAY, source="east", fetcher=fetcher)
    assert all(b["amount"] is None for b in bars)


def test_fetch_day_bars_a_pages_and_converts():
    class FakeApi:
        def __init__(self):
            self.calls = []
        def get_security_bars(self, category, market, code, start, count):
            self.calls.append((category, market, code, start, count))
            assert category == 0  # 5 分钟线
            if start == 0:
                return [{"datetime": f"{DAY} 09:35", "open": 10.0, "high": 10.5,
                         "low": 9.8, "close": 10.2, "vol": 10000, "amount": 1000.0},
                        {"datetime": f"{DAY} 09:40", "open": 10.0, "high": 10.5,
                         "low": 9.8, "close": 10.2, "vol": 9900, "amount": 990.0}]
            return []
    api = FakeApi()
    bars = mca.fetch_day_bars("600519.SH", DAY, source="A", pytdx_api=api)
    assert len(bars) == 2 and bars[0]["volume"] == 10000 and bars[0]["amount"] == 1000.0


# ── collect_day 编排 ─────────────────────────────────────────

def test_collect_day_writes_gated_rows_and_records_rejects():
    keys = [(DAY, "600519.SH"), (DAY, "000001.SZ")]
    daily = {(DAY, "600519.SH"): daily_ref_row(), (DAY, "000001.SZ"): daily_ref_row()}
    fetcher = lambda code: make_bars()  # noqa: E731
    out = mca.collect_day(keys, daily, source="B", fetcher=fetcher, created_at=CREATED_AT)
    assert len(out["rows"]) == 96 and not out["rejects"] and not out["fetch_errors"]


def test_collect_day_suspended_key_no_coverage_is_honest_reject():
    keys = [(DAY, "600519.SH")]
    fetcher = lambda code: []  # noqa: E731
    out = mca.collect_day(keys, {(DAY, "600519.SH"): daily_ref_row()},
                          source="B", fetcher=fetcher, created_at=CREATED_AT)
    assert out["rows"] == []
    assert out["rejects"][0]["reason"] == "no_tencent_coverage"


def test_collect_day_all_fetch_errors_is_unreachable():
    def boom(code):
        raise RuntimeError("腾讯 m5 拉取失败: timeout")
    keys = [(DAY, c) for c in ("600519.SH", "000001.SZ")]
    out = mca.collect_day(keys, {}, source="B", fetcher=boom, created_at=CREATED_AT)
    assert out["rows"] == [] and len(out["fetch_errors"]) == 2


def test_collect_day_zero_bars_answer_is_no_coverage_not_unreachable():
    # 源已应答但无行（如腾讯不覆盖 920xxx.BJ）→ no_tencent_coverage 拒绝，
    # 不计入 fetch_errors（≠源不可达）
    def zero(code):
        raise RuntimeError("腾讯 m5 bj920931 返回 0 根")
    keys = [(DAY, "920931.BJ")]
    out = mca.collect_day(keys, {(DAY, "920931.BJ"): daily_ref_row()},
                          source="B", fetcher=zero, created_at=CREATED_AT)
    assert out["rows"] == [] and out["fetch_errors"] == {}
    assert out["rejects"] == [{"code": "920931.BJ", "day": DAY,
                               "reason": "no_tencent_coverage"}]


def test_classify_all_missing_rejected_is_terminal_ok():
    """缺口全被日线仲裁拒绝且零抓取错误=终态0（不误报降级卡聚合）。"""
    from scripts.minute_collect_auto import classify_outcome, EXIT_OK
    assert classify_outcome(missing=2, fetched_ok=5568, rejects=2,
                            fetch_errors={}, codes=["a", "b"]) == EXIT_OK


def test_classify_outcome_exit_contract():
    # 全失败 → 3（源不可达报警）；部分失败 → 4；正常/无缺口 → 0
    assert mca.classify_outcome(missing=5, fetched_ok=0, rejects=0,
                                fetch_errors={"a": "x", "b": "y"}, codes=("a", "b")) == 3
    assert mca.classify_outcome(missing=5, fetched_ok=4, rejects=0,
                                fetch_errors={"b": "x"}, codes=("a", "b")) == 4
    assert mca.classify_outcome(missing=5, fetched_ok=5, rejects=0,
                                fetch_errors={}, codes=("a",)) == 0
    assert mca.classify_outcome(missing=100, fetched_ok=100, rejects=99,
                                fetch_errors={}, codes=tuple("c%d" % i for i in range(100))) == 4


# ── formal_write_auto（R16 治理镜像；fake client）────────────

class LockableFakeClient:
    """支撑 DbWriteLock + formal_write_auto 的最小 client 桩（含锁状态机）。"""

    def __init__(self, *, existing_keys=(), readback=None, bars=None):
        self.commands = []
        self.inserts = []
        self.lock_exists = False
        self.lock_holder = None
        self.existing_keys = list(existing_keys)
        self.readback = readback or {}
        self.bars = list(bars or [])
        self.dropped_ids = None

    def command(self, sql):
        self.commands.append(sql)
        if sql.startswith("CREATE TABLE stock.write_lock_"):
            if self.lock_exists:
                raise RuntimeError("table already exists")
            self.lock_exists = True
        elif sql.startswith("INSERT INTO stock.write_lock_"):
            self.lock_holder = {"scope": "kline_minute_backfill_window",
                                "owner": "aistock-engineer"}
        elif sql.startswith("DROP TABLE stock.write_lock_"):
            self.lock_exists, self.lock_holder = False, None
        elif sql.startswith("ALTER TABLE stock.kline_minute_5 DELETE"):
            import re as _re
            ids = _re.search(r"WHERE id IN \(([^)]*)\)", sql).group(1)
            self.dropped_ids = [int(x) for x in ids.split(",")]

    def query(self, sql, settings=None):
        class R:
            def __init__(self, names, rows):
                self.column_names = names
                self.result_rows = rows
        if "system.tables" in sql:
            return R(["table_count"], [(1 if self.lock_exists else 0,)])
        if "FROM stock.write_lock_" in sql:
            if self.lock_holder:
                return R(["scope", "owner", "purpose", "acquired_at", "host", "pid"],
                         [("kline_minute_backfill_window", "aistock-engineer",
                           "", "2026-11-09 09:00:00", "h", 1)])
            return R(["scope", "owner", "purpose", "acquired_at", "host", "pid"], [])
        if "SELECT DISTINCT code" in sql:
            return R(["code", "d"], [(c, d) for d, c in self.existing_keys])
        if "countIf(amount IS NOT NULL)" in sql:
            rows = []
            for (d, c), agg in sorted(self.readback.items()):
                rows.append((d, c, agg[0], agg[1], agg[2], agg[3]))
            return R(["d", "code", "n", "v", "a", "amt_nonnull"], rows)
        if "substring(toString(toDateTime" in sql and "FROM stock.kline_minute_5" in sql:
            rows = [(b["datetime"].strftime("%Y-%m-%d %H:%M"), "600519.SH",
                     b["open"], b["high"], b["low"], b["close"],
                     int(b["volume"]), b["amount"])
                    for b in sorted(self.bars, key=lambda x: x["datetime"])]
            return R(["dt", "code", "open", "high", "low", "close", "volume", "amount"], rows)
        raise AssertionError(f"unexpected query: {sql[:120]}")

    def insert(self, table, rows, column_names=None):
        self.inserts.append((table, list(rows), list(column_names or ())))


def good_rows(n_codes=1):
    rows = []
    for i in range(n_codes):
        code = f"6005{i:02d}.SH"
        rows.extend(mca.gate_bars(code, DAY, make_bars(), daily_ref_row(),
                                  source="B", created_at=CREATED_AT)[0])
    return rows


def readback_for(rows):
    agg = {}
    for r in rows:
        key = (r[1].astimezone(CN).strftime("%Y-%m-%d"), r[0])
        n, v, a, an = agg.get(key, (0, 0.0, 0.0, 0))
        agg[key] = (n + 1, v + r[6], a + (r[7] or 0.0), an + (r[7] is not None))
    return agg


def test_formal_write_auto_lock_insert_verify_release():
    rows = good_rows(2)
    client = LockableFakeClient(readback=readback_for(rows), bars=make_bars())
    events = []
    report = mca.formal_write_auto(lambda: client, rows, CH_WIN,
                                   journal=events.append,
                                   lock_owner="aistock-engineer", purpose="t")
    assert report["status"] == "ok"
    assert len(client.inserts) == 1
    assert client.inserts[0][0] == "kline_minute_5"
    assert client.inserts[0][2] == mb.INSERT_COLUMNS
    evs = [e["event"] for e in events]
    assert evs.index("lock_acquired") < evs.index("insert") < evs.index("lock_released")
    assert not client.lock_exists  # 已释放


def test_formal_write_auto_zero_rows_skips_lock_and_insert():
    client = LockableFakeClient()
    events = []
    report = mca.formal_write_auto(lambda: client, [], CH_WIN,
                                   journal=events.append, lock_owner="o", purpose="t")
    assert report["status"] == "noop"
    assert client.inserts == [] and not client.lock_exists
    assert all("lock_acquired" not in e.get("event", "") for e in events)


def test_formal_write_auto_existing_keys_fail_closed():
    rows = good_rows(1)
    code = rows[0][0]
    client = LockableFakeClient(existing_keys=[(DAY, code)])
    with pytest.raises(RuntimeError, match="存量键"):
        mca.formal_write_auto(lambda: client, rows, CH_WIN,
                              journal=lambda e: None, lock_owner="aistock-engineer", purpose="t")
    assert client.inserts == []
    assert not client.lock_exists  # 异常路径也必须释放锁


def test_formal_write_auto_verify_fail_rolls_back_by_ids():
    rows = good_rows(1)
    bad = dict(readback_for(rows))
    key = next(iter(bad))
    bad[key] = (bad[key][0], bad[key][1] * 2, bad[key][2], bad[key][3])  # volume 对不上
    client = LockableFakeClient(readback=bad)
    with pytest.raises(RuntimeError, match="读回"):
        mca.formal_write_auto(lambda: client, rows, CH_WIN,
                              journal=lambda e: None, lock_owner="aistock-engineer", purpose="t")
    assert client.dropped_ids == [r[9] for r in rows], "按精确 id 回滚"
    assert client.inserts and not client.lock_exists


def test_formal_write_auto_detects_fabricated_amount_for_none_rows():
    # B 行应全 NULL amount：读回发现非 NULL amount 计数 >0 → 视为脏数据 fail-closed
    rows = good_rows(1)
    key = (rows[0][1].astimezone(CN).strftime("%Y-%m-%d"), rows[0][0])
    client = LockableFakeClient(readback={key: (48, 4800.0, 123.0, 48)})  # amt_nonnull=48！
    with pytest.raises(RuntimeError, match="amount"):
        mca.formal_write_auto(lambda: client, rows, CH_WIN,
                              journal=lambda e: None, lock_owner="aistock-engineer", purpose="t")


def test_formal_write_auto_tolerates_null_amount_sum_from_db():
    # R22 生产实测：B 行全 NULL 时 CH 的 sum(amount) 返回 None（非 0）——
    # 读回聚合必须按 0 处理而不是 float(None) 崩溃（首次 11-03 B 写入因此回滚）
    rows = good_rows(1)
    key = (rows[0][1].astimezone(CN).strftime("%Y-%m-%d"), rows[0][0])
    client = LockableFakeClient(readback={key: (48, 4800.0, None, 0)}, bars=make_bars())  # a=None！
    report = mca.formal_write_auto(lambda: client, rows, CH_WIN,
                                   journal=lambda e: None, lock_owner="aistock-engineer",
                                   purpose="t")
    assert report["status"] == "ok"


def test_formal_write_auto_sample_check_ignores_adjacent_day():
    # ch_window(单日) 上界含相邻北京日（R19 陷阱）：读回若按 HH:MM 折叠，
    # 相邻日 bar 会覆盖目标日值 → 假 mismatch 回滚（R22 生产实测）。
    # 抽样校验必须只比目标日。
    rows = good_rows(1)
    key = (rows[0][1].astimezone(CN).strftime("%Y-%m-%d"), rows[0][0])
    adjacent = make_bars("2026-11-10", close_scale=2.0, volume=999.0)  # 不同值防同值巧合
    client = LockableFakeClient(readback={key: (48, 4800.0, 0.0, 0)},
                                bars=make_bars() + adjacent)
    report = mca.formal_write_auto(lambda: client, rows, CH_WIN,
                                   journal=lambda e: None, lock_owner="aistock-engineer",
                                   purpose="t")
    assert report["status"] == "ok"


# ── verify_existing（与库内 lc5 逐 bar 一致性）───────────────

class VerifyFakeClient:
    def __init__(self, bars, code="600519.SH"):
        self.bars = bars
        self.code = code

    def query(self, sql, settings=None):
        class R:
            def __init__(self, names, rows):
                self.column_names = names
                self.result_rows = rows
        assert "FROM stock.kline_minute_5" in sql
        rows = [(b["datetime"].strftime("%Y-%m-%d %H:%M"), self.code, b["open"], b["high"],
                 b["low"], b["close"], int(b["volume"]), b["amount"])
                for b in sorted(self.bars, key=lambda x: x["datetime"])]
        return R(["dt", "code", "open", "high", "low", "close", "volume", "amount"], rows)


def test_verify_existing_pass_and_mismatch():
    db_bars = make_bars()
    client = VerifyFakeClient(db_bars)
    same = lambda code: make_bars()  # noqa: E731
    rep = mca.verify_existing(client, DAY, ["600519.SH"], source="B", fetcher=same)
    assert rep["fail"] == 0 and rep["pass"] == 1

    def skewed(code):
        bars = make_bars()
        bars[10]["close"] = bars[10]["close"] * 1.5  # 超 1%
        return bars
    rep2 = mca.verify_existing(client, DAY, ["600519.SH"], source="B", fetcher=skewed)
    assert rep2["fail"] == 1 and rep2["detail"][0]["status"] == "mismatch"


def test_verify_existing_east_amount_mismatch():
    # east 带额：逐 bar 额 ≤3% 比对启用——源额偏 50% 必须检出
    db_bars = make_bars(amount=1000.0)
    client = VerifyFakeClient(db_bars)
    same = lambda code: make_bars(amount=1000.0)  # noqa: E731
    rep = mca.verify_existing(client, DAY, ["600519.SH"], source="east", fetcher=same)
    assert rep["fail"] == 0 and rep["pass"] == 1

    def skewed_amt(code):
        bars = make_bars(amount=1500.0)
        return bars
    rep2 = mca.verify_existing(client, DAY, ["600519.SH"], source="east", fetcher=skewed_amt)
    assert rep2["fail"] == 1
    assert any("amount" in i for i in rep2["detail"][0]["issues"])


def test_verify_existing_ignores_adjacent_day_bars():
    # 单日 ch_window 含相邻北京日：库里相邻日 bar 不得参与比对/日量合计
    db_bars = make_bars() + make_bars("2026-11-10", close_scale=2.0, volume=999.0)
    client = VerifyFakeClient(db_bars)
    same = lambda code: make_bars()  # noqa: E731
    rep = mca.verify_existing(client, DAY, ["600519.SH"], source="B", fetcher=same)
    assert rep["fail"] == 0 and rep["pass"] == 1
    assert rep["detail"][0]["db_bars"] == 48, "只统计目标日 48 根"


class ReadbackFakeClient:
    """day_readback_summary 查询面：当日在库码全集 + 逐 bar 行（含相邻日）。"""

    def __init__(self, codes, bars):
        self.codes = list(codes)
        self.bars = list(bars)

    def query(self, sql, settings=None):
        class R:
            def __init__(self, names, rows):
                self.column_names = names
                self.result_rows = rows
        if "SELECT DISTINCT code" in sql:
            return R(["code"], [(c,) for c in self.codes])
        if "FROM stock.kline_minute_5" in sql:
            rows = [(b["datetime"].strftime("%Y-%m-%d %H:%M"), "600519.SH",
                     b["open"], b["high"], b["low"], b["close"],
                     int(b["volume"]), b["amount"])
                    for b in sorted(self.bars, key=lambda x: x["datetime"])]
            return R(["dt", "code", "open", "high", "low", "close", "volume", "amount"], rows)
        raise AssertionError(f"unexpected: {sql[:80]}")


def test_day_readback_summary_counts_only_target_day():
    bars = make_bars() + make_bars("2026-11-10", close_scale=2.0)
    client = ReadbackFakeClient(["600519.SH"], bars)
    rep = mca.day_readback_summary(client, DAY)
    assert rep["codes"] == 1 and rep["total_bars"] == 48 and rep["bars_48"] == 1
    assert rep["bars_not_48"] == {}


# ── run() 端到端（注入 fake client/fetcher；无 DB/网络）──────

class RunFakeClient(LockableFakeClient):
    """run() 所需的查询面：日线 max/count、日历、逐日缺口、既有键读回。"""

    def __init__(self, *, latest_daily=DAY, daily_rows=None, missing_by_day=None,
                 calendar=None, existing_codes=(), minute_bars=None):
        super().__init__(existing_keys=[], readback=None)
        self.latest_daily = latest_daily
        self.daily_rows = daily_rows or {DAY: 5583}
        self.missing_by_day = missing_by_day or {DAY: 0}
        self.calendar = calendar or CAL
        self.existing_codes = list(existing_codes)
        self.minute_bars = minute_bars or []
        self.readback_calls = []

    def query(self, sql, settings=None):
        class R:
            def __init__(self, names, rows):
                self.column_names = names
                self.result_rows = rows
        if "max(trade_date)" in sql:
            return R(["m"], [(self.latest_daily,)])
        if "FROM stock.trade_calendar" in sql:
            rows = [(d, it) for d, it in self.calendar.items()]
            return R(["d", "it"], rows)
        if "SELECT DISTINCT code, toString" in sql:   # formal_write 锁内反连接
            return R(["code", "d"], [(c, d) for d, c in self.existing_keys])
        if "SELECT DISTINCT code" in sql:             # 当日在库码全集
            return R(["code"], [(c,) for c in self.existing_codes])
        if "FROM stock.kline_daily" in sql and "AND code IN" in sql:  # 日线基准行
            days = [d for d in self.daily_rows]
            rows = [(d, c, *daily_ref_row()) for d in days for c in _codes_from_sql(sql)]
            return R(["d", "code", "open", "high", "low", "close", "volume", "amount"], rows)
        if "FROM stock.kline_daily" in sql and "GROUP BY" in sql:
            rows = [(d, n) for d, n in self.daily_rows.items()]
            return R(["d", "n"], rows)
        if "LEFT ANTI JOIN" in sql:
            rows = []
            for d, n in self.missing_by_day.items():
                for i in range(n):
                    rows.append((d, f"6005{i:02d}.SH"))
            return R(["td", "code"], rows)
        if "countIf(amount IS NOT NULL)" in sql:
            self.readback_calls.append(sql)
            return R(["d", "code", "n", "v", "a", "amt_nonnull"], [])
        if "code IN (" in sql and "FROM stock.kline_minute_5" in sql:
            # verify_existing / day_readback 的逐码 bar 查询
            if self.minute_bars:
                rows = [(b["datetime"].strftime("%Y-%m-%d %H:%M"), "600519.SH",
                         b["open"], b["high"], b["low"], b["close"],
                         int(b["volume"]), b["amount"])
                        for b in sorted(self.minute_bars, key=lambda x: x["datetime"])]
                return R(["dt", "code", "open", "high", "low", "close", "volume", "amount"], rows)
            return R(["dt", "code", "open", "high", "low", "close", "volume", "amount"], [])
        return super().query(sql, settings)


def _codes_from_sql(sql):
    import re as _re
    m = _re.search(r"code IN \(([^)]*)\)", sql)
    if not m:
        return []
    return [x.strip().strip("'") for x in m.group(1).split(",") if x.strip()]


def test_run_no_gap_formal_write_zero_insert_and_readback(tmp_path):
    # 11-09 重跑契约：无缺口 → 全跳过、0 插入、(可选)存量抽样一致 + 读回汇总，退出码 0
    client = RunFakeClient(missing_by_day={DAY: 0}, existing_codes=["600519.SH"],
                           minute_bars=make_bars())
    rc = mca.run(["--mode", "formal-write", "--trade-date", DAY,
                  "--verify-existing", "1", "--out-dir", str(tmp_path)],
                 client_factory=lambda: client,
                 fetcher=lambda code: make_bars())
    assert rc == 0
    assert client.inserts == [], "0 插入"
    assert not client.lock_exists, "无缺口不取锁"
    report = json_load(tmp_path)
    assert report["plan"]["rows"] == 0
    assert report["verify_existing"]["fail"] == 0
    assert report["readback"]["codes"] >= 1


def json_load(tmp_path):
    files = sorted(tmp_path.glob("report_*.json"))
    assert files, "报告 json 必须落盘"
    return json.loads(files[-1].read_text(encoding="utf-8"))


def test_run_missing_day_formal_write_inserts(tmp_path):
    client = RunFakeClient(missing_by_day={DAY: 2},
                           daily_rows={DAY: 5583},
                           minute_bars=make_bars(amount=1000.0))
    # auto → east（R22 默认源，带额）：fetcher 必须供 amount 才过源契约门禁
    rows = mca.gate_bars("600500.SH", DAY, make_bars(amount=1000.0), daily_ref_row(),
                         source="east", created_at=CREATED_AT)[0]
    rows += mca.gate_bars("600501.SH", DAY, make_bars(amount=1000.0), daily_ref_row(),
                          source="east", created_at=CREATED_AT)[0]
    agg = readback_for(rows)

    def q(sql, settings=None):
        if "countIf(amount IS NOT NULL)" in sql and "GROUP BY" in sql:
            class R:
                column_names = ["d", "code", "n", "v", "a", "amt_nonnull"]
                result_rows = [(d, c, *v) for (d, c), v in sorted(agg.items())]
            return R()
        return RunFakeClient.query(client, sql, settings)
    client.query = q

    rc = mca.run(["--mode", "formal-write", "--trade-date", DAY,
                  "--out-dir", str(tmp_path)],
                 client_factory=lambda: client,
                 fetcher=lambda code: make_bars(amount=1000.0))
    assert rc == 0
    assert len(client.inserts) == 1 and client.inserts[0][2] == mb.INSERT_COLUMNS
    assert not client.lock_exists


def test_run_source_unreachable_nonzero_exit(tmp_path):
    client = RunFakeClient(missing_by_day={DAY: 2})
    def boom(code):
        raise RuntimeError("腾讯 m5 拉取失败: refused")
    rc = mca.run(["--mode", "formal-write", "--trade-date", DAY,
                  "--out-dir", str(tmp_path)],
                 client_factory=lambda: client, fetcher=boom)
    assert rc == mca.EXIT_SOURCE_UNREACHABLE != 0
    assert client.inserts == []


def test_run_auto_resolves_and_daily_missing_hint(tmp_path):
    # auto：11-09 无缺口、11-06 有缺口 → 目标 11-06
    client = RunFakeClient(missing_by_day={DAY: 0, "2026-11-06": 1},
                           daily_rows={DAY: 5583, "2026-11-06": 5583})
    rc = mca.run(["--mode", "audit", "--out-dir", str(tmp_path)],
                 client_factory=lambda: client)
    assert rc == 0
    assert json_load(tmp_path)["target"]["trade_date"] == "2026-11-06"

    # auto 选中的缺口日当日日线整日缺失 → fail-closed 提示 daily_kline_pipeline
    client2 = RunFakeClient(latest_daily="2026-11-06",
                            daily_rows={"2026-11-06": 5583},
                            missing_by_day={"2026-11-06": 0, "2026-11-05": 5})
    with pytest.raises(SystemExit, match="daily_kline_pipeline"):
        mca.run(["--mode", "audit", "--out-dir", str(tmp_path)],
                client_factory=lambda: client2)

# -*- coding: utf-8 -*-
"""R25（2026-11-09）：/market/emotion-trend 按需重算的单元测试（先于实现编写，TDD）。

背景：emotion_cycle 预计算表的写入方（情绪周期计算任务）已停用，首页情绪
周期最终日期停 2026-10-30。R25 把 daily 分支改为 kline_daily + kline_minute_5
基表 raw 直查逐日重算（镜像 R20b 模式），响应字段与结构完全不变。

口径提取（不重写）自 scripts/generate_emotion_cycle.py EmotionCycleGenerator
（api/system_config.py 情绪周期修复任务调用的本体，写入 emotion_cycle 表）：
  universe            = kline_daily FINAL JOIN stocks WHERE s.type='stock'
                        （注意：无 open>0 过滤，与 sentiment 链不同）
  close_up_rate       = pct>0 占比（None 不计入分子，计入 total）
  intraday_up_rate    = 写入方为 close_up_rate 占位（"暂时用收盘上涨率代替"）；
                        R25 改真口径：kline_minute_5 当日末根 close > 前收
                        （kline_daily 上一数据日 argMax(close, created_at)）；
                        5m 无覆盖日回退 close_up_rate（保持写入方占位语义）
  sh/sz/cyb_up_rate   = code LIKE '6%' / '0%'|'3%' / '3%'
  strong_up_rate      = pct>3；weak_up_rate = pct<-3（全体 universe）
  yesterday_monster   = 前一数据日 pct>9.5 的代码在当日 pct>0 占比
  yesterday_strong    = 前一数据日 pct>5；yesterday_weak = pct<-5
                        （分母=前日分类代码中当日有行的，停牌代码剔除）
  limit_up_follow_rate= 写入方常量 0.0（占位）
  total_stocks        = 当日 universe 行数；is_confirmed=表旗标，响应不含

守卫（R20b 镜像）：AST 结构守卫（emotion 链不引用 emotion_cycle 表读取、
quality_read/precise 视图、ORM session；网关调用全 raw=True）+ 运行时守卫
（fake 数据层断言每条 SQL raw=True 且不含视图 token）。

真实库上的口径对拍（十月样本日 13 字段 diff≤0.5pp）由部署前脚本独立完成，
不在本文件范围。
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import api.market_overview as mo

B = "2026-10-30"   # 缓冲/前一日
W1 = "2026-11-06"  # 窗口第 1 日
W2 = "2026-11-09"  # 最新有数据交易日
D3 = "2026-11-10"  # 数据日期推进
CALENDAR_TAIL = [W2, W1, B, "2026-10-29", "2026-10-28", "2026-10-27", "2026-10-26"]

# 合成 kline_daily：date -> {code: (change_pct|None, close)}
KLINE = {
    B: {
        "600001.SH": (10.0, 10.0),   # 妖股+强势种子
        "600002.SH": (5.5, 20.0),    # 强势种子
        "000001.SZ": (-6.0, 30.0),   # 弱势种子
        "600003.SH": (1.0, 40.0),
        "880001.BJ": (9.6, 50.0),    # 妖股种子但 W1 停牌（分母剔除样本）
        "300001.SZ": (0.5, 5.0),
        "000002.SZ": (0.2, 8.0),
        "000003.SZ": (0.0, 7.5),
    },
    W1: {
        "600001.SH": (-2.0, 9.8),
        "600002.SH": (1.0, 20.2),
        "000001.SZ": (2.0, 30.6),
        "600003.SH": (9.6, 44.0),    # W1 妖股（>9.5）→ W2 yesterday 种子
        "300001.SZ": (4.0, 5.2),
        "000003.SZ": (None, 7.2),    # change_pct NULL：计入 total、不进任何比率
    },
    W2: {
        "600001.SH": (3.5, 10.15),
        "600002.SH": (-1.0, 19.99),
        "000001.SZ": (0.5, 30.75),
        "600003.SH": (6.0, 42.4),
        "300001.SZ": (-4.0, 5.0),
        "000003.SZ": (None, 7.0),
    },
}
DATA_DATES = [B, W1, W2]

# 合成 5m 末根收盘：date -> {code: last_close}
LAST_BAR = {
    W1: {"600001.SH": 9.85, "600002.SH": 20.1, "000001.SZ": 30.5,
         "600003.SH": 44.0, "300001.SZ": 5.3, "000003.SZ": 7.2},
    W2: {"600001.SH": 10.2, "600002.SH": 19.9, "000001.SZ": 30.8,
         "600003.SH": 42.5, "300001.SZ": 4.9, "000003.SZ": 7.1},
}

# ── 手算期望（独立于 fake 聚合，双保险）────────────────────────────────────
EXP_W1 = {
    "close_up_rate": 4 / 6 * 100,        # 600002/000001/600003/300001
    "sh_up_rate": 2 / 3 * 100,           # 6 开头 3 行，涨 2
    "sz_up_rate": 2 / 3 * 100,           # 0|3 开头 3 行，涨 2（000003 NULL 不涨）
    "cyb_up_rate": 1 / 1 * 100,          # 300001 涨
    "strong_up_rate": 2 / 6 * 100,       # 9.6 与 4.0 > 3
    "weak_up_rate": 0.0,
    "yesterday_monster_up_rate": 0.0,    # B 妖股 {600001,880001}；600001 当日 -2、880001 停牌 → 0/1
    "yesterday_strong_up_rate": 1 / 2 * 100,   # B>5 {600001,600002,880001}；当日有行 2、涨 1
    "yesterday_weak_up_rate": 1 / 1 * 100,     # B<-5 {000001} → 当日 2.0 涨
    "intraday_up_rate": 4 / 6 * 100,     # 600002/000001/600003/300001 末根>前收
}
EXP_W2 = {
    "close_up_rate": 3 / 6 * 100,        # 600001/000001/600003
    "sh_up_rate": 2 / 3 * 100,
    "sz_up_rate": 1 / 3 * 100,
    "cyb_up_rate": 0.0,                  # 300001 唯一创业板块 -4
    "strong_up_rate": 2 / 6 * 100,       # 3.5 与 6.0
    "weak_up_rate": 1 / 6 * 100,         # -4.0
    "yesterday_monster_up_rate": 1 / 1 * 100,  # W1>9.5 {600003} → 当日 6.0 涨
    "yesterday_strong_up_rate": 1 / 1 * 100,
    "yesterday_weak_up_rate": 0.0,       # W1 无 <-5
    "intraday_up_rate": 2 / 6 * 100,     # 600001/000001 末根>前收
}

SERIES_FIELDS = (
    "close_up_rate", "intraday_up_rate", "sh_up_rate", "sz_up_rate", "cyb_up_rate",
    "yesterday_monster_up_rate", "yesterday_strong_up_rate", "yesterday_weak_up_rate",
    "strong_up_rate", "weak_up_rate", "limit_up_follow_rate",
)
PAYLOAD_KEYS = {"dates", *SERIES_FIELDS, "total", "source"}

# 发往 CH 的 SQL 必须保留的口径 token（防口径漂移；LIKE %% 为驱动绑定前形态）
CALIBER_TOKENS = [
    "s.type = 'stock'",
    "code LIKE '6%%'",
    "(code LIKE '0%%' OR code LIKE '3%%')",
    "code LIKE '3%%'",
    "pct > 9.5",
    "pct > 5",
    "pct < -5",
    "pct > 3",
    "pct < -3",
    "argMax(close, tuple(datetime, created_at))",
    "datetime >= ? AND datetime < ? + INTERVAL 1 DAY",
    "toHour(datetime) >= 6",
    "lagInFrame(toNullable(trade_date), 1, NULL)",
]

EMOTION_CHAIN_FUNCTIONS = [
    "get_emotion_trend",
    "_compute_emotion_trend",
    "_emotion_trend_window_dates",
    "_emotion_core_aggregates",
    "_emotion_yesterday_aggregates",
    "_emotion_intraday_aggregates",
]


# ── fake 数据层（SQL 形状路由，CH 语义镜像）───────────────────────────────
def _up(pct):
    return pct is not None and pct > 0


def _core_rows(dates):
    rows = []
    for d in dates:
        cols = KLINE[d]
        codes = list(cols)
        total = len(codes)
        sh = [c for c in codes if c.startswith("6")]
        sz = [c for c in codes if c.startswith("0") or c.startswith("3")]
        cyb = [c for c in codes if c.startswith("3")]
        pcts = [cols[c][0] for c in codes]

        def rate(sel):
            return round(sum(1 for c in sel if _up(cols[c][0])) / len(sel) * 100, 10) if sel else 0.0

        rows.append({
            "trade_date": d,
            "total_stocks": total,
            "close_up_rate": round(sum(1 for p in pcts if _up(p)) / total * 100, 10),
            "sh_up_rate": rate(sh),
            "sz_up_rate": rate(sz),
            "cyb_up_rate": rate(cyb),
            "strong_up_rate": round(sum(1 for p in pcts if p is not None and p > 3) / total * 100, 10),
            "weak_up_rate": round(sum(1 for p in pcts if p is not None and p < -3) / total * 100, 10),
        })
    return rows


def _yesterday_rows(dates):
    rows = []
    ordered = sorted(dates)
    for i, d in enumerate(ordered):
        if i == 0:
            rows.append({"trade_date": d, "yesterday_monster_up_rate": 0.0,
                         "yesterday_strong_up_rate": 0.0, "yesterday_weak_up_rate": 0.0})
            continue
        prev = ordered[i - 1]
        prev_cols = KLINE[prev]
        today_cols = KLINE[d]

        def cat_rate(cond):
            sel = [c for c, (p, _) in prev_cols.items() if p is not None and cond(p)]
            have = [c for c in sel if c in today_cols]
            if not have:
                return 0.0
            return round(sum(1 for c in have if _up(today_cols[c][0])) / len(have) * 100, 10)

        rows.append({
            "trade_date": d,
            "yesterday_monster_up_rate": cat_rate(lambda p: p > 9.5),
            "yesterday_strong_up_rate": cat_rate(lambda p: p > 5),
            "yesterday_weak_up_rate": cat_rate(lambda p: p < -5),
        })
    return rows


def _intraday_rows(dates, skip=()):
    rows = []
    ordered = sorted(dates)
    for i, d in enumerate(ordered):
        if d in skip or i == 0 or d not in LAST_BAR:
            continue
        prev = ordered[i - 1]
        prev_close = KLINE[prev]
        have, up = 0, 0
        for code, last in LAST_BAR[d].items():
            if code not in prev_close:
                continue
            pc = prev_close[code][1]
            if pc > 0 and last > 0:
                have += 1
                up += 1 if last > pc else 0
        if have:
            rows.append({"trade_date": d, "covered_stocks": have,
                         "intraday_up_rate": round(up / have * 100, 10)})
    return rows


class _FakeClickhouse:
    """按 SQL 形状路由的 fake 数据层，记录全部 (sql, params, raw)。

    运行时守卫：emotion 链发出的每条查询必须 raw=True（基表直查、绕过
    FINAL 视图重写），且 SQL 文本不得携带任何视图 token。
    """

    def __init__(self, skip_intraday=()):
        self.calls = []
        self.skip_intraday = set(skip_intraday)

    def _record(self, kind, sql, params, raw):
        assert raw is True, f"emotion 链查询必须 raw=True 基表直查: {sql[:120]}"
        assert "quality" not in sql and "precise" not in sql, (
            f"emotion 链 SQL 不得引用 FINAL 视图: {sql[:120]}"
        )
        self.calls.append((kind, sql, params, raw))

    def available(self):
        return True

    def scalar(self, sql, params=None, *, raw=False):
        self._record("scalar", sql, params, raw)
        if "max(trade_date)" in sql:
            return W2
        raise AssertionError(f"unexpected scalar sql: {sql[:120]}")

    def query_df(self, sql, params=None, *, raw=False):
        self._record("df", sql, params, raw)
        if "kline_minute_60" in sql:  # hourly SQL 同样含 AS close_up_rate/intraday_up_rate，须先判
            return pd.DataFrame([{
                "hour_time": pd.Timestamp("2026-11-09 14:00"),
                "close_up_rate": 55.0, "intraday_up_rate": 55.0,
                "sh_up_rate": 50.0, "sz_up_rate": 60.0, "cyb_up_rate": 40.0,
                "strong_up_rate": 10.0, "weak_up_rate": 5.0, "total_rows": 5000,
            }])
        if "FROM trade_calendar" in sql:
            assert params == [W2], f"日历窗口必须截至最新有数据交易日: {params}"
            return pd.DataFrame({"trade_date": CALENDAR_TAIL})
        if "AS intraday_up_rate" in sql:
            return pd.DataFrame(_intraday_rows(DATA_DATES, skip=self.skip_intraday))
        if "yesterday_monster_up_rate" in sql:
            return pd.DataFrame(_yesterday_rows(DATA_DATES))
        if "AS close_up_rate" in sql:
            return pd.DataFrame(_core_rows(DATA_DATES))
        raise AssertionError(f"unexpected df sql: {sql[:160]}")


@pytest.fixture(autouse=True)
def _clear_caches():
    mo._EMOTION_TREND_RESULT_CACHE.clear()
    yield
    mo._EMOTION_TREND_RESULT_CACHE.clear()


@pytest.fixture
def fake_env(monkeypatch):
    monkeypatch.setattr(mo, "MIN_EMOTION_CYCLE_STOCKS", 3)
    fake = _FakeClickhouse()
    monkeypatch.setattr(mo, "clickhouse_available", fake.available)
    monkeypatch.setattr(mo, "_has_clickhouse_table", lambda name: True)
    monkeypatch.setattr(mo, "_resolve_table_name", lambda base, candidates=None: base)
    monkeypatch.setattr(mo, "clickhouse_query_df", fake.query_df)
    monkeypatch.setattr(mo, "clickhouse_scalar", fake.scalar)
    monkeypatch.setattr(mo, "_kline_daily_latest_trade_date", lambda: W2)

    def _no_session():
        raise AssertionError("emotion 重算链不得使用 SQLAlchemy session（R25 禁令）")

    monkeypatch.setattr(mo, "db", SimpleNamespace(get_session=_no_session))
    monkeypatch.setattr(mo, "_intraday_emotion_snapshot", lambda: None)
    return fake


# ── 1) 口径：合成数据 → 手算期望 ───────────────────────────────────────────
def test_payload_caliber_matches_hand_computed(fake_env):
    payload = mo._compute_emotion_trend(days=2)

    assert payload["dates"] == [W1, W2]
    assert payload["total"] == 2
    assert payload["source"] == "clickhouse"
    assert set(payload.keys()) == PAYLOAD_KEYS
    for field, expected in EXP_W1.items():
        assert payload[field][0] == pytest.approx(round(expected, 2)), f"W1 {field}"
    for field, expected in EXP_W2.items():
        assert payload[field][1] == pytest.approx(round(expected, 2)), f"W2 {field}"
    # 涨停溢价率 = 写入方常量占位 0.0
    assert payload["limit_up_follow_rate"] == [0.0, 0.0]
    # 比率一律保留 2 位小数（表值口径）
    for field in SERIES_FIELDS:
        for v in payload[field]:
            assert v == round(v, 2)


def test_yesterday_denominator_drops_suspended(fake_env):
    payload = mo._compute_emotion_trend(days=2)
    # B 日妖股 880001.BJ 在 W1 停牌：分子分母都不得计入（0/1 而非 0/2）
    assert payload["yesterday_monster_up_rate"][0] == pytest.approx(0.0, abs=1e-9)


def test_null_change_pct_counts_in_total_not_in_rates(fake_env):
    payload = mo._compute_emotion_trend(days=2)
    # W2 000003.SZ change_pct=NULL：total=6，close_up_rate=3/6 而非 3/5
    assert payload["close_up_rate"][1] == pytest.approx(50.0)
    assert payload["sz_up_rate"][1] == pytest.approx(round(1 / 3 * 100, 2))


def test_intraday_falls_back_to_close_rate_without_5m(monkeypatch, fake_env):
    fake = _FakeClickhouse(skip_intraday=(W1,))
    monkeypatch.setattr(mo, "clickhouse_query_df", fake.query_df)
    payload = mo._compute_emotion_trend(days=2)
    # W1 无 5m 覆盖 → 回退收盘口径（写入方 intraday=close 占位语义）
    assert payload["intraday_up_rate"][0] == pytest.approx(round(EXP_W1["close_up_rate"], 2))
    assert payload["intraday_up_rate"][1] == pytest.approx(round(EXP_W2["intraday_up_rate"], 2))


def test_dates_below_min_stocks_are_dropped(monkeypatch, fake_env):
    monkeypatch.setattr(mo, "MIN_EMOTION_CYCLE_STOCKS", 7)
    payload = mo._compute_emotion_trend(days=2)
    assert payload.get("error")
    assert payload["dates"] == []


def test_days_param_clamped_for_bounded_scan(fake_env):
    mo._compute_emotion_trend(days=100000)
    calendar_sql = [c[1] for c in fake_env.calls if "FROM trade_calendar" in c[1]][0]
    assert "LIMIT 255" in calendar_sql, "days 必须钳制（默认上限 250，防全表 5m 扫描）"


def test_emitted_sql_preserves_caliber_tokens(fake_env):
    mo._compute_emotion_trend(days=2)
    all_sql = "\n".join(c[1] for c in fake_env.calls)
    for token in CALIBER_TOKENS:
        assert token in all_sql, f"emotion SQL 丢失口径 token: {token}"
    # 三条聚合的参数形态：范围谓词绑定数据日期
    core = [c for c in fake_env.calls if "AS close_up_rate" in c[1]][0]
    assert core[2] == ["2026-10-26", W2]


# ── 2) hourly 分支保持（结构不变 + raw 化）────────────────────────────────
def test_hourly_branch_unchanged_and_raw(fake_env):
    payload = mo._compute_emotion_trend(days=2, granularity="hourly")
    assert payload["granularity"] == "hourly"
    assert payload["source"] == "clickhouse:kline_minute_60"
    assert payload["dates"] == ["2026-11-09 14:00"]
    assert payload["close_up_rate"] == [55.0]
    hourly_calls = [c for c in fake_env.calls if "kline_minute_60" in c[1]]
    assert hourly_calls and all(c[3] is True for c in hourly_calls), "hourly 查询必须 raw=True"


# ── 3) 缓存：60s TTL + 数据日期绑定键 ─────────────────────────────────────
class _FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now


def _patch_cache_plumbing(monkeypatch, key_date):
    calls = {"compute": 0}

    def _compute(days=30, granularity="daily"):
        calls["compute"] += 1
        return {"dates": [W2], "close_up_rate": [1.0], "total": 1}

    clock = _FakeClock()
    holder = {"date": key_date}
    monkeypatch.setattr(mo, "time", clock)
    monkeypatch.setattr(mo, "_compute_emotion_trend", _compute)
    monkeypatch.setattr(mo, "_kline_daily_latest_trade_date", lambda: holder["date"])
    return calls, clock, holder


def test_cache_ttl_60s_recomputes_on_expiry(monkeypatch):
    calls, clock, _ = _patch_cache_plumbing(monkeypatch, W2)
    mo.get_emotion_trend(days=2)
    assert calls["compute"] == 1
    clock.now = 1000 + 59
    mo.get_emotion_trend(days=2)
    assert calls["compute"] == 1, "TTL 内必须命中缓存"
    clock.now = 1000 + 61
    mo.get_emotion_trend(days=2)
    assert calls["compute"] == 2, "TTL=60s 到期后必须重算"


def test_cache_key_bound_to_max_trade_date(monkeypatch):
    calls, clock, holder = _patch_cache_plumbing(monkeypatch, W2)
    mo.get_emotion_trend(days=2)
    assert calls["compute"] == 1
    holder["date"] = D3  # 当天补数完成，max(trade_date) 推进
    mo.get_emotion_trend(days=2)
    assert calls["compute"] == 2, "数据日期推进后的首请求必须重算"
    assert f"2|daily|{D3}" in mo._EMOTION_TREND_RESULT_CACHE
    assert f"2|daily|{W2}" not in mo._EMOTION_TREND_RESULT_CACHE, "同参数旧数据日期键必须清掉"


def test_cache_key_query_failure_disables_cache(monkeypatch):
    calls, _, _ = _patch_cache_plumbing(monkeypatch, None)
    mo.get_emotion_trend(days=2)
    mo.get_emotion_trend(days=2)
    assert calls["compute"] == 2, "max(trade_date) 查不到（None）时不允许吃缓存"
    assert mo._EMOTION_TREND_RESULT_CACHE == {}


def test_error_payload_is_not_cached(monkeypatch):
    calls, _, _ = _patch_cache_plumbing(monkeypatch, W2)

    def _boom(days=30, granularity="daily"):
        calls["compute"] += 1
        return {"error": "boom", "dates": []}

    monkeypatch.setattr(mo, "_compute_emotion_trend", _boom)
    mo.get_emotion_trend(days=2)
    mo.get_emotion_trend(days=2)
    assert calls["compute"] == 2
    assert mo._EMOTION_TREND_RESULT_CACHE == {}


# ── 4) R25 结构守卫：AST 级 ────────────────────────────────────────────────
def _chain_function_nodes():
    source = Path(mo.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    by_name = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in EMOTION_CHAIN_FUNCTIONS:
        assert name in by_name, f"market_overview.py 中找不到函数: {name}"
        node = by_name[name]
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:]  # 剥离 docstring
        yield name, node


def test_structure_guard_no_views_no_emotion_cycle_no_session():
    """emotion 链不得引用 FINAL 视图、emotion_cycle 表读取、ORM session。"""
    for name, node in _chain_function_nodes():
        code = ast.unparse(node)
        for token in ("quality_read", "precise", "emotion_cycle", "emotion_cycles"):
            assert token not in code, f"{name} 引用了禁用 token: {token}"
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id == "session":
                raise AssertionError(f"{name} 绑定了 ORM session 变量（R25 禁令）")
            if isinstance(sub, ast.arg) and sub.arg == "session":
                raise AssertionError(f"{name} 声明了 session 参数（R25 禁令）")
            if isinstance(sub, ast.Attribute) and sub.attr == "get_session":
                raise AssertionError(f"{name} 调用了 get_session（R25 禁令）")


def test_structure_guard_every_warehouse_call_is_raw():
    """emotion 链上每个 clickhouse_query_df/clickhouse_scalar 调用必须 raw=True。"""
    source = Path(mo.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = set(EMOTION_CHAIN_FUNCTIONS)
    checked = 0
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, (ast.Attribute, ast.Name))
                    and getattr(sub.func, "attr", getattr(sub.func, "id", None))
                    in ("clickhouse_query_df", "clickhouse_scalar")
                ):
                    raws = [kw.value for kw in sub.keywords if kw.arg == "raw"]
                    assert (
                        raws
                        and isinstance(raws[0], ast.Constant)
                        and raws[0].value is True
                    ), f"{node.name} 中 {getattr(sub.func, 'attr', sub.func)}(...) 必须显式 raw=True"
                    checked += 1
    assert checked >= 5, f"结构守卫应覆盖 emotion 链全部查询调用，实际覆盖 {checked} 处"


def test_orm_emotion_cycle_fallback_retired():
    """源码级：daily 分支不得再回退 ORM EmotionCycle 查询。"""
    source = Path(mo.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_compute_emotion_trend"
    )
    code = ast.unparse(node)
    assert "EmotionCycle" not in code
    assert "get_session" not in code
    assert "_append_intraday_emotion_snapshot" not in code, (
        "daily 分支已含最新完整数据日，不得再追加 kline_daily_intraday 盘中快照"
    )

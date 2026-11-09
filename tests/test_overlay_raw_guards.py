# -*- coding: utf-8 -*-
"""R23（2026-11-09）：两处同族遗留 raw 基表化守卫（先于实现编写，TDD）。

病灶（R20b 执行者真库实测留档 runtime/COORDINATION_R20B_SENTIMENT_RAW_20261109.md）：
1. api/stocks.py `_overlay_fresh_intraday_list_kline`（R14 版）：外层
   `max(as_of) AS as_of` 自引用别名被 CH 24.8 代入同层 argMax(close, as_of)
   → ILLEGAL_AGGREGATION，又被 except 吞成无堆栈 warning——个股列表盘中
   覆盖层自 R14 起一直静默失效；其 toDate(datetime)=? 谓词对 toYYYYMM 分区
   不裁剪（实测 894M 行/4.2GiB 全表扫）。
2. services/market_turnover_forecast.py 两查询经视图网关重写为无界视图
   （current_rows 冷启 55GiB、60 日 history 27.5GiB，126~223s）。

守卫分三层（镜像 test_market_sentiment_recompute.py 的 R20b 三件套）：
- AST 结构守卫：目标函数/模块不含视图 token（quality_read/precise/裸
  FINAL），链上每个网关调用显式 raw=True，toDate 基表谓词与自引用别名
  禁用；
- 运行时守卫：fake 数据层断言 SQL 形状——基表 kline_minute_5 + datetime
  范围谓词 + (code, datetime) 级 argMax(col, created_at) 去重 + 外层别名
  避开 as_of（同层别名代入聚合即 ILLEGAL_AGGREGATION 病灶）；
- fail-closed 守卫：覆盖层聚合异常必须 ERROR 级带堆栈留痕并返回不可用态
  （基表日频数据、无 is_provisional 标记），禁止降级为 warning 静默吞掉。

真库性能/口径验证由部署后独立核查完成（任务书第 3 条），不在本文件范围。
"""
import ast
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import api.stocks as stk
import services.market_turnover_forecast as mtf

# 2026-11-09 是周一；盘中时钟取 10:00（落在 9:30-15:35 会话窗内）。
# R27b：分钟表 datetime 已标注 Asia/Shanghai，存储值即北京业务时间。
CLOCK = datetime(2026, 11, 9, 10, 0, 0)
STORED_AS_OF = datetime(2026, 11, 9, 10, 0, 0)  # R27b：datetime 已北京钟，存储值即业务时间


def _function_node(module, name):
    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{Path(module.__file__).name} 中找不到函数: {name}")


def _module_code(module):
    return ast.unparse(ast.parse(Path(module.__file__).read_text(encoding="utf-8")))


def _assert_call_raw_true(call):
    raws = [kw.value for kw in call.keywords if kw.arg == "raw"]
    assert (
        raws and isinstance(raws[0], ast.Constant) and raws[0].value is True
    ), "网关调用必须显式 raw=True（基表直查、绕过视图重写）"


# ── 1) AST 结构守卫：api/stocks.py 覆盖层 ─────────────────────────────────


def test_structure_guard_overlay_view_free_raw_range_predicate():
    """覆盖层函数：无视图 token、无 toDate 基表谓词、无自引用 as_of 别名、
    唯一网关调用 raw=True。"""
    node = _function_node(stk, "_overlay_fresh_intraday_list_kline")
    code = ast.unparse(node)
    for token in ("quality_read", "precise"):
        assert token not in code, f"覆盖层引用了视图 token: {token}"
    assert not re.search(r"\bFINAL\b", code), "覆盖层不得使用裸 FINAL（改为 argMax 去重）"
    assert "toDate(" not in code, "toDate 基表谓词在 toYYYYMM 分区上不裁剪，禁用"
    assert "max(as_of) AS as_of" not in code, (
        "同层别名 as_of 被代入 argMax(close, as_of) 即 ILLEGAL_AGGREGATION（R14 病灶）"
    )
    calls = [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, (ast.Attribute, ast.Name))
        and getattr(sub.func, "attr", getattr(sub.func, "id", None))
        in ("clickhouse_query_df", "clickhouse_scalar")
    ]
    assert calls, "覆盖层必须经网关发出聚合查询"
    for call in calls:
        _assert_call_raw_true(call)


# ── 2) 运行时守卫：覆盖层 SQL 形状 + 盘中覆盖语义 ─────────────────────────


class _TradingClock:
    @classmethod
    def now(cls, tz=None):
        return CLOCK


def test_overlay_runtime_sql_shape_and_provisional_entry(monkeypatch):
    captured = []

    def _df(sql, params=None, *, raw=False):
        captured.append((sql, params, raw))
        return pd.DataFrame([
            {"code": "600000.SH", "close": 12.5, "amount": 1.2e8, "latest_as_of": STORED_AS_OF},
            # 业务时间停在昨天：过期 feed 不得当作实时报价（R27b 北京墙钟）
            {"code": "000001.SZ", "close": 9.9, "amount": 2.2e8, "latest_as_of": datetime(2026, 11, 8, 10, 0)},
        ])

    monkeypatch.setattr(stk, "datetime", _TradingClock)
    monkeypatch.setattr(stk, "clickhouse_available", lambda: True)
    monkeypatch.setattr(stk, "clickhouse_table_exists", lambda name: True)
    monkeypatch.setattr(stk, "clickhouse_query_df", _df)

    stocks = [SimpleNamespace(code="600000.SH"), SimpleNamespace(code="000001.SZ")]
    base_map = {
        "600000.SH": SimpleNamespace(trade_date=date(2026, 11, 6), close=12.0, change_pct=-0.5, amount=9e8),
        "000001.SZ": SimpleNamespace(trade_date=date(2026, 11, 6), close=9.5, change_pct=0.2, amount=8e8),
    }
    result = stk._overlay_fresh_intraday_list_kline(stocks, base_map)

    sql, params, raw = captured[0]
    assert raw is True, "覆盖层查询必须 raw=True 基表直查"
    for token in (
        "FROM kline_minute_5",
        "argMax(close, created_at)",
        "argMax(amount, created_at)",
        "GROUP BY code, datetime",
        "datetime >= ? AND datetime < ? + INTERVAL 1 DAY",
        "max(as_of) AS latest_as_of",
        "code IN (?,?)",
    ):
        assert token in sql, f"覆盖层 SQL 丢失 R20b 镜像 token: {token}"
    assert "toDate(" not in sql and "quality" not in sql and "precise" not in sql
    assert not re.search(r"max\(\s*as_of\s*\)\s+AS\s+as_of", sql), "自引用别名复发（ILLEGAL_AGGREGATION 病灶）"
    assert params == ["600000.SH", "000001.SZ", date(2026, 11, 9), date(2026, 11, 9)]

    fresh = result["600000.SH"]
    assert fresh.is_provisional is True
    assert fresh.close == pytest.approx(12.5)
    assert fresh.change_pct == pytest.approx((12.5 / 12.0 - 1) * 100)
    assert fresh.as_of == CLOCK
    assert fresh.source == "clickhouse:kline_minute_5"
    # 过期 feed：保留基表日频数据，无 provisional 标记
    stale = result["000001.SZ"]
    assert not hasattr(stale, "is_provisional")
    assert stale.close == 9.5


# ── 3) fail-closed 守卫：异常 ERROR 级带堆栈 + 不可用态 ──────────────────


class _LogRecorder:
    def __init__(self):
        self.records = []

    def error(self, message, *args, **kwargs):
        self.records.append(("error", message, kwargs))

    def warning(self, message, *args, **kwargs):
        self.records.append(("warning", message, kwargs))


def test_overlay_fail_closed_on_aggregation_error(monkeypatch):
    def _df(sql, params=None, *, raw=False):
        raise RuntimeError("Code: 184. ILLEGAL_AGGREGATION: AGGREGATE function not allowed")

    recorder = _LogRecorder()
    monkeypatch.setattr(stk, "datetime", _TradingClock)
    monkeypatch.setattr(stk, "clickhouse_available", lambda: True)
    monkeypatch.setattr(stk, "clickhouse_table_exists", lambda name: True)
    monkeypatch.setattr(stk, "clickhouse_query_df", _df)
    monkeypatch.setattr(stk, "logger", recorder)

    stocks = [SimpleNamespace(code="600000.SH")]
    base_map = {"600000.SH": SimpleNamespace(trade_date=date(2026, 11, 6), close=12.0, change_pct=-0.5, amount=9e8)}
    result = stk._overlay_fresh_intraday_list_kline(stocks, base_map)

    # 不可用态 = 基表日频数据原样返回（无 provisional 伪装）
    assert result is base_map
    assert not hasattr(result["600000.SH"], "is_provisional")
    # 异常必须 ERROR 级、带堆栈、正文含异常信息——禁止 warning 静默吞掉
    errors = [r for r in recorder.records if r[0] == "error"]
    assert len(errors) == 1, f"聚合异常必须恰好记录 1 条 ERROR，实际: {recorder.records}"
    level, message, kwargs = errors[0]
    assert "ILLEGAL_AGGREGATION" in str(message)
    assert kwargs.get("exc_info") is True, "ERROR 必须带 exc_info 堆栈（R14 版静默失效的根因）"
    assert not [r for r in recorder.records if r[0] == "warning"], "禁止降级为 warning 吞异常"


# ── 3b) 第三处同族守卫：kline_daily 最新行查询的自引用别名 ───────────────


def test_structure_guard_no_self_alias_in_kline_daily_latest_queries():
    """R23 补修（真库现网日志实证）：api/stocks.py 中 kline_daily 最新行查询
    `max(trade_date) AS trade_date` 自引用别名被代入同层
    argMax(col, (trade_date, created_at)) → ILLEGAL_AGGREGATION（Code 184），
    with-limit 的 latest_kline 因此自 R14 起全部为空。别名必须避开聚合实参
    中出现的列名（latest_trade_date），模块内禁止该形状复发。"""
    source = Path(stk.__file__).read_text(encoding="utf-8")
    pattern = re.compile(r"max\(\s*trade_date\s*\)\s+AS\s+trade_date", re.IGNORECASE)
    assert not pattern.search(source), (
        "kline_daily 最新行查询复发 max(trade_date) AS trade_date 自引用别名"
        "（代入 argMax 实参即 ILLEGAL_AGGREGATION）"
    )
    # 修复后形状：argMax 元组去重保留，max 别名改 latest_trade_date
    assert source.count("max(trade_date) AS latest_trade_date") == 2, (
        "with-limit 两个分支（CH 表 stocks / ORM stocks 回退）的 kline_daily "
        "最新行查询都应使用 latest_trade_date 别名"
    )



# ── 4) AST 结构守卫：services/market_turnover_forecast.py 全模块 ──────────


def test_structure_guard_turnover_forecast_module_raw_and_view_free():
    """模块内每个网关调用 raw=True；无视图 token、无裸 FINAL、无 toDate 基表谓词。"""
    code = _module_code(mtf)
    for token in ("quality_read", "precise"):
        assert token not in code, f"成交额预测模块引用了视图 token: {token}"
    assert not re.search(r"\bFINAL\b", code), "成交额预测不得依赖裸 FINAL（改为 argMax 去重）"
    assert "toDate(k.datetime)" not in code, "toDate 基表谓词不裁剪，禁用（分组用 toDate(b.bar_time) 不受限）"

    tree = ast.parse(Path(mtf.__file__).read_text(encoding="utf-8"))
    checked = 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
            and getattr(node.func, "attr", getattr(node.func, "id", None))
            in ("clickhouse_query_df", "clickhouse_scalar")
        ):
            _assert_call_raw_true(node)
            checked += 1
    assert checked >= 2, f"结构守卫应覆盖两处查询调用，实际覆盖 {checked} 处"


# ── 5) 运行时守卫：两查询 SQL 形状 + 端到端可用载荷 ────────────────────────


def test_turnover_forecast_runtime_sql_shapes(monkeypatch):
    import scheduler.trading_calendar as trading_calendar

    captured = []

    def _make_history():
        rows = []
        for i in range(25):
            trade_day = date(2026, 10, 6) + timedelta(days=i)
            rows.append({
                "trade_date": trade_day,
                "code_count": 5000,
                "day_total": 1.0e12 + i * 1.0e10,
                "cumulative_at_time": 4.0e11 + i * 1.0e9,
                "completion_ratio": 0.4 + i * 0.01,
            })
        return pd.DataFrame(rows)

    def _df(sql, params=None, *, raw=False):
        captured.append((sql, params, raw))
        if len(captured) == 1:
            return pd.DataFrame([{
                # R27b：datetime 列已标注 Asia/Shanghai，驱动读回 aware 北京墙
                "as_of": datetime(2026, 11, 9, 11, 25, tzinfo=timezone(timedelta(hours=8))),
                "code_count": 5100,
                "total_amount": 6.0e11,
                "sh_amount": 3.0e11,
                "sz_amount": 2.8e11,
                "bj_amount": 2.0e10,
            }])
        return _make_history()

    monkeypatch.setattr(mtf, "clickhouse_available", lambda: True)
    monkeypatch.setattr(mtf, "clickhouse_table_exists", lambda name: True)
    monkeypatch.setattr(mtf, "clickhouse_query_df", _df)
    monkeypatch.setattr(
        trading_calendar.TradingCalendar, "is_trading_day",
        classmethod(lambda cls, value: True),
    )
    mtf._CACHE.update(payload=None, expires_at=0.0)

    payload = mtf.get_intraday_turnover_forecast(current_snapshot=None)

    assert len(captured) == 2, f"应恰好发出两条查询，实际 {len(captured)}"
    today = datetime.now(mtf.SH_TZ).date()

    current_sql, current_params, current_raw = captured[0]
    assert current_raw is True
    for token in (
        "FROM kline_minute_5 AS k",
        "INNER JOIN stocks AS s ON s.code = k.code",
        "s.type = 'stock'",
        "argMax(k.amount, k.created_at)",
        "GROUP BY k.code, k.datetime",
        "k.datetime >= ? AND k.datetime < ? + INTERVAL 1 DAY",
        "toHour(k.datetime) BETWEEN 9 AND 15",
    ):
        assert token in current_sql, f"current_rows 查询丢失 token: {token}"
    assert "toDate(k.datetime)" not in current_sql
    assert current_params == [today, today]

    history_sql, history_params, history_raw = captured[1]
    assert history_raw is True
    for token in (
        "FROM kline_minute_5 AS k",
        "INNER JOIN stocks AS s ON s.code = k.code",
        "s.type = 'stock'",
        "argMax(k.amount, k.created_at)",
        "GROUP BY k.code, k.datetime",
        "k.datetime >= ? AND k.datetime < ?",
        "toDate(b.bar_time) AS trade_date",
    ):
        assert token in history_sql, f"60 日 history 查询丢失 token: {token}"
    assert "toDate(k.datetime)" not in history_sql
    assert history_params[0] == "11:25:00"
    assert history_params[1] == today - timedelta(days=mtf.HISTORY_DAYS)
    assert history_params[2] == today

    assert payload["available"] is True
    assert payload["current_amounts"]["total"] == pytest.approx(6.0e11)
    assert payload["sample_days"] == 25

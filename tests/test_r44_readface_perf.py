"""R44 放行面性能修复守卫（35 条 GET 冷/热时延：>2s 归因修复，零语义变化）。

修复模式（按 R14/R15 已验证套路）：
  P1 gen3 `_attach_latest_market_prices`：quality_read 视图自 JOIN（query_log
     1.07GiB/40M 行/50~166s）→ raw=True 基表直查 + argMax(close,(trade_date,
     created_at)) 等价重写；输出别名 latest_trade_date 避开聚合实参列名（R23）。
  P2 trading `_resolve_latest_stock_trade_date` / `_previous_stock_trade_date`：
     ORM before_cursor_execute 同样被视图改写（82s/53s）→ market_warehouse
     raw=True 直查；max() 对重复版本不敏感。
  P3 trading 分钟表日期谓词：toDate(datetime)=（不裁剪 toYYYYMM 分区，R20b
     前科）→ 裸列 datetime 范围（[d, d+1)），行集等价（002015@11-10 16=16）。
  P4 `_load_current_runtime` mtime+15sTTL memo；`_freshness_file_memo` 对
     freshness 检查的重复 parquet/CSV 读记忆化（文件未变结果不变）。
  P5 7 个 gen3 慢 GET handler async def 零 await → 普通 def（同步体出事件
     循环，快端点不再被拖排队）。

本文件：AST 结构守卫（防回退）+ 行为守卫（语义不变 + 严格日期形护栏）。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_DIR = PROJECT_ROOT / "api"


def _ast_of(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _calls(fn: ast.AST, func_name: str) -> list[ast.Call]:
    return [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == func_name
    ]


GSA = _ast_of(API_DIR / "gen3_state_alpha.py")
TRD = _ast_of(API_DIR / "trading.py")


# ---------------------------------------------------------------- P1 最新价 raw+argMax

def test_p1_latest_market_prices_raw_argmax_shape():
    fn = _func(GSA, "_attach_latest_market_prices")
    calls = _calls(fn, "clickhouse_query_df")
    assert calls, "must call clickhouse_query_df"
    call = calls[0]
    assert any(
        k.arg == "raw" and isinstance(k.value, ast.Constant) and k.value.value is True
        for k in call.keywords
    )
    sql_node = call.args[0]
    assert isinstance(sql_node, ast.JoinedStr), "SQL is an f-string over quoted codes"
    parts = [
        seg.value if isinstance(seg, ast.Constant) else "{code_list}"
        for seg in sql_node.values
    ]
    sql = "".join(parts)
    assert "argMax(close, (trade_date, created_at))" in sql
    assert "INNER" not in sql and "JOIN" not in sql, "self-join through view was the 50s+ pathology"
    assert "WHERE code IN" in sql
    assert "today() - INTERVAL 120 DAY" in sql, "bounded window (R44b) required"
    # R23 alias discipline: no `max(X) AS X` output alias feeding aggregate args
    assert "max(trade_date) AS trade_date" not in sql


def test_p1_price_by_code_uses_latest_trade_date_alias():
    src = (API_DIR / "gen3_state_alpha.py").read_text(encoding="utf-8")
    assert '_date_text(item.get("latest_trade_date"))' in src


def test_p1_behavior_maps_fake_frame(monkeypatch):
    import api.gen3_state_alpha as gsa

    captured: dict = {}

    def fake_query_df(sql, params=None, *, raw=False):
        captured["sql"] = sql
        captured["raw"] = raw
        return pd.DataFrame(
            {
                "code": ["000001.SZ", "300054.SZ"],
                "close": [10.5, 61.33],
                "latest_trade_date": [pd.Timestamp("2026-11-10").date(), pd.Timestamp("2026-11-10").date()],
            }
        )

    import utils.market_warehouse as mw
    monkeypatch.setattr(mw, "clickhouse_query_df", fake_query_df, raising=False)
    # 函数内是局部 import，patch 模块属性即可
    monkeypatch.setattr(sys.modules["utils.market_warehouse"], "clickhouse_query_df", fake_query_df)
    rows = [
        {"code": "000001.SZ", "current_price": 0},
        {"code": "300054.SZ"},
    ]
    out = gsa._attach_latest_market_prices(rows)
    assert captured["raw"] is True
    assert "argMax(close, (trade_date, created_at))" in captured["sql"]
    assert out[0]["current_price"] == 10.5
    assert out[0]["latest_price_date"] == "2026-11-10"
    assert out[1]["latest_price"] == 61.33


def test_p1_behavior_fail_closed_without_ch(monkeypatch):
    import api.gen3_state_alpha as gsa

    def boom(sql, params=None, *, raw=False):
        raise RuntimeError("ch down")

    monkeypatch.setattr(sys.modules["utils.market_warehouse"], "clickhouse_query_df", boom)
    rows = [{"code": "000001.SZ"}]
    assert gsa._attach_latest_market_prices(rows) == rows


def test_p1_behavior_window_fallback_merges_missing_codes(monkeypatch):
    import api.gen3_state_alpha as gsa

    queries: list[str] = []

    def fake_query_df(sql, params=None, *, raw=False):
        queries.append(sql)
        if "INTERVAL 120 DAY" in sql:
            return pd.DataFrame(
                {"code": ["000001.SZ"], "close": [10.5], "latest_trade_date": [pd.Timestamp("2026-11-10").date()]}
            )
        return pd.DataFrame(
            {"code": ["301999.SZ"], "close": [33.3], "latest_trade_date": [pd.Timestamp("2025-01-02").date()]}
        )

    monkeypatch.setattr(sys.modules["utils.market_warehouse"], "clickhouse_query_df", fake_query_df)
    rows = [{"code": "000001.SZ"}, {"code": "301999.SZ"}]
    out = gsa._attach_latest_market_prices(rows)
    assert len(queries) == 2, "missing code must trigger the unbounded fallback query"
    assert "INTERVAL 120 DAY" not in queries[1]
    assert out[0]["latest_price"] == 10.5 and out[0]["latest_price_date"] == "2026-11-10"
    assert out[1]["latest_price"] == 33.3 and out[1]["latest_price_date"] == "2025-01-02"


# ---------------------------------------------------------------- P2 交易日解析 raw

@pytest.mark.parametrize("fn_name", ["_resolve_latest_stock_trade_date", "_previous_stock_trade_date"])
def test_p2_trade_date_resolvers_no_orm_view_path(fn_name):
    fn = _func(TRD, fn_name)
    body = ast.dump(fn)
    assert "Attribute(value=Name(id='db'), attr='engine')" not in body, "ORM path is rewritten by the quality view listener"
    calls = _calls(fn, "clickhouse_scalar")
    assert calls, "must use clickhouse_scalar"
    assert any(
        k.arg == "raw" and isinstance(k.value, ast.Constant) and k.value.value is True
        for c in calls for k in c.keywords
    ), "raw=True required"
    src = (API_DIR / "trading.py").read_text(encoding="utf-8")
    seg = ast.get_source_segment(src, fn)
    assert seg is not None and "max(k.trade_date)" in seg


def test_p2_resolvers_sql_shape():
    src = (API_DIR / "trading.py").read_text(encoding="utf-8")
    assert "SELECT max(k.trade_date)" in src
    assert "INNER JOIN stocks AS s ON s.code = k.code" in src
    # ORM 旧形状不得复活
    assert "SELECT MAX(k.trade_date) AS latest_date" not in src
    assert "SELECT MAX(k.trade_date) AS prev_date" not in src


def test_p2_previous_trade_date_invalid_input_returns_none(monkeypatch):
    import api.trading as tr

    def boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("no SQL should run for invalid date")

    monkeypatch.setattr(sys.modules["utils.market_warehouse"], "clickhouse_scalar", boom)
    assert tr._previous_stock_trade_date("2026-11-10'; DROP TABLE x--") is None
    assert tr._previous_stock_trade_date("not-a-date' OR '1'='1") is None


# ---------------------------------------------------------------- P3 toDate→范围谓词

def test_p3_no_toDate_minute_predicates_left():
    src = (API_DIR / "trading.py").read_text(encoding="utf-8")
    for banned in ("toDate(datetime) =", "toDate({date_col}) =", "toDate(datetime) = toDate"):
        assert banned not in src, f"partition-killing predicate still present: {banned}"


def test_p3_minute_status_range_predicate_shape():
    fn = _func(TRD, "_gen2_minute_table_status")
    src_seg = ast.get_source_segment(API_DIR.joinpath("trading.py").read_text(encoding="utf-8"), fn)
    assert src_seg is not None
    assert "INTERVAL 1 DAY" in src_seg
    assert "00:00:00" in src_seg


def test_p3_minute_status_invalid_date_never_queries(monkeypatch):
    import api.trading as tr
    from datetime import datetime

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("no SQL should run for invalid date")

    monkeypatch.setattr(tr, "clickhouse_query_df", boom)
    monkeypatch.setattr(tr, "clickhouse_available", lambda: True)
    monkeypatch.setattr(tr, "clickhouse_table_exists", lambda t: True)
    monkeypatch.setattr(tr, "_latest_required_bar_time", lambda now, period: "10:00")
    result = tr._gen2_minute_table_status("2026-11-10' OR '1'='1", 15, datetime.now())
    assert result["ok"] is False
    assert "invalid signal_date" in result["reason"]


def test_p3_coverage_status_invalid_date_never_queries(monkeypatch):
    import api.trading as tr

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("no SQL should run for invalid date")

    monkeypatch.setattr(tr, "clickhouse_query_df", boom)
    result = tr._daily_stock_coverage_status("2026-11-10; DROP", "D-day daily source")
    assert result["ok"] is False
    assert "invalid trade_date" in (result.get("message") or result.get("reason") or "")


def test_p3_coverage_raw_dedup_shape(monkeypatch):
    import api.trading as tr

    captured: dict = {}

    def fake_query_df(sql, params=None, *, raw=False):
        captured["sql"] = sql
        captured["raw"] = raw
        return pd.DataFrame({"row_count": [5583], "code_count": [5583]})

    monkeypatch.setattr(tr, "clickhouse_query_df", fake_query_df)
    out = tr._daily_stock_coverage_status("2026-11-10", "D-day daily source")
    assert captured["raw"] is True
    assert "GROUP BY code" in captured["sql"]
    assert "trade_date = '2026-11-10'" in captured["sql"]
    assert out["ok"] is True
    assert out["code_count"] == 5583


# ---------------------------------------------------------------- P4 memo

def test_p4_runtime_memo_same_mtime_reuses(monkeypatch, tmp_path):
    import api.gen3_state_alpha as gsa

    alpha = tmp_path / "gen3_state_alpha"
    router = tmp_path / "gen3_state_router_shadow"
    alpha.mkdir()
    router.mkdir()
    files = [
        alpha / "latest_summary.json",
        alpha / "latest_shadow_tickets.csv",
        alpha / "afterhours_latest_summary.json",
        alpha / "afterhours_latest_shadow_tickets.csv",
        alpha / "shadow_ledger.csv",
        router / "latest_summary.json",
        router / "latest_route_diagnostics.csv",
    ]
    for f in files:
        f.write_text("{}\n" if f.suffix == ".json" else "code\n", encoding="utf-8")

    monkeypatch.setattr(gsa, "STATE_ALPHA_RUNTIME_DIR", alpha)
    monkeypatch.setattr(gsa, "STATE_ROUTER_RUNTIME_DIR", router)
    monkeypatch.setattr(gsa, "_read_json", lambda p: {"generated_at": "2026-11-10 15:00:00"})
    monkeypatch.setattr(gsa, "_read_csv_records", lambda p, limit=None: [])
    monkeypatch.setattr(gsa, "_read_shadow_ledger_records", lambda p, limit=None: [{"code": "300054.SZ"}])
    monkeypatch.setattr(gsa, "_enrich_trade_strategy_records", lambda rows: list(rows or []))
    monkeypatch.setattr(gsa, "_broker_snapshot", lambda: {})

    counter = {"n": 0}

    def counting_attach(rows, *a, **k):
        counter["n"] += 1
        return list(rows or [])

    monkeypatch.setattr(gsa, "_attach_latest_market_prices", counting_attach)
    gsa._CURRENT_RUNTIME_MEMO.clear()
    gsa._load_current_runtime(50)
    assert counter["n"] == 1
    gsa._load_current_runtime(50)
    assert counter["n"] == 1, "second call within TTL must hit the memo (no recompute)"
    # 文件 mtime 变化 → 键失效 → 重算
    (files[0]).write_text('{"generated_at": "2026-11-10 16:00:00"}\n', encoding="utf-8")
    gsa._load_current_runtime(50)
    assert counter["n"] == 2, "changed artifact mtime must invalidate the memo"


def test_p4_runtime_memo_clears():
    import api.gen3_state_alpha as gsa

    gsa._CURRENT_RUNTIME_MEMO.clear()
    assert gsa._CURRENT_RUNTIME_MEMO == {}


def test_p4_runtime_memo_survives_missing_artifacts(monkeypatch, tmp_path):
    """R44b 回归：preview 栈只挂 gen3_state_alpha 子树、router 文件缺件时，
    memo 键必须用 None 占位而不是整体失效（部署容器实测 size=0 的病灶）。"""
    import api.gen3_state_alpha as gsa

    alpha = tmp_path / "gen3_state_alpha"
    alpha.mkdir()
    router = tmp_path / "gen3_state_router_shadow"  # 目录整体不创建：两文件缺件
    for name in ("latest_summary.json", "latest_shadow_tickets.csv", "shadow_ledger.csv"):
        (alpha / name).write_text("{}\n" if name.endswith(".json") else "code\n", encoding="utf-8")

    monkeypatch.setattr(gsa, "STATE_ALPHA_RUNTIME_DIR", alpha)
    monkeypatch.setattr(gsa, "STATE_ROUTER_RUNTIME_DIR", router)
    monkeypatch.setattr(gsa, "_read_json", lambda p: {"generated_at": "2026-11-10 15:00:00"})
    monkeypatch.setattr(gsa, "_read_csv_records", lambda p, limit=None: [])
    monkeypatch.setattr(gsa, "_read_shadow_ledger_records", lambda p, limit=None: [])
    monkeypatch.setattr(gsa, "_enrich_trade_strategy_records", lambda rows: list(rows or []))
    monkeypatch.setattr(gsa, "_broker_snapshot", lambda: {})
    monkeypatch.setattr(gsa, "_attach_latest_market_prices", lambda rows, *a, **k: list(rows or []))

    gsa._CURRENT_RUNTIME_MEMO.clear()
    gsa._load_current_runtime(50)
    assert len(gsa._CURRENT_RUNTIME_MEMO) == 1, "missing router artifacts must not disable the memo"


def test_p4_freshness_file_memo():
    import api.trading as tr

    tr._FRESHNESS_FILE_CACHE.clear()
    calls = {"n": 0}

    def compute():
        calls["n"] += 1
        return "value"

    a = tr._freshness_file_memo("k", 1.0, compute)
    b = tr._freshness_file_memo("k", 1.0, compute)
    c = tr._freshness_file_memo("k", 2.0, compute)
    assert a == b == c == "value"
    assert calls["n"] == 2, "same (key, mtime) must not recompute; changed mtime must"


# ---------------------------------------------------------------- P5 handler 出事件循环

SYNC_GET_HANDLERS = [
    ("gen3_state_alpha", "get_gen3_state_alpha_current"),
    ("gen3_state_alpha", "get_gen3_state_alpha_historical_trades"),
    ("gen3_state_alpha", "get_gen3_state_alpha_replacement_assessment"),
    ("gen3_state_alpha", "get_gen3_state_alpha_workflow_status"),
    ("gen3_state_alpha", "get_gen3_state_alpha_shadow_exit_monitor_status"),
    ("gen3_state_alpha", "get_gen3_state_alpha_broker_post_sync_acceptance"),
    ("gen3_state_alpha", "get_gen3_state_alpha_live_action_console"),
]


@pytest.mark.parametrize("module_name,handler_name", SYNC_GET_HANDLERS)
def test_p5_slow_get_handlers_are_sync(module_name, handler_name):
    tree = _ast_of(API_DIR / f"{module_name}.py")
    fn = _func(tree, handler_name)
    assert not isinstance(fn, ast.AsyncFunctionDef), (
        f"{handler_name} must be plain def: async def with a sync body blocks the "
        "event loop and drags every fast endpoint (R44 baseline: 504 collateral)"
    )
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Await)], "no await expected in body"

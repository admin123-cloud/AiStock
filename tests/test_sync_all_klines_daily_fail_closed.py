# -*- coding: utf-8 -*-
"""切片 2A 收容测试（test-only）——legacy 日线外部兜底必须 fail closed。

任务书：F:/Aistock/data/data/runtime/task_briefs/20261105_qmt_slice2a_daily_fallback_test.md
PM 复审意见（v1 拒收，本文件为 v2 修订）：
F:/Aistock/data/data/runtime/qmt_source_replacement_20261105/PM_REVIEW_SLICE2A_TEST_V1.md

固定路径只读 AST 断言 scripts/sync_all_klines.py 中
KlineSyncer.sync_kline_for_stock 在 period="1d" 且 manager 取不到数据时必须
fail closed：
1) 方法体内不得出现对 _fetch_external_daily 的调用（AkShare/Tushare 未获批源）；
2) 方法主流程必须存在实际的 top-level 空-DataFrame 守卫
   （`df is None or df.empty` 或等价显式条件），其 body 直接 return False，
   且该守卫（AST 节点定位，非词法行序）先于任何 _save_kline_to_db 调用——
   “无数据先返回 False，再谈写库”。不以方法内最早出现的无关 return False
   词法行序作近似（PM 对 v1 的拒收项，v2 已删除该近似）；
3) 方法内任何 except 处理器都不得调用 _fetch_external_daily 或
   _save_kline_to_db（异常路径不得兜底取数、不得写库）。

不阻断获批 manager 源路径：market_data_source.get_stock_history 等常规调用
不在检查范围；__init__ 的兜底配置键（external_fallback_*）不在本切片范围；
daily_kline_pipeline.py 不在检查范围。本测试是纯静态结构检查（AST），
不是运行期行为证明。

边界声明：
- 模块级仅导入标准库（ast、pathlib）；不导入任何项目模块、配置、PyYAML，
  也不导入本切片被改过的 data_fetcher.manager。
- 路径按任务书词法构造：Path(__file__).parents[1] / "scripts" /
  "sync_all_klines.py"，不调用任何路径解析 API——导入期不做真实路径
  解析，交由 PM 的 no-follow 预检。
- 运行期唯一文件打开：只读读取 scripts/sync_all_klines.py 本体（utf-8）。
  零写入、零网络、零子进程、零环境变量访问、零 sys.modules 污染。
- 断言消息只含标识符名与行号，不打印文件内容。

RED 依据（现状源证据，2026-11-05）：scripts/sync_all_klines.py 第 907-920 行，
manager 取数失败后 `if df is None or df.empty:` + `if period == "1d":` 分支在
第 909 行调用 self._fetch_external_daily(...)——违反断言 1，本测试当前必红。
断言 2/3 在现状源上应为绿：第 922 行 top-level 守卫 body 第 927 行直接
return False，先于第 947 行 _save_kline_to_db；except 处理器内无取数/写库
调用。断言 2/3 用于约束 GREEN 实现不得移除该守卫、不得把外部兜底挪进
异常路径。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parents[1]
SYNC_ALL_KLINES_PY = REPO_ROOT / "scripts" / "sync_all_klines.py"

_FORBIDDEN_FETCH = "_fetch_external_daily"
_WATCHED_WRITE = "_save_kline_to_db"


def _find_kline_sync_method(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Return the KlineSyncer.sync_kline_for_stock FunctionDef (fail if absent)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "KlineSyncer":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "sync_kline_for_stock":
                    return item
    raise AssertionError(f"KlineSyncer.sync_kline_for_stock not found in {SYNC_ALL_KLINES_PY}")


def _call_lines(scope: ast.AST, callee_name: str) -> list:
    """Line numbers of `x.callee_name(...)` / `callee_name(...)` calls inside scope."""
    lines = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == callee_name) or (
                isinstance(func, ast.Name) and func.id == callee_name
            ):
                lines.append(node.lineno)
    return sorted(lines)


def _iter_main_flow_stmts(stmts):
    """Yield statements at the method's main-flow (top-level) block level.

    只下钻保持主流程的包装语句：Try 的 body/orelse/finalbody、
    (Async)With 的 body、If 的 orelse（elif 链）；不下钻条件分支体
    （If.body）、循环体（For/While）、嵌套函数体——只有主流程层的守卫
    才算本测试要求的 top-level 空-DataFrame 守卫。
    """
    for stmt in stmts:
        yield stmt
        if isinstance(stmt, ast.Try):
            yield from _iter_main_flow_stmts(stmt.body)
            yield from _iter_main_flow_stmts(stmt.orelse)
            yield from _iter_main_flow_stmts(stmt.finalbody)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            yield from _iter_main_flow_stmts(stmt.body)
        elif isinstance(stmt, ast.If):
            yield from _iter_main_flow_stmts(stmt.orelse)


def _empty_guard_var(test: ast.expr) -> str | None:
    """DataFrame 变量名——当 test 是显式空数据条件时返回变量名，否则 None。

    认可形式：`df is None or df.empty`（析取项顺序可换）或等价
    显式条件（如 `df is None or len(df) == 0`）：要求同一变量同时出现
    `<var> is None` 检查与正向 `<var>.empty` / `len(<var>) == 0` 检查。
    否定形式一律不接受：`not <var>.empty` 是逻辑反转的错误条件，
    不得作为空数据守卫（PM v2 复审拒收项，已删除对其的接受）。
    """
    parts = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) else [test]
    none_vars = []
    empty_vars = []
    for part in parts:
        # `<var> is None`
        if (
            isinstance(part, ast.Compare)
            and len(part.ops) == 1
            and isinstance(part.ops[0], ast.Is)
            and isinstance(part.left, ast.Name)
            and len(part.comparators) == 1
            and isinstance(part.comparators[0], ast.Constant)
            and part.comparators[0].value is None
        ):
            none_vars.append(part.left.id)
            continue
        # `<var>.empty`（必须正向；否定形式 `not <var>.empty` 是逻辑反转的
        # 错误条件，不作为空数据守卫接受——PM v2 复审拒收项，已删除剥离）
        if isinstance(part, ast.Attribute) and part.attr == "empty" and isinstance(part.value, ast.Name):
            empty_vars.append(part.value.id)
            continue
        # `len(<var>) == 0`
        if (
            isinstance(part, ast.Compare)
            and len(part.ops) == 1
            and isinstance(part.ops[0], ast.Eq)
            and isinstance(part.left, ast.Call)
            and isinstance(part.left.func, ast.Name)
            and part.left.func.id == "len"
            and len(part.left.args) == 1
            and isinstance(part.left.args[0], ast.Name)
            and len(part.comparators) == 1
            and isinstance(part.comparators[0], ast.Constant)
            and part.comparators[0].value == 0
        ):
            empty_vars.append(part.left.args[0].id)
    common = sorted(set(none_vars) & set(empty_vars))
    return common[0] if common else None


def _direct_false_return(guard: ast.If):
    """guard.body 中直接出现的字面量 `return False` 语句（无则 None）。"""
    for item in guard.body:
        if (
            isinstance(item, ast.Return)
            and isinstance(item.value, ast.Constant)
            and item.value.value is False
        ):
            return item
    return None


def test_sync_kline_for_stock_daily_fallback_fail_closed() -> None:
    source = SYNC_ALL_KLINES_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SYNC_ALL_KLINES_PY))
    method = _find_kline_sync_method(tree)

    # 1) 收容核心：方法内不得调用未获批外部日线兜底（AkShare/Tushare）
    external_lines = _call_lines(method, _FORBIDDEN_FETCH)
    assert not external_lines, (
        "KlineSyncer.sync_kline_for_stock 在无数据路径上调用了未获批外部日线兜底 "
        f"_fetch_external_daily（行 {external_lines}）；period='1d' 必须 fail closed："
        "不得经外部兜底数据到达 _save_kline_to_db，应直接返回 False"
    )

    # 2) fail-closed 结构：主流程 top-level 空-DataFrame 守卫的 body 直接
    #    return False，且守卫先于任何写库调用（AST 定位实际守卫节点；
    #    不用全局 return False 词法行序近似）
    guards = [
        stmt
        for stmt in _iter_main_flow_stmts(method.body)
        if isinstance(stmt, ast.If) and _empty_guard_var(stmt.test) is not None
    ]
    assert guards, (
        "KlineSyncer.sync_kline_for_stock 主流程缺少 top-level 空-DataFrame 守卫"
        "（`df is None or df.empty` 或等价显式条件）"
    )
    write_lines = _call_lines(method, _WATCHED_WRITE)
    guard_hits = [
        (guard.lineno, direct.lineno)
        for guard in guards
        if (direct := _direct_false_return(guard)) is not None
        and (not write_lines or (guard.lineno < min(write_lines) and direct.lineno < min(write_lines)))
    ]
    assert guard_hits, (
        "KlineSyncer.sync_kline_for_stock 的 top-level 空-DataFrame 守卫必须"
        " body 直接 return False，且先于任何 _save_kline_to_db 调用"
        f"（守卫行 {[g.lineno for g in guards]}，写库行 {write_lines}）"
    )

    # 3) 异常路径：任何 except 处理器内不得兜底取数、不得写库
    for handler in (n for n in ast.walk(method) if isinstance(n, ast.ExceptHandler)):
        bad_fetch = _call_lines(handler, _FORBIDDEN_FETCH)
        bad_write = _call_lines(handler, _WATCHED_WRITE)
        assert not (bad_fetch or bad_write), (
            "sync_kline_for_stock 的 except 处理器内不得调用 "
            f"_fetch_external_daily/_save_kline_to_db（fetch 行 {bad_fetch}，"
            f"write 行 {bad_write}）；异常路径必须 fail closed"
        )

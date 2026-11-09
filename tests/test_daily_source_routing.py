# -*- coding: utf-8 -*-
"""切片 1 首个 TDD 周期（V3）——manager 不再注册 QMT 源（唯一测试）。

按 PM_REVIEW_SLICE1_V2.md「Required V3 resubmission」收敛为一个行为、一个测试：
固定路径只读 AST 断言 `data_fetcher/manager.py` 不再含任何已退役 QMT 源的
注册面（SOURCE_CLASSES 注册键、get_source 别名归一、preferred_source
默认值、QmtMini* 类 import/引用、qmtmini/xtquant 模块导入）。

边界声明：
- 模块级仅导入标准库（ast、pathlib）；不导入 PyYAML、不导入任何项目模块，
  也不导入尚未评审的 utils.market_sources.daily_source_routing。
- 运行期唯一文件打开：只读读取 manager.py 本体（utf-8）。
  零写入、零网络、零子进程、零环境变量写入、零 atexit、零 sys.modules 污染。
- 断言消息只含命中的字面量/标识符名，不打印文件内容。

RED 依据（现状，2026-11-05 盘点）：manager.py 仍注册 qmt_xtquant ——
SOURCE_CLASSES = {"qmt_xtquant": QmtMiniDataSource}、get_source 别名
{"qmt", "qmtmini", "xtquant"} → "qmt_xtquant"、get_all_stocks/get_all_indices
的 preferred_source 默认 "qmt_xtquant"。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGER_PY = REPO_ROOT / "data_fetcher" / "manager.py"

# 已退役 QMT 源名（strip+lower 后精确匹配，避免误伤文档串/相邻标识符）
RETIRED_QMT_SOURCE_NAMES = frozenset({"qmt", "qmtmini", "qmt_xtquant", "xtquant"})
# QMT 客户端/数据源类标识前缀（import 名、Name/Attribute 引用）
_QMT_CLASS_PREFIX = "QmtMini"
# 禁止出现的模块导入成分（含 import a.b.qmtmini 与 from x.qmtmini import Y）
_QMT_MODULE_COMPONENTS = ("qmtmini", "xtquant")


def test_manager_no_longer_registers_qmt_source() -> None:
    source = MANAGER_PY.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(MANAGER_PY))

    # 1) 源名注册面：字典键/别名集合与归一目标/默认参数值中的精确字符串字面量
    name_offenders = sorted(
        {
            node.value.strip().lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.strip().lower() in RETIRED_QMT_SOURCE_NAMES
        }
    )
    assert not name_offenders, (
        "manager.py 仍含已退役 QMT 源名字面量（SOURCE_CLASSES 注册键/"
        "get_source 别名归一/preferred_source 默认值须全部移除）："
        f"{name_offenders}"
    )

    # 2) 类/模块引用面：QmtMini* import、Name/Attribute，qmtmini/xtquant 模块导入
    ref_offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(part in _QMT_MODULE_COMPONENTS for part in module.split(".")):
                ref_offenders.append(f"from {module} import ...")
            for alias in node.names:
                if alias.name.startswith(_QMT_CLASS_PREFIX):
                    ref_offenders.append(f"from {module} import {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    part in _QMT_MODULE_COMPONENTS
                    for part in alias.name.split(".")
                ):
                    ref_offenders.append(f"import {alias.name}")
        elif isinstance(node, ast.Name) and node.id.startswith(_QMT_CLASS_PREFIX):
            ref_offenders.append(f"Name {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith(
            _QMT_CLASS_PREFIX
        ):
            ref_offenders.append(f"Attribute {node.attr}")
    assert not ref_offenders, (
        "manager.py 仍引用 QmtMini* 类或 qmtmini/xtquant 模块"
        "（import 与注册值须移除，不得构造 QMT 客户端）："
        f"{sorted(set(ref_offenders))}"
    )

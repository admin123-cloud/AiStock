# -*- coding: utf-8 -*-
"""[DEPRECATED 2026-11-14] 本脚本已收拢进统一入口 scripts/ths_client.py。

全部实现（R50 Phase1 L2 面板 UIA 零风险勘察：源码自审计/UIA 只读枚举/提交类按钮清单/身份 fail-closed）已迁至 ths_client.py 的 survey 段；本文件保留为薄 wrapper，
CLI 参数原样转发到 `ths_client.py survey`，外部引用（本脚本原为手动勘察入口）零改动。
请勿在本文件新增逻辑；模块级 import 兼容：内部代码/测试请改为
`import ths_client`。勘察代码位于 ths_client 文件末尾（_self_audit 自标记处扫描至 EOF）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import ths_client  # noqa: E402  统一入口（实现所在）


def main(argv: Optional[list[str]] = None) -> int:
    forwarded = list(sys.argv[1:] if argv is None else argv)
    return ths_client.main(["survey", *forwarded])


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())

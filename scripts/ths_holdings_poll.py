# -*- coding: utf-8 -*-
"""[DEPRECATED 2026-11-14] 本脚本已收拢进统一入口 scripts/ths_client.py。

全部实现（--once 单轮巡读 / --probe-capture / --replay-morning）已迁至 ths_client.py 的 poll-holdings 段；本文件保留为薄 wrapper，
CLI 参数原样转发到 `ths_client.py poll-holdings`，外部引用——schtask AiStock_THSHoldingsPoll（现禁用中）重新启用时无需改任务定义零改动。
请勿在本文件新增逻辑；模块级 import 兼容：内部代码/测试请改为
`import ths_client`。零交互铁律（PrintWindow+UIA 只读，连查询菜单都不点）在 ths_client 中不变。
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
    return ths_client.main(["poll-holdings", *forwarded])


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())

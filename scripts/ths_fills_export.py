# -*- coding: utf-8 -*-
"""[DEPRECATED 2026-11-14] 本脚本已收拢进统一入口 scripts/ths_client.py。

全部实现（多账号当日成交导出：R51 身份门禁多锚/热切换/unavailable 标记/幂等）已迁至 ths_client.py 的 export-fills 段；本文件保留为薄 wrapper，
CLI 参数原样转发到 `ths_client.py export-fills`，外部引用——hermes cron ths_fills_daily.py（15:05）与 PM 手动重跑继续经本文件工作零改动。
请勿在本文件新增逻辑；模块级 import 兼容：内部代码/测试请改为
`import ths_client`（gate_identity_decision 等函数现在都在 ths_client）。
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
    return ths_client.main(["export-fills", *forwarded])


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())

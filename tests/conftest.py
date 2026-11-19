"""Pytest 全局配置：兼容历史测试通过裸模块名导入已迁入 pipelines/jobs、host_bridges、services/engines 的模块。"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _rel in ("", "pipelines/jobs", "host_bridges", "services/engines"):
    _p = str(_ROOT / _rel) if _rel else str(_ROOT)
    if _p not in sys.path:
        sys.path.insert(0, _p)

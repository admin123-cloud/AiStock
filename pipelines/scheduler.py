#!/usr/bin/env python3
"""aistock-pipeline-runner 容器标准入口。"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = str(Path(__file__).resolve().parent)
ROOT = str(Path(__file__).resolve().parents[1])
# 防止 python pipelines/scheduler.py 时 sys.path[0]=/app/pipelines 遮蔽顶层 scheduler 包
sys.path[:] = [p for p in sys.path if p != SCRIPT_DIR]
if ROOT in sys.path:
    sys.path.remove(ROOT)
sys.path.insert(0, ROOT)

from scripts.pipeline_scheduler import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

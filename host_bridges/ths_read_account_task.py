#!/usr/bin/env python3
"""宿主机计划任务 AiStock_THSReadAccount / ReadMultiAccount 标准入口。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ths_read_account_task import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

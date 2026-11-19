#!/usr/bin/env python3
"""aistock-postclose-watcher 容器标准入口。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.postclose_deadline_watch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

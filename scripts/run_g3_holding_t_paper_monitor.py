"""CLI entrypoint for the paper-only G3 holding T monitor."""

from __future__ import annotations

import json
import sys
from pathlib import Path


# Scheduled Tasks start from a system directory, not the repository root.
# Make the standalone monitor importable without relying on a working folder.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.g3_holding_t_paper_monitor import run_once


if __name__ == "__main__":
    result = run_once()
    print(json.dumps({
        "ok": result.get("ok"),
        "reason": result.get("reason"),
        "evaluated_count": result.get("evaluated_count", 0),
        "order_path_enabled": result.get("order_path_enabled"),
    }, ensure_ascii=False))

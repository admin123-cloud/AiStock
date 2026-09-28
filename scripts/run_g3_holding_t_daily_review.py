"""Scheduled 16:00 entrypoint for the paper-only G3 holding-T review."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.g3_holding_t_daily_review import run_daily_review


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Trading date in YYYY-MM-DD; defaults to today")
    args = parser.parse_args()
    review = run_daily_review(args.date)
    print(json.dumps({
        "ok": True,
        "trade_date": review.get("trade_date"),
        "stock_count": review.get("stock_count"),
        "snapshot_count": review.get("snapshot_count"),
        "order_path_enabled": review.get("order_path_enabled"),
    }, ensure_ascii=False))

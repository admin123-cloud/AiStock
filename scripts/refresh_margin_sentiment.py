"""Refresh confirmed SSE/SZSE margin-financing sentiment facts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.margin_sentiment import refresh_margin_sentiment


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh homepage margin sentiment from official exchange summaries.")
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()
    result = refresh_margin_sentiment(days=args.days)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("written", 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())

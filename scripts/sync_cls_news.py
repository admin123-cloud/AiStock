from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.cls_news_service import fetch_store_and_score_cls_news, query_cls_signals


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Sync CLS telegraph news and build event signals.")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--rn", type=int, default=50)
    parser.add_argument("--show", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=0.0)
    args = parser.parse_args(argv)

    result = fetch_store_and_score_cls_news(pages=args.pages, rn=args.rn)
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    if args.show > 0:
        rows = query_cls_signals(limit=args.show, min_score=args.min_score)
        for row in rows:
            print(
                f"{row.get('event_time')} {row.get('signal_class')} "
                f"score={row.get('opportunity_score')} title={row.get('title') or row.get('content', '')[:40]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

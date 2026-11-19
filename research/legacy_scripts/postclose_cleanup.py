# -*- coding: utf-8 -*-
"""S7 盘后清理 CLI 入口（默认 dry-run / plan-only；生产 apply 须全证全许）。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AiStock S7 16:15 前精准清理入口（默认 plan-only；apply 须由总控传入已认证证据链）"
    )
    parser.add_argument("--plan-id", required=True, help="盘后计划 ID")
    parser.add_argument("--trade-date", required=True, help="目标交易日 YYYY-MM-DD")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="危险正式删除开关：仅允许通过 services.postclose_orchestrator 注入持锁与证书链执行",
    )
    args = parser.parse_args(argv)

    if args.apply:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "rc": 2,
                    "reason": "standalone_apply_forbidden_use_postclose_orchestrator",
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "status": "plan_only",
                "rc": 0,
                "plan_id": args.plan_id,
                "trade_date": args.trade_date,
                "note": "dry-run only: no database DELETE or mood mutation issued",
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""S8 盘后单一权威总控 CLI 入口（默认 dry-run；--apply 须完整证书与锁配置，禁止无证旁路）。"""

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
        description="AiStock S8 盘后单一权威编排入口（默认 dry-run；--apply 须具备完整证书与操作许可包）"
    )
    parser.add_argument("--plan-id", required=True, help="盘后计划 ID")
    parser.add_argument("--trade-date", required=True, help="目标交易日 YYYY-MM-DD")
    parser.add_argument("--bundle-json", default="", help="已签发证书/许可/清单输入包路径")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="执行模式：必须提供完整认证 --bundle-json 并通过 Owner 审批",
    )
    args = parser.parse_args(argv)

    if args.apply and not str(args.bundle_json or "").strip():
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "rc": 2,
                    "reason": "missing_certified_bundle_for_apply",
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "status": "plan_only" if not args.apply else "blocked",
                "rc": 0 if not args.apply else 2,
                "plan_id": args.plan_id,
                "trade_date": args.trade_date,
                "note": "use services.postclose_orchestrator.run_postclose_orchestration for certified execution",
            },
            ensure_ascii=False,
        )
    )
    return 0 if not args.apply else 2


if __name__ == "__main__":
    raise SystemExit(main())

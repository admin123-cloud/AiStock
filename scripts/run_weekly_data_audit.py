"""Publish a bounded, read-only weekly data-delivery audit."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.operations.delivery import build_delivery_calendar
from services.operations.health import BUSINESS_TZ, write_snapshot
from services.operations.read_models import read_json
from utils.market_warehouse import clickhouse_client
from utils.paths import runtime_path


BAD_STATUSES = {"partial", "missing", "unknown", "unverified"}


def summarize(calendar: dict) -> dict:
    datasets = []
    for dataset in calendar.get("datasets", []):
        cells = list(dataset.get("cells") or [])
        bad = [cell for cell in cells if cell.get("status") in BAD_STATUSES]
        datasets.append({
            "id": dataset.get("id"), "label": dataset.get("label"),
            "checked_days": len(cells), "problem_days": len(bad),
            "missing_keys": sum(int(cell.get("missing") or 0) for cell in bad),
            "problem_dates": [cell.get("date") for cell in bad],
            "error": dataset.get("error"),
        })
    problems = sum(item["problem_days"] for item in datasets)
    return {"status": "healthy" if not problems else "degraded", "datasets": datasets,
            "problem_days": problems,
            "scope": "只读覆盖审计；不下载行情、不写主表、不执行修复或备份"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly read-only AiStock delivery audit")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output", type=Path, default=runtime_path("operations", "weekly_audit", "latest.json"))
    args = parser.parse_args()
    if not 1 <= args.days <= 60:
        parser.error("--days must be in 1..60")
    client = clickhouse_client()
    try:
        calendar = build_delivery_calendar(
            client, days=args.days, now=datetime.now(BUSINESS_TZ), query_timeout_seconds=90,
            sector_universe=read_json(runtime_path("operations", "sector_universe.json")),
        )
    finally:
        client.close()
    payload = {"schema_version": 1, "generated_at": datetime.now(BUSINESS_TZ).isoformat(),
               "audit_kind": "weekly_data_delivery", "days": args.days, **summarize(calendar),
               "calendar": calendar}
    write_snapshot(payload, args.output)
    print(json.dumps({"status": payload["status"], "problem_days": payload["problem_days"],
                      "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

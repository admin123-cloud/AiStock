"""Read-only audit of the formal daily table scope and per-code spans.

The command performs only SELECTs with ``readonly=1``.  It does not create
tables, mutate ClickHouse, or infer an expected date universe from formal
rows.  Pass the accepted historical-stock classification file explicitly;
the output is a local JSON evidence report for later date-universe/member and
minute-line completeness work.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.daily_coverage_audit import derive_formal_scope_sets
from utils.market_warehouse import clickhouse_client


BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
CORE_INDEX_CODES = frozenset(
    {
        "000001.SH",
        "399001.SZ",
        "399006.SZ",
        "399106.SZ",
        "000985.SH",
        "000680.SH",
        "000300.SH",
        "000016.SH",
        "000905.SH",
        "000852.SH",
        "000688.SH",
    }
)


def load_historical_candidates(path: Path) -> set[str]:
    candidates: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if str(row.get("classification") or "").strip().lower() == "stock":
            candidates.add(str(row["code"]).strip().upper())
    return candidates


def _query(client: Any, sql: str):
    return client.query(
        sql,
        settings={"readonly": 1, "max_execution_time": 180, "max_threads": 2},
    ).result_rows


def build_scope_report(
    *,
    client: Any,
    historical_candidates: set[str],
    as_of: str,
) -> dict[str, Any]:
    master_rows = [
        {
            "code": row[0],
            "type": row[1],
            "quit": row[2],
            "listing_status": row[3],
            "list_date": row[4],
            "delist_date": row[5],
        }
        for row in _query(
            client,
            "SELECT code,type,quit,listing_status,list_date,delist_date FROM stock.stocks FINAL",
        )
    ]
    scope = derive_formal_scope_sets(
        master_rows,
        historical_candidate_codes=historical_candidates,
        core_index_codes=CORE_INDEX_CODES,
        as_of=as_of,
    )
    formal_rows = _query(
        client,
        "SELECT code,count(),min(trade_date),max(trade_date) "
        "FROM stock.kline_daily FINAL GROUP BY code ORDER BY code",
    )
    spans: list[dict[str, Any]] = []
    category_counts = {
        "current_stock": 0,
        "historical_stock": 0,
        "core_index": 0,
        "out_of_scope": 0,
    }
    formal_codes: set[str] = set()
    for code, rows, first_date, last_date in formal_rows:
        canonical = str(code).upper()
        formal_codes.add(canonical)
        if canonical in scope.core_index_codes:
            category = "core_index"
        elif canonical in scope.current_stock_codes:
            category = "current_stock"
        elif canonical in scope.historical_stock_codes:
            category = "historical_stock"
        else:
            category = "out_of_scope"
        category_counts[category] += 1
        spans.append(
            {
                "code": canonical,
                "scope_category": category,
                "rows": int(rows),
                "first_trade_date": str(first_date),
                "last_trade_date": str(last_date),
            }
        )

    status_counts = [
        {"status": row[0], "review_state": row[1], "rows": int(row[2])}
        for row in _query(
            client,
            "SELECT status,review_state,count() "
            "FROM stock.kline_daily_market_status_audit FINAL "
            "GROUP BY status,review_state ORDER BY status,review_state",
        )
    ]
    missing_current = sorted(scope.current_stock_codes - formal_codes)
    return {
        "generated_at": datetime.now(BUSINESS_TZ).isoformat(),
        "readonly": True,
        "scope": {
            "as_of": as_of,
            "current_stock_codes": len(scope.current_stock_codes),
            "historical_stock_codes": len(scope.historical_stock_codes),
            "core_index_codes": len(scope.core_index_codes),
            "allowed_codes": len(scope.allowed_codes),
        },
        "formal": {
            "codes": len(formal_rows),
            "category_counts": category_counts,
            "out_of_scope_codes": [row["code"] for row in spans if row["scope_category"] == "out_of_scope"],
            "missing_current_stock_codes": missing_current,
            "spans": spans,
        },
        "market_status_audit_counts": status_counts,
        "next_gate": "publish accepted date-universe registry/member and build daily_coverage_audit rows",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-candidates", type=Path, required=True)
    parser.add_argument("--as-of", default=datetime.now(BUSINESS_TZ).date().isoformat())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = build_scope_report(
        client=clickhouse_client(),
        historical_candidates=load_historical_candidates(args.historical_candidates),
        as_of=args.as_of,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "formal_codes": report["formal"]["codes"],
        "category_counts": report["formal"]["category_counts"],
        "out_of_scope": len(report["formal"]["out_of_scope_codes"]),
        "missing_current_stock": len(report["formal"]["missing_current_stock_codes"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

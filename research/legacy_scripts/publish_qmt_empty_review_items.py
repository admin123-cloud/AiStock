"""Publish unresolved QMT empty-return daily gaps to the data-review queue."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utils.paths import runtime_path
from utils.market_warehouse import clickhouse_client


EMPTY_CODES = [
    "301390.SZ", "301686.SZ", "600825.SH", "600929.SH", "603159.SH",
    "605577.SH", "688291.SH", "688432.SH", "688496.SH", "920201.BJ",
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    root = runtime_path() / "minute_migration_v2"
    evidence_path = runtime_path() / "daily_kline_coverage" / "retry_20260911.json"
    output = root / "scoped_anomalies_qmt_empty_20260911.json"
    if not evidence_path.exists():
        raise SystemExit(f"missing evidence: {evidence_path}")
    evidence_hash = sha256(evidence_path)
    client = clickhouse_client()
    code_sql = ",".join("'" + code + "'" for code in EMPTY_CODES)
    stock_rows = client.query(
        f"SELECT code, type, listing_status, list_date FROM stocks FINAL WHERE code IN ({code_sql})"
    ).result_rows
    stock_map = {str(row[0]).upper(): row[1:] for row in stock_rows}
    status_rows = client.query(
        f"SELECT code, start_date, end_date, status, evidence_url, reason "
        f"FROM kline_daily_market_status_audit FINAL WHERE code IN ({code_sql}) "
        "AND review_state = 'verified' AND status IN ('suspended', 'delisted', 'not_trading') "
        "AND start_date <= toDate('2026-09-11') AND end_date >= toDate('2026-09-11')"
    ).result_rows
    status_map = {str(row[0]).upper(): row[1:] for row in status_rows}
    items = []
    for code in EMPTY_CODES:
        stock = stock_map.get(code)
        status = status_map.get(code)
        if stock and str(stock[0]).lower() != "stock":
            category = f"non_stock:{stock[0]}"
        elif stock and str(stock[1]).lower() not in {"active", "listed"}:
            category = f"listing:{stock[1]}"
        elif status:
            category = f"status:{status[2]}"
        else:
            category = "qmt_empty_after_retry"
        items.append({
            "code": code,
            "date": "2026-09-11",
            "count": 0,
            "category": category,
            "missing_count": 1,
            "period": "日线",
            "evidence": {
                "qmt_retry_path": str(evidence_path),
                "qmt_retry_sha256": evidence_hash,
                "qmt_attempts": 5,
                "qmt_result": "empty",
                "listing_status": stock[1] if stock else None,
                "status_evidence": {
                    "start": str(status[0]), "end": str(status[1]), "status": str(status[2]),
                    "evidence_url": str(status[3]), "reason": str(status[4]),
                } if status else None,
            },
            "authority": "QMT 5 次单代码重试结果",
            "validity": "blocked_evidence",
        })
    payload = {
        "status": "blocked_evidence",
        "run_id": "qmt_empty_20260911",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": len(items),
        "categories": {},
        "blocked_evidence_rows": len(items),
        "read_only": True,
        "formal_table_mutation": False,
        "items": items,
    }
    for item in items:
        payload["categories"][item["category"]] = payload["categories"].get(item["category"], 0) + 1
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(output)
    print(json.dumps({"output": str(output), "rows": len(items), "evidence_sha256": evidence_hash}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

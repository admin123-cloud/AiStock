"""WStock .wdz assessment report: can this archive repair purchased-history gaps?

Findings recorded here:
  1. The supplied E:\\AStockHistory\\wstock\\*.wdz files hold encrypted payloads.
  2. The vendor ships a Windows converter (wsWDZ_setup_*.exe) that decrypts them.
  3. The vendor's FREE trial accounts are restricted by stock-code tail digit and
     by calendar date, so a large share of the queued gap codes cannot be
     exported without buying a full licence.

Read-only: inspects the downloaded .wdz files and the live review queue.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.paths import report_path

WDZ_DIR = Path(r"E:\AStockHistory\wstock\原始压缩包\WDZ")
QUEUE = "stock.purchased_history_data_review_items"
RUN_ID = "purchased_history_review_20260918_5m_source_closeout_v14"

# Vendor terms (wstock.net): free trial accounts can only export stocks whose
# code ends in a digit matching the account name, and since 2020-01 tail 8/9 and
# since 2020-02 tail 1/2 are excluded from free accounts entirely.
FREE_UNSUPPORTED_TAILS = {"1", "2", "8", "9"}


def main() -> int:
    client = clickhouse_client()
    rows = client.query(
        f"""
        SELECT code, toString(trade_date) AS d, issue_type
        FROM {QUEUE}
        WHERE run_id = %(run)s
        """,
        parameters={"run": RUN_ID},
    ).result_rows

    tails = Counter(str(c)[5] for c, _d, _t in rows)
    unsupported = [r for r in rows if str(r[0])[5] in FREE_UNSUPPORTED_TAILS]
    by_date_total = Counter(str(d) for _c, d, _t in rows)
    by_date_blocked = Counter(str(d) for c, d, _t in rows if str(c)[5] in FREE_UNSUPPORTED_TAILS)

    wdz = []
    for p in sorted(WDZ_DIR.glob("*.wdz")):
        wdz.append({"file": p.name, "size": p.stat().st_size})

    report = {
        "wdz_files": wdz,
        "wdz_total_bytes": sum(w["size"] for w in wdz),
        "queue_run_id": RUN_ID,
        "queue_rows": len(rows),
        "tail_digit_distribution": dict(sorted(tails.items())),
        "free_account_blocked_rows": len(unsupported),
        "free_account_blocked_pct": round(100.0 * len(unsupported) / max(len(rows), 1), 1),
        "top_dates": [
            {"date": d, "total": by_date_total[d], "blocked_by_free_account": by_date_blocked.get(d, 0)}
            for d in sorted(by_date_total, key=lambda x: -by_date_total[x])[:10]
        ],
        "format": {
            "encrypted": True,
            "needs_vendor_tool": "wsWDZ_setup_*.exe (Windows only)",
            "compressed_claim": "vendor states wdz compresses better than rar, but measured payload is ciphertext (entropy 7.9997, all 65536 byte-pairs present)",
        },
        "conclusion": (
            "Archive is usable ONLY after decryption with the vendor's Windows tool. "
            "Even then, the free trial account cannot export 1/2/8/9-tail codes, which "
            "cover a large share of the queued gaps; a paid licence is required to cover "
            "all of them. Whether the decrypted values actually repair the corrupted "
            "bar ranges is still UNVERIFIED and must be tested on the specific gap days."
        ),
    }

    out = report_path("purchased_history", "wstock", "wdz_assessment.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

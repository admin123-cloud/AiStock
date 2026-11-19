from __future__ import annotations

"""Create a strict read-only stock-master whitelist evidence snapshot."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client
from utils.ocean_whitelist import build_stock_whitelist_snapshot, write_stock_whitelist_snapshot
from utils.paths import runtime_path


SOURCE_TABLE = "stock.stocks"
SOURCE_QUERY = "SELECT code,type FROM stock.stocks FINAL WHERE type='stock' ORDER BY code"


def main() -> int:
    client = clickhouse_client()
    result = client.query(SOURCE_QUERY)
    snapshot = build_stock_whitelist_snapshot(
        result.result_rows,
        source_table=SOURCE_TABLE,
        source_query=SOURCE_QUERY,
    )
    evidence = write_stock_whitelist_snapshot(
        runtime_path("ocean_vps_local_stage"), snapshot,
    )
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

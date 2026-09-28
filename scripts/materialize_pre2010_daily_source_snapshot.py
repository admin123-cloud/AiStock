from __future__ import annotations

"""Materialize the independent purchased daily source as an immutable snapshot."""

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client


STAGE = "stock.purchased_history_daily_snapshot_stage"
BATCH = "purchased_daily_snapshot_pre2010_20260921"
TARGET = "stock.kline_daily_pre2010_source_v20260921"
OUT = Path(r"F:\AiStock\data\data\runtime\pre2010_daily_source_v20260921")


DDL = f"""CREATE TABLE {TARGET}
(
 `code` String, `trade_date` Date, `open` Float64, `high` Float64,
 `low` Float64, `close` Float64, `volume` Float64, `amount` Float64,
 `amplitude` Float64, `change_pct` Float64, `change_amount` Float64,
 `turnover_rate` Float64, `source_batch` String,
 `quality_status` LowCardinality(String), `created_at` DateTime('Asia/Shanghai'),
 `id` UInt64
) ENGINE = MergeTree PARTITION BY toYYYYMM(trade_date)
ORDER BY (code, trade_date) SETTINGS index_granularity=8192"""


def main() -> int:
    c = clickhouse_client()
    exists = c.query("SELECT count() FROM system.tables WHERE database='stock' AND name=%(n)s", parameters={"n": TARGET.split('.', 1)[1]}).result_rows[0][0]
    if exists:
        raise RuntimeError(f"refusing to append or mutate existing target: {TARGET}")
    c.command(DDL)
    c.command(f"""
        INSERT INTO {TARGET}
        SELECT code, trade_date, open, high, low, close,
               toFloat64(volume_lots), toFloat64(amount_yuan),
               0, ifNull(change_pct,0), ifNull(change_amount,0),
               ifNull(turnover_rate,0), %(batch)s, 'independent_daily_source_valid', now(), id
        FROM {STAGE}
        WHERE batch_id=%(batch)s
          AND quality_status='valid'
          AND toYear(trade_date) BETWEEN 2000 AND 2009
          AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL
          AND volume_lots IS NOT NULL AND amount_yuan IS NOT NULL
        SETTINGS max_threads=4, max_memory_usage=12000000000,
                 max_bytes_before_external_group_by=1000000000
    """, parameters={"batch": BATCH})
    total, unique = c.query(f"SELECT count(), uniqExact(tuple(code,trade_date)) FROM {TARGET}").result_rows[0]
    nulls = c.query(f"SELECT countIf(isNull(open) OR isNull(high) OR isNull(low) OR isNull(close) OR isNull(volume) OR isNull(amount)) FROM {TARGET}").result_rows[0][0]
    by_year = c.query(f"SELECT toYear(trade_date),count(),uniqExact(tuple(code,trade_date)) FROM {TARGET} GROUP BY toYear(trade_date) ORDER BY toYear(trade_date)").result_rows
    manifest = {
        "schema_version": "pre2010-independent-daily-source-v1",
        "scope": [2000, 2009],
        "source_stage": STAGE,
        "batch_id": BATCH,
        "target": TARGET,
        "source_units": {"volume": "lots", "amount": "yuan", "archive_amount_field": "thousand_yuan_x_1000"},
        "blank_ohlc_policy": "excluded_from_valid_snapshot",
        "checks": {"rows": int(total), "unique_keys": int(unique), "null_metric_rows": int(nulls), "by_year": [[int(y), int(n), int(u)] for y,n,u in by_year]},
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "old_formal_tables_untouched": True,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    OUT.joinpath("manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


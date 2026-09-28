"""Derive safe 2010 daily candidates from complete purchased 5m bars."""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client

STAGE = "stock.purchased_history_daily_snapshot_stage"
MINUTE = "stock.kline_minute_5"
CANDIDATE = "stock.purchased_daily_2010_derived_5m_candidate_20260921"
TZ = ZoneInfo("Asia/Shanghai")


def main() -> int:
    c = clickhouse_client()
    c.command(f"DROP TABLE IF EXISTS {CANDIDATE}")
    c.command(f"""CREATE TABLE {CANDIDATE} (
        code String, trade_date Date, open Float64, high Float64, low Float64, close Float64,
        volume Float64, amount Float64, source_bars UInt16, source_kind LowCardinality(String),
        created_at DateTime('Asia/Shanghai'), id UInt64
    ) ENGINE=MergeTree ORDER BY (trade_date,code)""")
    settings = "SETTINGS max_threads=1, max_memory_usage=10000000000, max_bytes_before_external_group_by=1000000000, max_bytes_before_external_sort=1000000000"
    total = 0
    for prefix in "0123456789":
        sql = f"""
        INSERT INTO {CANDIDATE}
        SELECT s.code, s.trade_date,
               argMin(m.open,m.datetime), max(m.high), min(m.low), argMax(m.close,m.datetime),
               sum(m.volume), sum(m.amount), toUInt16(count()), 'purchased_5m_derived_daily',
               now('Asia/Shanghai'), cityHash64(s.code,toString(s.trade_date),'purchased_daily_2010_derived_5m_candidate_20260921')
        FROM (SELECT code,trade_date FROM {STAGE}
              WHERE toYear(trade_date)=2010 AND quality_status='missing_ohlc' AND startsWith(code,'{prefix}')
              GROUP BY code,trade_date) s
        INNER JOIN {MINUTE} m
          ON s.code=m.code AND s.trade_date=toDate(m.datetime)
        WHERE toYear(m.datetime)=2010
        GROUP BY s.code,s.trade_date
        HAVING count()=48 AND uniqExact(m.datetime)=48
           AND min(m.open)>0 AND min(m.high)>0 AND min(m.low)>0 AND min(m.close)>0
        {settings}
        """
        c.command(sql)
        n = int(c.query(f"SELECT count() FROM {CANDIDATE} WHERE startsWith(code,'{prefix}')").result_rows[0][0])
        total += n
        print({"prefix": prefix, "candidate_rows": n}, flush=True)

    existing = c.query(f"""
        SELECT count() FROM {CANDIDATE} c
        INNER JOIN stock.kline_daily d ON c.code=d.code AND c.trade_date=d.trade_date
    """).result_rows[0][0]
    print({"candidate_rows": total, "already_formal": int(existing)}, flush=True)

    # Write only missing formal daily keys. Derived rows have zero auxiliary fields;
    # OHLC/volume/amount are fully sourced from 48 purchased 5m bars.
    c.command(f"""
        INSERT INTO stock.kline_daily
        (code,trade_date,open,high,low,close,volume,amount,amplitude,change_pct,change_amount,turnover_rate,created_at,id)
        SELECT c.code,c.trade_date,c.open,c.high,c.low,c.close,c.volume,c.amount,0.,0.,0.,0.,now('Asia/Shanghai'),c.id
        FROM {CANDIDATE} c
        LEFT JOIN stock.kline_daily d ON c.code=d.code AND c.trade_date=d.trade_date
        WHERE d.code IS NULL
        {settings}
    """)
    print({"formal_inserted": c.query(f"SELECT count() FROM stock.kline_daily WHERE toYear(trade_date)=2010 AND code IN (SELECT code FROM {CANDIDATE})").result_rows[0][0]}, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

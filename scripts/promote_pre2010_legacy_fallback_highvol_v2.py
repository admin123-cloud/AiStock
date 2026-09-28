from __future__ import annotations

"""Build an immutable pre-2010 snapshot with a legacy-formal fallback.

The preferred v20260921 snapshot is kept untouched.  For code-days absent from
that snapshot, this script admits complete 48-bar days from the existing formal
5m table as a lower-confidence fallback.  If a positive-volume minute has a
zero amount, amount is estimated as ``high * volume_lots * 100`` (yuan).  The
factor 100 is required by the project contract because volume is in lots, while
amount is in yuan.

No existing table is altered, deleted, or mutated.  The output tables carry a
new suffix and explicit source/quality labels.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.market_warehouse import clickhouse_client


BASE5 = "stock.kline_minute_5_pre2010_v20260921"
BASE15 = "stock.kline_minute_15_pre2010_v20260921"
BASE30 = "stock.kline_minute_30_pre2010_v20260921"
BASE60 = "stock.kline_minute_60_pre2010_v20260921"
BASED = "stock.kline_daily_pre2010_v20260921"
OLD5 = "stock.kline_minute_5"
OLDD = "stock.kline_daily"
SUFFIX = "_r2_fallback_highvol"
T5 = BASE5 + SUFFIX
T15 = BASE15 + SUFFIX
T30 = BASE30 + SUFFIX
T60 = BASE60 + SUFFIX
TD = BASED + SUFFIX
OUT = Path(r"F:\AiStockData\data\runtime\pre2010_fallback_high_volume_v20260921")


def ddl_minute(name: str) -> str:
    return f"""CREATE TABLE {name}
(
 `code` String, `datetime` DateTime('Asia/Shanghai'), `open` Float64,
 `high` Float64, `low` Float64, `close` Float64, `volume` Int64,
 `amount` Float64, `source_batch` String,
 `quality_status` LowCardinality(String), `created_at` DateTime('Asia/Shanghai'),
 `id` UInt64
) ENGINE = MergeTree PARTITION BY toYYYYMM(datetime)
ORDER BY (code, datetime) SETTINGS index_granularity=8192"""


def ddl_daily(name: str) -> str:
    return f"""CREATE TABLE {name}
(
 `code` String, `trade_date` Date, `open` Float64, `high` Float64,
 `low` Float64, `close` Float64, `volume` Float64, `amount` Float64,
 `amplitude` Float64, `change_pct` Float64, `change_amount` Float64,
 `turnover_rate` Float64, `source_batch` String,
 `quality_status` LowCardinality(String), `created_at` DateTime('Asia/Shanghai'),
 `id` UInt64
) ENGINE = MergeTree PARTITION BY toYYYYMM(trade_date)
ORDER BY (code, trade_date) SETTINGS index_granularity=8192"""


def settings() -> str:
    return "SETTINGS max_threads=4, max_memory_usage=12000000000, max_bytes_before_external_group_by=2000000000"


def create_empty(c) -> None:
    for t, ddl in ((T5, ddl_minute(T5)), (T15, ddl_minute(T15)),
                   (T30, ddl_minute(T30)), (T60, ddl_minute(T60)),
                   (TD, ddl_daily(TD))):
        exists = c.query("SELECT count() FROM system.tables WHERE database='stock' AND name=%(n)s", parameters={"n": t.split('.', 1)[1]}).result_rows[0][0]
        if exists:
            raise RuntimeError(f"target already exists; refusing to append or mutate: {t}")
        c.command(ddl)


def copy_preferred(c) -> None:
    c.command(f"INSERT INTO {T5} SELECT * FROM {BASE5} {settings()}")
    c.command(f"INSERT INTO {T15} SELECT * FROM {BASE15} {settings()}")
    c.command(f"INSERT INTO {T30} SELECT * FROM {BASE30} {settings()}")
    c.command(f"INSERT INTO {T60} SELECT * FROM {BASE60} {settings()}")
    c.command(f"INSERT INTO {TD} SELECT * FROM {BASED} {settings()}")


def fallback_5m(c, year: int) -> None:
    c.command(f"""
    INSERT INTO {T5}
    SELECT
      f.code, f.datetime, ifNull(f.open,0), ifNull(f.high,0), ifNull(f.low,0), ifNull(f.close,0),
      toInt64(ifNull(f.volume,0)),
      if(ifNull(f.amount,0)>0 OR ifNull(f.volume,0)<=0 OR ifNull(f.high,0)<=0,
         ifNull(f.amount,0), ifNull(f.high,0)*toFloat64(f.volume)*100.0),
      'legacy_formal_fallback_highvol_20260921', 'legacy_fallback_high_price_x_volume',
      now(), f.id
    FROM (SELECT * FROM {OLD5} FINAL) AS f
    LEFT ANTI JOIN (SELECT code, toDate(datetime) AS d FROM {BASE5} GROUP BY code, d) AS p
      ON p.code=f.code AND p.d=toDate(f.datetime)
    WHERE toYear(f.datetime)=%(y)s
      AND f.code IS NOT NULL AND f.datetime IS NOT NULL
    {settings()}
    """, parameters={"y": year})


def fallback_derived(c, target: str, period: int, bars: int, year: int) -> None:
    c.command(f"""
    INSERT INTO {target}
    SELECT code, bucket, argMin(open, datetime), max(high), min(low), argMax(close, datetime),
             sum(volume), sum(amount), 'legacy_formal_fallback_highvol_20260921',
           'derived_from_legacy_fallback_highvol', now(), cityHash64(code,toString(bucket),%(label)s,'fallback')
    FROM
    (
      SELECT code, datetime, open, high, low, close, volume, amount,
             session_start + toIntervalMinute(if(elapsed<=0,%(period)i,intDiv(elapsed+%(period)i-1,%(period)i)*%(period)i)) AS bucket
      FROM
      (
        SELECT code, datetime, open, high, low, close, volume, amount,
               if(toHour(datetime)<12,
                  toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),
                  toStartOfDay(datetime)+toIntervalHour(13)) AS session_start,
               dateDiff('minute', session_start, datetime) AS elapsed
        FROM (SELECT * FROM {OLD5} FINAL) AS f
        LEFT ANTI JOIN (SELECT code, toDate(datetime) AS d FROM {BASE5} GROUP BY code, d) AS p
          ON p.code=f.code AND p.d=toDate(f.datetime)
        WHERE toYear(f.datetime)=%(y)s
      )
    )
    GROUP BY code, bucket
    HAVING count()=%(bars)i
    {settings()}
    """, parameters={"y": year, "period": period, "label": f"{period}m", "bars": bars})


def fallback_daily(c, year: int) -> None:
    c.command(f"""
    INSERT INTO {TD}
    SELECT a.code, a.trade_date, a.open, a.high, a.low, a.close, a.volume, a.amount,
           ifNull(d.amplitude,0), ifNull(d.change_pct,0), ifNull(d.change_amount,0), ifNull(d.turnover_rate,0),
           'legacy_formal_fallback_highvol_20260921', 'derived_from_legacy_fallback_highvol', now(),
           cityHash64(a.code,toString(a.trade_date),'1d','fallback')
    FROM
    (
      SELECT code, toDate(datetime) AS trade_date, argMin(open,datetime) AS open,
             max(high) AS high, min(low) AS low, argMax(close,datetime) AS close,
             toFloat64(sum(volume)) AS volume, sum(amount) AS amount
      FROM (SELECT * FROM {OLD5} FINAL) AS f
      LEFT ANTI JOIN (SELECT code, toDate(datetime) AS d FROM {BASE5} GROUP BY code, d) AS p
        ON p.code=f.code AND p.d=toDate(f.datetime)
      WHERE toYear(f.datetime)=%(y)s
      GROUP BY code, trade_date
      HAVING count()=48 AND uniqExact(datetime)=48
    ) a
    LEFT JOIN
    (
      SELECT code, trade_date, amplitude, change_pct, change_amount, turnover_rate
      FROM {OLDD} FINAL WHERE toYear(trade_date)=%(y)s
    ) d ON d.code=a.code AND d.trade_date=a.trade_date
    {settings()}
    """, parameters={"y": year})


def check(c, table: str, date_col: str) -> dict[str, int]:
    row = c.query(f"SELECT count(), uniqExact(tuple(code,{date_col})) FROM {table}").result_rows[0]
    return {"rows": int(row[0]), "unique_keys": int(row[1])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="perform immutable v2 build")
    args = ap.parse_args()
    c = clickhouse_client()
    if not args.execute:
        print(json.dumps({"dry_run": True, "targets": [T5,T15,T30,T60,TD], "amount_formula": "high * volume_lots * 100 yuan"}, ensure_ascii=False))
        return 0
    create_empty(c)
    copy_preferred(c)
    for y in range(2000, 2010):
        fallback_5m(c, y)
        print(json.dumps({"year": y, "phase": "5m_fallback", **check(c,T5,"datetime")}, ensure_ascii=False), flush=True)
    for y in range(2000, 2010):
        fallback_derived(c, T15, 15, 3, y)
        fallback_derived(c, T30, 30, 6, y)
        fallback_derived(c, T60, 60, 12, y)
        fallback_daily(c, y)
        print(json.dumps({"year": y, "phase": "derived_fallback", "15m": check(c,T15,"datetime"), "30m": check(c,T30,"datetime"), "60m": check(c,T60,"datetime"), "1d": check(c,TD,"trade_date")}, ensure_ascii=False), flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "pre2010-fallback-high-volume-v2",
        "scope": [2000, 2009],
        "preferred_snapshot": BASE5,
        "fallback_source": OLD5,
        "fallback_condition": "complete legacy formal code-day absent from preferred snapshot",
        "amount_policy": "if amount is zero while volume_lots and high are positive, estimate amount_yuan = high * volume_lots * 100; keep zero for zero-volume rows",
        "tables": [T5,T15,T30,T60,TD],
        "formal_old_tables_untouched": True,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "checks": {T5: check(c,T5,"datetime"), T15: check(c,T15,"datetime"), T30: check(c,T30,"datetime"), T60: check(c,T60,"datetime"), TD: check(c,TD,"trade_date")},
    }
    OUT.joinpath("manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

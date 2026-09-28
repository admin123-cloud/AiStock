from __future__ import annotations

"""Build an isolated 30-minute replacement candidate for 2000-2009.

The source precedence is intentionally explicit:

1. a complete code-day from the purchased native 30m archive;
2. a complete code-day aggregated from the frozen, audited 5m r2 table.

The script never writes ``stock.kline_minute_30`` and never mutates an existing
candidate.  It creates one versioned MergeTree table and records a JSON
manifest after each year.  The 1m archive is not silently treated as a source:
code-days for which neither source is complete remain unresolved in the
manifest.  A later, separately audited 1m recovery may be added only as a new
candidate version.
"""

import argparse
import json
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

import clickhouse_connect

START_YEAR = int(os.getenv("AISTOCK_NATIVE30_START_YEAR", "2000"))
END_YEAR = int(os.getenv("AISTOCK_NATIVE30_END_YEAR", "2009"))
RUN_ID = os.getenv("AISTOCK_NATIVE30_RUN_ID", "purchased_native_30m_candidate_20260921")
TARGET = f"stock.{RUN_ID}"
NATIVE = os.getenv("AISTOCK_NATIVE30_NATIVE", "stock.purchased_native_30_pre2010_v20260921_stage")
FIVE = os.getenv("AISTOCK_NATIVE30_FIVE", "stock.kline_minute_5_pre2010_v20260921_r2_fallback_highvol")


def connect():
    for attempt in range(24):
        try:
            return clickhouse_connect.get_client(
                host="127.0.0.1", port=8123, username="default", password="",
                send_receive_timeout=600,
            )
        except Exception as exc:
            if attempt == 23:
                raise
            print(f"connect retry {attempt + 1}: {str(exc)[:160]}", flush=True)
            time.sleep(5)


def query(c, sql: str, parameters=None):
    for attempt in range(30):
        try:
            return c.query(sql, parameters=parameters).result_rows
        except Exception as exc:
            text = str(exc)
            if "TOO_MANY_SIMULTANEOUS" not in text and "MEMORY_LIMIT_EXCEEDED" not in text:
                raise
            if attempt == 29:
                raise
            time.sleep(min(30, 5 + attempt))


def command(c, sql: str):
    for attempt in range(30):
        try:
            return c.command(sql)
        except Exception as exc:
            text = str(exc)
            if "TOO_MANY_SIMULTANEOUS" not in text:
                raise
            if attempt == 29:
                raise
            time.sleep(min(30, 5 + attempt))


def write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="manifest.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ddl() -> str:
    return f"""CREATE TABLE {TARGET}
(
 `code` String, `datetime` DateTime('Asia/Shanghai'),
 `open` Float64, `high` Float64, `low` Float64, `close` Float64,
 `volume` Float64, `amount` Float64,
 `source_kind` LowCardinality(String), `source_priority` UInt8,
 `source_year` UInt16, `source_ref` String,
 `quality_status` LowCardinality(String), `candidate_run_id` String,
 `created_at` DateTime('Asia/Shanghai')
) ENGINE = MergeTree PARTITION BY toYYYYMM(datetime)
ORDER BY (code, datetime) SETTINGS index_granularity=8192"""


SETTINGS = "SETTINGS max_threads=2, max_memory_usage=10000000000, max_bytes_before_external_group_by=1000000000, max_bytes_before_external_sort=1000000000"


def native_insert(c, year: int) -> int:
    sql = f"""
    INSERT INTO {TARGET}
    SELECT s.code, s.datetime, s.open, s.high, s.low, s.close, s.volume, s.amount,
           'native_purchased', toUInt8(1), toUInt16(s.source_year), s.source_member,
           'native_complete_code_day', '{RUN_ID}', now('Asia/Shanghai')
    FROM {NATIVE} AS s
    WHERE s.source_year = {year}
      AND (s.code, toDate(s.datetime)) IN
      (
        SELECT code, toDate(datetime) AS d
        FROM {NATIVE}
        WHERE source_year = {year}
        GROUP BY code, toDate(datetime)
        HAVING count() = 8 AND uniqExact(datetime) = 8
      )
    {SETTINGS}
    """
    query(c, sql)
    return int(query(c, f"SELECT count() FROM {TARGET} WHERE source_year={year} AND source_kind='native_purchased'")[0][0])


def five_bucket_sql(year: int) -> str:
    # The 5m source timestamps are bar end times: 09:35..11:30 and
    # 13:05..15:00.  Six bars ending at 10:00 form the first 30m bar, etc.
    return f"""
    WITH base AS
    (
      SELECT code, datetime, open, high, low, close, volume, amount,
        if(toHour(datetime) < 12,
           toStartOfDay(datetime) + toIntervalHour(9) + toIntervalMinute(30),
           toStartOfDay(datetime) + toIntervalHour(13)) AS session_start,
        dateDiff('minute', session_start, datetime) AS elapsed
      FROM {FIVE}
      WHERE toYear(datetime) = {year}
    ), mapped AS
    (
      SELECT *, session_start + toIntervalMinute(if(elapsed <= 0, 30, intDiv(elapsed + 29, 30) * 30)) AS bucket
      FROM base
    ), complete_days AS
    (
      SELECT code, toDate(bucket) AS d
      FROM mapped
      GROUP BY code, d
      HAVING count() = 48 AND uniqExact(datetime) = 48 AND uniqExact(bucket) = 8
    ), native_days AS
    (
      SELECT code, toDate(datetime) AS d
      FROM {NATIVE}
      WHERE source_year = {year}
      GROUP BY code, d
      HAVING count() = 8 AND uniqExact(datetime) = 8
    ), buckets AS
    (
      SELECT code, bucket, toDate(bucket) AS d,
             argMin(open, datetime) AS open,
             max(high) AS high,
             min(low) AS low,
             argMax(close, datetime) AS close,
             sum(volume) AS volume,
             sum(amount) AS amount,
             count() AS n,
             uniqExact(datetime) AS un
      FROM mapped
      WHERE (code, toDate(bucket)) IN complete_days
        AND (code, toDate(bucket)) NOT IN native_days
      GROUP BY code, bucket, d
      HAVING n = 6 AND un = 6
    )
    SELECT code, bucket AS datetime, open, high, low, close, volume, amount
    FROM buckets
    """


def five_insert(c, year: int) -> int:
    sql = f"""
    INSERT INTO {TARGET}
    SELECT code, datetime, open, high, low, close, volume, amount,
           'five_minute_derived', toUInt8(2), toUInt16({year}),
           'stock.kline_minute_5_pre2010_v20260921_r2_fallback_highvol',
           'five_minute_complete_code_day', '{RUN_ID}', now('Asia/Shanghai')
    FROM ({five_bucket_sql(year)})
    {SETTINGS}
    """
    query(c, sql)
    return int(query(c, f"SELECT count() FROM {TARGET} WHERE source_year={year} AND source_kind='five_minute_derived'")[0][0])


def year_stats(c, year: int) -> dict:
    rows = query(c, f"""
      SELECT source_kind, count(), uniqExact(tuple(code, datetime)), uniqExact(tuple(code,toDate(datetime)))
      FROM {TARGET} WHERE source_year={year} GROUP BY source_kind ORDER BY source_kind
    """)
    unresolved = query(c, f"""
      WITH native_days AS
      (
        SELECT code, toDate(datetime) AS d FROM {NATIVE} WHERE source_year={year}
        GROUP BY code, d HAVING count()=8 AND uniqExact(datetime)=8
      ), five_days AS
      (
        SELECT code, toDate(bucket) AS d
        FROM
        (
          SELECT code, datetime,
            ss + toIntervalMinute(if(el<=0,30,intDiv(el+29,30)*30)) AS bucket
          FROM
          (
            SELECT code, datetime,
              if(toHour(datetime)<12,
                 toStartOfDay(datetime)+toIntervalHour(9)+toIntervalMinute(30),
                 toStartOfDay(datetime)+toIntervalHour(13)) AS ss,
              dateDiff('minute', ss, datetime) AS el
            FROM {FIVE} WHERE toYear(datetime)={year}
          )
        )
        GROUP BY code, d
        HAVING count()=48 AND uniqExact(datetime)=48 AND uniqExact(bucket)=8
      ), all_days AS
      (
        SELECT code, d FROM native_days UNION DISTINCT SELECT code, d FROM five_days
      )
      SELECT count() FROM all_days
    """)[0][0]
    return {"rows_by_source": [list(x) for x in rows], "resolved_code_days": int(unresolved)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=START_YEAR)
    ap.add_argument("--end-year", type=int, default=END_YEAR)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()
    if not 2000 <= args.start_year <= args.end_year <= 2010:
        raise SystemExit("scope supports 2000-2010")
    manifest_path = Path(r"F:\AiStockData\data\runtime") / RUN_ID / "manifest.json"
    c = connect()
    exists = bool(query(c, f"SELECT count() FROM system.tables WHERE database='stock' AND name='{RUN_ID}'")[0][0])
    if exists and not args.resume:
        raise SystemExit(f"refusing existing candidate {TARGET}; use --resume explicitly")
    if not exists:
        command(c, ddl())
    manifest = {
        "schema_version": "purchased-native-30m-candidate-v1",
        "run_id": RUN_ID, "scope": [args.start_year, args.end_year],
        "target_table": TARGET, "native_stage": NATIVE, "five_minute_source": FIVE,
        "source_precedence": ["native_purchased_complete_code_day", "five_minute_complete_code_day"],
        "standard_slots": ["10:00", "10:30", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00"],
        "expected_bars_per_day": 8, "formal_tables_touched": False,
        "status": "running", "years": {}, "started_at": datetime.now().astimezone().isoformat(),
        "one_minute_fallback": "not silently included; unresolved code-days remain review",
    }
    if args.resume and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "resuming"
    write_json(manifest_path, manifest)
    for year in range(args.start_year, args.end_year + 1):
        already = int(query(c, f"SELECT count() FROM {TARGET} WHERE source_year={year}")[0][0])
        if args.resume and already:
            print(json.dumps({"year": year, "phase": "skip_existing", "rows": already}, ensure_ascii=False), flush=True)
            continue
        started = time.time()
        native_rows = native_insert(c, year)
        five_rows = five_insert(c, year)
        stats = year_stats(c, year)
        manifest["years"][str(year)] = {
            "native_rows": native_rows, "five_minute_rows": five_rows,
            "candidate": stats, "elapsed_seconds": round(time.time() - started, 2),
        }
        write_json(manifest_path, manifest)
        print(json.dumps({"year": year, "native_rows": native_rows, "five_minute_rows": five_rows, **stats, "elapsed_seconds": round(time.time()-started,2)}, ensure_ascii=False), flush=True)
    final = query(c, f"SELECT count(), uniqExact(tuple(code,datetime)) FROM {TARGET} WHERE source_year BETWEEN {args.start_year} AND {args.end_year}")[0]
    manifest["final_rows"] = int(final[0]); manifest["final_unique_keys"] = int(final[1])
    manifest["status"] = "candidate_built"; manifest["finished_at"] = datetime.now().astimezone().isoformat()
    write_json(manifest_path, manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
